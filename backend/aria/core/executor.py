"""core/executor.py — Stage 1–7 Full Loop-Engineering executor.

Реализует полный цикл:
  Stage 1: Vault-check
  Stage 2: Plan (Oracle + PlanStep validation)
  Stage 3: Sub-agent execution
  Stage 4: Dual audit (correctness + integrity)
  Stage 5: Hooks (secret-scan, post-tool-call)
  Stage 6: Retry? (bounded retry + escalation)
  Stage 7: Delivery (vault-note generation)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from aria.core.integrity import IntegrityFlag, run_integrity_audit
from aria.core.locking import file_lock
from aria.core.delegate import run_bounded
from aria.core.notifiers.protocol import Notifier, NotifierError
from aria.core.plan_validator import (
    MAX_REPLAN_ATTEMPTS,
    PlanStep,
    check_oracle_naebal,
    topological_waves,
    validate_plan,
)
from aria.core.secretscanner import ScanResult, assert_no_secrets, scan_changed_files
from aria.db.enums import PLAN_PHASES, AuditVerdict, IntegrityVerdict, PlanStatus, TaskStatus
from aria.db.models import IntegrityEvent, Task, TaskPlan, ToolCall
from aria.storage.obsidian_vault import search_vault, vault_root, write_note_atomic
from aria.config import get_settings
from aria.llm.providers.base import ChatMessage
from aria.llm.router import ProviderRouter, ProviderUnavailable

# Transient DB errors that should be retried
_TRANSIENT_DB_ERRORS = (
    "database is locked",
    "locking protocol",
    "deadlock",
    "timeout",
    "connection refused",
    "connection reset",
    "temporary failure",
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# PR-2: единственная дверь смены статуса — repo.set_task_status (§8.1)
# ═══════════════════════════════════════════════════════════════════


def _set_phase(session: OrmSession, task: Task, phase: str) -> None:
    """PR-4: пишет внутреннюю фазу плана в task_plans.phase.

    Грубые состояния остаются в TaskStatus (§8.1); фаза уточняет, где именно внутри
    прогона находится задача, и переживает рестарт процесса и воркера.
    """
    from sqlalchemy import select

    if phase not in PLAN_PHASES:
        raise ValueError(f"unknown plan phase {phase!r}; allowed={PLAN_PHASES}")
    plan = session.execute(
        select(TaskPlan).where(TaskPlan.task_id == task.id).order_by(TaskPlan.created_at.desc())
    ).scalars().first()
    if plan is not None:
        plan.phase = phase
        session.flush()


def _transition(
    session: OrmSession,
    task: Task,
    status: TaskStatus,
    *,
    error_code: str | None = None,
    error_message: str | None = None,
) -> bool:
    """Переводит задачу в ``status`` ТОЛЬКО если переход разрешён картой §8.1.

    До PR-2 executor писал ``task.status = ...`` напрямую, минуя TASK_TRANSITIONS:
    переход approved -> done был разрешён кодом, хотя карта его запрещает, и
    защита из §8.1 в этом пути не работала.

    Returns:
        True — переход выполнен, False — запрещён или статус уже такой.
    """
    from aria.db import repository as repo
    from aria.db.enums import TASK_TRANSITIONS

    if task.status == status:
        return False
    if status not in TASK_TRANSITIONS.get(task.status, set()):
        logger.warning(
            "executor FSM: переход %s -> %s запрещён §8.1 — статус оставлен без изменений",
            task.status, status,
        )
        return False
    repo.set_task_status(session, task, status, error_code=error_code, error_message=error_message)
    return True


# ═══════════════════════════════════════════════════════════════════
# Plan History helpers (retention: max 20 entries)
# ═══════════════════════════════════════════════════════════════════

PLAN_HISTORY_MAX = 20
MAX_DB_RETRIES = 3
MAX_ZABYL_RETRIES = 3

# PR-5: ниже этого объёма свободной RAM параллелизм EXECUTE падает до 1 узла.
EXECUTE_MIN_FREE_RAM_GB = 1.5

# Oracle и registry используют canonical names (file_write, shell_execute);
# старые планы могут нести legacy aliases (write_file, terminal).
_TOOL_ALIASES = {"write_file": "file_write", "terminal": "shell_execute"}
_DB_RETRY_DELAY_SEC = [0.5, 1.0, 2.0]  # progressive


async def _with_db_retry(
    fn, *args, _session: OrmSession | None = None, **kwargs
):
    """Retry DB-bound operations on transient errors.

    Retries up to MAX_DB_RETRIES with progressive delay.
    Non-transient errors propagate immediately.
    """
    import asyncio

    last_exc = None
    for attempt in range(MAX_DB_RETRIES):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:
            msg = str(exc).lower()
            is_transient = any(t in msg for t in _TRANSIENT_DB_ERRORS)
            if not is_transient:
                raise  # non-transient — propagate immediately
            last_exc = exc
            if attempt < MAX_DB_RETRIES - 1:
                delay = _DB_RETRY_DELAY_SEC[min(attempt, len(_DB_RETRY_DELAY_SEC) - 1)]
                logger.warning(
                    "DB transient error (attempt %d/%d): %s. Retry in %.1fs",
                    attempt + 1, MAX_DB_RETRIES, exc, delay,
                )
                await asyncio.sleep(delay)
    raise last_exc  # exhausted retries


def _build_replan_feedback(zabyl_flags: list[IntegrityFlag], tool_calls_raw: list[dict]) -> str:
    """Собирает feedback для Oracle из ЗАБЫЛ-флагов Stage 6.

    Одно предложение на флаг: что не выполнил (reason) + какие шаги
    пропустил (missing_steps). Эта строка уходит в user-промпт Oracle
    при replan (force_replan=True).
    """
    lines: list[str] = []
    for flag in zabyl_flags:
        parts = [flag.reason] if flag.reason else []
        if flag.missing_steps:
            parts.append("пропущенные шаги: " + ", ".join(flag.missing_steps))
        lines.append("; ".join(parts))
    summary = ". ".join(f"- {ln}" for ln in lines)
    executed = ", ".join(
        str(tc.get("tool_ref", "?")) for tc in tool_calls_raw
    ) if tool_calls_raw else "ни одного"
    return f"Предыдущий план частично выполнен: {summary}. Выполненные шаги: {executed}."


def _append_plan_history(plan: TaskPlan) -> list[dict]:
    """Append current plan state to history, enforce retention."""
    history = list(plan.plan_history or [])
    entry = {
        "version": plan.version,
        "plan_json": plan.plan_json,
        "changed_at": datetime.now(timezone.utc).isoformat(),
    }
    history.append(entry)
    # Retention: shift oldest if over limit
    while len(history) > PLAN_HISTORY_MAX:
        history.pop(0)
    return history


# ═══════════════════════════════════════════════════════════════════
# Delivery-контракт: генерация vault-заметки
# ═══════════════════════════════════════════════════════════════════


def _extra_sections(task_spec: Any | None, criteria_status: dict | None) -> str:
    """PR-6: критерии приёмки и допущения — часть отчёта, а не деталь гейта."""
    if task_spec is None:
        return ""
    marks = {"pass": "✅", "fail": "❌"}
    out: list[str] = []
    criteria = list(getattr(task_spec, "acceptance_criteria", []) or [])
    if criteria:
        out.append("## Acceptance criteria\n")
        for c in criteria:
            st = (criteria_status or {}).get(str(c.id)) or {}
            mark = marks.get(str(st.get("status", "")), "❔")
            note = st.get("note") or ""
            out.append(f"- {mark} `{c.id}` — {c.statement}" + (f" ({note})" if note else ""))
        # Критерий мог появиться в отчёте позже правки TaskSpec (новая версия контракта):
        # он всё равно попадает в заметку, иначе результат аудита терялся бы.
        known = {str(c.id) for c in criteria}
        for cid, st in sorted((criteria_status or {}).items()):
            if cid in known:
                continue
            mark = marks.get(str(st.get("status", "")), "❔")
            note = st.get("note") or ""
            out.append(f"- {mark} `{cid}`" + (f" — {note}" if note else ""))
        out.append("")
    assumptions = list(getattr(task_spec, "assumptions", []) or [])
    if assumptions:
        out.append("## Assumptions\n")
        out.extend(f"- {x}" for x in assumptions)
        out.append("")
    return ("\n" + "\n".join(out)) if out else ""


def _generate_delivery_note(
    task: Task,
    plan: TaskPlan,
    integrity_flags: list[IntegrityFlag],
    task_spec: Any | None = None,
    criteria_status: dict | None = None,
) -> str:
    """Генерирует vault-заметку целиком из plan_json + final_result_json.

    Args:
        task: объект задачи.
        plan: объект плана.
        integrity_flags: результаты integrity-аудита.

    Returns:
        str: полный .md контент заметки.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    step_count = len(plan.plan_json or [])

    # Build step summary
    step_lines = []
    for step in (plan.plan_json or []):
        sid = str(step.get("step_id", ""))[:8]
        obj = step.get("objective", "")[:60]
        st = step.get("status", "unknown")
        tool = step.get("tool_ref") or step.get("skill_ref") or "freeform"
        tcid_count = len(step.get("tool_call_ids", []))
        step_lines.append(f"- `{sid}` [{st}] **{obj}** — {tool} ({tcid_count} tool_calls)")

    # Build integrity summary
    integrity_lines = []
    for flag in integrity_flags:
        integrity_lines.append(f"- **{flag.kind.upper()}**: {flag.reason[:120]}")

    iv = plan.integrity_verdict or "unknown"

    # PR-3: допущения TaskSpec — часть отчёта, а не внутренняя деталь гейта.
    assumptions = list(getattr(task_spec, "assumptions", []) or [])
    assumptions_lines = "\n".join(f"- {a}" for a in assumptions)

    # PR-6: отчёт собирается из TaskSpec, статусов критериев, допущений и проверок.
    criteria_status = criteria_status or {}
    criteria_lines = []
    for criterion in (getattr(task_spec, "acceptance_criteria", None) or []):
        data = criteria_status.get(criterion.id) or {}
        criteria_lines.append(
            f"- `{criterion.id}` **{data.get('status', 'unknown')}** — {criterion.statement}"
            + (f" ({data['note']})" if data.get("note") else "")
        )
    for cid, data in sorted(criteria_status.items()):
        if cid in {c.id for c in (getattr(task_spec, "acceptance_criteria", None) or [])}:
            continue
        criteria_lines.append(f"- `{cid}` **{data.get('status')}**" + (f" ({data['note']})" if data.get("note") else ""))
    criteria_block = "\n".join(criteria_lines)

    return _extra_sections(task_spec, criteria_status) + (
        f"---\n"
        f"task_id: {task.id}\n"
        f"status: {task.status.value}\n"
        f"created: {now}\n"
        f"steps: {step_count}\n"
        f"integrity: {iv}\n"
        f"---\n\n"
        f"# {task.objective[:80]}\n\n"
        f"**ID:** `{task.id}`\n\n"
        f"## Plan ({step_count} steps)\n\n"
        + "\n".join(step_lines) + "\n\n"
        f"## Integrity Verdict\n\n"
        f"**{iv}**\n\n"
        + ("\n".join(integrity_lines) + "\n\n" if integrity_lines else "")
        + (f"## Acceptance criteria\n\n{criteria_block}\n\n" if criteria_block else "")
        + (f"## Assumptions\n\n{assumptions_lines}\n\n" if assumptions_lines else "")
        + (
            f"## Result\n\n"
            f"{json.dumps(plan.final_result_json, indent=2, ensure_ascii=False)[:500]}\n"
            if plan.final_result_json
            else ""
        )
    )


# ═══════════════════════════════════════════════════════════════════
# Main executor loop
# ═══════════════════════════════════════════════════════════════════


async def run_task(
    session: OrmSession,
    task: Task,
    router: ProviderRouter | None = None,
    notifier: Notifier | None = None,
) -> dict[str, Any]:
    """Stage 1–7 + хуки H4 ``task_start`` / ``task_close`` / ``on_error`` вокруг исполнения."""
    from aria.core import toolhooks

    await toolhooks.lifecycle("task_start", session_id=task.session_id, task_id=task.id, mode="plan")
    try:
        result = await _run_task_impl(session, task, router, notifier)
    except Exception as exc:
        await toolhooks.lifecycle("on_error", session_id=task.session_id, task_id=task.id, code=type(exc).__name__, message=str(exc))
        raise
    await toolhooks.lifecycle("task_close", session_id=task.session_id, task_id=task.id, status=str(result.get("status")))
    return result


async def _run_task_impl(
    session: OrmSession,
    task: Task,
    router: ProviderRouter | None = None,
    notifier: Notifier | None = None,
) -> dict[str, Any]:
    """Stage 1–7 full loop.

    Args:
        session: асинхронная сессия БД.
        task: объект задачи.
        router: ProviderRouter для LLM-вызовов (Oracle, audit). None = деградация.
        notifier: опциональный Notifier для escalation.

    Returns:
        dict с результатами: {status, plan_id, note_path, integrity_flags}.
    """
    task_id = str(task.id)
    logger.info("Executor: starting task %s — %s", task_id[:8], task.objective[:60])

    # PR-2: вход в in_progress через FSM, а не присваиванием (approved -> in_progress).
    _transition(session, task, TaskStatus.in_progress)

    # ── Stage 1: Vault-check ──────────────────────────────────────
    logger.info("Stage 1: Vault-check for task %s", task_id[:8])
    vault_context = search_vault(task.objective)
    logger.info("Stage 1: found %d vault results", len(vault_context.get("matches", [])))

    # ── Stage 2: Plan ─────────────────────────────────────────────
    logger.info("Stage 2: Plan for task %s", task_id[:8])
    _set_phase(session, task, "compile")
    task_plan, oracle_flags = await _stage2_plan(session, task, vault_context, router)
    _set_phase(session, task, "plan")

    if oracle_flags:
        # Oracle НАЕБАЛ — escalation, выход
        await _handle_naebal(session, task, task_plan, oracle_flags, notifier)
        return {
            "status": "escalated",
            "plan_id": str(task_plan.id),
            "integrity_flags": [str(f) for f in oracle_flags],
            "note_path": None,
        }

    # ── Stage 3: Execute ──────────────────────────────────────────
    logger.info("Stage 3: Execute for task %s", task_id[:8])
    _set_phase(session, task, "execute")
    tool_calls_raw = await _stage3_execute(session, task, task_plan, router)
    blocked_cmds = _approval_blocked_commands(tool_calls_raw)
    if blocked_cmds:
        return await _fail_approval_required(session, task, task_plan, blocked_cmds)

    # ── Stage 4: Audit ────────────────────────────────────────────
    logger.info("Stage 4: Audit for task %s", task_id[:8])
    _set_phase(session, task, "audit")
    _transition(session, task, TaskStatus.under_audit)
    integrity_flags = await _stage4_audit(session, task, task_plan, tool_calls_raw, router)
    await _log_integrity_events(session, task_id, integrity_flags, tool_calls_raw)

    # ── Stage 5: Hooks ────────────────────────────────────────────
    logger.info("Stage 5: Hooks for task %s", task_id[:8])
    hook_result = _stage5_hooks(tool_calls_raw)

    # ── Stage 6: Retry? ───────────────────────────────────────────
    # ЗАБЫЛ с оставшимися попытками → feedback в Oracle → replan → повторный
    # прогон Stage 3-6. Исчерпаны MAX_ZABYL_RETRIES → escalation.
    while True:
        if hook_result.get("blocked"):
            _transition(
                session, task, TaskStatus.failed,
                error_code="hook_blocked",
                error_message=str(hook_result.get("error") or "pre-delivery hook blocked")[:500],
            )
            session.flush()
            return {
                "status": "blocked",
                "plan_id": str(task_plan.id),
                "integrity_flags": [str(f) for f in integrity_flags],
                "note_path": None,
            }

        logger.info("Stage 6: Retry check for task %s", task_id[:8])
        naebal_flags = [f for f in integrity_flags if f.kind == "naebal"]

        if naebal_flags:
            # НАЕБАЛ — retry запрещён
            await _handle_naebal(session, task, task_plan, naebal_flags, notifier)
            return {
                "status": "escalated",
                "plan_id": str(task_plan.id),
                "integrity_flags": [str(f) for f in integrity_flags],
                "note_path": None,
            }

        zabyl_flags = [f for f in integrity_flags if f.kind == "zabyl"]
        proebal_flags = [f for f in integrity_flags if f.kind == "proebal"]
        retryable_flags = zabyl_flags + proebal_flags
        if not retryable_flags:
            break

        if task_plan.iteration_count >= MAX_ZABYL_RETRIES:
            # ЗАБЫЛ/ПРОЕБАЛ + исчерпаны retry → escalation
            task_plan.status = PlanStatus.escalated.value
            reason = "; ".join(f.reason for f in retryable_flags) or "bounded retry exhausted"
            _transition(
                session, task, TaskStatus.failed,
                error_code="bounded_retry_exhausted", error_message=reason[:500],
            )
            if notifier:
                try:
                    await notifier.send_escalation(
                        task_id=task_id,
                        objective=task.objective,
                        claimed_result="bounded retry exhausted",
                        audit_findings=reason,
                        iteration=task_plan.iteration_count,
                    )
                except Exception as e:
                    logger.warning("Notifier failed for task %s: %s", task_id[:8], e)
            session.flush()
            return {
                "status": "escalated",
                "plan_id": str(task_plan.id),
                "integrity_flags": [str(f) for f in integrity_flags],
                "note_path": None,
            }

        # ЗАБЫЛ/ПРОЕБАЛ + есть попытки → replan с feedback и повторный прогон
        task_plan.iteration_count += 1
        feedback = _build_replan_feedback(retryable_flags, tool_calls_raw)
        reuse = _reusable_calls([dict(st) for st in (task_plan.plan_json or [])], tool_calls_raw)
        logger.info(
            "Stage 6: %s retry %d/%d for task %s",
            "ЗАБЫЛ/PROEBAL" if proebal_flags else "ЗАБЫЛ",
            task_plan.iteration_count, MAX_ZABYL_RETRIES, task_id[:8],
        )
        task_plan, oracle_flags = await _stage2_plan(
            session, task, vault_context, router,
            force_replan=True, feedback=feedback,
        )
        if oracle_flags:
            # Oracle НАЕБАЛ на replan — escalation
            await _handle_naebal(session, task, task_plan, oracle_flags, notifier)
            return {
                "status": "escalated",
                "plan_id": str(task_plan.id),
                "integrity_flags": [str(f) for f in oracle_flags],
                "note_path": None,
            }

        tool_calls_raw = await _stage3_execute(session, task, task_plan, router, reuse=reuse)
        blocked_cmds = _approval_blocked_commands(tool_calls_raw)
        if blocked_cmds:
            return await _fail_approval_required(session, task, task_plan, blocked_cmds)
        _transition(session, task, TaskStatus.under_audit)
        integrity_flags = await _stage4_audit(session, task, task_plan, tool_calls_raw, router)
        await _log_integrity_events(session, task_id, integrity_flags, tool_calls_raw)
        hook_result = _stage5_hooks(tool_calls_raw)

    # ── Stage 7: Delivery ─────────────────────────────────────────
    logger.info("Stage 7: Delivery for task %s", task_id[:8])
    _set_phase(session, task, "report")
    note_path = await _stage7_delivery(session, task, task_plan, integrity_flags)

    task_plan.status = PlanStatus.done.value
    # PR-2: under_audit -> done (карта §8.1); если стадия аудита была пропущена,
    # сначала переводим в under_audit, чтобы не прыгать in_progress -> done.
    _transition(session, task, TaskStatus.under_audit)
    _transition(session, task, TaskStatus.done)
    session.flush()
    _post_result_message(session, task, task_plan, tool_calls_raw, note_path)

    logger.info("Executor: task %s completed — status=done", task_id[:8])
    return {
        "status": "done",
        "plan_id": str(task_plan.id),
        "integrity_flags": [str(f) for f in integrity_flags],
        "note_path": note_path,
    }


# ═══════════════════════════════════════════════════════════════════
# Stage implementations
# ═══════════════════════════════════════════════════════════════════


async def _stage2_plan(
    session: OrmSession,
    task: Task,
    vault_context: dict,
    router: ProviderRouter | None = None,
    force_replan: bool = False,
    feedback: str = "",
) -> tuple[TaskPlan, list[IntegrityFlag]]:
    """Stage 2: Oracle plan generation + PlanStep validation.

    Использует router.route_chat для реального LLM-вызова Oracle.
    Если router is None — деградация до mock-плана (3 шага).
    force_replan=True + feedback — повторная генерация плана с учётом
    обратной связи (Stage 6 ЗАБЫЛ-retry), существующий план обновляется
    in-place (сохраняется iteration_count, версия + история плана).

    Returns:
        (TaskPlan, oracle_naebal_flags).
    """
    task_id = str(task.id)

    # PR-4 (COMPILE): контракт задачи -> постановка для Oracle. Плана без критериев
    # приёмки не бывает, когда TaskSpec их содержит.
    from aria.core import clarifier as _clarifier
    from aria.core import compiler as _compiler

    spec = _clarifier.load_spec(session, task.id)
    criteria_ids = [c.id for c in spec.acceptance_criteria] if spec is not None else None

    # Проверяем существующий план
    result = session.execute(
        select(TaskPlan).where(TaskPlan.task_id == task.id).order_by(TaskPlan.created_at.desc())
    )
    existing = result.scalars().first()

    if existing and existing.plan_json and not force_replan:
        # Уже есть план — используем его
        return existing, []

    # Oracle-генерация плана через LLM (или деградация при отсутствии router)
    plan_attempts = 0
    plan_steps: list[dict] = []
    last_error = ""

    while plan_attempts <= MAX_REPLAN_ATTEMPTS:
        plan_attempts += 1
        try:
            if router is not None:
                # Реальный LLM-вызов Oracle
                oracle_prompt_path = Path(__file__).parent.parent / "prompts" / "oracle.md"
                oracle_system = oracle_prompt_path.read_text(encoding="utf-8") if oracle_prompt_path.exists() else "Ты oracle — генератор плана."

                messages = [
                    ChatMessage(role="system", content=oracle_system),
                    ChatMessage(
                        role="user",
                        content=(
                            (_compiler.compile_task_input(spec, vault_context) if spec is not None else f"Objective: {task.objective}")
                            + (f"\n\nОбратная связь от аудита (переделай план с учётом этого):\n{feedback}" if feedback else "")
                        ),
                    ),
                ]
                result = await router.route_chat(
                    "premium_reasoning", messages, tools=[],
                    allow_degrade=True, db=session,
                    resilient=True, fallback_classes=("standard_reasoning", "free_tier_reasoning"),
                )
                raw = result.response.text.strip()
                # Парсим JSON из ответа
                if raw.startswith("```json"):
                    raw = raw.split("```json", 1)[1].split("```", 1)[0].strip()
                elif raw.startswith("```"):
                    raw = raw.split("```", 1)[1].split("```", 1)[0].strip()
                plan_steps = json.loads(raw)
                if not isinstance(plan_steps, list):
                    raise ValueError("Oracle response is not a list")
            else:
                # Волна 1 (A9): тихий мок-план в боевом пути удалён.
                raise ProviderUnavailable(
                    "no LLM provider configured — Oracle plan cannot be generated"
                )

            plan_steps = validate_plan(plan_steps, criteria_ids=criteria_ids)
            break
        except (ValueError, json.JSONDecodeError, KeyError) as e:
            last_error = str(e)
            logger.warning(
                "Oracle plan attempt %d/%d failed: %s",
                plan_attempts, MAX_REPLAN_ATTEMPTS + 1, e,
            )

    oracle_flag = check_oracle_naebal(plan_attempts, last_error)
    if oracle_flag:
        return TaskPlan(task_id=task.id, plan_json=[], status=PlanStatus.escalated.value), [oracle_flag]

    new_plan_json = [s.model_dump(mode="json") for s in plan_steps]

    if existing and existing.plan_json and force_replan:
        # Replan: обновляем существующую запись in-place, сохраняя iteration_count.
        existing.plan_json = new_plan_json
        existing.status = PlanStatus.in_progress.value
        existing.version = (existing.version or 1) + 1
        existing.plan_history = _append_plan_history(existing)
        session.add(existing)
        session.flush()
        return existing, []

    # Создаём запись плана
    task_plan = TaskPlan(
        task_id=task.id,
        plan_json=new_plan_json,
        status=PlanStatus.in_progress.value,
    )
    session.add(task_plan)
    session.flush()

    return task_plan, []


def _failed_step_payload(step: dict, exc: BaseException) -> dict:
    """Payload упавшего узла плана — тем же контрактом, что и успешного."""
    step_id = str(step.get("step_id", ""))
    return {
        "id": f"tc-{step_id[:8]}" if step_id else "tc-failed",
        "tool_name": step.get("tool_ref") or step.get("skill_ref") or "unknown",
        "input_json": {"objective": step.get("objective", "")},
        "output_json": {"error": f"{type(exc).__name__}: {exc}"},
        "status": "error",
    }


def _execute_parallelism() -> int:
    """Сколько узлов плана исполнять одновременно (PR-5).

    Берётся меньший из двух лимитов (§7.2 delegate_max_parallel и пул TaskRunner),
    а при свободной памяти меньше EXECUTE_MIN_FREE_RAM_GB всё сводится к 1:
    дешевле выполнить последовательно, чем попасть в swap или быть убитым OOM.
    """
    settings = get_settings()
    limit = max(1, min(int(settings.runner_max_workers), int(settings.delegate_max_parallel)))
    try:
        import psutil

        free_gb = psutil.virtual_memory().available / (1024 ** 3)
        if free_gb < EXECUTE_MIN_FREE_RAM_GB:
            logger.warning(
                "свободной памяти %.2f ГБ < %.1f ГБ — параллелизм EXECUTE снижен до 1 узла",
                free_gb, EXECUTE_MIN_FREE_RAM_GB,
            )
            return 1
    except Exception:  # noqa: BLE001 — нет psutil/нет данных: работаем по конфигу, а не падаем
        logger.debug("psutil недоступен — лимит параллелизма EXECUTE берётся из настроек")
    return limit


def _serialize_by_write_scope(steps: list[dict], wave: list[int]) -> list[list[int]]:
    """Делит волну на группы так, чтобы внутри группы write_scope не пересекались.

    Два узла, пишущие один и тот же путь, обязаны идти последовательно: иначе
    результат зависит от того, кто из них успел записать последним. Узлы без
    write_scope конфликтов не создают.
    """
    def scope_of(idx: int) -> set[str]:
        return {str(w) for w in (steps[idx].get("write_scope") or []) if str(w)}

    def unsocial(idx: int) -> bool:
        # П.1.4 ТЗ: пишущий шаг без write_scope — только одиночной группой.
        tool = str(steps[idx].get("tool_ref") or steps[idx].get("skill_ref") or "").strip().lower()
        return tool in ("shell_execute", "file_write") and not scope_of(idx)

    groups: list[list[int]] = []
    for idx in wave:
        scope = scope_of(idx)
        if unsocial(idx):
            groups.append([idx])
            continue
        for group in groups:
            used: set[str] = set()
            for other in group:
                used |= scope_of(other)
            if not (scope & used):
                group.append(idx)
                break
        else:
            groups.append([idx])
    return groups


async def _execute_step_isolated(
    step: dict,
    *,
    index: int,
    task_id: Any,
    sandbox_root: str,
    router: ProviderRouter | None,
) -> dict:
    """Исполняет один узел плана в собственном коротком session_scope (PR-5).

    Узел не держит сессию БД на время сетевого вызова инструмента: сначала работа
    (LLM/файл/shell), потом короткая транзакция, которая пишет только ToolCall.
    Раньше весь прогон шёл в одной sync-сессии, и параллельный EXECUTE был бы для
    SQLite гонкой за writer-lock.

    Returns:
        dict того же вида, что элементы tool_calls_raw: id, tool_name, input_json,
        output_json, status.
    """
    from aria.core import toolhooks
    from aria.db import repository as repo
    from aria.db.base import session_scope
    from aria.db.enums import ToolStatus
    from aria.tools.registry import get_tool
    from aria.tools.validators import validate_tool_input

    step_id = step.get("step_id", "")
    objective = step.get("objective", "")
    role = step.get("role", "general")
    skill_ref = step.get("skill_ref") if not step.get("tool_ref") else None
    # Шаг со skill_ref раньше искался в реестре тулов и падал «unknown tool_ref»: навыки не исполнялись никогда.
    # Теперь это LLM-шаг, в промпт которого подставлено тело навыка (SKILL.md).
    raw_tool_ref = "llm_task" if skill_ref else (step.get("tool_ref") or "shell_execute")
    tool_ref = _TOOL_ALIASES.get(raw_tool_ref, raw_tool_ref)
    tc_id = f"tc-{str(step_id)[:8]}" if step_id else f"tc-{index}"

    if router is None:
        # Волна 1 (A9): тихие «успешные» мок-вызовы удалены — без LLM шаг
        # не исполняется по-настоящему, значит это ошибка, а не ok.
        raise ProviderUnavailable(f"no LLM provider configured — step {step_id} cannot be executed")

    with session_scope() as db:
        _task = repo.get_task(db, task_id)
        session_id = _task.session_id if _task is not None else None

    spec = None
    handler_input: dict = {}
    try:
        if tool_ref == "llm_task":
            # Шаг, который сам по себе является LLM-задачей (не registry tool)
            skill_text = ""
            if skill_ref:
                from aria.tools.handlers.skills import read_skill_body

                body = read_skill_body(str(skill_ref))
                if body is None:
                    raise ValueError(f"skill_ref «{skill_ref}» не найден (навык добавляется во вкладке SKILLS или POST /skills)")
                skill_text = f"\n\nСледуй инструкциям навыка «{skill_ref}»:\n{body}"
            messages = [
                ChatMessage(role="system", content=f"Ты {role}. {objective}{skill_text}"),
                ChatMessage(role="user", content=objective),
            ]
            result = await router.route_chat(
                "subagent_execution", messages, tools=[],
                allow_degrade=True, db=None,
                resilient=True, fallback_classes=("free_tier_reasoning", "standard_reasoning"),
            )
            spec = type("Spec", (), {"tool_name": "llm_task", "risk_level": type("R", (), {"value": "medium"})()})()
            handler_input = {"objective": objective, "role": role}
            output_json = {"content": result.response.text or ""}
        else:
            try:
                spec = get_tool(tool_ref)
            except KeyError:
                spec = None

            if spec is None:
                # Неизвестный tool_ref — явный fail (НЕ деградация в текст)
                logger.warning("Stage 3: unknown tool_ref=%s for step %s", raw_tool_ref, step_id)
                raise ValueError(f"unknown tool_ref: {raw_tool_ref} (нет в registry §11)")

            if spec.handler is None:
                # delegate_task и прочие без handler'а — не исполняются в executor
                raise ValueError(f"tool_ref={tool_ref} не имеет handler'а в registry (только orchestrator loop)")

            if tool_ref == "file_write":
                content = _strip_code_fence(await _llm_generate_content(router, None, role, objective))
                if not content.strip():
                    # Пустой ответ модели раньше молча превращался в пустой файл со status=ok.
                    raise ValueError(f"LLM вернул пустое содержимое для файла (шаг {step_id[:8] or index}) — файл не создан")
                handler_input = {"path": step.get("path", f"generated/{tc_id}.txt"), "content": content}
            elif tool_ref == "shell_execute":
                command = await _llm_generate_command(router, None, role, objective)
                handler_input = {"command": command}
            elif tool_ref == "file_read":
                handler_input = {"path": step.get("path", ".")}
            elif tool_ref == "file_search":
                handler_input = {"glob": step.get("glob", "**/*")}
            elif tool_ref == "web_search":
                handler_input = {"query": step.get("query", objective), "max_results": step.get("max_results", 5)}
            elif tool_ref == "web_fetch":
                handler_input = {"url": step.get("url", ""), "max_chars": step.get("max_chars", 8000)}
            else:
                # Остальные registry-инструменты — input прямо из шага
                handler_input = step.get("input") or step.get("input_json") or {}
                if not handler_input:
                    raise ValueError(f"tool_ref={tool_ref} требует step.input/input_json")

            # Тот же барьер аргументов, что в loop._execute_tool: Plan-режим ходит в handler напрямую,
            # и без этого пустой url/path/glob давал бы «успех» или невнятный KeyError.
            arg_errors = validate_tool_input(spec.input_schema, handler_input)
            if arg_errors:
                raise ValueError(f"invalid_arguments ({spec.tool_name}): " + "; ".join(arg_errors))

            hook_decision, checkpoint_id = await toolhooks.pre_tool(
                spec.tool_name, handler_input,
                session_id=session_id, task_id=task_id, sandbox_root=sandbox_root,
            )
            if hook_decision.blocked:
                raise ValueError(f"hook_blocked: {hook_decision.reason}")

            output_json = await spec.handler(
                input_json=handler_input,
                timeout_sec=spec.timeout_sec,
                sandbox_root=sandbox_root,
            )
            await toolhooks.post_tool(
                spec.tool_name, handler_input, output_json,
                "error" if isinstance(output_json, dict) and output_json.get("error") else "ok",
                session_id=session_id, task_id=task_id, sandbox_root=sandbox_root, checkpoint_id=checkpoint_id,
            )
    except Exception as e:  # noqa: BLE001 — узел плана не роняет волну целиком
        logger.warning("Stage 3: шаг %s упал: %s", step_id[:8] if step_id else "?", e)
        return {
            "id": tc_id,
            "tool_name": tool_ref,
            "input_json": {"objective": objective, "role": role},
            "output_json": {"error": str(e)},
            "status": "error",
        }

    status_ok = not (isinstance(output_json, dict) and output_json.get("error"))

    # Свой короткий session_scope на запись ToolCall: writer-lock держится ровно
    # на время транзакции, а не на весь сетевой вызов инструмента.
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        agent_session = repo.get_session(db, task.session_id) if task is not None and task.session_id else None
        if agent_session is not None and task is not None:
            call = repo.start_tool_call(
                db=db,
                session=agent_session,
                task=task,
                tool_name=spec.tool_name,
                role=role,
                risk_level=spec.risk_level.value,
                input_json=handler_input,
                attempt_no=1,
            )
            repo.finish_tool_call(
                db=db,
                call=call,
                status=ToolStatus.ok if status_ok else ToolStatus.error,
                output_json=output_json,
                error_code=None if status_ok else "handler_error",
                error_message=None if status_ok else (output_json.get("error") if isinstance(output_json, dict) else None),
            )
            tc_id = str(call.id)

    return {
        "id": tc_id,
        "tool_name": spec.tool_name,
        "input_json": handler_input,
        "output_json": output_json,
        "status": "ok" if status_ok else "error",
    }


async def _stage3_execute(
    session: OrmSession,
    task: Task,
    task_plan: TaskPlan,
    router: ProviderRouter | None = None,
    reuse: dict[tuple, dict] | None = None,
) -> list[dict]:
    """Stage 3: EXECUTE — волны DAG, внутри волны параллельно (PR-5).

    Прежняя версия шла линейным for по шагам. Здесь:

    * план разбирается на волны (topological_waves): внутри волны зависимости уже
      выполнены, значит узлы можно запускать одновременно;
    * узлы с пересекающимся write_scope разводятся в последовательные группы —
      параллельная запись одного и того же пути недопустима;
    * параллелизм ограничен run_bounded (не отдельный TaskGroup): тот же примитив,
      что и в агентском цикле, с тем же лимитом delegate_max_parallel;
    * каждый узел работает в собственном коротком session_scope.

    Returns:
        list of ToolCall dicts (порядок соответствует порядку шагов плана).
    """
    from aria.config import get_settings as _gs

    settings = _gs()
    sandbox_root = str((Path(__file__).parent.parent.parent / settings.agent_sandbox_root).resolve())

    steps = list(task_plan.plan_json or [])
    if not steps:
        return []

    if router is None:
        # Волна 1 (A9): без LLM шаги не «выполняются» фиктивно. Параллельный путь
        # не должен глотать это в failed-узел — это отказ прогона, а не ошибка шага.
        raise ProviderUnavailable(
            f"no LLM provider configured — plan of {len(steps)} steps cannot be executed"
        )

    # PR-5: узлы пишут ToolCall своими короткими session_scope. Если вызывающий
    # держит открытую writer-транзакцию (SQLite), вложенная запись получит
    # «database is locked» — освобождаем её до волн.
    session.commit()

    limit = _execute_parallelism()
    # П.1.6 ТЗ: аварийный выключатель параллельного EXECUTE.
    if not bool(getattr(settings, "plan_dag_parallel_enabled", True)):
        waves = [[i] for i in range(len(steps))]
        limit = 1
        logger.info("Stage 3: plan_dag_parallel_enabled=False — строго последовательный EXECUTE")
    else:
        waves = topological_waves(steps)
    logger.info(
        "Stage 3: %d шагов, %d волн, лимит параллелизма %d (task %s)",
        len(steps), len(waves), limit, str(task.id)[:8],
    )

    tool_calls_raw: list[dict] = []
    for wave_no, wave in enumerate(waves, start=1):
        session.commit()  # PR-5: writer-lock отпущен перед параллельной волной
        for group in _serialize_by_write_scope(steps, wave):
            logger.info(
                "Stage 3: волна %d/%d, группа %s — до %d узлов параллельно",
                wave_no, len(waves), group, min(limit, len(group)),
            )
            reuse_map = reuse or {}
            run_idx = [i for i in group if _step_signature(steps[i]) not in reuse_map]
            factories = [
                (lambda i=idx: _execute_step_isolated(
                    steps[i], index=i, task_id=task.id, sandbox_root=sandbox_root, router=router,
                ))
                for idx in run_idx
            ]
            outcomes = await run_bounded(factories, limit=min(limit, len(run_idx))) if factories else []
            pairs = list(zip(run_idx, outcomes))
            # шаги, уже выполненные в прошлой итерации, не гоняем заново (см. _reusable_calls)
            pairs += [(i, reuse_map[_step_signature(steps[i])]) for i in group if i not in run_idx]
            for idx, outcome in pairs:
                if isinstance(outcome, BaseException):
                    logger.warning(
                        "Stage 3: узел %s завершился исключением: %s",
                        steps[idx].get("step_id"), outcome,
                    )
                    outcome = _failed_step_payload(steps[idx], outcome)
                tool_calls_raw.append(outcome)
                steps[idx]["status"] = "done" if outcome.get("status") == "ok" else "failed"
                # tool_call_ids — ТОЛЬКО при ok, чтобы ЗАБЫЛ-детектор срабатывал на failure
                if outcome.get("status") == "ok":
                    steps[idx]["tool_call_ids"] = [outcome["id"]]
                else:
                    steps[idx].pop("tool_call_ids", None)

    # Сохраняем обновлённый план
    task_plan.plan_json = steps
    task_plan.plan_history = _append_plan_history(task_plan)
    session.flush()

    return tool_calls_raw


MAX_RESULT_CHARS = 6000


def _result_files(tool_calls: list[dict], sandbox_root: str) -> list[tuple[str, str]]:
    """(путь, текст) файлов, записанных успешными file_write; только внутри песочницы."""
    root = Path(sandbox_root).resolve()
    out: list[tuple[str, str]] = []
    for c in tool_calls:
        o = c.get("output_json")
        if c.get("tool_name") != "file_write" or c.get("status") != "ok" or not isinstance(o, dict) or not o.get("path"):
            continue
        rel = str(o["path"])
        try:
            full = (root / rel).resolve()
            if root not in full.parents and full != root:
                continue
            if full.is_file():
                out.append((rel, full.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return out


def build_result_message(task: Task, files: list[tuple[str, str]], spec: Any | None,
                         criteria_status: dict | None, note_path: str | None) -> str:
    """Сообщение ассистента для чата: сам результат, статус критериев, допущения, пути файлов."""
    lines = ["Задача выполнена."]
    if files:
        shown = 0
        for rel, text in files:
            room = MAX_RESULT_CHARS - shown
            if room <= 0:
                lines.append(f"\n(файл `{rel}` не показан целиком — открой его по пути ниже)")
                continue
            body = text.strip()
            cut = body[:room]
            lines += ["", f"**Результат — `{rel}`**", "", cut + ("\n…(обрезано, полный текст в файле)" if len(body) > room else "")]
            shown += len(cut)
    else:
        lines.append("Файлов с результатом шаги не создали; ход выполнения — в карточке задачи.")
    if criteria_status:
        lines += ["", "**Критерии приёмки:**"]
        lines += [f"- {cid}: {st}" for cid, st in criteria_status.items()]
    assumptions = list(getattr(spec, "assumptions", None) or [])
    if assumptions:
        lines += ["", "**Допущения:**"] + [f"- {a}" for a in assumptions]
    where = [f"`{rel}` (песочница)" for rel, _ in files]
    if note_path:
        where.append(f"`{note_path}` (vault)")
    if where:
        lines += ["", "Файлы: " + ", ".join(where)]
    return "\n".join(lines)


def _post_result_message(session: OrmSession, task: Task, task_plan: TaskPlan,
                         tool_calls: list[dict], note_path: str | None) -> None:
    """Кладёт результат в чат сессии. Сбой доставки в чат не должен ронять уже выполненную задачу."""
    try:
        from aria.config import get_settings
        from aria.core import clarifier as _clarifier
        from aria.core.events import event_bus
        from aria.db import repository as repo
        from aria.http_utils import serialize_message

        agent_session = repo.get_session(session, task.session_id)
        if agent_session is None:
            return
        spec = _clarifier.load_spec(session, task.id)
        reports = repo.list_audit_reports(session, task.id)
        criteria = dict((reports[-1].plan_vs_fact or {}).get("criteria_status") or {}) if reports else {}
        files = _result_files(tool_calls, get_settings().agent_sandbox_root)
        msg = repo.append_message(session, agent_session, role="assistant",
                                  content=build_result_message(task, files, spec, criteria, note_path))
        session.flush()
        event_bus.emit("message.created", serialize_message(msg), session_id=task.session_id, task_id=task.id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Result message for task %s not posted: %s", str(task.id)[:8], exc)


def _approval_blocked_commands(tool_calls: list[dict]) -> list[str]:
    """Команды, которые shell-политика не запустила (approval_required) — шаг не выполнен, а не «упал»."""
    out: list[str] = []
    for c in tool_calls:
        o = c.get("output_json")
        if isinstance(o, dict) and o.get("status") == "approval_required":
            out.append(str(o.get("command", "?"))[:200])
    return out


def _step_signature(step: dict) -> tuple:
    return (
        str(step.get("step_id", "")),
        str(step.get("tool_ref") or step.get("skill_ref") or ""),
        str(step.get("objective", "")),
        str(step.get("path", "")),
    )


def _reusable_calls(prev_steps: list[dict], tool_calls: list[dict]) -> dict[tuple, dict]:
    """Успешно выполненные шаги прошлой итерации: сигнатура шага -> его tool_call.

    Повторный прогон ЗАБЫЛ-retry не должен заново писать уже записанные файлы: идентичная перезапись давала
    hash_before == hash_after и валила всю задачу как НАЕБАЛ, хотя виноват был упавший соседний шаг.
    """
    by_id = {str(c.get("id")): c for c in tool_calls if c.get("status") == "ok"}
    reuse: dict[tuple, dict] = {}
    for st in prev_steps:
        ids = st.get("tool_call_ids") or []
        if st.get("status") == "done" and ids and str(ids[0]) in by_id:
            reuse[_step_signature(st)] = by_id[str(ids[0])]
    return reuse


async def _fail_approval_required(session: OrmSession, task: Task, task_plan: TaskPlan, commands: list[str]) -> dict[str, Any]:
    """Plan-режим не умеет ждать Approve посреди плана: честный отказ с причиной вместо ЗАБЫЛ-ретраев и ложного НАЕБАЛ."""
    task_plan.status = PlanStatus.escalated.value
    shown = "; ".join(commands[:3])
    _transition(
        session, task, TaskStatus.failed,
        error_code="approval_required",
        error_message=(f"Шаги плана требуют подтверждения владельца (shell-политика default-deny), исполнение не начато: "
                       f"{shown}. Запусти команду вручную либо выполни задачу в режиме Agent, где есть Approve."),
    )
    session.flush()
    return {"status": "blocked", "plan_id": str(task_plan.id), "integrity_flags": [], "note_path": None}


def _strip_code_fence(text: str) -> str:
    """Модель часто оборачивает файл в ```lang ... ``` вопреки промпту — в файл это попадать не должно."""
    t = (text or "").strip()
    if not t.startswith("```"):
        return text or ""  # без обёртки содержимое не трогаем
    first_nl = t.find("\n")
    t = t[first_nl + 1:] if first_nl != -1 else ""
    if t.rstrip().endswith("```"):
        t = t.rstrip()[:-3]
    t = t.strip("\n")
    return t + "\n" if t.strip() else ""


async def _llm_generate_content(
    router: ProviderRouter,
    session: OrmSession | None,
    role: str,
    objective: str,
) -> str:
    """LLM генерирует контент для записи в файл."""
    messages = [
        ChatMessage(
            role="system",
            content=f"Ты {role}. Сгенерируй содержимое файла для задачи: {objective}. "
                    "Верни ТОЛЬКО содержимое файла, без markdown-обёртки, без пояснений.",
        ),
        ChatMessage(role="user", content=objective),
    ]
    result = await router.route_chat(
        "subagent_execution", messages, tools=[],
        allow_degrade=True, db=session,
        resilient=True, fallback_classes=("free_tier_reasoning", "standard_reasoning"),
    )
    return result.response.text or ""


async def _llm_generate_command(
    router: ProviderRouter,
    session: OrmSession | None,
    role: str,
    objective: str,
) -> str:
    """LLM генерирует shell-команду для выполнения."""
    messages = [
        ChatMessage(
            role="system",
            content=f"Ты {role}. Для задачи: {objective} — напиши ОДНУ shell-команду "
                    "для выполнения. Верни ТОЛЬКО команду, без markdown-обёртки, без пояснений.",
        ),
        ChatMessage(role="user", content=objective),
    ]
    result = await router.route_chat(
        "subagent_execution", messages, tools=[],
        allow_degrade=True, db=session,
        resilient=True, fallback_classes=("free_tier_reasoning", "standard_reasoning"),
    )
    return result.response.text.strip().strip("`").strip() or "echo 'no command generated'"


async def _stage4_audit(
    session: OrmSession,
    task: Task,
    task_plan: TaskPlan,
    tool_calls_raw: list[dict],
    router: ProviderRouter | None = None,
) -> list[IntegrityFlag]:
    """Stage 4: Dual audit — integrity (pure functions) + correctness (LLM via QA).

    Порядок:
    1. Integrity-детекторы (НАЕБАЛ/ЗАБЫЛ) — чистая логика, без LLM.
    2. Если integrity pass и есть router + агентская сессия — correctness QA
       через run_audit (qa_auditor). needs_rework → ПРОЕБАЛ (честная ошибка → retry).

    Returns:
        list of IntegrityFlag.
    """
    from aria.core.audit import run_audit
    from aria.db import repository as repo

    flags = run_integrity_audit(
        tool_calls=tool_calls_raw,
        plan_json=task_plan.plan_json,
    )

    # Определяем итоговый вердикт
    naebal = any(f.kind == "naebal" for f in flags)
    zabyl = any(f.kind == "zabyl" for f in flags)

    if naebal:
        task_plan.integrity_verdict = IntegrityVerdict.naebal.value
        return flags
    if zabyl:
        # ЗАБЫЛ не смешиваем с correctness QA — retry решает Stage 6
        task_plan.integrity_verdict = IntegrityVerdict.zabyl.value
        return flags

    # Корректностная QA — только если есть чем и где аудитить.
    # Без router или без агентской сессии run_audit вернёт пустой отчёт
    # «no tool calls» → ложный needs_rework.
    if router is not None and getattr(task, "session_id", None) is not None:
        agent_session = repo.get_session(session, task.session_id)
        if agent_session is not None:
            from aria.core import clarifier as _clarifier

            spec = _clarifier.load_spec(session, task.id)
            try:
                report = await run_audit(
                    session, agent_session, task, router,
                    criteria=[c.model_dump(mode="json") for c in spec.acceptance_criteria] if spec else None,
                    assumptions=list(spec.assumptions) if spec else None,
                )
            except Exception:  # noqa: BLE001
                logger.exception("Correctness audit crashed — integrity verdict stands")
                task_plan.integrity_verdict = IntegrityVerdict.pass_.value
                return flags

            if report.verdict == AuditVerdict.needs_rework:
                task_plan.integrity_verdict = IntegrityVerdict.proebal.value
                flags.append(
                    IntegrityFlag(
                        kind="proebal",
                        reason="Correctness QA (run_audit) verdict=needs_rework",
                    )
                )
                return flags

    task_plan.integrity_verdict = IntegrityVerdict.pass_.value
    return flags


def _stage5_hooks(tool_calls_raw: list[dict]) -> dict:
    """Stage 5: Pre-delivery hooks.

    - Secret-scan: only changed files from current delivery
    - Post-tool-call: hash_before/after for write_file/patch

    Returns:
        dict с ключами blocked, scan_result, warnings.
    """
    # Secret-scan: определяем changed files из tool_calls
    changed_files: list[Path] = []
    for tc in tool_calls_raw:
        inp = tc.get("input_json", {}) or {}
        path_str = inp.get("path", "")
        if path_str:
            changed_files.append(Path(path_str))

    if changed_files:
        result = scan_changed_files(changed_files)
        try:
            assert_no_secrets(result, min_severity="critical")
        except Exception as e:
            return {"blocked": True, "error": str(e), "scan_result": result}
    else:
        result = ScanResult()

    return {"blocked": False, "scan_result": result, "warnings": []}


async def _stage7_delivery(
    session: OrmSession,
    task: Task,
    task_plan: TaskPlan,
    integrity_flags: list[IntegrityFlag],
) -> str | None:
    """Stage 7: Delivery — генерация vault-заметки с file-lock.

    Returns:
        str: путь к заметке, или None если не удалось.
    """
    task_id = str(task.id)

    # PR-3: контракт задачи читаем из БД — он версионируется и переживает рестарт.
    from aria.core import clarifier as _clarifier

    task_spec = _clarifier.load_spec(session, task.id)

    # PR-6: статусы критериев берём из последнего отчёта аудита (его пишет _stage4_audit).
    from aria.db import repository as repo  # раньше не был импортирован: Stage 7 падал NameError на ЛЮБОЙ задаче

    reports = repo.list_audit_reports(session, task.id)
    criteria_status = {}
    if reports:
        criteria_status = dict((reports[-1].plan_vs_fact or {}).get("criteria_status") or {})

    # Delivery-контракт: проверяем, что есть результат для записи
    if task_plan.final_result_json is None:
        content = _generate_delivery_note(task, task_plan, integrity_flags, task_spec, criteria_status)
        content = content.replace("# Status\n\n**done**", "# Status\n\n**unfinished**\n\nЗадача не завершена. Причина: final_result_json не заполнен (возможно escalation).")
    else:
        content = _generate_delivery_note(task, task_plan, integrity_flags, task_spec, criteria_status)

    # Проверка delivery-контракта
    required_fields = [
        ("task_id", str(task.id)),
        ("steps", str(len(task_plan.plan_json or []))),
        ("integrity", task_plan.integrity_verdict or "unknown"),
    ]
    for name, val in required_fields:
        if val in content:
            continue
        logger.warning("Delivery contract violation: missing field %s in note", name)
        return None

    # Атомарная запись через file-lock
    vroot = vault_root()
    note_name = f"task-{task_id[:12]}"
    try:
        with file_lock(vroot, task_id):
            result = write_note_atomic(note_name, content, folder="00-TASKS")
        return result.get("path")
    except Exception as e:
        logger.error("Delivery failed for task %s: %s", task_id[:8], e)
        return None


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════


async def _handle_naebal(
    session: OrmSession,
    task: Task,
    task_plan: TaskPlan,
    flags: list[IntegrityFlag],
    notifier: Notifier | None,
) -> None:
    """Handle НАЕБАЛ: hard fail, escalation via notifier."""
    task_id = str(task.id)
    task_plan.status = PlanStatus.escalated.value
    task_plan.integrity_verdict = IntegrityVerdict.naebal.value

    reason = "; ".join(f.reason for f in flags) if flags else "НАЕБАЛ (unknown)"
    _transition(
        session, task, TaskStatus.failed,
        error_code="integrity_naebal", error_message=reason[:500],
    )

    if notifier:
        try:
            await notifier.send_escalation(
                task_id=task_id,
                objective=task.objective,
                claimed_result="integrity audit failed",
                audit_findings=reason,
                iteration=task_plan.iteration_count,
            )
        except Exception as e:
            # Notifier failure ≠ integrity-нарушение
            logger.warning("Notifier failed for task %s: %s", task_id[:8], e)

    session.flush()


async def _log_integrity_events(
    session: OrmSession,
    task_id: str,
    flags: list[IntegrityFlag],
    tool_calls: list[dict],
) -> None:
    """Пишет integrity-вердикты в integrity_events таблицу.

    false_positive — только ручная метка (Phase 8).
    """
    from uuid import UUID

    task_uuid = UUID(task_id)
    for flag in flags:
        # Find matching tool_call_id if any
        tool_call_id = None
        if flag.tool_call_id and tool_calls:
            for tc in tool_calls:
                if tc.get("id") == flag.tool_call_id:
                    try:
                        tool_call_id = UUID(flag.tool_call_id)
                    except (ValueError, TypeError):
                        pass
                    break

        event = IntegrityEvent(
            task_id=task_uuid,
            tool_call_id=tool_call_id,
            detector=flag.kind,
            verdict=flag.verdict if hasattr(flag, 'verdict') else "fail",
            artifact_hash=flag.artifact_hash if hasattr(flag, 'artifact_hash') else None,
            detail=flag.reason[:500] if flag.reason else None,
        )
        session.add(event)

    session.flush()
    logger.info(
        "IntegrityEvents: task=%s wrote %d events",
        task_id[:8], len(flags),
    )

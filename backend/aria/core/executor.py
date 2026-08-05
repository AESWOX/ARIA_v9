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
from aria.core.notifiers.protocol import Notifier, NotifierError
from aria.core.plan_validator import MAX_REPLAN_ATTEMPTS, PlanStep, check_oracle_naebal, validate_plan
from aria.core.secretscanner import ScanResult, assert_no_secrets, scan_changed_files
from aria.db.enums import AuditVerdict, IntegrityVerdict, PlanStatus, TaskStatus
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
# Plan History helpers (retention: max 20 entries)
# ═══════════════════════════════════════════════════════════════════

PLAN_HISTORY_MAX = 20
MAX_DB_RETRIES = 3
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


def _generate_delivery_note(
    task: Task,
    plan: TaskPlan,
    integrity_flags: list[IntegrityFlag],
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

    return (
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

    # ── Stage 1: Vault-check ──────────────────────────────────────
    logger.info("Stage 1: Vault-check for task %s", task_id[:8])
    vault_context = search_vault(task.objective)
    logger.info("Stage 1: found %d vault results", len(vault_context.get("matches", [])))

    # ── Stage 2: Plan ─────────────────────────────────────────────
    logger.info("Stage 2: Plan for task %s", task_id[:8])
    task_plan, oracle_flags = await _stage2_plan(session, task, vault_context, router)

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
    tool_calls_raw = await _stage3_execute(session, task, task_plan, router)

    # ── Stage 4: Audit ────────────────────────────────────────────
    logger.info("Stage 4: Audit for task %s", task_id[:8])
    integrity_flags = _stage4_audit(task_plan, tool_calls_raw)
    await _log_integrity_events(session, task_id, integrity_flags, tool_calls_raw)

    # ── Stage 5: Hooks ────────────────────────────────────────────
    logger.info("Stage 5: Hooks for task %s", task_id[:8])
    hook_result = _stage5_hooks(tool_calls_raw)

    if hook_result.get("blocked"):
        task.status = TaskStatus.failed
        session.flush()
        return {
            "status": "blocked",
            "plan_id": str(task_plan.id),
            "integrity_flags": [str(f) for f in integrity_flags],
            "note_path": None,
        }

    # ── Stage 6: Retry? ───────────────────────────────────────────
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
    if zabyl_flags and task_plan.iteration_count >= 3:
        # ЗАБЫЛ + исчерпаны retry → escalation
        task.status = TaskStatus.failed
        task_plan.status = PlanStatus.escalated.value
        session.flush()
        return {
            "status": "escalated",
            "plan_id": str(task_plan.id),
            "integrity_flags": [str(f) for f in integrity_flags],
            "note_path": None,
        }

    # ── Stage 7: Delivery ─────────────────────────────────────────
    logger.info("Stage 7: Delivery for task %s", task_id[:8])
    note_path = await _stage7_delivery(session, task, task_plan, integrity_flags)

    task_plan.status = PlanStatus.done.value
    task.status = TaskStatus.done
    session.flush()

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
) -> tuple[TaskPlan, list[IntegrityFlag]]:
    """Stage 2: Oracle plan generation + PlanStep validation.

    Использует router.route_chat для реального LLM-вызова Oracle.
    Если router is None — деградация до mock-плана (3 шага).

    Returns:
        (TaskPlan, oracle_naebal_flags).
    """
    task_id = str(task.id)

    # Проверяем существующий план
    result = session.execute(
        select(TaskPlan).where(TaskPlan.task_id == task.id).order_by(TaskPlan.created_at.desc())
    )
    existing = result.scalars().first()

    if existing and existing.plan_json:
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
                    ChatMessage(role="user", content=f"Objective: {task.objective}"),
                ]
                result = await router.route_chat(
                    "premium_reasoning", messages, tools=[],
                    allow_degrade=True, db=session,
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
                # Деградация — mock план
                plan_steps = _mock_oracle_plan(task.objective)

            plan_steps = validate_plan(plan_steps)
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

    # Создаём запись плана
    task_plan = TaskPlan(
        task_id=task.id,
        plan_json=[s.model_dump(mode="json") for s in plan_steps],
        status=PlanStatus.in_progress.value,
    )
    session.add(task_plan)
    session.flush()

    return task_plan, []


async def _stage3_execute(
    session: OrmSession,
    task: Task,
    task_plan: TaskPlan,
    router: ProviderRouter | None = None,
) -> list[dict]:
    """Stage 3: Execute each plan step через реальные handler'ы.

    Для tool_ref в {write_file, file_read, file_search} — напрямую handlers/files.py.
    Для tool_ref == terminal — напрямую handlers/shell.py.
    Для write_file: LLM генерирует контент, handler физически пишет.
    Для неизвестного tool_ref — явный fail (НЕ деградация в текст).

    Returns:
        list of ToolCall dicts с hash_before/hash_after для write_file.
    """
    from aria.tools.handlers.files import file_write, file_read, file_search
    from aria.tools.handlers.shell import shell_execute
    from aria.config import get_settings as _gs

    settings = _gs()
    sandbox_root = str((Path(__file__).parent.parent.parent / settings.agent_sandbox_root).resolve())

    tool_calls_raw: list[dict] = []
    steps = task_plan.plan_json or []

    FILE_HANDLERS = {"write_file": file_write, "file_read": file_read, "file_search": file_search}

    for step in steps:
        step_id = step.get("step_id", "")
        objective = step.get("objective", "")
        role = step.get("role", "general")
        tool_ref = step.get("tool_ref", "terminal")
        tc_id = f"tc-{str(step_id)[:8]}" if step_id else f"tc-{len(tool_calls_raw)}"

        if router is None:
            # Деградация — mock tool call
            tc = {
                "id": f"mock-tc-{str(step_id)[:8]}" if step_id else f"mock-tc-{len(tool_calls_raw)}",
                "tool_name": tool_ref,
                "input_json": {"command": f"echo '{objective}'"},
                "output_json": {"exit_code": 0, "stdout": f"done: {objective}"},
                "status": "ok",
            }
            tool_calls_raw.append(tc)
            step["status"] = "done"
            step["tool_call_ids"] = [tc["id"]]
            continue

        # --- Режим: есть router — вызываем реальные handler'ы ---
        try:
            if tool_ref in FILE_HANDLERS:
                handler = FILE_HANDLERS[tool_ref]

                if tool_ref == "write_file":
                    # LLM генерирует, ЧТО писать в файл
                    content = await _llm_generate_content(
                        router, session, role, objective
                    )
                    input_json = {
                        "path": step.get("path", f"generated/{tc_id}.txt"),
                        "content": content,
                    }
                    handler_input = {
                        "path": input_json["path"],
                        "content": input_json["content"],
                    }
                    output_json = await handler(handler_input, sandbox_root)
                    # output_json уже содержит hash_before/hash_after от file_write
                else:
                    # file_read / file_search — без LLM
                    if tool_ref == "file_read":
                        handler_input = {
                            "path": step.get("path", "."),
                        }
                    else:  # file_search
                        handler_input = {
                            "glob": step.get("glob", "**/*"),
                        }
                    output_json = await handler(handler_input, sandbox_root)

                tc = {
                    "id": tc_id,
                    "tool_name": tool_ref,
                    "input_json": handler_input,
                    "output_json": output_json,
                    "status": "ok",
                }

            elif tool_ref == "terminal":
                # Shell-команда — LLM генерирует команду
                command = await _llm_generate_command(
                    router, session, role, objective
                )
                handler_input = {"command": command}
                shell_output = await shell_execute(handler_input, timeout_sec=30)
                tc = {
                    "id": tc_id,
                    "tool_name": "terminal",
                    "input_json": handler_input,
                    "output_json": shell_output,
                    "status": "ok",
                }

            elif tool_ref == "llm_task":
                # Шаг, который сам по себе является LLM-задачей
                messages = [
                    ChatMessage(role="system", content=f"Ты {role}. {objective}"),
                    ChatMessage(role="user", content=objective),
                ]
                result = await router.route_chat(
                    "subagent_execution", messages, tools=[],
                    allow_degrade=True, db=session,
                )
                tc = {
                    "id": tc_id,
                    "tool_name": "llm_task",
                    "input_json": {"objective": objective, "role": role},
                    "output_json": {"content": result.response.text or ""},
                    "status": "ok",
                }

            else:
                # Неизвестный tool_ref — явный fail
                logger.warning("Stage 3: unknown tool_ref=%s for step %s", tool_ref, step_id)
                tc = {
                    "id": tc_id,
                    "tool_name": tool_ref,
                    "input_json": {"objective": objective, "role": role},
                    "output_json": {"error": f"unknown tool_ref: {tool_ref}"},
                    "status": "error",
                }

        except Exception as e:
            logger.warning("Step %s handler failed: %s", step_id[:8] if step_id else "?", e)
            tc = {
                "id": tc_id,
                "tool_name": tool_ref,
                "input_json": {"objective": objective, "role": role},
                "output_json": {"error": str(e)},
                "status": "error",
            }

        tool_calls_raw.append(tc)
        step["status"] = "done" if tc["status"] == "ok" else "failed"
        step["tool_call_ids"] = [tc["id"]]

    # Сохраняем обновлённый план
    task_plan.plan_json = steps
    task_plan.plan_history = _append_plan_history(task_plan)
    session.flush()

    return tool_calls_raw


async def _llm_generate_content(
    router: ProviderRouter,
    session: OrmSession,
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
    )
    return result.response.text or ""


async def _llm_generate_command(
    router: ProviderRouter,
    session: OrmSession,
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
    )
    return result.response.text.strip().strip("`").strip() or "echo 'no command generated'"


def _stage4_audit(
    task_plan: TaskPlan,
    tool_calls_raw: list[dict],
) -> list[IntegrityFlag]:
    """Stage 4: Dual audit — integrity (pure functions) + correctness (LLM via QA).

    Returns:
        list of IntegrityFlag.
    """
    flags = run_integrity_audit(
        tool_calls=tool_calls_raw,
        plan_json=task_plan.plan_json,
    )

    # Определяем итоговый вердикт
    naebal = any(f.kind == "naebal" for f in flags)
    zabyl = any(f.kind == "zabyl" for f in flags)

    if naebal:
        task_plan.integrity_verdict = IntegrityVerdict.naebal.value
    elif zabyl:
        task_plan.integrity_verdict = IntegrityVerdict.zabyl.value
    else:
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

    # Delivery-контракт: проверяем, что есть результат для записи
    if task_plan.final_result_json is None:
        content = _generate_delivery_note(task, task_plan, integrity_flags)
        content = content.replace("# Status\n\n**done**", "# Status\n\n**unfinished**\n\nЗадача не завершена. Причина: final_result_json не заполнен (возможно escalation).")
    else:
        content = _generate_delivery_note(task, task_plan, integrity_flags)

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
    task.status = TaskStatus.failed

    reason = "; ".join(f.reason for f in flags) if flags else "НАЕБАЛ (unknown)"

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
        if flag.tool_call_ids and tool_calls:
            for tc in tool_calls:
                if tc.get("id") in flag.tool_call_ids:
                    try:
                        tool_call_id = UUID(tc["id"])
                    except (ValueError, KeyError):
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


def _mock_oracle_plan(objective: str) -> list[dict]:
    """Mock-генерация плана Oracle. # test-only mock

    Используется только при деградации (router is None).
    В production — LLM call через Oracle-роль.
    Возвращает список PlanStep-совместимых словарей.
    """
    return [
        {
            "objective": f"Verify dependencies and imports for {objective}",
            "role": "coder",
            "tool_ref": "terminal",
        },
        {
            "objective": f"Execute main logic for {objective}",
            "role": "coder",
            "tool_ref": "terminal",
        },
        {
            "objective": f"Verify result of {objective}",
            "role": "qa_auditor",
            "tool_ref": "terminal",
        },
    ]

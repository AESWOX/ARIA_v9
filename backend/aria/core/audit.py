"""core/audit.py — §8.3 audit lifecycle + принцип §4.5 'Success определяется
структурой, а не словами модели'.

Verdict строится в два прохода:
1. Структурная проверка (обязательна, не зависит от LLM): все ли запланированные
   tool_calls завершились успешно, есть ли артефакт, нет ли blocked_policy без
   резолюции.
2. Качественная проверка через auditor role (qa_auditor, provider class
   standard_reasoning) — если провайдер недоступен или бюджет исчерпан,
   verdict деградирует в `unaudited`, а не маскирует отсутствие аудита (§25 DoD п.8).
"""
from __future__ import annotations

import json
import re

from sqlalchemy.orm import Session as OrmSession

from aria.config import get_settings
from aria.core import timing
from aria.db import models as m
from aria.db import repository as repo
from aria.core.criteria_audit import (
    STATUS_FAIL,
    check_criteria,
    failed_criteria,
    merge_statuses,
)
from aria.db.enums import AuditVerdict, ToolStatus
from aria.llm.providers.base import ChatMessage
from aria.llm.router import ProviderRouter, ProviderUnavailable


def _structural_check(tool_calls: list[m.ToolCall]) -> tuple[bool, list[str]]:
    # Пустой tool_calls обрабатывается раньше, в run_audit (см. §4.5 note там) —
    # сюда он не долетает.
    missing: list[str] = []

    failed = [c for c in tool_calls if c.status in (ToolStatus.error, ToolStatus.blocked_policy)]
    unresolved_attention = [c for c in tool_calls if c.status == ToolStatus.needs_attention]

    if failed:
        missing.append(f"{len(failed)} tool call(s) завершились с error/blocked_policy: " + ", ".join(c.tool_name for c in failed))
    if unresolved_attention:
        missing.append(f"{len(unresolved_attention)} tool call(s) остались needs_attention без резолюции.")

    return (len(missing) == 0), missing


def _tool_calls_detail(tool_calls: list[m.ToolCall]) -> str:
    """Компактное представление реальных input/output tool calls для qa_auditor —
    без него LLM не может подтвердить, что артефакт соответствует objective."""
    lines: list[str] = []
    for c in tool_calls:
        entry: dict = {"tool": c.tool_name, "status": str(c.status)}
        if c.input_json:
            entry["input"] = c.input_json
        if c.output_json:
            entry["output"] = c.output_json
        lines.append(json.dumps(entry, ensure_ascii=False))
    return "\n".join(lines)


# PR-2: pass-маркер целиком как слово, чтобы "token"/"look" не считались успехом.
_PASS_RE = re.compile(
    r"(?<!\w)(?:ok|pass|passed|success|yes)(?!\w)|(?<!\w)соответств\w*",
    re.IGNORECASE,
)


def _parse_audit_verdict(text: str) -> tuple[AuditVerdict, list[str]]:
    """Парсит ответ qa_auditor в (verdict, issues).

    Основной путь — JSON-обёртка, которую просит система в промпте. Фолбэк —
    keyword-эвристика для моделей, проигнорировавших формат. Неоднозначный
    ответ трактуем консервативно: needs_rework (§25 DoD п.8 — отсутствие
    явного pass не маскируется под успех).
    """
    candidates: list[str] = []
    for block in re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL):
        candidates.append(block.strip())
    if not candidates:
        candidates.append(text.strip())

    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        verdict_raw = str(data.get("verdict", "")).strip().lower()
        raw_issues = data.get("issues", data.get("недостатки", []))
        if isinstance(raw_issues, str):
            raw_issues = [raw_issues]
        issues = [str(i).strip() for i in (raw_issues or []) if str(i).strip()]
        if verdict_raw in ("pass", "ok", "yes", "success"):
            # pass с замечаниями — противоречие, трактуем как needs_rework
            return (AuditVerdict.needs_rework, issues) if issues else (AuditVerdict.pass_, [])
        if verdict_raw in ("needs_info", "need_info", "needs_more_info", "info", "more_info"):
            # П.5.1 ТЗ: нехватка данных — это запрос уточнения, а не переделка.
            return AuditVerdict.needs_info, issues
        if verdict_raw in ("fail", "needs_rework", "rework", "no", "not_ok", "error"):
            return AuditVerdict.needs_rework, issues
        break  # JSON распознан, но verdict неизвестен — уходим в эвристику

    low = text.lower()
    fail_markers = (
        "недостат", "ошибк", "не соответств", "проблем", "исправь",
        "не выполн", "rework", "not ok",
    )
    if any(m in low for m in fail_markers):
        lines = [ln.strip(" -*\t") for ln in text.splitlines() if ln.strip(" -*\t")]
        return AuditVerdict.needs_rework, [ln for ln in lines if ln][:5]

    # PR-2: pass-маркер ищется как отдельное слово. Раньше сравнение шло подстрокой,
    # и "ok" внутри "token"/"look"/"book" засчитывался как успех аудита.
    if _PASS_RE.search(text):
        return AuditVerdict.pass_, []

    return AuditVerdict.needs_rework, ["Неоднозначный ответ qa_auditor — требуется ручная проверка."]

def _parse_criteria_statuses(text: str) -> dict[str, dict]:
    """Достаёт из ответа qa_auditor статусы по каждому criterion_id.

    Ожидаемый вид: {"criteria": [{"id": "ac-1", "status": "pass", "note": "..."}]}.
    Неизвестный статус и отсутствующие id просто не попадают в результат: за них
    держится структурная проверка (criteria_audit), а не догадка парсера.
    """
    out: dict[str, dict] = {}
    blocks = re.findall(r"```(?:json)?\s*(.*?)```", text or "", flags=re.DOTALL) or [text or ""]
    for block in blocks:
        try:
            data = json.loads(block.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        items = data.get("criteria")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            status = str(item.get("status") or "").strip().lower()
            if not cid or status not in ("pass", "fail"):
                continue
            out[cid] = {"status": status, "note": str(item.get("note") or "")}
    return out


@timing.timed("audit")
async def run_audit(
    db: OrmSession,
    session: m.Session,
    task: m.Task,
    router: ProviderRouter | None,
    level: str = "strict",
    auditor_class: str = "standard_reasoning",
    auditor_prefer: str | None = None,
    criteria: list | None = None,
    assumptions: list[str] | None = None,
) -> m.AuditReport:
    """``level``: ``strict`` — структура + проверка второй моделью (по умолчанию); ``light`` — только
    структурная проверка, без вызова модели; ``off`` — аудит отключён владельцем (``unaudited``)."""
    settings = get_settings()

    if level == "off":
        return repo.create_audit_report(
            db, session, task,
            attempt_no=task.audit_attempt_no,
            auditor_role="qa_auditor",
            auditor_model="off",
            budget_degraded=False,
            verdict=AuditVerdict.unaudited,
            plan_vs_fact={"objective": task.objective, "note": "audit disabled in the run profile"},
            tool_success_summary={},
            missing_requirements=[],
            patch_suggestions=[],
            metrics_compared={},
        )

    tool_calls = repo.list_tool_calls(db, task.id)

    if not tool_calls:
        # §4.5: без единого tool call структурно нечего подтверждать — это
        # не провал выполнения (модель просто ответила текстом, например в
        # чисто разговорном обмене), а отсутствие предмета для аудита.
        # НЕ инкрементируем audit_attempt_no и не тратим audit_max_attempts
        # на это: иначе N подряд чисто разговорных /start на одном и том же
        # task_id (main.py post_message переиспользует task_id для чата)
        # необратимо загоняют задачу в failed после audit_max_attempts
        # попыток, хотя ничего структурно не сломано — инцидент 2026-07-22.
        report = repo.create_audit_report(
            db,
            session,
            task,
            attempt_no=task.audit_attempt_no,
            auditor_role="qa_auditor",
            auditor_model="structural-only",
            budget_degraded=False,
            verdict=AuditVerdict.needs_rework,
            plan_vs_fact={"objective": task.objective, "note": "no tool calls — conversational turn, not a failure"},
            tool_success_summary={"total": 0, "ok": 0, "error": 0, "blocked_policy": 0},
            missing_requirements=["Разговорный ход без tool calls — не расходует audit_attempt_no."],
            patch_suggestions=[],
            metrics_compared={},
        )
        return report

    attempt_no = task.audit_attempt_no + 1
    task.audit_attempt_no = attempt_no
    db.flush()

    structural_ok, missing = _structural_check(tool_calls)

    # PR-6: критерии приёмки TaskSpec проверяются по фактам, до модели.
    criteria_status = check_criteria(criteria or [], [
        {
            "id": str(c.id),
            "tool_name": c.tool_name,
            "status": getattr(c.status, "value", str(c.status)),
            "input_json": c.input_json or {},
            "output_json": c.output_json or {},
        }
        for c in tool_calls
    ])
    failed_by_fact = failed_criteria(criteria_status)

    tool_success_summary = {
        "total": len(tool_calls),
        "ok": len([c for c in tool_calls if c.status == ToolStatus.ok]),
        "error": len([c for c in tool_calls if c.status == ToolStatus.error]),
        "blocked_policy": len([c for c in tool_calls if c.status == ToolStatus.blocked_policy]),
    }

    budget_degraded = False
    verdict: AuditVerdict
    patch_suggestions: list[str] = []
    auditor_model = "structural-only"

    if not structural_ok:
        verdict = AuditVerdict.needs_rework if attempt_no < settings.audit_max_attempts else AuditVerdict.fail_after_max_attempts
        patch_suggestions = [f"Исправить: {reason}" for reason in missing]
    elif failed_by_fact:
        # Критерий провален по фактам: мнение модели тут не требуется и не спрашивается.
        verdict = AuditVerdict.needs_rework if attempt_no < settings.audit_max_attempts else AuditVerdict.fail_after_max_attempts
        auditor_model = "criteria-deterministic"
        patch_suggestions = [
            f"Критерий приёмки {cid} не выполнен: {criteria_status[cid].get('note')}" for cid in failed_by_fact
        ]
    elif level == "light":
        verdict = AuditVerdict.pass_  # лёгкий аудит: только структура, второй модели нет
    else:
        # структура в порядке — пробуем качественный проход через auditor role
        if router is None:
            verdict = AuditVerdict.unaudited
            budget_degraded = True
        else:
            try:
                criteria_block = ""
                if criteria:
                    criteria_block = (
                        "\nAcceptance criteria: "
                        + json.dumps([dict(c) if isinstance(c, dict) else c for c in criteria], ensure_ascii=False, default=str)
                        + "\nПроставь статус КАЖДОМУ критерию в поле criteria."
                    )
                messages = [
                    ChatMessage(
                        role="system",
                        content=(
                            "Ты qa_auditor. Проверь, соответствует ли результат objective задачи и её критериям приёмки.\n"
                            "Ответь строго в формате JSON: "
                            "{\"verdict\": \"pass\" | \"needs_rework\" | \"needs_info\", \"issues\": [\"недостаток 1\", \"...\"], "
                            "\"criteria\": [{\"id\": \"ac-1\", \"status\": \"pass\" | \"fail\", \"note\": \"коротко\"}]}. "
                            "pass — только если результат полностью соответствует objective, все критерии выполнены и замечаний нет."
                        ),
                    ),
                    ChatMessage(
                        role="user",
                        content=(
                            f"Objective: {task.objective}\n"
                            f"Tool calls summary: {tool_success_summary}\n"
                            f"Tool calls detail: {_tool_calls_detail(tool_calls)}"
                            f"{criteria_block}"
                        ),
                    ),
                ]
                with timing.stage("audit_model_call"):  # вторая модель отдельно от структурной проверки
                    result = await router.route_chat(
                        auditor_class, messages, tools=[], allow_degrade=True, db=db, prefer_provider_id=auditor_prefer,
                        resilient=True, fallback_classes=("standard_reasoning", "free_tier_reasoning"),
                        reject_empty=True,
                    )
                auditor_model = result.provider_id
                budget_degraded = result.degraded_to_free
                verdict, parsed_issues = _parse_audit_verdict(result.response.text)
                if parsed_issues:
                    patch_suggestions = [f"QA: {issue}" for issue in parsed_issues]
                # PR-6: статусы критериев от модели накладываются на структурные.
                criteria_status = merge_statuses(criteria_status, _parse_criteria_statuses(result.response.text))
                model_failed = failed_criteria(criteria_status)
                if model_failed:
                    verdict = AuditVerdict.needs_rework
                    patch_suggestions += [
                        f"Критерий приёмки {cid} не выполнен: {criteria_status[cid].get('note')}" for cid in model_failed
                    ]
            except ProviderUnavailable:
                verdict = AuditVerdict.unaudited
                budget_degraded = True

    report = repo.create_audit_report(
        db,
        session,
        task,
        attempt_no=attempt_no,
        auditor_role="qa_auditor",
        auditor_model=auditor_model,
        budget_degraded=budget_degraded,
        verdict=verdict,
        plan_vs_fact={
            "objective": task.objective,
            "tool_success_summary": tool_success_summary,
            "criteria_status": criteria_status,
            "assumptions": list(assumptions or []),
        },
        tool_success_summary=tool_success_summary,
        missing_requirements=missing,
        patch_suggestions=patch_suggestions,
        metrics_compared={"criteria": criteria_status},
    )
    return report

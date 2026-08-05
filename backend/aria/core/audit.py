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

from sqlalchemy.orm import Session as OrmSession

from aria.config import get_settings
from aria.db import models as m
from aria.db import repository as repo
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


async def run_audit(
    db: OrmSession,
    session: m.Session,
    task: m.Task,
    router: ProviderRouter | None,
) -> m.AuditReport:
    settings = get_settings()

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
    else:
        # структура в порядке — пробуем качественный проход через auditor role
        if router is None:
            verdict = AuditVerdict.unaudited
            budget_degraded = True
        else:
            try:
                messages = [
                    ChatMessage(
                        role="system",
                        content="Ты qa_auditor. Проверь, соответствует ли результат objective задачи. Ответь коротко: OK или список недостатков.",
                    ),
                    ChatMessage(role="user", content=f"Objective: {task.objective}\nTool calls: {tool_success_summary}"),
                ]
                result = await router.route_chat("standard_reasoning", messages, tools=[], allow_degrade=True, db=db)
                auditor_model = result.provider_id
                budget_degraded = result.degraded_to_free
                verdict = AuditVerdict.pass_
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
        plan_vs_fact={"objective": task.objective, "tool_success_summary": tool_success_summary},
        tool_success_summary=tool_success_summary,
        missing_requirements=missing,
        patch_suggestions=patch_suggestions,
        metrics_compared={},
    )
    return report

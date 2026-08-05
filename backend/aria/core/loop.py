"""core/loop.py — §6.1 верхнеуровневый поток + §6.2 обязательный модуль.

Один основной event loop (принцип §4.2). Каждая итерация: LLM выбирает
tool call или финальный ответ -> валидация registry/whitelist/high-risk ->
исполнение -> запись в БД -> WS event -> следующая итерация, до
loop.max_iterations (15) или финального ответа. После этого — audit-loop.

ВАЖНО (фикс database is locked): event_bus.emit() всегда вызывается с
db=db, если мы уже внутри открытого session_scope(). Открытие второй,
независимой сессии поверх ещё не закоммиченной — гарантированный
self-deadlock на SQLite (внешняя транзакция держит writer-lock, внутренняя
ждёт его же до истечения busy_timeout и падает с 'database is locked').

Также: await compression.maybe_compress(...) вызывается ВНЕ
session_scope() — сетевой HTTP-запрос внутри открытой write-транзакции
держит writer-lock на всё время round-trip (включая ретраи KeyPool),
блокируя любые параллельные записи.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session as OrmSession

from aria.config import get_settings
from aria.core import approvals
from aria.core.audit import run_audit
from aria.core.events import event_bus
from aria.llm import compression
from aria.core.roles import get_role
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import AuditVerdict, SourceTrust, TaskStatus, ToolStatus
from aria.llm.providers.base import ChatMessage
from aria.llm.router import ProviderRouter, ProviderUnavailable
from aria.tools.registry import get_tool
from aria.tools.validators import ToolValidationError, assert_role_allowed, is_high_risk_command
from aria.core.guardrails import ToolCallGuardrailController

logger = logging.getLogger("local_agent.loop")


def _tool_schemas_for_role(tool_whitelist: tuple[str, ...]) -> list[dict]:
    schemas = []
    for name in tool_whitelist:
        try:
            spec = get_tool(name)
        except KeyError:
            continue
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": spec.tool_name,
                    "description": spec.description,
                    "parameters": spec.input_schema,
                },
            }
        )
    return schemas


def _load_persona() -> str:
    """Load persona.md from the prompts directory."""
    import os
    p = os.path.join(os.path.dirname(__file__), "..", "prompts", "persona.md")
    try:
        with open(p) as f:
            return "\n" + f.read()
    except FileNotFoundError:
        logger.warning("persona.md not found at %s", p)
        return ""


def _build_messages(role_prompt: str, history: list[m.Message]) -> list[ChatMessage]:
    messages = [ChatMessage(role="system", content=role_prompt)]
    for msg in history:
        content = msg.content
        if msg.source_trust == SourceTrust.untrusted:
            # §14.1: untrusted content изолируется явными delimiters, не может менять system policy
            content = f"<untrusted_content source_trust=\"untrusted\">\n{content}\n</untrusted_content>"
        messages.append(ChatMessage(role=msg.role if msg.role in ("user", "assistant", "system") else "user", content=content))
    return messages


# ══════════════════════════════════════════════════════════════════
# LOW-LEVEL: один ReAct-цикл для суб-агента (старый path)
# Используется main.py для прямых запросов и саб-агентов.
# НЕ вызывается executor.run_task — другой pipeline (Stage 1-7).
# ══════════════════════════════════════════════════════════════════


async def execute_agent_loop(task_id: uuid.UUID, router: ProviderRouter, sandbox_root: str) -> None:
    """Run the agent loop for a given task."""
    settings = get_settings()
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task is None:
            raise ValueError(f"unknown task_id={task_id}")
        session = repo.get_session(db, task.session_id)
        role = get_role(task.role)

        if task.status not in (TaskStatus.approved, TaskStatus.in_progress, TaskStatus.needs_rework):
            raise ValueError(f"task {task_id} is not runnable from status={task.status}")

        repo.set_task_status(db, task, TaskStatus.in_progress)
        event_bus.emit("task.status_changed", {"status": TaskStatus.in_progress.value}, session_id=session.id, task_id=task.id, db=db)

    tool_whitelist = role.tool_whitelist
    tool_schemas = _tool_schemas_for_role(tool_whitelist)
    role_prompt = f"Ты в роли {role.role_id}. {role.description}\nДоступные инструменты: {', '.join(tool_whitelist) or 'нет'}."
    role_prompt += _load_persona()

    iterations = 0
    final_verdict_needed = True
    guardrails = ToolCallGuardrailController()

    while iterations < settings.loop_max_iterations:
        iterations += 1

        with session_scope() as db:
            task = repo.get_task(db, task_id)
            session = repo.get_session(db, task.session_id)
            if task.status == TaskStatus.awaiting_attention:
                logger.info("task %s paused awaiting_attention, stopping loop iteration", task_id)
                return  # loop будет продолжен вызовом resume_after_approval
            session_id_for_compression = session.id

        # ВНЕ session_scope(): сетевой запрос компрессии не должен держать
        # writer-lock на всё время HTTP round-trip (см. docstring модуля).
        await compression.maybe_compress(session_id_for_compression)

        with session_scope() as db:
            task = repo.get_task(db, task_id)
            session = repo.get_session(db, task.session_id)
            history = repo.list_messages_for_prompt(db, session.id)
            messages = _build_messages(role_prompt, history)

        try:
            routing = await router.route_chat(role.default_model_policy, messages, tool_schemas, allow_degrade=True)
        except ProviderUnavailable as exc:
            logger.error("task %s: provider_unavailable: %s", task_id, exc)
            with session_scope() as db:
                task = repo.get_task(db, task_id)
                repo.set_task_status(db, task, TaskStatus.failed, error_code="provider_unavailable", error_message=str(exc))
                event_bus.emit("task.status_changed", {"status": "failed", "error": str(exc)}, session_id=task.session_id, task_id=task.id, db=db)
            return

        response = routing.response

        if not response.tool_calls:
            with session_scope() as db:
                session = repo.get_session(db, task.session_id)
                repo.append_message(db, session, role="assistant", content=response.text or "")
            break

        for call in response.tool_calls:
            with session_scope() as db:
                task = repo.get_task(db, task_id)
                session = repo.get_session(db, task.session_id)
                try:
                    assert_role_allowed(role.role_id, tool_whitelist, call.tool_name)
                    spec = get_tool(call.tool_name)
                except (ToolValidationError, KeyError) as exc:
                    repo.append_message(db, session, role="tool", content=f"blocked_policy: {exc}")
                    continue

                if call.tool_name == "shell_execute":
                    command = call.arguments.get("command", "")
                    if is_high_risk_command(command):
                        item = approvals.check_command_and_maybe_request_approval(db, session, task, command)
                        event_bus.emit(
                            "attention_item.created",
                            {"id": str(item.id), "type": item.type.value, "title": item.title},
                            session_id=session.id,
                            task_id=task.id,
                            db=db,
                        )
                        event_bus.emit("task.status_changed", {"status": TaskStatus.awaiting_attention.value}, session_id=session.id, task_id=task.id, db=db)
                        return  # пауза до резолюции attention item

                # Создаём ToolCall запись для всех tool call'ов
                tool_call_row = repo.start_tool_call(
                    db, session, task, spec.tool_name, role.role_id, spec.risk_level.value, call.arguments,
                    source_trust_snapshot=session.source_trust_aggregate,
                )
                event_bus.emit(
                    "tool_call.updated",
                    {"id": str(tool_call_row.id), "tool_name": spec.tool_name, "status": "running"},
                    session_id=session.id,
                    task_id=task.id,
                    db=db,
                )

                # §7.2: delegate_task — сохраняем контекст, затем выполняем ВНЕ транзакции
                if call.tool_name == "delegate_task":
                    from aria.tools.registry import _delegate_task as _do_delegate

                    _pending_delegate = {
                        "tool_call_row": tool_call_row,
                        "session_id": session.id,
                        "delegation_depth": task.delegation_depth,
                        "do_delegate": _do_delegate,
                    }
                else:
                    _pending_delegate = None

            # — ВНЕ session_scope() —

            if _pending_delegate:
                delegate_result = await _pending_delegate["do_delegate"](
                    call.arguments,
                    timeout_sec=120,
                    sandbox_root=sandbox_root,
                    router=router,
                    session_id=_pending_delegate["session_id"],
                    parent_task_id=task_id,
                    delegation_depth=_pending_delegate["delegation_depth"],
                )
                with session_scope() as db:
                    task = repo.get_task(db, task_id)
                    session = repo.get_session(db, task.session_id)
                    repo.append_message(
                        db, session, role="tool",
                        content=f"delegate_task -> {delegate_result.get('status', 'unknown')}",
                        content_json={"tool_name": "delegate_task", "output": delegate_result, "status": delegate_result.get("status", "unknown")},
                    )
                    repo.finish_tool_call(db, _pending_delegate["tool_call_row"], ToolStatus.ok, output_json=delegate_result)
                    event_bus.emit(
                        "tool_call.updated",
                        {"id": str(_pending_delegate["tool_call_row"].id), "tool_name": "delegate_task", "status": delegate_result.get("status", "unknown")},
                        session_id=session.id,
                        task_id=task.id,
                        db=db,
                    )
                continue

            # Стандартный tool call — guardrails + _execute_tool
            guardrail_decision = guardrails.before_call(spec.tool_name, call.arguments)
            if guardrail_decision.action in ("block",):
                with session_scope() as db:
                    repo.finish_tool_call(
                        db, tool_call_row, ToolStatus.blocked_policy,
                        output_json={"guardrail_code": guardrail_decision.code,
                                      "message": guardrail_decision.message},
                        error_code=guardrail_decision.code,
                        error_message=guardrail_decision.message,
                    )
                    session = repo.get_session(db, task.session_id)
                    repo.append_message(
                        db, session, role="tool",
                        content=f"{spec.tool_name} -> guardrail_blocked: {guardrail_decision.message}",
                    )
                continue

            output, status, error_code, error_message = await _execute_tool(spec, call.arguments, sandbox_root)

            # После tool call — guardrail recording для loop detection
            guardrails.after_call(spec.tool_name, call.arguments, output)

            with session_scope() as db:
                repo.finish_tool_call(db, tool_call_row, status, output_json=output, error_code=error_code, error_message=error_message)
                session = repo.get_session(db, task.session_id)
                repo.append_message(db, session, role="tool", content_json={"tool_name": spec.tool_name, "output": output, "status": status.value}, content=f"{spec.tool_name} -> {status.value}")
                event_bus.emit(
                    "tool_call.updated",
                    {"id": str(tool_call_row.id), "tool_name": spec.tool_name, "status": status.value},
                    session_id=session.id,
                    task_id=task.id,
                    db=db,
                )

    await finalize_task(task_id, router)


async def _execute_tool(spec, arguments: dict, sandbox_root: str):
    settings = get_settings()
    try:
        import asyncio

        output = await asyncio.wait_for(
            spec.handler(input_json=arguments, timeout_sec=spec.timeout_sec, sandbox_root=sandbox_root),
            timeout=spec.timeout_sec,
        )
        return output, ToolStatus.ok, None, None
    except TimeoutError:
        return None, ToolStatus.timeout, "tool_timeout", f"{spec.tool_name} exceeded {spec.timeout_sec}s"
    except Exception as exc:  # noqa: BLE001
        return None, ToolStatus.error, "tool_error", str(exc)


# ── friend_memory observer (§6 v11) ──────────────────────────────────────


def _observe_user_patterns(db: OrmSession, session: m.Session, task: m.Task) -> None:
    """Scan session history for user style/preference signals and persist to friend_memory.

    Currently writes a heartbeat entry per session to confirm the write point fires.
    Future iterations may use LLM-based summarization for deeper pattern extraction.
    """
    history = repo.list_messages_for_prompt(db, session.id)
    user_msgs = [m for m in history if m.role == "user"]
    assistant_msgs = [m for m in history if m.role == "assistant"]

    # Observation 1: session_exists — confirms the agent ran at all
    repo.upsert_friend_memory(
        db,
        category="session",
        key=str(session.id),
        value_json={
            "session_title": session.title or "",
            "task_id": str(task.id),
            "task_objective": (task.objective or "")[:300],
            "user_messages": len(user_msgs),
            "assistant_messages": len(assistant_msgs),
            "outcome": task.status.value,
        },
        source="agent",
        session_id=session.id,
    )


async def finalize_task(task_id: uuid.UUID, router: ProviderRouter) -> None:
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        session = repo.get_session(db, task.session_id)
        repo.set_task_status(db, task, TaskStatus.under_audit)
        event_bus.emit("task.status_changed", {"status": TaskStatus.under_audit.value}, session_id=session.id, task_id=task.id, db=db)

        # §6 (v11) — observe user patterns from this session before audit
        _observe_user_patterns(db, session, task)

        report = None
        try:
            report = await run_audit(db, session, task, router)
        except Exception as exc:  # noqa: BLE001
            logger.exception("audit crashed for task %s", task_id)
            repo.set_task_status(db, task, TaskStatus.failed, error_code="audit_crash", error_message=str(exc))
            event_bus.emit("task.status_changed", {"status": "failed"}, session_id=session.id, task_id=task.id, db=db)
            return

        event_bus.emit(
            "audit_report.created",
            {"verdict": report.verdict.value, "attempt_no": report.attempt_no},
            session_id=session.id,
            task_id=task.id,
            db=db,
        )

        if report.verdict == AuditVerdict.pass_:
            repo.set_task_status(db, task, TaskStatus.done)
        elif report.verdict == AuditVerdict.unaudited:
            repo.set_task_status(db, task, TaskStatus.done_unaudited)
        elif report.verdict == AuditVerdict.needs_rework:
            repo.set_task_status(db, task, TaskStatus.needs_rework)
        else:
            repo.set_task_status(db, task, TaskStatus.failed, error_code="fail_after_max_attempts")

        event_bus.emit("task.status_changed", {"status": task.status.value}, session_id=session.id, task_id=task.id, db=db)


async def resume_after_approval(task_id: uuid.UUID, router: ProviderRouter, sandbox_root: str, attention_item: m.AttentionItem) -> None:
    """Вызывается api-слоем после approve high_risk_shell: исполняет ранее
    заблокированную команду, затем продолжает основной loop."""
    from aria.tools.registry import get_tool as _get_tool

    command = attention_item.payload_json.get("command")
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        session = repo.get_session(db, task.session_id)
        spec = _get_tool("shell_execute")
        tool_call_row = repo.start_tool_call(
            db, session, task, spec.tool_name, task.role, spec.risk_level.value, {"command": command},
            source_trust_snapshot=session.source_trust_aggregate,
        )

    output, status, error_code, error_message = await _execute_tool(spec, {"command": command}, sandbox_root)

    with session_scope() as db:
        repo.finish_tool_call(db, tool_call_row, status, output_json=output, error_code=error_code, error_message=error_message)
        session = repo.get_session(db, task.session_id)
        repo.append_message(db, session, role="tool", content_json={"tool_name": "shell_execute", "output": output, "status": status.value}, content=f"shell_execute (approved) -> {status.value}")
        event_bus.emit("tool_call.updated", {"id": str(tool_call_row.id), "status": status.value}, session_id=session.id, task_id=task.id, db=db)

    await run_task(task_id, router, sandbox_root)

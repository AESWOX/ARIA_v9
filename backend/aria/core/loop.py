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

import asyncio
import json
import logging
import uuid

import httpx

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from aria.config import get_settings
from aria.core import approvals, runprofile
from aria.core.audit import run_audit
from aria.core.events import event_bus
from aria.llm import compression
from aria.core.roles import get_role
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import AuditVerdict, SourceTrust, TaskStatus, ToolStatus
from aria.llm.providers.base import ChatMessage
from aria.llm.router import ProviderRouter, ProviderUnavailable, tried_suffix
from aria.tools.registry import get_tool
from aria.core import toolhooks
from aria.tools.validators import (
    ToolValidationError,
    assert_role_allowed,
    classify_shell_command,
)
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
        with open(p, encoding="utf-8") as f:
            return "\n" + f.read()
    except FileNotFoundError:
        logger.warning("persona.md not found at %s", p)
        return ""


# A22: сколько символов результата тула показываем модели (остальное — в БД и UI).
MAX_TOOL_RESULT_CHARS = 6000


# A26: сколько раз подряд цикл подавляет немедленный повтор уже выполненного вызова, прежде чем завершить задачу.
MAX_REPEAT_SUPPRESSED = 2
TOOL_RESULT_NOTE = (
    "\n(Это результат вызова, который ты уже сделал и который уже выполнен. Не вызывай тот же тул с теми же "
    "аргументами снова: используй результат и дай итоговый ответ.)"
)


def _is_immediate_repeat(history: list, tool_name: str, arguments: dict) -> bool:
    """A26: модель просит тот же вызов (тул + аргументы), результат которого последним лежит в истории и был ok."""
    if not history:
        return False
    last = history[-1]
    payload = getattr(last, "content_json", None)
    if getattr(last, "role", None) != "tool" or not isinstance(payload, dict):
        return False
    return (
        payload.get("tool_name") == tool_name
        and payload.get("status") in ("ok", "repeat_suppressed")
        and payload.get("arguments") == dict(arguments or {})
    )


def _render_tool_result(msg: m.Message) -> str | None:
    """Текст результата тула для модели или None, если у сообщения нет результата.

    До A22 модель видела только «file_read -> ok» без тела результата: ни содержимого
    файла, ни stdout команды. Результат — недоверенные данные (файл, веб, вывод команды
    могут содержать инструкции-ловушки), поэтому он идёт в явных разделителях (§14.1).
    """
    payload = msg.content_json
    if not isinstance(payload, dict) or "output" not in payload:
        return None
    name = str(payload.get("tool_name") or "tool")
    status = str(payload.get("status") or "unknown")
    try:
        body = json.dumps(payload.get("output"), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        body = str(payload.get("output"))
    if len(body) > MAX_TOOL_RESULT_CHARS:
        body = f"{body[:MAX_TOOL_RESULT_CHARS]}...[truncated {len(body) - MAX_TOOL_RESULT_CHARS} chars]"
    args = payload.get("arguments")
    args_attr = ""
    if args:
        try:
            args_attr = " arguments=" + json.dumps(json.dumps(args, ensure_ascii=False, default=str)[:300], ensure_ascii=False)
        except (TypeError, ValueError):
            args_attr = ""
    # A26: в истории нет хода ассистента с вызовом тула, поэтому модель не знала, что вызов уже сделан и выполнен,
    # и просила его снова (живой смоук 12.10: shell_execute x3, write_note x2). Говорим это прямо.
    note = TOOL_RESULT_NOTE if status == "ok" else ""
    return (
        f'<tool_result tool="{name}" status="{status}" source_trust="untrusted"{args_attr}>\n{body}\n</tool_result>{note}'
    )


def _build_messages(role_prompt: str, history: list[m.Message]) -> list[ChatMessage]:
    messages = [ChatMessage(role="system", content=role_prompt)]
    for msg in history:
        content = msg.content
        if msg.role == "tool":
            rendered = _render_tool_result(msg)
            if rendered is not None:
                content = rendered
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


# Запасные классы агентского цикла: standard, затем быстрая модель (free_tier: на ней чат и так работает).
# Без free_tier агент не имел запасного пути, если оба standard-провайдера отвечали 402/404.
AGENT_FALLBACK_CLASSES = ("standard_reasoning", "free_tier_reasoning")

# A23: сколько раз подряд одна и та же делегация может упасть, прежде чем цикл перестанет её исполнять.
MAX_DELEGATE_RETRIES = 2


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

    await toolhooks.lifecycle("task_start", session_id=session.id, task_id=task_id, role=task.role)

    tool_whitelist = role.tool_whitelist
    try:
        # H7: тулы подключённых MCP-серверов (только для ролей из MCP_ROLES); сбой сервера не ломает задачу.
        from aria.mcp.manager import get_manager as _get_mcp

        _mcp = _get_mcp()
        await _mcp.ensure_loaded()
        tool_whitelist = tuple(role.tool_whitelist) + _mcp.tool_names_for_role(role.role_id)
    except Exception:  # noqa: BLE001
        logger.warning("MCP tools unavailable for task %s", task_id, exc_info=True)
    tool_schemas = _tool_schemas_for_role(tool_whitelist)
    with session_scope() as db:
        _t = repo.get_task(db, task_id)
        profile = runprofile.get_profile(db, _t.session_id)
        delegation_depth = _t.delegation_depth or 0
    role_prompt = f"Ты в роли {role.role_id}. {role.description}\nДоступные инструменты: {', '.join(tool_whitelist) or 'нет'}."
    role_prompt += (
        "\nЭто список инструментов ТВОЕЙ РОЛИ, а не всей системы: в реестре ARIA их больше, часть доступна только другим ролям "
        "(write/shell/delegate — у obsidian_keeper, coder, orchestrator). Не называй этот список «всеми инструментами системы». "
        "Число навыков, заметок, задач и т.п. называй только после реального вызова инструмента (skill_lookup возвращает total)."
    )
    role_prompt += _load_persona()
    _hint = runprofile.thinking_hint(profile)
    if _hint:
        role_prompt += "\n\n" + _hint

    iterations = 0
    final_verdict_needed = True
    guardrails = ToolCallGuardrailController()
    delegate_failures: dict[tuple[str, str], int] = {}
    repeat_suppressed = 0

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
            model_class, preferred_model = runprofile.resolve_for_task(profile, role.default_model_policy, delegation_depth, router)
            # resilient: повтор 5xx и переход на запасной класс/провайдера (как в чате, A20). Без этого один
            # 503/429 от Gemini сразу ронял всю задачу (живой смоук 11.10: агентские стадии падали за 1.7 с).
            routing = await router.route_chat(
                model_class, messages, tool_schemas, allow_degrade=True, prefer_provider_id=preferred_model,
                resilient=True, fallback_classes=AGENT_FALLBACK_CLASSES,
            )
        except (ProviderUnavailable, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                # тело ответа провайдера — единственное, что объясняет 400/404 (модель, схема тулов, параметры)
                code = f"provider_http_{exc.response.status_code}"
                who = getattr(exc, "provider_id", None) or "the model provider"
                message = f"HTTP {exc.response.status_code} from {who}: {exc.response.text[:300]}{tried_suffix(exc)}"
            else:
                code, message = "provider_unavailable", str(exc)
            logger.error("task %s: %s: %s", task_id, code, message)
            await toolhooks.lifecycle("on_error", session_id=session.id, task_id=task_id, code=code, message=message)
            with session_scope() as db:
                task = repo.get_task(db, task_id)
                repo.set_task_status(db, task, TaskStatus.failed, error_code=code, error_message=message[:500])
                event_bus.emit("task.status_changed", {"status": "failed", "error": message}, session_id=task.session_id, task_id=task.id, db=db)
            return

        response = routing.response

        if not response.tool_calls:
            with session_scope() as db:
                session = repo.get_session(db, task.session_id)
                repo.append_message(db, session, role="assistant", content=response.text or "")
            break

        approval_ctx = None
        _parent_paused = False
        delegate_batch: list[dict] = []  # B5-D1: delegate_task из одного ответа исполняются пачкой, параллельно
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

                if _is_immediate_repeat(history, call.tool_name, call.arguments):
                    repeat_suppressed += 1
                    logger.warning("task %s: immediate repeat of %s suppressed (%d)", task_id, call.tool_name, repeat_suppressed)
                    repo.append_message(
                        db, session, role="tool",
                        content=f"{call.tool_name} -> repeat_suppressed",
                        content_json={
                            "tool_name": call.tool_name, "arguments": dict(call.arguments or {}), "status": "repeat_suppressed",
                            "output": {"message": "Этот вызов уже выполнен, его результат выше. Не повторяй его: дай итоговый ответ."},
                        },
                    )
                    continue

                if call.tool_name.startswith("mcp__") and spec.requires_approval:
                    # H7: тул стороннего MCP-сервера, который может менять данные, — только после Approve.
                    from aria.mcp.manager import get_manager as _mcp_mgr

                    origin = _mcp_mgr().origin(call.tool_name) or ("", "")
                    item = approvals.request_mcp_tool_approval(
                        db, session, task, call.tool_name, call.arguments, server=origin[0], tool=origin[1],
                    )
                    event_bus.emit(
                        "attention_item.created",
                        {"id": str(item.id), "type": item.type.value, "title": item.title},
                        session_id=session.id,
                        task_id=task.id,
                        db=db,
                    )
                    event_bus.emit("task.status_changed", {"status": TaskStatus.awaiting_attention.value}, session_id=session.id, task_id=task.id, db=db)
                    approval_ctx = (session.id, task.id, str(item.id), f"{call.tool_name} {json.dumps(call.arguments, ensure_ascii=False, default=str)[:300]}")
                    break  # пауза до Approve; остальные tool calls ответа отбрасываются

                if call.tool_name == "shell_execute":
                    command = call.arguments.get("command", "")
                    if classify_shell_command(command) != "allow":
                        item = approvals.check_command_and_maybe_request_approval(db, session, task, command)
                        event_bus.emit(
                            "attention_item.created",
                            {"id": str(item.id), "type": item.type.value, "title": item.title},
                            session_id=session.id,
                            task_id=task.id,
                            db=db,
                        )
                        event_bus.emit("task.status_changed", {"status": TaskStatus.awaiting_attention.value}, session_id=session.id, task_id=task.id, db=db)
                        approval_ctx = (session.id, task.id, str(item.id), command)
                        break  # пауза: остальные tool calls ответа отбрасываются (как раньше при return)

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
                _dkey = (str(call.arguments.get("role", "")), str(call.arguments.get("objective", ""))[:200])
                if delegate_failures.get(_dkey, 0) >= MAX_DELEGATE_RETRIES:
                    # A23: модель повторяла одну и ту же упавшую делегацию десятки раз. Третий раз не исполняем.
                    msg = (
                        f"this delegation already failed {delegate_failures[_dkey]} times; do not retry it. "
                        "Do the work yourself with your own tools or report the failure to the owner."
                    )
                    with session_scope() as db:
                        task = repo.get_task(db, task_id)
                        session = repo.get_session(db, task.session_id)
                        repo.finish_tool_call(
                            db, _pending_delegate["tool_call_row"], ToolStatus.blocked_policy,
                            output_json={"guardrail_code": "delegate_repeat", "message": msg},
                            error_code="delegate_repeat", error_message=msg,
                        )
                        repo.append_message(db, session, role="tool", content=f"delegate_task -> guardrail_blocked: {msg}")
                    continue
                # B5-D1: не ждём ребёнка здесь — копим вызов, исполним пачкой после разбора всех tool calls ответа.
                delegate_batch.append({"call": call, "pending": _pending_delegate, "dkey": _dkey})
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

            if guardrail_decision.action == "warn":
                # Встроить warning в историю, чтобы LLM сменил стратегию на следующей итерации.
                with session_scope() as db:
                    session = repo.get_session(db, task.session_id)
                    repo.append_message(
                        db, session, role="tool",
                        content=f"{spec.tool_name} -> guardrail_warning: {guardrail_decision.message}",
                    )

            # H4: pre_tool хуки (могут заблокировать тул) и чекпоинт файла перед записью.
            hook_decision, checkpoint_id = await toolhooks.pre_tool(
                spec.tool_name, call.arguments, session_id=task.session_id, task_id=task_id, sandbox_root=sandbox_root,
            )
            if hook_decision.blocked:
                with session_scope() as db:
                    repo.finish_tool_call(
                        db, tool_call_row, ToolStatus.blocked_policy,
                        output_json={"hook_blocked": True, "reason": hook_decision.reason},
                        error_code="hook_blocked", error_message=hook_decision.reason,
                    )
                    session = repo.get_session(db, task.session_id)
                    repo.append_message(
                        db, session, role="tool",
                        content=f"{spec.tool_name} -> hook_blocked: {hook_decision.reason}",
                    )
                continue

            output, status, error_code, error_message = await _execute_tool(spec, call.arguments, sandbox_root)

            # После tool call — guardrail recording для loop detection
            guardrails.after_call(spec.tool_name, call.arguments, output)

            with session_scope() as db:
                repo.finish_tool_call(db, tool_call_row, status, output_json=output, error_code=error_code, error_message=error_message)
                session = repo.get_session(db, task.session_id)
                repo.append_message(db, session, role="tool", content_json={"tool_name": spec.tool_name, "arguments": dict(call.arguments or {}), "output": output, "status": status.value}, content=f"{spec.tool_name} -> {status.value}")
                event_bus.emit(
                    "tool_call.updated",
                    {"id": str(tool_call_row.id), "tool_name": spec.tool_name, "status": status.value},
                    session_id=session.id,
                    task_id=task.id,
                    db=db,
                )

            await toolhooks.post_tool(
                spec.tool_name, call.arguments, output, status.value,
                session_id=task.session_id, task_id=task_id, sandbox_root=sandbox_root, checkpoint_id=checkpoint_id,
            )
            if status in (ToolStatus.error, ToolStatus.timeout):
                await toolhooks.lifecycle(
                    "on_error", session_id=task.session_id, task_id=task_id,
                    tool=spec.tool_name, code=error_code, message=error_message,
                )

        if delegate_batch:
            if await _run_delegate_batch(
                delegate_batch, task_id=task_id, router=router, sandbox_root=sandbox_root, delegate_failures=delegate_failures,
            ):
                _parent_paused = True

        if _parent_paused and approval_ctx is None:
            logger.info("task %s paused: its sub-task awaits owner approval", task_id)
            return  # продолжит deliver_child_result + execute_agent_loop (Approve) или отказ (Reject)

        if approval_ctx is not None:
            await toolhooks.lifecycle(
                "on_approval", session_id=approval_ctx[0], task_id=approval_ctx[1],
                state="requested", item_id=approval_ctx[2], command=approval_ctx[3],
            )
            return  # пауза до резолюции attention item (loop продолжит resume_after_approval)

        if repeat_suppressed >= MAX_REPEAT_SUPPRESSED:
            logger.warning("task %s: model keeps repeating an executed call; finishing the task", task_id)
            break

    await finalize_task(task_id, router)


async def _run_delegate_batch(
    batch: list[dict], *, task_id: uuid.UUID, router: ProviderRouter, sandbox_root: str,
    delegate_failures: dict[tuple[str, str], int],
) -> bool:
    """B5-D1: исполнить все delegate_task одного ответа модели ОДНОВРЕМЕННО (потолок delegate_max_parallel).

    У каждого ребёнка своя задача и свои короткие сессии БД (``session_scope`` на каждый шаг), поэтому общего
    писателя нет. Результаты записываются в историю родителя в порядке вызовов.
    Возвращает True, если хотя бы один ребёнок ждёт Approve: родитель тогда встаёт на паузу и продолжится,
    когда решения получены по ВСЕМ ожидающим детям (см. ``deliver_child_result``).
    """
    from aria.core.delegate import run_bounded

    def _factory(item: dict):
        pend, call = item["pending"], item["call"]
        return lambda: pend["do_delegate"](
            call.arguments, timeout_sec=120, sandbox_root=sandbox_root, router=router,
            session_id=pend["session_id"], parent_task_id=task_id, delegation_depth=pend["delegation_depth"],
        )

    results = await run_bounded([_factory(i) for i in batch])
    parent_paused = False
    for item, res in zip(batch, results):
        if isinstance(res, asyncio.CancelledError):
            raise res
        if isinstance(res, BaseException):
            res = {"status": "failed", "summary": "", "error": f"sub-task crashed: {res}"}
        pend = item["pending"]
        dstatus = str(res.get("status", "unknown"))
        with session_scope() as db:
            task = repo.get_task(db, task_id)
            session = repo.get_session(db, task.session_id)
            repo.append_message(
                db, session, role="tool",
                content=f"delegate_task -> {dstatus}",
                content_json={"tool_name": "delegate_task", "output": res, "status": dstatus},
            )
            repo.finish_tool_call(db, pend["tool_call_row"], ToolStatus.ok, output_json=res)
            event_bus.emit(
                "tool_call.updated",
                {"id": str(pend["tool_call_row"].id), "tool_name": "delegate_task", "status": dstatus},
                session_id=session.id, task_id=task.id, db=db,
            )
            if dstatus in ("failed", "timeout", "cancelled"):
                delegate_failures[item["dkey"]] = delegate_failures.get(item["dkey"], 0) + 1
            if dstatus == "awaiting_attention":
                # A23: ребёнок ждёт Approve. Родитель тоже встаёт на паузу (а не «додумывает» и не делегирует заново);
                # результат ему вернёт deliver_child_result после решения.
                if task.status != TaskStatus.awaiting_attention:
                    repo.set_task_status(db, task, TaskStatus.awaiting_attention)
                    event_bus.emit("task.status_changed", {"status": TaskStatus.awaiting_attention.value}, session_id=session.id, task_id=task.id, db=db)
                parent_paused = True
    return parent_paused


async def _execute_tool(spec, arguments: dict, sandbox_root: str, *, approved: bool = False):
    settings = get_settings()
    # Аргументы приходят от модели: сверяем с input_schema ДО handler'а. Ошибка уходит модели понятным
    # текстом (какой аргумент и что не так), а не «KeyError: 'path'» и не молчаливым «ok» на пустой вызов.
    from aria.tools.validators import validate_tool_input
    arg_errors = validate_tool_input(spec.input_schema, arguments)
    if arg_errors:
        return None, ToolStatus.error, "invalid_arguments", f"{spec.tool_name}: " + "; ".join(arg_errors)
    try:
        import asyncio

        output = await asyncio.wait_for(
            spec.handler(
                input_json=arguments, timeout_sec=spec.timeout_sec, sandbox_root=sandbox_root,
                **({"approved": True} if approved else {}),
            ),
            timeout=spec.timeout_sec,
        )
        if isinstance(output, dict) and output.get("status") == "approval_required":
            # Команда НЕ исполнялась — это не «ok» (иначе аудит считает действие выполненным).
            return output, ToolStatus.blocked_policy, "approval_required", output.get("error")
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
    closing = await _finalize_task_inner(task_id, router)
    if closing is not None:
        await toolhooks.lifecycle("task_close", session_id=closing[0], task_id=closing[1], status=closing[2])


async def _finalize_task_inner(task_id: uuid.UUID, router: ProviderRouter) -> tuple | None:
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        session = repo.get_session(db, task.session_id)
        repo.set_task_status(db, task, TaskStatus.under_audit)
        event_bus.emit("task.status_changed", {"status": TaskStatus.under_audit.value}, session_id=session.id, task_id=task.id, db=db)

        # §6 (v11) — observe user patterns from this session before audit
        _observe_user_patterns(db, session, task)

        report = None
        try:
            audit_level, audit_class, audit_prefer = runprofile.audit_settings(runprofile.get_profile(db, session.id), router)
            report = await run_audit(db, session, task, router, level=audit_level, auditor_class=audit_class, auditor_prefer=audit_prefer)

            # П.5.1 ТЗ: вердикт needs_info — аудитору не хватает данных. Задача уходит
            # на уточнение контракта (under_audit -> awaiting_clarification), а не в переделку.
            if getattr(report, "verdict", None) == AuditVerdict.needs_info:
                try:
                    repo.set_task_status(db, task, TaskStatus.awaiting_clarification)
                except Exception:  # noqa: BLE001 — переход мог быть уже совершён
                    logger.info("needs_info: статус задачи %s уже изменён", str(task.id)[:8])
                try:
                    session_row = repo.get_session(db, task.session_id)
                    if session_row is not None:
                        repo.append_message(
                            db, session_row, role="assistant",
                            content="Аудит запросил уточнение контракта (needs_info): "
                                    + "; ".join(list(getattr(report, 'missing_requirements', []) or [])[:5])
                                    or "Аудит запросил уточнение контракта (needs_info).",
                        )
                except Exception:  # noqa: BLE001
                    logger.exception("needs_info: не удалось записать сообщение в чат")
        except Exception as exc:  # noqa: BLE001
            logger.exception("audit crashed for task %s", task_id)
            repo.set_task_status(db, task, TaskStatus.failed, error_code="audit_crash", error_message=str(exc))
            event_bus.emit("task.status_changed", {"status": "failed"}, session_id=session.id, task_id=task.id, db=db)
            return (session.id, task.id, "failed")

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
        return (session.id, task.id, task.status.value)


async def resume_after_approval(task_id: uuid.UUID, router: ProviderRouter, sandbox_root: str, attention_item: m.AttentionItem) -> None:
    """Вызывается api-слоем (через TaskRunner) после Approve: исполняет ранее заблокированное
    действие — shell-команду или тул MCP-сервера — и продолжает основной loop.

    Действие исполняется не больше одного раза: строка tool_call привязывается к attention item
    (``approval_item_id``) ДО исполнения. Если такая строка уже есть (дубль/повтор после
    перезапуска), второй раз действие не выполняется.
    """
    from aria.tools.registry import get_tool as _get_tool

    payload = dict(attention_item.payload_json or {})
    tool_name = str(payload.get("tool_name") or "shell_execute")
    if tool_name == "shell_execute":
        arguments: dict = {"command": payload.get("command")}
    else:
        arguments = dict(payload.get("arguments") or {})
        try:
            from aria.mcp.manager import get_manager as _mcp_mgr

            await _mcp_mgr().ensure_loaded()  # после перезапуска тулы MCP ещё не зарегистрированы
        except Exception:  # noqa: BLE001
            logger.warning("MCP not available while resuming task %s", task_id, exc_info=True)

    with session_scope() as db:
        task = repo.get_task(db, task_id)
        session = repo.get_session(db, task.session_id)
        already_run = (
            db.execute(select(m.ToolCall.id).where(m.ToolCall.approval_item_id == attention_item.id).limit(1)).first()
            is not None
        )
        spec = None
        tool_call_row = None
        if not already_run:
            try:
                spec = _get_tool(tool_name)
            except KeyError:
                spec = None
            if spec is not None:
                tool_call_row = repo.start_tool_call(
                    db, session, task, spec.tool_name, task.role, spec.risk_level.value, arguments,
                    source_trust_snapshot=session.source_trust_aggregate,
                )
                tool_call_row.approval_item_id = attention_item.id
                db.flush()
            else:
                repo.append_message(
                    db, session, role="tool",
                    content_json={"tool_name": tool_name, "output": {"error": "tool is no longer available"}, "status": "error"},
                    content=f"{tool_name} (approved) -> error: tool is no longer available",
                )
        sess_id = task.session_id

    if already_run:
        logger.info("approval %s was already executed; continuing without re-running it", attention_item.id)
    elif tool_call_row is not None:
        hook_decision, _cp = await toolhooks.pre_tool(
            tool_name, arguments, session_id=sess_id, task_id=task_id, sandbox_root=sandbox_root,
        )
        if hook_decision.blocked:
            output, status, error_code, error_message = (
                {"hook_blocked": True, "reason": hook_decision.reason}, ToolStatus.blocked_policy, "hook_blocked", hook_decision.reason,
            )
        else:
            output, status, error_code, error_message = await _execute_tool(spec, arguments, sandbox_root, approved=True)
            await toolhooks.post_tool(
                tool_name, arguments, output, status.value,
                session_id=sess_id, task_id=task_id, sandbox_root=sandbox_root,
            )

        with session_scope() as db:
            repo.finish_tool_call(db, tool_call_row, status, output_json=output, error_code=error_code, error_message=error_message)
            session = repo.get_session(db, sess_id)
            repo.append_message(db, session, role="tool", content_json={"tool_name": tool_name, "arguments": arguments, "output": output, "status": status.value}, content=f"{tool_name} (approved) -> {status.value}")
            event_bus.emit("tool_call.updated", {"id": str(tool_call_row.id), "status": status.value}, session_id=session.id, task_id=task_id, db=db)

    await execute_agent_loop(task_id, router, sandbox_root)


def deliver_child_result(child_task_id: uuid.UUID) -> uuid.UUID | None:
    """A23: дочерняя задача завершилась (после Approve или Reject) — вернуть результат родителю.

    Добавляет в историю результат ``delegate_task`` и переводит ждавшего родителя в ``in_progress``.
    B5-D1: детей может быть несколько (параллельное делегирование). Результат каждого ребёнка записывается сразу,
    а родитель просыпается только когда НИ ОДИН его ребёнок больше не ждёт решения (``awaiting_attention``).
    Возвращает id родителя, которого нужно продолжить, или None (нет родителя, он не ждёт, ребёнок ещё не
    закончил, остались другие ждущие дети, результат этого ребёнка уже был доставлен).
    """
    terminal = (TaskStatus.done, TaskStatus.done_unaudited, TaskStatus.failed, TaskStatus.cancelled)
    with session_scope() as db:
        child = repo.get_task(db, child_task_id)
        if child is None or child.parent_task_id is None or child.status not in terminal:
            return None
        parent = repo.get_task(db, child.parent_task_id)
        if parent is None or parent.status != TaskStatus.awaiting_attention:
            return None
        session = repo.get_session(db, parent.session_id)
        status = "done" if child.status in (TaskStatus.done, TaskStatus.done_unaudited) else child.status.value
        already = any(
            isinstance(msg.content_json, dict)
            and msg.content_json.get("tool_name") == "delegate_task"
            and isinstance(msg.content_json.get("output"), dict)
            and msg.content_json["output"].get("task_id") == str(child.id)
            and str(msg.content or "").startswith("delegate_task (resumed)")
            for msg in db.execute(
                select(m.Message).where(m.Message.session_id == session.id, m.Message.role == "tool")
            ).scalars().all()
        )
        if not already:
            summary = (child.objective or "")[:200]
            if status == "done":
                last = db.execute(
                    select(m.Message).where(m.Message.session_id == session.id, m.Message.role == "assistant")
                    .order_by(m.Message.seq_no.desc()).limit(1)
                ).scalars().first()
                if last is not None and last.content:
                    summary = last.content[:1500]
            output = {
                "status": status, "task_id": str(child.id), "summary": summary,
                "error": child.error_message if status != "done" else None,
            }
            repo.append_message(
                db, session, role="tool",
                content=f"delegate_task (resumed) -> {status}",
                content_json={"tool_name": "delegate_task", "output": output, "status": status},
            )
        still_waiting = [
            k for k in repo.list_child_tasks(db, parent.id)
            if k.id != child.id and k.status == TaskStatus.awaiting_attention
        ]
        if still_waiting:
            return None  # родитель ждёт остальных детей — продолжится, когда решено по всем
        repo.set_task_status(db, parent, TaskStatus.in_progress)
        parent_id = parent.id
    event_bus.emit("task.status_changed", {"status": TaskStatus.in_progress.value}, session_id=session.id, task_id=parent_id)
    return parent_id

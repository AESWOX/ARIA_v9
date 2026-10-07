"""Task pipeline routes (moved from aria.main)."""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from aria.api.auth import require_runtime_token
from aria.core.loop import execute_agent_loop
from aria.core.events import event_bus
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import AuditVerdict, SourceTrust, TaskStatus, ToolStatus
from aria.http_utils import (
    emit_message_created,
    emit_task_status,
    serialize_audit,
    serialize_task,
    serialize_tool_call,
)
from aria.config import get_settings

logger = logging.getLogger("local_agent.main")

router = APIRouter(tags=["tasks"])


async def _run_demo_task(task_id: uuid.UUID) -> None:
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task is None:
            return
        session = repo.get_session(db, task.session_id)
        if task.status in (TaskStatus.cancelled, TaskStatus.done, TaskStatus.failed):
            return
        repo.set_task_status(db, task, TaskStatus.in_progress)
    emit_task_status(task)
    await asyncio.sleep(0.2)

    with session_scope() as db:
        task = repo.get_task(db, task_id)
        session = repo.get_session(db, task.session_id)
        call = repo.start_tool_call(
            db,
            session,
            task,
            tool_name="file_search",
            role=task.role,
            risk_level="low",
            input_json={"glob": "**/*"},
            source_trust_snapshot=session.source_trust_aggregate,
        )
    event_bus.emit("tool_call.updated", serialize_tool_call(call), session_id=session.id, task_id=task.id)
    await asyncio.sleep(0.2)

    with session_scope() as db:
        task = repo.get_task(db, task_id)
        session = repo.get_session(db, task.session_id)
        call = db.get(m.ToolCall, call.id)
        repo.finish_tool_call(db, call, ToolStatus.ok, output_json={"matches": ["backend/app/main.py", "desktop/src/App.tsx"]})
        tool_msg = repo.append_message(
            db,
            session,
            role="tool",
            content="file_search -> ok",
            content_json={"tool_name": "file_search", "output": {"matches": ["backend/app/main.py", "desktop/src/App.tsx"]}, "status": "ok"},
            source_trust=SourceTrust.trusted,
        )
        assistant = repo.append_message(
            db,
            session,
            role="assistant",
            content=(
                "Задача выполнена в MVP-режиме: backend принял цель, записал tool-call, сохранил состояние в БД, "
                "отправил WS-события и подготовил результат для audit-loop."
            ),
            source_trust=SourceTrust.trusted,
        )
        repo.set_task_status(db, task, TaskStatus.under_audit)
    event_bus.emit("tool_call.updated", serialize_tool_call(call), session_id=session.id, task_id=task.id)
    emit_message_created(tool_msg, session.id, task.id)
    emit_message_created(assistant, session.id, task.id)
    emit_task_status(task)

    with session_scope() as db:
        task = repo.get_task(db, task_id)
        session = repo.get_session(db, task.session_id)
        report = repo.create_audit_report(
            db,
            session,
            task,
            attempt_no=task.audit_attempt_no + 1,
            auditor_role="qa_auditor",
            auditor_model="stub-standard",
            budget_degraded=False,
            verdict=AuditVerdict.pass_,
            plan_vs_fact={"objective": task.objective, "mode": "mvp-demo"},
            tool_success_summary={"total": 1, "ok": 1, "error": 0, "blocked_policy": 0},
            missing_requirements=[],
            patch_suggestions=[],
            metrics_compared={},
        )
        task.audit_attempt_no += 1
        repo.set_task_status(db, task, TaskStatus.done)
    event_bus.emit("audit_report.created", serialize_audit(report), session_id=session.id, task_id=task.id)
    emit_task_status(task)


@router.post("/tasks/{task_id}/start")
async def start_task(
    task_id: uuid.UUID,
    request: Request,
    payload: dict[str, Any] | None = None,
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if task.status in (TaskStatus.draft, TaskStatus.awaiting_clarification):
            repo.set_task_status(db, task, TaskStatus.approved)
        elif task.status == TaskStatus.needs_rework:
            # §8.1: needs_rework допускает только in_progress/cancelled, approved запрещён
            repo.set_task_status(db, task, TaskStatus.in_progress)
    settings = get_settings()
    execution_mode = (payload or {}).get("mode") or ("codex" if settings.codex_enabled else "real")

    if execution_mode == "codex":
        from aria.agents.codex_exec_bridge import run_codex_task

        model = (payload or {}).get("model")
        await run_codex_task(task_id, model=model)
    elif execution_mode == "demo":
        # explicit opt-in only now — больше не дефолт, см. §3.3 backend-аудита
        await _run_demo_task(task_id)
    else:
        await execute_agent_loop(task_id, request.app.state.router, settings.agent_sandbox_root)
    return {"ok": True}


@router.post("/tasks/{task_id}/run-executor")
async def run_executor_pipeline(
    task_id: uuid.UUID,
    request: Request,
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    """Запустить Stage 1-7 executor pipeline для задачи.

    Использует executor.run_task с реальным ProviderRouter.
    Если TELEGRAM_BOT_TOKEN настроен — подключает TelegramNotifier.
    В отличие от /tasks/{task_id}/start (ReAct loop), это Stage 1-7
    loop-engineering pipeline: vault-check → plan → real handlers → audit → hooks → delivery.
    """
    from aria.core.executor import run_task as executor_run_task
    from aria.core.notifiers.telegram import TelegramNotifier

    notifier = None

    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")

        settings = get_settings()

        # Telegram notifier, если настроен
        if settings.telegram_bot_token and settings.telegram_chat_id:
            try:
                notifier = TelegramNotifier(
                    bot_token=settings.telegram_bot_token,
                    chat_id=settings.telegram_chat_id,
                )
            except Exception as e:
                logger.warning("Failed to init TelegramNotifier: %s", e)

        result = await executor_run_task(
            session=db,
            task=task,
            router=request.app.state.router,
            notifier=notifier,
        )

    return result


@router.get("/tasks/{task_id}/children")
async def get_task_children(task_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> list[dict[str, Any]]:
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        children = repo.list_child_tasks(db, task_id)
        return [serialize_task(child) for child in children]


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        repo.set_task_status(db, task, TaskStatus.cancelled)
    emit_task_status(task)
    return {"ok": True}


@router.get("/tasks/{task_id}/audit-reports")
async def get_audit_reports(task_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> list[dict[str, Any]]:
    with session_scope() as db:
        return [serialize_audit(row) for row in repo.list_audit_reports(db, task_id)]

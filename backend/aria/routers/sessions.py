"""Session & attention-item routes (moved from aria.main)."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from aria.api.auth import require_runtime_token
from aria.core.loop import execute_agent_loop
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import SourceTrust, TaskStatus
from aria.http_utils import (
    _safe_export_filename,
    emit_message_created,
    emit_session_updated,
    emit_task_status,
    render_session_export_markdown,
    serialize_attention,
    serialize_session,
    session_snapshot_payload,
)
from aria.config import get_settings

router = APIRouter(tags=["sessions"])


@router.get("/sessions")
async def list_sessions(_: str = Depends(require_runtime_token)) -> list[dict[str, Any]]:
    with session_scope() as db:
        return [serialize_session(row) for row in repo.list_sessions(db)]


@router.get("/sessions/stats")
async def sessions_stats_alias(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    with session_scope() as db:
        sessions = repo.list_sessions(db)
    return {
        "total_sessions": len(sessions),
        "active_sessions": sum(1 for s in sessions if s.status.value in ("active", "running")),
    }


@router.get("/sessions/empty/count")
async def empty_sessions_count_alias(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"count": 0}


@router.post("/sessions")
async def create_session(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    title = str(payload.get("title") or "Untitled session")
    with session_scope() as db:
        session = repo.create_session(db, title, active_role="general")
        task = repo.create_task(db, session, role="general", objective=f"Новая задача: {title}", draft_tz_md=f"# {title}\n")
        repo.set_task_status(db, task, TaskStatus.draft)
    emit_session_updated(session)
    emit_task_status(task)
    return {"session_id": str(session.id), "task_id": str(task.id)}


@router.get("/sessions/{session_id}")
async def get_session(session_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return session_snapshot_payload(session_id)


@router.post("/sessions/{session_id}/messages")
async def post_message(
    session_id: uuid.UUID,
    payload: dict[str, Any],
    request: Request,
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    content = str(payload.get("content") or "").strip()
    role = str(payload.get("role") or "user")
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    with session_scope() as db:
        session = repo.get_session(db, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        task = repo.get_task(db, session.current_task_id) if session.current_task_id else None
        msg = repo.append_message(db, session, role=role, content=content, source_trust=SourceTrust.trusted)
        if task and role == "user":
            task.objective = content
            if task.status in (TaskStatus.draft, TaskStatus.awaiting_clarification):
                repo.set_task_status(db, task, TaskStatus.approved)
            elif task.status == TaskStatus.needs_rework:
                # §8.1: needs_rework допускает только in_progress/cancelled, approved запрещён
                repo.set_task_status(db, task, TaskStatus.in_progress)
    emit_message_created(msg, session_id, task.id if task else None)
    if task:
        emit_task_status(task)

    if task and role == "user" and task.status in (TaskStatus.approved, TaskStatus.in_progress):
        settings = get_settings()
        execution_mode = "codex" if settings.codex_enabled else "real"
        if execution_mode == "codex":
            from aria.agents.codex_exec_bridge import run_codex_task

            await run_codex_task(task.id)
        else:
            await execute_agent_loop(task.id, request.app.state.router, settings.agent_sandbox_root)

    return {"ok": True}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    with session_scope() as db:
        session = repo.get_session(db, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        repo.delete_session(db, session_id)
    from aria.core.events import event_bus

    event_bus.emit("session.deleted", {"id": str(session_id)}, session_id=session_id, task_id=None)
    return {"ok": True}


@router.get("/sessions/{session_id}/export.md")
async def export_session_markdown(session_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> PlainTextResponse:
    with session_scope() as db:
        session = repo.get_session(db, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        task = repo.get_task(db, session.current_task_id) if session.current_task_id else None
        messages = repo.list_messages(db, session_id)
        tool_calls = repo.list_tool_calls_by_session(db, session_id)
        audit_reports = repo.list_audit_reports_by_session(db, session_id)
    content = render_session_export_markdown(session, task, messages, tool_calls, audit_reports)
    headers = {"Content-Disposition": f'attachment; filename="{_safe_export_filename(session.title, session_id)}"'}
    return PlainTextResponse(content=content, media_type="text/markdown; charset=utf-8", headers=headers)


@router.get("/attention-items")
async def list_attention_items(_: str = Depends(require_runtime_token)) -> list[dict[str, Any]]:
    with session_scope() as db:
        return [serialize_attention(item) for item in repo.list_attention_items(db)]


@router.post("/attention-items/{item_id}/approve")
async def approve_attention(item_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    from aria.core import approvals
    from aria.core.events import event_bus

    with session_scope() as db:
        item = repo.get_attention_item(db, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="attention item not found")
        resolved = approvals.resolve(db, item, approve=True)
    event_bus.emit("attention_item.resolved", serialize_attention(resolved), session_id=resolved.session_id, task_id=resolved.task_id)
    return {"ok": True}


@router.post("/attention-items/{item_id}/reject")
async def reject_attention(item_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    from aria.core import approvals
    from aria.core.events import event_bus

    with session_scope() as db:
        item = repo.get_attention_item(db, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="attention item not found")
        resolved = approvals.resolve(db, item, approve=False)
    event_bus.emit("attention_item.resolved", serialize_attention(resolved), session_id=resolved.session_id, task_id=resolved.task_id)
    return {"ok": True}

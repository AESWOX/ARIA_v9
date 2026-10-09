"""Session & attention-item routes (moved from aria.main)."""
from __future__ import annotations

import uuid
from typing import Any

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from aria.api.auth import require_runtime_token
from aria.core.loop import execute_agent_loop
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import SourceTrust, TaskStatus
from aria.http_utils import (
    serialize_message,
    _safe_export_filename,
    emit_message_created,
    emit_session_updated,
    emit_task_status,
    iso,
    render_session_export_markdown,
    serialize_attention,
    serialize_session,
    session_snapshot_payload,
    utc_now,
)
from aria.config import get_settings

router = APIRouter(tags=["sessions"])


def _epoch_ms(dt: datetime | None) -> int | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _session_info(
    session: m.Session,
    message_count: int,
    tool_call_count: int,
    messages: list[m.Message],
) -> dict[str, Any]:
    preview = ""
    for msg in messages:
        if msg.role == "user" and msg.content.strip():
            preview = msg.content.strip()[:200]
            break
    if not preview and messages:
        preview = messages[-1].content.strip()[:200]
    is_active = session.status.value == "active"
    info = {
        "id": str(session.id),
        "source": None,
        "model": None,
        "title": session.title,
        "started_at": _epoch_ms(session.created_at),
        "ended_at": None,
        "last_active": _epoch_ms(session.updated_at),
        "is_active": is_active,
        "message_count": message_count,
        "tool_call_count": tool_call_count,
        "input_tokens": 0,
        "output_tokens": 0,
        "preview": preview or None,
        "parent_session_id": None,
    }
    # Дополнительные поля старого контракта — не ломать прежних потребителей.
    info["status"] = session.status.value if hasattr(session.status, "value") else str(session.status)
    info["active_role"] = session.active_role
    info["current_task_id"] = str(session.current_task_id) if session.current_task_id else None
    info["source_trust_aggregate"] = session.source_trust_aggregate
    info["updated_at"] = iso(session.updated_at)
    return info


def _bulk_counts(db) -> tuple[dict, dict]:
    msg_counts = dict(
        db.query(m.Message.session_id, func.count(m.Message.id)).group_by(m.Message.session_id).all()
    )
    tc_counts = dict(
        db.query(m.ToolCall.session_id, func.count(m.ToolCall.id)).group_by(m.ToolCall.session_id).all()
    )
    return msg_counts, tc_counts


@router.get("/sessions")
async def list_sessions(
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    order: str = Query(default="created"),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    with session_scope() as db:
        sessions = list(repo.list_sessions(db, limit=None))
    if order == "recent":
        sessions.sort(key=lambda s: s.updated_at, reverse=True)
    else:
        sessions.sort(key=lambda s: s.created_at, reverse=True)
    total = len(sessions)
    page = sessions[offset : offset + limit]
    ids = [s.id for s in page]
    with session_scope() as db:
        msg_counts, tc_counts = _bulk_counts(db)
        msgs_by_session: dict = {}
        if ids:
            rows = (
                db.query(m.Message)
                .filter(m.Message.session_id.in_(ids))
                .order_by(m.Message.seq_no.asc())
                .all()
            )
            for row in rows:
                msgs_by_session.setdefault(row.session_id, []).append(row)
    items = [
        _session_info(s, msg_counts.get(s.id, 0), tc_counts.get(s.id, 0), msgs_by_session.get(s.id, []))
        for s in page
    ]
    return {"sessions": items, "total": total, "limit": limit, "offset": offset}


@router.get("/sessions/stats")
async def sessions_stats_alias(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    with session_scope() as db:
        sessions = repo.list_sessions(db, limit=None)
        msg_counts, _ = _bulk_counts(db)
        msg_total = db.query(func.count(m.Message.id)).scalar() or 0
    total = len(sessions)
    active_store = sum(1 for s in sessions if s.status.value == "active")
    archived = total - active_store
    return {
        "total": total,
        "active_store": active_store,
        "archived": archived,
        "messages": msg_total,
        "by_source": {"local": total},
    }


@router.get("/sessions/empty/count")
async def empty_sessions_count_alias(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    with session_scope() as db:
        sessions = repo.list_sessions(db, limit=None)
        msg_counts, _ = _bulk_counts(db)
    empty = [s for s in sessions if msg_counts.get(s.id, 0) == 0]
    return {"count": len(empty)}


@router.get("/sessions/empty")
async def list_empty_sessions(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    with session_scope() as db:
        sessions = repo.list_sessions(db, limit=None)
        msg_counts, tc_counts = _bulk_counts(db)
    empty = [s for s in sessions if msg_counts.get(s.id, 0) == 0]
    return {
        "sessions": [_session_info(s, 0, tc_counts.get(s.id, 0), []) for s in empty],
        "count": len(empty),
    }


@router.delete("/sessions/empty")
async def delete_empty_sessions(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    with session_scope() as db:
        sessions = repo.list_sessions(db, limit=None)
        msg_counts, _ = _bulk_counts(db)
        deleted = 0
        for s in sessions:
            if msg_counts.get(s.id, 0) == 0:
                if repo.delete_session(db, s.id):
                    deleted += 1
    from aria.core.events import event_bus

    if deleted:
        event_bus.emit("sessions.empty_deleted", {"deleted": deleted}, session_id=None, task_id=None)
    return {"ok": True, "deleted": deleted}


@router.post("/sessions/bulk-delete")
async def bulk_delete_sessions(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    raw_ids = payload.get("ids") or []
    ids: list[uuid.UUID] = []
    for raw in raw_ids:
        try:
            ids.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            continue
    from aria.core.events import event_bus

    deleted = 0
    for sid in ids:
        with session_scope() as db:
            if repo.delete_session(db, sid):
                deleted += 1
        event_bus.emit("session.deleted", {"id": str(sid)}, session_id=sid, task_id=None)
    return {"ok": True, "deleted": deleted}


@router.post("/sessions/prune")
async def prune_sessions(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    try:
        older_than_days = float(payload.get("older_than_days") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="older_than_days must be a number")
    if older_than_days <= 0:
        raise HTTPException(status_code=400, detail="older_than_days must be > 0")
    cutoff = utc_now() - timedelta(days=older_than_days)
    from aria.core.events import event_bus

    removed = 0
    with session_scope() as db:
        sessions = repo.list_sessions(db, limit=None)
        msg_counts, _ = _bulk_counts(db)
        for s in sessions:
            created = s.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created >= cutoff:
                continue
            if repo.delete_session(db, s.id):
                removed += 1
    if removed:
        event_bus.emit("sessions.pruned", {"removed": removed}, session_id=None, task_id=None)
    return {"ok": True, "removed": removed}


@router.get("/sessions/search")
async def search_sessions(
    q: str = Query(default="", max_length=200),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    query = q.strip()
    if not query:
        return {"results": []}
    with session_scope() as db:
        rows = (
            db.query(m.Message, m.Session)
            .join(m.Session, m.Session.id == m.Message.session_id)
            .filter(m.Message.content.ilike(f"%{query}%"))
            .order_by(m.Message.seq_no.asc())
            .limit(200)
            .all()
        )
    results: dict[str, dict[str, Any]] = {}
    for msg, session in rows:
        sid = str(session.id)
        if sid in results:
            continue
        snippet = msg.content.strip()
        idx = snippet.lower().find(query.lower())
        if idx > 80:
            snippet = "…" + snippet[idx - 60 :]
        results[sid] = {
            "session_id": sid,
            "snippet": snippet[:300],
            "role": msg.role,
            "source": None,
            "model": None,
            "session_started": _epoch_ms(session.created_at),
        }
    return {"results": list(results.values())[:50]}


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


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    """Shape = SessionMessagesResponse in desktop/src/lib/api.ts (SessionsPage expands rows with it)."""
    with session_scope() as db:
        if repo.get_session(db, session_id) is None:
            raise HTTPException(status_code=404, detail="session not found")
        items = []
        for msg in repo.list_messages(db, session_id, limit=1000):
            row = serialize_message(msg)
            row["timestamp"] = msg.created_at.timestamp() if msg.created_at else None
            items.append(row)
        return {"session_id": str(session_id), "messages": items}


_TERMINAL_TASK_STATUSES = (
    TaskStatus.done,
    TaskStatus.done_unaudited,
    TaskStatus.failed,
    TaskStatus.cancelled,
)


@router.get("/sessions/{session_id}/run")
async def get_session_run(session_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    """Лёгкое состояние запуска для опроса из UI: статус задачи + ожидающие подтверждения."""
    with session_scope() as db:
        session = repo.get_session(db, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        task = repo.get_task(db, session.current_task_id) if session.current_task_id else None
        status = None
        if task is not None:
            status = task.status.value if hasattr(task.status, "value") else str(task.status)
        pending = [
            serialize_attention(item)
            for item in repo.list_attention_items(db, only_pending=True)
            if item.session_id == session.id or (task is not None and item.task_id == task.id)
        ]
    return {
        "session_id": str(session_id),
        "task_id": str(task.id) if task is not None else None,
        "status": status,
        "terminal": status in {s.value for s in _TERMINAL_TASK_STATUSES},
        "attention": pending,
    }


@router.post("/sessions/{session_id}/messages")
async def post_message(
    session_id: uuid.UUID,
    payload: dict[str, Any],
    request: Request,
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    content = str(payload.get("content") or "").strip()
    role = str(payload.get("role") or "user")
    mode = str(payload.get("mode") or "agent").strip().lower()
    if mode not in ("agent", "plan"):
        raise HTTPException(status_code=400, detail="mode must be one of ['agent', 'plan']")
    if not content:
        raise HTTPException(status_code=400, detail="content required")
    with session_scope() as db:
        session = repo.get_session(db, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        task = repo.get_task(db, session.current_task_id) if session.current_task_id else None
        # «Одна дверь»: сообщение пользователя в сессии без задачи (чат) или с
        # завершённой задачей начинает новую задачу, а не оседает в истории.
        if role == "user" and (task is None or task.status in _TERMINAL_TASK_STATUSES):
            task = repo.create_task(db, session, role="general", objective=content, draft_tz_md=f"# {content[:80]}\n")
            repo.set_task_status(db, task, TaskStatus.draft)
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
            runner = getattr(request.app.state, "task_runner", None)
            if runner is not None:
                # Волна 1 (A10): агент больше не исполняется внутри HTTP-запроса.
                # Результат приходит событиями WS (message.created, task.status_changed).
                queued = runner.submit(task.id, mode=mode, source="ui")
                return {"ok": True, "queued": bool(queued.get("queued")), "task_id": str(task.id), "mode": mode}
            # Без раннера (юнит-тесты без lifespan) — прежний синхронный путь.
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


@router.patch("/sessions/{session_id}")
async def rename_session(session_id: uuid.UUID, payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    title = str(payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    with session_scope() as db:
        session = repo.get_session(db, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        repo.touch_session(db, session, title=title)
    emit_session_updated(session)
    return {"ok": True, "title": title}


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

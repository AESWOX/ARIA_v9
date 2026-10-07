"""Plain chat: message -> model -> reply, persisted per session.

This is the short path for conversation. It does NOT go through the 7-stage
task pipeline (plan/execute/audit/delivery in core/executor.py) - that stays
behind POST /sessions/{id}/messages for real tasks.

    GET  /chat/status                        is a real model configured?
    GET  /chat/sessions                      sessions with message counts
    POST /chat/sessions                      create an empty chat session
    GET  /chat/sessions/{id}/messages        full history
    POST /chat/sessions/{id}/send            {content} or {retry:true} -> user + assistant message
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select

from aria.api.auth import require_runtime_token
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import SourceTrust
from aria.http_utils import emit_message_created, emit_session_updated, iso, serialize_message
from aria.llm.key_pool import NoAvailableKeys
from aria.llm.providers.base import ChatMessage
from aria.llm.router import ProviderUnavailable

logger = logging.getLogger("local_agent.chat")
router = APIRouter(prefix="/chat", tags=["chat"])

CHAT_PROVIDER_CLASS = "free_tier_reasoning"  # Gemini Flash first, then Groq
HISTORY_MESSAGES = 40
HISTORY_CHAR_BUDGET = 60_000
MAX_CONTENT_CHARS = 20_000
DEFAULT_TITLES = {"", "Untitled session", "New chat"}
SYSTEM_PROMPT = (
    "You are ARIA, a helpful assistant running locally on the user's machine. "
    "Reply in the language the user writes in. Be concise and honest; say so when you are unsure."
)
NOT_CONFIGURED_HINT = (
    "No language model is configured. Open Keys and add GEMINI_API_KEYS (a free key from "
    "aistudio.google.com), then send the message again."
)


def _real_provider_ids(request: Request) -> list[str]:
    llm_router = getattr(request.app.state, "router", None)
    if llm_router is None:
        return []
    return sorted(
        {
            p.provider_id
            for providers in llm_router.providers_by_class.values()
            for p in providers
            if not p.provider_id.startswith("stub")
        }
    )


def _build_prompt(history: list[tuple[str, str]]) -> list[ChatMessage]:
    """System prompt + the most recent turns that fit the budget (oldest dropped first)."""
    turns = [(role, text) for role, text in history if role in ("user", "assistant") and text]
    turns = turns[-HISTORY_MESSAGES:]
    kept: list[tuple[str, str]] = []
    used = 0
    for role, text in reversed(turns):
        if used + len(text) > HISTORY_CHAR_BUDGET and kept:
            break
        kept.append((role, text))
        used += len(text)
    kept.reverse()
    return [ChatMessage(role="system", content=SYSTEM_PROMPT)] + [ChatMessage(role=r, content=t) for r, t in kept]


def _explain_llm_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (ProviderUnavailable, NoAvailableKeys)):
        return HTTPException(
            status_code=503,
            detail="All configured models are unavailable right now (rate limit or bad keys). "
            "Wait a minute and retry, or add another key in Keys.",
        )
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if code == 429:
            return HTTPException(status_code=429, detail="The model's rate limit is reached. Retry in a minute.")
        return HTTPException(status_code=502, detail=f"The model API answered HTTP {code}.")
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return HTTPException(status_code=504, detail="The model API did not answer in time (network or timeout).")
    return HTTPException(status_code=502, detail=f"Model call failed: {type(exc).__name__}")


@router.get("/status")
async def chat_status(request: Request, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    providers = _real_provider_ids(request)
    return {"configured": bool(providers), "providers": providers, "hint": None if providers else NOT_CONFIGURED_HINT}


@router.get("/sessions")
async def chat_sessions(_: str = Depends(require_runtime_token)) -> list[dict[str, Any]]:
    with session_scope() as db:
        counts = dict(
            db.execute(select(m.Message.session_id, func.count()).group_by(m.Message.session_id)).all()
        )
        sessions = repo.list_sessions(db)
        rows = [
            {
                "id": str(s.id),
                "title": s.title,
                "message_count": int(counts.get(s.id, 0)),
                "updated_at": iso(getattr(s, "updated_at", None) or getattr(s, "created_at", None)),
            }
            for s in sessions
        ]
    rows.sort(key=lambda r: r["updated_at"] or "", reverse=True)
    return rows


@router.post("/sessions")
async def chat_create_session(payload: dict[str, Any] | None = None, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    title = str((payload or {}).get("title") or "New chat")[:120]
    with session_scope() as db:
        session = repo.create_session(db, title, active_role="general")
        session_id = str(session.id)
    emit_session_updated(session)
    return {"session_id": session_id, "title": title}


@router.get("/sessions/{session_id}/messages")
async def chat_messages(session_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> list[dict[str, Any]]:
    with session_scope() as db:
        if repo.get_session(db, session_id) is None:
            raise HTTPException(status_code=404, detail="session not found")
        return [serialize_message(msg) for msg in repo.list_messages(db, session_id, limit=1000)]


@router.post("/sessions/{session_id}/send")
async def chat_send(
    session_id: uuid.UUID,
    payload: dict[str, Any],
    request: Request,
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    content = str(payload.get("content") or "").strip()
    retry = bool(payload.get("retry"))  # re-ask using the last stored user message (no duplicate)
    if not content and not retry:
        raise HTTPException(status_code=400, detail="content required")
    if len(content) > MAX_CONTENT_CHARS:
        raise HTTPException(status_code=413, detail=f"message too long (max {MAX_CONTENT_CHARS} characters)")
    if not _real_provider_ids(request):
        raise HTTPException(status_code=503, detail=NOT_CONFIGURED_HINT)

    # 1) store the user message, collect history (one short DB scope)
    with session_scope() as db:
        session = repo.get_session(db, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        if retry:
            stored = repo.list_messages(db, session_id, limit=1000)
            if not stored or stored[-1].role != "user":
                raise HTTPException(status_code=400, detail="nothing to retry")
            user_msg = stored[-1]
        else:
            user_msg = repo.append_message(db, session, role="user", content=content, source_trust=SourceTrust.trusted)
        session.updated_at = datetime.now(timezone.utc)  # message inserts don't touch the sessions row
        if (session.title or "") in DEFAULT_TITLES:
            session.title = (content or user_msg.content).replace("\n", " ")[:60]
        history = [(msg.role, msg.content) for msg in repo.list_messages_for_prompt(db, session_id, limit=500)]
        user_payload = serialize_message(user_msg)
    if not retry:
        emit_message_created(user_msg, session_id, None)

    # 2) call the model outside any DB transaction
    try:
        result = await request.app.state.router.route_chat(
            CHAT_PROVIDER_CLASS, _build_prompt(history), [], timeout_sec=60
        )
    except Exception as exc:  # noqa: BLE001 - mapped to a clear HTTP error below
        logger.warning("chat: model call failed: %s", exc)
        raise _explain_llm_error(exc) from exc
    reply = (result.response.text or "").strip()
    if not reply:
        raise HTTPException(status_code=502, detail="The model returned an empty answer. Try again.")
    if result.provider_id.startswith("stub"):
        raise HTTPException(status_code=503, detail=NOT_CONFIGURED_HINT)

    # 3) store the assistant message
    with session_scope() as db:
        session = repo.get_session(db, session_id)
        if session is None:  # deleted while the model was thinking
            raise HTTPException(status_code=404, detail="session was deleted")
        assistant_msg = repo.append_message(db, session, role="assistant", content=reply, source_trust=SourceTrust.trusted)
        session.updated_at = datetime.now(timezone.utc)
        assistant_payload = serialize_message(assistant_msg)
    emit_message_created(assistant_msg, session_id, None)
    return {
        "user": user_payload,
        "assistant": assistant_payload,
        "provider_id": result.provider_id,
        "degraded": bool(result.degraded_to_free),
    }

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
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select

from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.core import runprofile, timing
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import SourceTrust
from aria.http_utils import emit_message_created, emit_session_updated, iso, serialize_message
from aria.llm import compression
from aria.llm.key_pool import NoAvailableKeys
from aria.llm.providers.base import ChatMessage
from aria.llm.router import ProviderUnavailable, tried_suffix
from aria.memory import store as memory_store

logger = logging.getLogger("local_agent.chat")
router = APIRouter(prefix="/chat", tags=["chat"])

CHAT_PROVIDER_CLASS = "free_tier_reasoning"  # Gemini Flash first, then Groq
CHAT_FALLBACK_CLASSES = ("standard_reasoning",)  # DeepSeek chat, then Gemini Pro (A20)
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


def _build_prompt(history: list[tuple[str, str]], memory_block: str = "") -> list[ChatMessage]:
    """System prompt + the most recent turns that fit the budget (oldest dropped first).

    CTX: сводки компрессии («[compression summary …]») идут в системный промпт одним блоком;
    с включённой компрессией бюджет — порог токенов (по умолчанию 100к), иначе старые лимиты.
    Если сводка уже есть, «голова» диалога (всё до неё) остаётся дословно и бюджетом не обрезается:
    именно её компрессия и защищает, а режется только хвост."""
    settings = get_settings()
    last_summary = max((i for i, (role, text) in enumerate(history) if role == "system" and text.startswith(compression.SUMMARY_PREFIX)), default=-1)
    summaries = [text for role, text in history if role == "system" and text.startswith(compression.SUMMARY_PREFIX)]
    head = [(r, t) for r, t in history[:last_summary] if r in ("user", "assistant") and t] if last_summary >= 0 else []
    turns = [(r, t) for r, t in history[last_summary + 1:] if r in ("user", "assistant") and t]
    if settings.compression_enabled and settings.compression_token_threshold > 0:
        used = sum(compression.estimate_tokens(t) for t in summaries) + sum(compression.estimate_tokens(t) for _, t in head)
        kept: list[tuple[str, str]] = []
        for role, text in reversed(turns):
            cost = compression.estimate_tokens(text)
            if used + cost > settings.compression_token_threshold and kept:
                break
            kept.append((role, text))
            used += cost
    else:
        turns = turns[-HISTORY_MESSAGES:]
        kept = []
        used = 0
        for role, text in reversed(turns):
            if used + len(text) > HISTORY_CHAR_BUDGET and kept:
                break
            kept.append((role, text))
            used += len(text)
    kept.reverse()
    # PR-1 (prompt cache): messages[0] — ВСЕГДА дословно SYSTEM_PROMPT, без динамики.
    # Сводки компрессии и memory_block уходят отдельным system-сообщением перед последним
    # user-ходом: OpenAI-совместимые провайдеры (DeepSeek) кэшируют по префиксу, и правка
    # первого сообщения обнуляла кэш на весь запрос.
    messages = [ChatMessage(role="system", content=SYSTEM_PROMPT)] + [
        ChatMessage(role=r, content=t) for r, t in head + kept
    ]
    return _attach_dynamic(messages, _dynamic_system_block(summaries, memory_block))


def _dynamic_system_block(summaries: list[str], memory_block: str) -> str:
    """Динамическая часть системного контекста: сводки компрессии + релевантная память.

    Вынесена из messages[0] намеренно (PR-1): префикс промпта должен быть бит-в-бит
    одинаковым между запросами, иначе prompt-кэш провайдера не работает.
    """
    parts: list[str] = []
    if summaries:
        parts.append(
            "Сводка более ранней части диалога (сжата автоматически):\n" + "\n\n".join(summaries[-3:])
        )
    if memory_block:
        parts.append(memory_block)
    return "\n\n".join(parts)


def _attach_dynamic(prompt: list[ChatMessage], dynamic: str) -> list[ChatMessage]:
    """Вставляет динамический system-блок непосредственно перед последним user-сообщением.

    Пустой/пробельный dynamic — промпт возвращается без изменений (стабильный префикс).
    """
    if not dynamic or not dynamic.strip():
        return prompt
    insert_at = len(prompt)
    for i in range(len(prompt) - 1, 0, -1):
        if prompt[i].role == "user":
            insert_at = i
            break
    return prompt[:insert_at] + [ChatMessage(role="system", content=dynamic)] + prompt[insert_at:]


def _dlp_mask(prompt: list[ChatMessage], session_id: uuid.UUID) -> tuple[list[ChatMessage], bool]:
    """PR-7: маскирует исходящий промпт чата, если DLP включён флагом.

    Returns:
        (промпт, применено_ли_маскирование). При выключенном флаге промпт возвращается
        как есть — путь чата не меняется вовсе.
    """
    settings = get_settings()
    if not settings.dlp_chat_enabled:
        return prompt, False
    from aria.core.privacy import EntityAnonymizer

    with session_scope() as db:
        row = repo.get_privacy_shadow_map(db, session_id)
        shadow = dict(row.map_json or {}) if row is not None else {}

    anonymizer = EntityAnonymizer(names=settings.dlp_names_list, shadow_map=shadow)
    masked = [ChatMessage(role=m.role, content=anonymizer.anonymize(m.content)) for m in prompt]
    with session_scope() as db:
        repo.upsert_privacy_shadow_map(db, session_id, anonymizer.shadow_map)
    return masked, True


def _dlp_restore(text: str, session_id: uuid.UUID) -> str:
    """PR-7: возвращает владельцу исходные значения вместо плейсхолдеров."""
    from aria.core.privacy import EntityAnonymizer

    with session_scope() as db:
        row = repo.get_privacy_shadow_map(db, session_id)
        shadow = dict(row.map_json or {}) if row is not None else {}
    return EntityAnonymizer(shadow_map=shadow).restore(text)


def _explain_llm_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (ProviderUnavailable, NoAvailableKeys)):
        tried = tried_suffix(exc)
        if "empty reply" in tried:
            return HTTPException(
                status_code=502,
                detail=f"The models answered but returned no text. Try again or pick another model.{tried}",
            )
        return HTTPException(
            status_code=503,
            detail="All configured models are unavailable right now (rate limit or bad keys). "
            f"Wait a minute and retry, or add another key in Keys.{tried}",
        )
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        tried = tried_suffix(exc)
        if code == 429:
            return HTTPException(status_code=429, detail=f"The model's rate limit is reached. Retry in a minute.{tried}")
        if code in (402, 403):
            who = getattr(exc, "provider_id", None)
            return HTTPException(
                status_code=502,
                detail=(f"The model provider {who + ' ' if who else ''}rejected the account (HTTP {code}: no balance or no access). "
                        f"Top up that account or remove its key in Keys; other providers are tried automatically.{tried}"),
            )
        if code == 404:
            return HTTPException(
                status_code=502,
                detail="The model name is not available at the provider (HTTP 404). "
                f"Open Env, fix GEMINI_FLASH_MODEL / GEMINI_PRO_MODEL to a model from the provider's list, and restart the app.{tried}",
            )
        return HTTPException(status_code=502, detail=f"The model API answered HTTP {code}.{tried}")
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return HTTPException(status_code=504, detail="The model API did not answer in time (network or timeout).")
    return HTTPException(status_code=502, detail=f"Model call failed: {type(exc).__name__}")


@router.get("/status")
async def chat_status(request: Request, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    providers = _real_provider_ids(request)
    return {"configured": bool(providers), "providers": providers, "hint": None if providers else NOT_CONFIGURED_HINT}


@router.get("/models")
async def chat_models(request: Request, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    """Что реально доступно по ключам владельца: классы (tier) и конкретные модели."""
    llm_router = getattr(request.app.state, "router", None)
    models: list[dict[str, Any]] = []
    tiers: dict[str, bool] = {t: False for t in runprofile.TIERS}
    if llm_router is not None:
        for cls, providers in llm_router.providers_by_class.items():
            for p in providers:
                if p.provider_id.startswith("stub") or cls == "vision_multimodal":
                    continue
                models.append({"id": p.provider_id, "model": getattr(p, "model", p.provider_id), "tier": cls})
                for tier, tier_class in runprofile.TIERS.items():
                    if tier_class == cls:
                        tiers[tier] = True
    models.sort(key=lambda r: (-runprofile.class_rank(r["tier"]), r["id"]))
    return {"tiers": tiers, "models": models}


def _validated_profile(raw: Any, request: Request) -> tuple[dict, list[str]]:
    try:
        return runprofile.normalize(raw, getattr(request.app.state, "router", None))
    except runprofile.ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/profile")
async def chat_default_profile(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    """Профиль для новой сессии: последний сохранённый, иначе встроенный."""
    with session_scope() as db:
        profile = repo.get_agent_state(db, runprofile.DEFAULT_KEY)
    return {"profile": profile or runprofile.builtin_profile(), "saved": bool(profile)}


@router.get("/sessions/{session_id}/profile")
async def chat_get_profile(session_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    with session_scope() as db:
        if repo.get_session(db, session_id) is None:
            raise HTTPException(status_code=404, detail="session not found")
        profile = runprofile.get_profile(db, session_id)
    return {"profile": profile or runprofile.builtin_profile(), "saved": bool(profile)}


@router.put("/sessions/{session_id}/profile")
async def chat_put_profile(
    session_id: uuid.UUID, payload: dict[str, Any], request: Request, _: str = Depends(require_runtime_token)
) -> dict[str, Any]:
    profile, notes = _validated_profile(payload, request)
    with session_scope() as db:
        if repo.get_session(db, session_id) is None:
            raise HTTPException(status_code=404, detail="session not found")
        runprofile.save_profile(db, session_id, profile)
    return {"profile": profile, "notes": notes}


@router.get("/sessions")
async def chat_sessions(_: str = Depends(require_runtime_token)) -> list[dict[str, Any]]:
    """Список чатов + индикаторы для UI: pending_approvals (ждёт Approve), busy (задача идёт),
    last_reply_at (время последнего ответа ассистента - UI сравнивает с «прочитано»)."""
    with session_scope() as db:
        counts = dict(
            db.execute(select(m.Message.session_id, func.count()).group_by(m.Message.session_id)).all()
        )
        last_reply = dict(
            db.execute(
                select(m.Message.session_id, func.max(m.Message.created_at))
                .where(m.Message.role == "assistant")
                .group_by(m.Message.session_id)
            ).all()
        )
        pending = repo.list_attention_items(db, only_pending=True)
        terminal = {"done", "done_unaudited", "failed", "cancelled"}
        sessions = repo.list_sessions(db)
        rows = []
        for s in sessions:
            task = repo.get_task(db, s.current_task_id) if getattr(s, "current_task_id", None) else None
            status = None
            if task is not None:
                status = task.status.value if hasattr(task.status, "value") else str(task.status)
            n_pending = sum(
                1 for it in pending if it.session_id == s.id or (task is not None and it.task_id == task.id)
            )
            rows.append(
                {
                    "id": str(s.id),
                    "title": s.title,
                    "message_count": int(counts.get(s.id, 0)),
                    "updated_at": iso(getattr(s, "updated_at", None) or getattr(s, "created_at", None)),
                    "pending_approvals": n_pending,
                    "busy": status is not None and status not in terminal,
                    "last_reply_at": iso(last_reply.get(s.id)),
                }
            )
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
    # chat_total: весь хендлер, от запроса до сохранённого ответа (4xx/5xx считаются как errors).
    # Не декоратор: при ``from __future__ import annotations`` FastAPI разбирает аннотации в globals обёртки.
    with timing.stage("chat_total"):
        return await _chat_send(session_id, payload, request)


async def _chat_send(session_id: uuid.UUID, payload: dict[str, Any], request: Request) -> dict[str, Any]:
    t_start = time.monotonic()
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
        user_payload = serialize_message(user_msg)
    if not retry:
        emit_message_created(user_msg, session_id, None)

    # 1a) CTX: сжать старую часть истории, если она выросла выше порога токенов.
    # Вне DB-scope: сетевой вызов суммаризатора не держит writer-lock; сбой сжатия чат не роняет.
    try:
        await compression.maybe_compress(session_id)
    except Exception:  # noqa: BLE001
        logger.exception("chat: compression failed, continuing with full history")
    with session_scope() as db:
        history = [(msg.role, msg.content) for msg in repo.list_messages_for_prompt(db, session_id, limit=5000)]

    # 1b) long-term memory (H3): «запомни: …» saves a fact; relevant notes go into the system prompt.
    # Outside the DB scope above: the memory store uses its own connection.
    last_user_text = content or user_payload.get("content") or ""
    memory_block = ""
    if not retry:
        fact = memory_store.parse_remember(content)
        if fact and memory_store.enabled():
            try:
                memory_store.add("fact", fact, source="user", session_id=str(session_id))
            except Exception:  # noqa: BLE001 — память не должна ронять чат
                logger.exception("chat: could not save memory")
    if memory_store.enabled():
        memory_block = memory_store.recall_block(last_user_text)

    # 2) call the model outside any DB transaction
    with session_scope() as db:
        profile = runprofile.get_profile(db, session_id)
    chat_class, chat_fallback, chat_prefer = runprofile.chat_target(
        profile, request.app.state.router, CHAT_PROVIDER_CLASS, CHAT_FALLBACK_CLASSES
    )
    prompt = _build_prompt(history, memory_block)
    hint = runprofile.thinking_hint(profile)
    # PR-1: thinking_hint больше не дописывается в messages[0] — он тоже ломал кэш.
    prompt = _attach_dynamic(prompt, hint)
    # PR-7: DLP применяется к исходящему промпту и снимается с ответа. Только чат —
    # агентский цикл не трогаем: плейсхолдеры сломали бы пути и команды в tool calls.
    prompt, dlp_applied = _dlp_mask(prompt, session_id)
    # всё до вызова модели (запись сообщения, сжатие, память, сборка промпта): то, что пользователь ждёт зря
    timing.record("chat_prepare", (time.monotonic() - t_start) * 1000.0)
    try:
        result = await request.app.state.router.route_chat(
            chat_class,
            prompt,
            [],
            timeout_sec=60,
            resilient=True,
            fallback_classes=chat_fallback,
            prefer_provider_id=chat_prefer,
            reject_empty=True,
        )
    except Exception as exc:  # noqa: BLE001 - mapped to a clear HTTP error below
        logger.warning("chat: model call failed: %s", exc)
        raise _explain_llm_error(exc) from exc
    reply = (result.response.text or "").strip()
    if dlp_applied and reply:
        reply = _dlp_restore(reply, session_id)
    if not reply:
        raise HTTPException(status_code=502, detail="The model returned an empty answer. Try again.")
    if result.provider_id.startswith("stub"):
        raise HTTPException(status_code=503, detail=NOT_CONFIGURED_HINT)

    # 3) store the assistant message
    with timing.stage("chat_store"), session_scope() as db:
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
        "fallback": bool(result.fallback),
    }

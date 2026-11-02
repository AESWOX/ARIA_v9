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

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select

from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.core import runprofile, timing
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import SourceTrust, TaskStatus
from aria.http_utils import emit_message_created, emit_session_updated, iso, serialize_message
from aria.llm import compression
from aria.llm.key_pool import NoAvailableKeys
from aria.llm.providers.base import ChatMessage, LlmProvider
from aria.llm.router import ProviderUnavailable, tried_suffix
from aria.memory import store as memory_store

logger = logging.getLogger("local_agent.chat")
router = APIRouter(prefix="/chat", tags=["chat"])

# UX-1: отдельная точка замера, чтобы тесты подменяли часы без секундомера.
_monotonic = time.monotonic

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
    "No language model is configured. Open Keys and add a key for any provider "
    "(DeepSeek, Gemini or Groq), then send the message again."
)


@dataclass
class _ChatStream:
    stop_event: asyncio.Event
    task: asyncio.Task | None = None


_ACTIVE_STREAMS: dict[uuid.UUID, _ChatStream] = {}

_TERMINAL_TASK_STATUSES = {
    TaskStatus.done,
    TaskStatus.done_unaudited,
    TaskStatus.failed,
    TaskStatus.cancelled,
}


async def _switch_to_agent(session_id: uuid.UUID, payload: dict[str, Any], request: Request) -> dict[str, Any] | None:
    """19.4b B2: запрос, которому нужен инструмент, уходит из Chat в Agent.

    Возвращает ответ с ``mode="agent"`` (и ``task_id``), если сессия переключена,
    иначе None — сообщение идёт обычным чат-путём. Гейт уточнений здесь не запускается:
    действие уже названо, карточки только мешают (см. 19.4b B1/B2).
    """
    from aria.core import clarifier

    content = str(payload.get("content") or "").strip()
    if not content or payload.get("retry"):
        return None
    if clarifier.is_small_talk(content) or not clarifier.needs_tools(content):
        return None
    runner = getattr(request.app.state, "task_runner", None)
    if runner is None:
        return None
    with session_scope() as db:
        session = repo.get_session(db, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        task = repo.get_task(db, session.current_task_id) if session.current_task_id else None
        if task is not None and task.status not in _TERMINAL_TASK_STATUSES:
            return None  # задача уже идёт: не переключаем повторно
        task = repo.create_task(
            db,
            session,
            role=runprofile.root_role(runprofile.get_profile(db, session.id)),
            objective=content,
            draft_tz_md=f"# {content[:80]}\n",
        )
        repo.set_task_status(db, task, TaskStatus.approved)
        user_msg = repo.append_message(
            db, session, role="user", content=content, source_trust=SourceTrust.trusted
        )
        session.updated_at = datetime.now(timezone.utc)
        if (session.title or "") in DEFAULT_TITLES:
            session.title = content.replace("\n", " ")[:60]
        task_id = task.id
    emit_message_created(user_msg, session_id, task_id)
    queued = bool(runner.submit(task_id, mode="agent", source="ui").get("queued"))
    return {"ok": True, "mode": "agent", "task_id": str(task_id), "queued": queued}


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


@router.get("/sessions/{session_id}/usage")
async def chat_session_usage(session_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    """Стоимость сессии и контекст по llm_usage (E1/E2/E3).

    ``session_cost`` — сумма известных cost_usd строк llm_usage сессии. Если часть
    вызовов без цены, ``session_cost_complete=False`` и ``priced=False``, но
    ``session_cost_partial=True``: частичную сумму показываем, а не скрываем (D-38).
    """
    with session_scope() as db:
        if repo.get_session(db, session_id) is None:
            raise HTTPException(status_code=404, detail="session not found")
        history = repo.list_messages_for_prompt(db, session_id)
        context_tokens = compression.history_tokens(history)
        profile = runprofile.get_profile(db, session_id) or runprofile.builtin_profile()
        main_seat = profile.get("main") if isinstance(profile, dict) else None
        provider_id = main_seat.get("model") if isinstance(main_seat, dict) else None  # здесь фактически provider_id
        context_window: int | None = None
        if provider_id:
            windows = [
                int(row.context_window)
                for row in repo.list_provider_models(db, provider_id)
                if row.context_window
            ]
            context_window = min(windows) if windows else None
        costs = list(
            db.execute(select(m.LlmUsage.cost_usd).where(m.LlmUsage.session_id == session_id)).scalars()
        )
        cache_rows = db.execute(
            select(m.LlmUsage.prompt_hit, m.LlmUsage.prompt_miss).where(m.LlmUsage.session_id == session_id)
        ).all()
    known = [(h, mi) for h, mi in cache_rows if h is not None and mi is not None]
    cache_hits = sum(int(h) for h, _mi in known)
    cache_total = sum(int(h) + int(mi) for h, mi in known)
    cache_hit_ratio = round(cache_hits / cache_total, 3) if cache_total > 0 else None
    priced_messages = sum(1 for c in costs if c is not None)
    unpriced_messages = len(costs) - priced_messages
    session_cost_complete = bool(costs) and unpriced_messages == 0
    session_cost_partial = priced_messages > 0 and unpriced_messages > 0
    priced_total = sum(float(c) for c in costs if c is not None)
    context_pct = round(context_tokens / context_window * 100, 1) if context_window else None
    return {
        "context_tokens": context_tokens,
        "context_window": context_window,
        "context_pct": context_pct,
        "session_cost": round(priced_total, 6) if priced_messages else None,
        "session_cost_complete": session_cost_complete,
        "session_cost_partial": session_cost_partial,
        "priced": priced_messages > 0 and session_cost_complete,
        "priced_messages": priced_messages,
        "unpriced_messages": unpriced_messages,
        "cache_hit_ratio": cache_hit_ratio,
    }


@router.post("/sessions/{session_id}/send")
async def chat_send(
    session_id: uuid.UUID,
    payload: dict[str, Any],
    request: Request,
    _: str = Depends(require_runtime_token),
) -> Any:
    # chat_total: весь хендлер, от запроса до сохранённого ответа (4xx/5xx считаются как errors).
    # Не декоратор: при ``from __future__ import annotations`` FastAPI разбирает аннотации в globals обёртки.
    with timing.stage("chat_total"):
        switched = await _switch_to_agent(session_id, payload, request)
        if switched is not None:
            if payload.get("stream"):
                async def _switched_events() -> AsyncIterator[str]:
                    yield _ndjson_line({"type": "done", "result": switched})

                return StreamingResponse(
                    _switched_events(),
                    media_type="application/x-ndjson",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
                )
            return switched
        if payload.get("stream"):
            return await _chat_send_stream(session_id, payload, request)
        return await _chat_send(session_id, payload, request)


@router.post("/sessions/{session_id}/stop")
async def chat_stop(session_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    """Прервать идущую генерацию ответа; сессия остаётся живой и принимает новое сообщение."""
    entry = _ACTIVE_STREAMS.get(session_id)
    if entry is None:
        return {"ok": True, "stopped": False}
    entry.stop_event.set()
    return {"ok": True, "stopped": True}


@dataclass
class _Prepared:
    user_payload: dict[str, Any]
    prompt: list[ChatMessage]
    chat_class: str
    chat_fallback: tuple[str, ...]
    chat_prefer: str | None
    dlp_applied: bool
    sensitive: bool = False
    started_monotonic: float = 0.0


async def _prepare_chat(session_id: uuid.UUID, payload: dict[str, Any], request: Request) -> _Prepared:
    """Запись user-сообщения, сжатие истории, память и сборка промпта — общая для стрима и не-стрима."""
    t_start = time.monotonic()
    started_monotonic = _monotonic()
    content = str(payload.get("content") or "").strip()
    retry = bool(payload.get("retry"))  # re-ask using the last stored user message (no duplicate)
    if not content and not retry:
        raise HTTPException(status_code=400, detail="content required")
    if len(content) > MAX_CONTENT_CHARS:
        raise HTTPException(status_code=413, detail=f"message too long (max {MAX_CONTENT_CHARS} characters)")
    if not _real_provider_ids(request):
        raise HTTPException(status_code=503, detail=NOT_CONFIGURED_HINT)

    # E9: детект чувствительного — по СЫРОМУ тексту, до DLP-маски.
    from aria.core.privacy import contains_sensitive

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
        sensitive = contains_sensitive(content if not retry else (user_msg.content or ""))
    if not retry:
        emit_message_created(user_msg, session_id, None)

    # 1a) CTX: сжать старую часть истории, если она выросла выше порога токенов.
    # Вне DB-scope: сетевой вызов суммаризатора не держит writer-lock; сбой сжатия чат не роняет.
    try:
        await compression.maybe_compress(session_id, request.app.state.router)
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
    return _Prepared(
        user_payload=user_payload,
        prompt=prompt,
        chat_class=chat_class,
        chat_fallback=chat_fallback,
        chat_prefer=chat_prefer,
        sensitive=sensitive,
        dlp_applied=dlp_applied,
        started_monotonic=started_monotonic,
    )


def _log_chat_timing(prepared: _Prepared, session_id: uuid.UUID, first_token_at: float | None = None) -> None:
    """UX-1: одна строка на готовый ответ чата, включая стрим. Без текстов сообщений."""
    now = _monotonic()
    total_ms = max(0.0, (now - prepared.started_monotonic) * 1000.0)
    first_ms = (
        total_ms
        if first_token_at is None
        else max(0.0, (first_token_at - prepared.started_monotonic) * 1000.0)
    )
    logger.info(
        "chat_timing first_token_ms=%.0f total_ms=%.0f session_id=%s",
        first_ms,
        total_ms,
        str(session_id)[:8],
    )


async def _run_nonstream(session_id: uuid.UUID, request: Request, prepared: _Prepared) -> dict[str, Any]:
    """Полный не-стрим ответ: route_chat + DLP-restore + проверки + запись ассистента."""
    try:
        result = await request.app.state.router.route_chat(
            prepared.chat_class,
            prepared.prompt,
            [],
            timeout_sec=60,
            resilient=True,
            fallback_classes=prepared.chat_fallback,
            prefer_provider_id=prepared.chat_prefer,
            reject_empty=True,
            purpose="chat",
            session_id=session_id,
            sensitive=prepared.sensitive,
        )
    except Exception as exc:  # noqa: BLE001 - mapped to a clear HTTP error below
        logger.warning("chat: model call failed: %s", exc)
        raise _explain_llm_error(exc) from exc
    reply = (result.response.text or "").strip()
    if prepared.dlp_applied and reply:
        reply = _dlp_restore(reply, session_id)
    if not reply:
        raise HTTPException(status_code=502, detail="The model returned an empty answer. Try again.")
    if result.provider_id.startswith("stub"):
        raise HTTPException(status_code=503, detail=NOT_CONFIGURED_HINT)

    # 19.6a: usage из ответа провайдера — для /usage и стоимости сессии.
    provider = request.app.state.router.find_provider(result.provider_id)
    assistant_meta = {
        "model": getattr(provider, "model", None),
        "provider_id": result.provider_id,
        "usage": dict(result.response.usage or {}),
    }

    # 3) store the assistant message
    with timing.stage("chat_store"), session_scope() as db:
        session = repo.get_session(db, session_id)
        if session is None:  # deleted while the model was thinking
            raise HTTPException(status_code=404, detail="session was deleted")
        assistant_msg = repo.append_message(
            db,
            session,
            role="assistant",
            content=reply,
            source_trust=SourceTrust.trusted,
            content_json=assistant_meta,
        )
        session.updated_at = datetime.now(timezone.utc)
        assistant_payload = serialize_message(assistant_msg)
    emit_message_created(assistant_msg, session_id, None)
    _log_chat_timing(prepared, session_id)
    return {
        "user": prepared.user_payload,
        "assistant": assistant_payload,
        "provider_id": result.provider_id,
        "degraded": bool(result.degraded_to_free),
        "fallback": bool(result.fallback),
    }


async def _chat_send(session_id: uuid.UUID, payload: dict[str, Any], request: Request) -> dict[str, Any]:
    prepared = await _prepare_chat(session_id, payload, request)
    return await _run_nonstream(session_id, request, prepared)


def _ndjson_line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, ensure_ascii=False) + "\n"


def _pick_stream_provider(request: Request, prepared: _Prepared) -> LlmProvider | None:
    """Прямая выборка провайдера для стрима (router.py не меняется): prefer -> класс -> fallback.

    Stub-провайдеры пропускаются: стримить заглушку бессмысленно — не-стрим путь вернёт NOT_CONFIGURED.
    """
    llm_router = getattr(request.app.state, "router", None)
    if llm_router is None:
        return None
    if prepared.chat_prefer:
        p = llm_router.find_provider(prepared.chat_prefer)
        if p is not None and not p.provider_id.startswith("stub"):
            return p
    for cls in (prepared.chat_class, *prepared.chat_fallback):
        for p in llm_router.providers_by_class.get(cls, []):
            if not p.provider_id.startswith("stub"):
                return p
    return None


async def _chat_send_stream(session_id: uuid.UUID, payload: dict[str, Any], request: Request) -> StreamingResponse:
    prepared = await _prepare_chat(session_id, payload, request)
    return StreamingResponse(
        _stream_events(session_id, request, prepared),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_events(session_id: uuid.UUID, request: Request, prepared: _Prepared) -> AsyncIterator[str]:
    """NDJSON: {"type":"delta","text":"…"} … {"type":"done","result":{…}} / {"type":"error",…}.

    Любой сбой стрима — не-стрим fallback через ``_run_nonstream``; тогда клиент получает только
    ``done`` с полным ответом (частичные delta до этого отбрасываются фронтендом).

    Stop (19.2): ``provider.stream()`` идёт в отдельной задаче; генератор гоняется с stop-событием
    сессии и при Stop отменяет задачу, отдавая ``{"type":"stopped"}`` без сохранения ассистента.
    """
    provider = _pick_stream_provider(request, prepared)
    if provider is None or prepared.dlp_applied or prepared.sensitive:
        result = await _run_nonstream(session_id, request, prepared)
        yield _ndjson_line({"type": "done", "result": result})
        return

    queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
    stop_event = asyncio.Event()
    entry = _ChatStream(stop_event=stop_event)
    _ACTIVE_STREAMS[session_id] = entry

    async def consume() -> None:
        try:
            async for chunk in provider.stream(prepared.prompt, [], 60):
                await queue.put(("chunk", chunk))
                if chunk.final:
                    return
            await queue.put(("end", None))
        except Exception as exc:  # noqa: BLE001 — fallback на не-стрим
            await queue.put(("error", exc))

    entry.task = asyncio.create_task(consume())
    reply_parts: list[str] = []
    final_usage: dict = {}
    first_token_at: float | None = None
    stopped = False
    try:
        while True:
            getter = asyncio.ensure_future(queue.get())
            stopper = asyncio.ensure_future(stop_event.wait())
            await asyncio.wait({getter, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if stopper.done():
                stopped = True
                getter.cancel()
                break
            stopper.cancel()
            kind, payload = getter.result()
            if kind == "error":
                logger.warning("chat: stream failed (%s), falling back to non-stream", type(payload).__name__)
                result = await _run_nonstream(session_id, request, prepared)
                yield _ndjson_line({"type": "done", "result": result})
                return
            if kind == "end":
                break
            chunk = payload
            if chunk.text and not chunk.final:
                if first_token_at is None:
                    first_token_at = _monotonic()
                reply_parts.append(chunk.text)
                yield _ndjson_line({"type": "delta", "text": chunk.text})
            if chunk.final:
                final_usage = dict(chunk.usage or {})
                break
    finally:
        entry.task.cancel()
        _ACTIVE_STREAMS.pop(session_id, None)

    if stopped:
        yield _ndjson_line({"type": "stopped"})
        return

    reply = "".join(reply_parts).strip()
    if not reply:
        logger.warning("chat: stream produced an empty reply, falling back to non-stream")
        result = await _run_nonstream(session_id, request, prepared)
        yield _ndjson_line({"type": "done", "result": result})
        return
    if provider.provider_id.startswith("stub"):
        result = await _run_nonstream(session_id, request, prepared)
        yield _ndjson_line({"type": "done", "result": result})
        return

    with timing.stage("chat_store"), session_scope() as db:
        session = repo.get_session(db, session_id)
        if session is None:  # deleted while the model was streaming
            raise HTTPException(status_code=404, detail="session was deleted")
        assistant_msg = repo.append_message(
            db,
            session,
            role="assistant",
            content=reply,
            source_trust=SourceTrust.trusted,
            content_json={
                "model": getattr(provider, "model", None),
                "provider_id": provider.provider_id,
                "usage": final_usage,
            },
        )
        session.updated_at = datetime.now(timezone.utc)
        assistant_payload = serialize_message(assistant_msg)
    if final_usage:
        # E1: стрим идёт мимо route_chat — пишем строку ledger'а здесь, один раз.
        from aria.llm import usage as llm_usage

        llm_usage.record_usage(
            provider_id=provider.provider_id,
            model=getattr(provider, "model", None),
            usage=final_usage,
            purpose="chat",
            session_id=session_id,
        )
    emit_message_created(assistant_msg, session_id, None)
    _log_chat_timing(prepared, session_id, first_token_at)
    yield _ndjson_line(
        {
            "type": "done",
            "result": {
                "user": prepared.user_payload,
                "assistant": assistant_payload,
                "provider_id": provider.provider_id,
                "degraded": False,
                "fallback": False,
            },
        }
    )

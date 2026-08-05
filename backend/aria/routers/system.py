"""System routes: status, health, model info, shutdown, self-test, feedback, websocket."""
from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect

from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.core.events import event_bus
from aria.core.rate_limit import feedback_limiter, shutdown_limiter
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.http_utils import health_payload, iso, serialize_session, utc_now

router = APIRouter(tags=["system"])


@router.get("/status")
async def status_alias() -> dict[str, Any]:
    return await get_health()


@router.get("/health")
async def get_health() -> dict[str, Any]:
    return health_payload()


@router.get("/model/info")
async def model_info_alias(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    from aria.routers.providers import TIER_HINTS

    with session_scope() as db:
        rows = repo.list_provider_models(db)
    return {
        "models": [
            {
                "id": r.id,
                "provider_id": r.provider_id,
                "model_id": r.model_id,
                "context_window": r.context_window,
                "is_free_tier": r.is_free_tier,
            }
            for r in rows
        ],
        "tier_hints": TIER_HINTS,
    }


@router.post("/system/shutdown")
async def shutdown_backend(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    if not shutdown_limiter.allow("global"):
        raise HTTPException(status_code=429, detail="shutdown rate-limited")
    loop = asyncio.get_running_loop()
    loop.call_later(0.15, lambda: os._exit(0))
    return {'ok': True}


@router.get("/system/self-test")
async def system_self_test() -> dict[str, Any]:
    """§0: живой эндпоинт целостности — проверяет БД, модели, репо, роутер, скиллы."""
    try:
        _ = m.ProviderHealth
    except Exception as exc:
        return {"status": "error", "issues": [f"Module m not accessible: {exc}"]}
    issues: list[str] = []
    checks: dict[str, Any] = {}
    settings = get_settings()

    # 1. База данных
    try:
        with session_scope() as db:
            # простой запрос — жива ли БД
            from sqlalchemy import text
            db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {exc}"
        issues.append(f"Database unreachable: {exc}")

    # 2. Модели — все ли таблицы созданы
    from sqlalchemy import inspect as sa_inspect
    try:
        with session_scope() as db:
            inspector = sa_inspect(db.bind)
            tables = set(inspector.get_table_names())
        expected = {"sessions", "tasks", "messages", "tool_calls", "audit_reports",
                    "attention_items", "provider_health", "agent_state", "events"}
        missing = expected - tables
        if missing:
            checks["models"] = f"missing tables: {missing}"
            issues.append(f"Missing DB tables: {missing}")
        else:
            checks["models"] = f"ok ({len(tables)} tables)"
    except Exception as exc:
        checks["models"] = f"error: {exc}"
        issues.append(f"Model check failed: {exc}")

    # 3. Router — проверяем провайдеры через БД (самый надёжный способ)
    try:
        with session_scope() as db:
            count = db.query(m.ProviderHealth).count()
            classes = db.query(m.ProviderHealth.provider_class).distinct().count()
        checks["router"] = f"ok ({count} providers, {classes} classes)"
    except Exception as exc:
        checks["router"] = f"error: {exc}"
        issues.append(f"Router check failed: {exc}")

    # 4. Config — чувствительные поля не пустые
    try:
        config_issues = []
        if not settings.loop_max_iterations:
            config_issues.append("loop_max_iterations is 0")
        if not settings.audit_max_attempts:
            config_issues.append("audit_max_attempts is 0")
        if config_issues:
            checks["config"] = f"issues: {', '.join(config_issues)}"
            issues.extend(config_issues)
        else:
            checks["config"] = "ok"
    except Exception as exc:
        checks["config"] = f"error: {exc}"

    # 5. Version / identity
    try:
        import importlib.metadata
        version = importlib.metadata.version("local-agent") if hasattr(importlib.metadata, "version") else "0.0.0"
    except Exception:
        version = "0.0.0 (dev)"

    # 6. Provider catalog — реально ли есть модели в БД
    try:
        with session_scope() as db:
            catalog_count = db.query(m.ProviderModel).count()
        checks["provider_catalog"] = f"{catalog_count} models cached" if catalog_count else "empty (not refreshed yet or no real providers configured)"
    except Exception as exc:
        checks["provider_catalog"] = f"error: {exc}"

    # 7. Vault / skills — честная проверка вместо декларации в markdown
    try:
        import os as _os
        vault_path = settings.OBSIDIAN_VAULT_PATH
        vault_exists = _os.path.isdir(vault_path)
        checks["vault"] = "ok" if vault_exists else "missing"
        if not vault_exists:
            issues.append(f"OBSIDIAN_VAULT_PATH={vault_path} does not exist")
    except Exception as exc:
        checks["vault"] = f"error: {exc}"

    try:
        with session_scope() as db:
            skills_count = db.query(m.SkillMeta).count()
        checks["skills"] = f"{skills_count} skills in skills_meta"
    except Exception as exc:
        checks["skills"] = f"error: {exc}"

    # 8. Budget enforcement — пороги не нулевые, бюджет активен
    try:
        if settings.budget_warn_threshold_pct and settings.budget_block_threshold_pct:
            checks["budget"] = f"warn@{settings.budget_warn_threshold_pct}% block@{settings.budget_block_threshold_pct}%"
        else:
            checks["budget"] = "disabled (thresholds are 0)"
            issues.append("Budget enforcement thresholds are zero")
    except Exception as exc:
        checks["budget"] = f"error: {exc}"

    # 9. Guardrail state — модуль загружен и интегрирован в loop
    try:
        from aria.core.guardrails import ToolCallGuardrailController
        g = ToolCallGuardrailController()
        # verify interface: before_call + after_call as entry points
        assert hasattr(g, 'before_call'), "missing before_call"
        assert hasattr(g, 'after_call'), "missing after_call"
        # verify all three detection counters exist (set by dataclass __init__)
        assert hasattr(g, '_exact_failure_counts'), "missing exact-failure counter"
        assert hasattr(g, '_same_tool_failure_counts'), "missing same-tool counter"
        assert hasattr(g, '_no_progress'), "missing no-progress counter"
        checks["guardrails"] = "ok (3 detectors via before_call/after_call: exact-failure, same-tool, no-progress)"
    except Exception as exc:
        checks["guardrails"] = f"error: {exc}"
        issues.append(f"Guardrail check failed: {exc}")

    status = "ok" if not issues else "degraded"
    return {
        "agent": "ARIA",
        "version": version,
        "status": status,
        "issues": issues,
        "checks": checks,
        "ts": iso(utc_now()),
    }


@dataclass
class FeedbackBody:
    task_id: str
    rating: int = 5  # 1-5
    comment: str = ""
    skill_name: str | None = None


@router.post("/system/feedback")
async def system_feedback(body: FeedbackBody, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    """§0: фидбек-луп — сохраняет отзыв и опционально создаёт skill-candidate."""
    if not feedback_limiter.allow(body.task_id or "anon"):
        raise HTTPException(status_code=429, detail="feedback rate-limited")
    if body.rating < 1 or body.rating > 5:
        raise HTTPException(status_code=422, detail="rating must be 1-5")

    with session_scope() as db:
        # Сохраняем фидбек как event
        event_bus.emit(
            "feedback.created",
            {
                "task_id": body.task_id,
                "rating": body.rating,
                "comment": body.comment,
                "skill_name": body.skill_name,
            },
            db=db,
        )

        # Если рейтинг >= 4 и есть skill_name — создаём черновик скилла
        skill_created = None
        if body.rating >= 4 and body.skill_name:
            # Проверяем, существует ли уже такой скилл
            from aria.tools.registry import TOOL_REGISTRY
            existing = TOOL_REGISTRY.get(body.skill_name)
            if existing:
                return {"ok": True, "feedback_saved": True, "skill_created": False, "reason": "skill already exists"}

            # Создаём как message-заметку в сессии task
            task = repo.get_task(db, uuid.UUID(body.task_id)) if body.task_id else None
            if task:
                session = repo.get_session(db, task.session_id)
                repo.append_message(
                    db, session, role="system",
                    content=f"[FEEDBACK-SKILL-CANDIDATE] name={body.skill_name}, rating={body.rating}, comment={body.comment[:500]}",
                )
                skill_created = True

        return {"ok": True, "feedback_saved": True, "skill_created": skill_created or False}


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, last_event_id: str | None = Query(default=None)) -> None:
    settings = get_settings()
    await websocket.accept()
    queue = event_bus.subscribe()
    try:
        backlog_sent = False
        if last_event_id:
            try:
                last_uuid = uuid.UUID(last_event_id)
            except ValueError:
                last_uuid = None
            with session_scope() as db:
                events = repo.list_events_after(db, last_uuid, limit=settings.ws_backfill_limit) if last_uuid else []
            if events:
                for event in events:
                    await websocket.send_json(
                        {
                            "event_id": str(event.event_id),
                            "event_type": event.event_type,
                            "session_id": str(event.session_id) if event.session_id else None,
                            "task_id": str(event.task_id) if event.task_id else None,
                            "ts": iso(event.ts),
                            "payload": event.payload,
                        }
                    )
                backlog_sent = True
        if not backlog_sent:
            await websocket.send_json({
                "event_id": str(uuid.uuid4()),
                "event_type": "backend.health_changed",
                "session_id": None,
                "task_id": None,
                "ts": iso(utc_now()),
                "payload": health_payload(ws_connected=True, reconnect_mode="snapshot"),
            })
            with session_scope() as db:
                sessions = repo.list_sessions(db)
            for session in sessions:
                await websocket.send_json({
                    "event_id": str(uuid.uuid4()),
                    "event_type": "session.updated",
                    "session_id": str(session.id),
                    "task_id": str(session.current_task_id) if session.current_task_id else None,
                    "ts": iso(utc_now()),
                    "payload": serialize_session(session),
                })
        while True:
            envelope = await queue.get()
            await websocket.send_json(envelope)
    except WebSocketDisconnect:
        pass
    finally:
        event_bus.unsubscribe(queue)

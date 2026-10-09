"""Minimal scheduler jobs module for v7.1 package completeness.

Это не полноценный scheduler runner, а явная точка интеграции для watchdog / TTL
jobs, чтобы релизная структура соответствовала ТЗ и кодовая ответственность была
очевидной.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from aria.db import repository as repo
from aria.db.base import session_scope


DEFAULT_SCHEDULER_JOBS: list[dict] = [
    {
        "name": "expire_stale_attention_items",
        "schedule": "*/5 * * * *",
        "objective": "TTL watchdog: expire pending attention items whose expires_at elapsed.",
        "allowed_tools": [],
    },
    {
        "name": "refresh_provider_models",
        "schedule": "*/30 * * * *",
        "objective": "Refresh provider model catalog (upsert + purge stale providers).",
        "allowed_tools": [],
    },
]


def seed_default_scheduler_jobs() -> int:
    """L2 prod-release: создать дефолтные scheduler_jobs при старте, если их ещё нет.

    Идемпотентно: при повторном запуске не создаёт дубликаты с тем же именем.
    """
    created = 0
    with session_scope() as db:
        existing = {row.name for row in repo.list_scheduler_jobs(db)}
        for spec in DEFAULT_SCHEDULER_JOBS:
            if spec["name"] in existing:
                continue
            repo.create_scheduler_job(db, **spec)
            created += 1
    return created


def expire_stale_attention_items_job() -> int:
    """Expire pending attention items whose TTL elapsed.

    Реальный результат (expired>0) фиксируется событием ``scheduler.job_run``
    в таблице ``events`` — критерий L2: job реально вызывается и наблюдаем.
    """
    with session_scope() as db:
        expired = repo.expire_stale_attention_items(db)
        if expired:
            repo.persist_event(
                db,
                "scheduler.job_run",
                {"job": "expire_stale_attention_items", "expired": expired},
            )
        return expired


async def run_scheduler_job_by_name(name: str, router=None) -> dict:
    """L2: реальный запуск job'а по имени (для POST /api/cron/jobs/{id}/trigger).

    Неизвестные имена — best-effort (только запись run, без executor).
    """
    if name == "expire_stale_attention_items":
        expired = expire_stale_attention_items_job()
        return {"ok": True, "job": name, "expired": expired}
    if name == "refresh_provider_models":
        if router is None:
            return {"ok": False, "job": name, "error": "router unavailable"}
        count = await refresh_provider_models_job(router)
        return {"ok": True, "job": name, "models": count}
    return {"ok": True, "job": name, "note": "no registered runner (best-effort)"}


def list_scheduler_jobs_payload() -> list[dict]:
    with session_scope() as db:
        rows = repo.list_scheduler_jobs(db)
    return [
        {
            "job_id": str(row.job_id),
            "name": row.name,
            "schedule": row.schedule,
            "enabled": row.enabled,
            "role": row.role,
            "objective": row.objective,
            "allowed_tools": row.allowed_tools,
            "allowed_high_risk_patterns": row.allowed_high_risk_patterns,
            "timeout_sec": row.timeout_sec,
            "max_retries": row.max_retries,
            "last_run_status": row.last_run_status,
            "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        }
        for row in rows
    ]

async def refresh_provider_models_job(router) -> int:
    """Fetches /models from registered providers and upserts provider_models."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    refreshed = 0
    seen: set[str] = set()

    for providers in router.providers_by_class.values():
        for provider in providers:
            provider_id = getattr(provider, "provider_id", "")
            if not provider_id or provider_id in seen:
                continue
            seen.add(provider_id)
            try:
                models = await provider.list_models()
            except Exception:
                continue
            with session_scope() as db:
                for item in models:
                    repo.upsert_provider_model(
                        db,
                        provider_id,
                        str(item.get("model_id") or provider_id),
                        context_window=item.get("context_window"),
                        is_free_tier=item.get("is_free_tier"),
                        price_prompt_usd=item.get("price_prompt_usd"),
                        price_completion_usd=item.get("price_completion_usd"),
                        last_seen=now,
                    )
                    refreshed += 1

    # Purge stale providers no longer registered
    with session_scope() as db:
        current_providers = {row[0] for row in db.execute(text("SELECT DISTINCT provider_id FROM provider_models")).fetchall()}
        if seen:
            stale = current_providers - seen
        else:
            # When ALL providers are offline, seen is empty — keep what we have,
            # but still purge any provider_ids that are obviously fake/stub
            stale = {p for p in current_providers if any(x in p.lower() for x in ["fake", "stub", "dup"])}
        for pid in stale:
                db.execute(text("DELETE FROM provider_models WHERE provider_id = :pid"), {"pid": pid})
                db.execute(text("DELETE FROM provider_health WHERE provider_id = :pid"), {"pid": pid})
    return refreshed


# ═══════════════════════════════════════════════════════════════════
# Волна 1 (A12) — расписания реально исполняются
# ═══════════════════════════════════════════════════════════════════

_FIRST_SEEN: dict[str, datetime] = {}
_BUILTIN_RUNNERS = ("expire_stale_attention_items", "refresh_provider_models")


def _next_run_after(expr: str, base: datetime) -> datetime | None:
    """Следующее срабатывание cron-выражения после base (None — выражение битое)."""
    try:
        from croniter import croniter
    except Exception:  # croniter не обязателен для ядра, но без него cron слеп
        return None
    try:
        return croniter(expr, base).get_next(datetime)
    except Exception:
        return None


def _enqueue_scheduled_task(job_name: str, objective: str, role: str, tools: list, runner) -> str | None:
    """Незарегистрированный cron-job превращается в задачу для TaskRunner."""
    from aria.db import models as m

    with session_scope() as db:
        session = repo.create_session(db, title=f"cron: {job_name}"[:120], active_role=role)
        task = repo.create_task(db, session, role=role, objective=objective or job_name)
        from aria.db.enums import TaskStatus as _TS

        repo.set_task_status(db, task, _TS.approved)
        task_id = str(task.id)
    runner.submit(uuid.UUID(task_id), mode="agent", source=f"cron:{job_name}")
    return task_id


async def run_due_jobs(router=None, runner=None) -> dict:
    """Найти jobs, у которых наступило расписание, и реально их запустить.

    Builtin-имена исполняются напрямую (run_scheduler_job_by_name), остальные
    кладутся задачей в TaskRunner (единая дверь, A10). ``last_run_at``
    обновляется в обоих случаях — иначе job срабатывает бесконечно.
    """
    now = datetime.now(timezone.utc)
    due: list[str] = []
    submitted: list[str] = []

    with session_scope() as db:
        snapshot = [
            (str(r.job_id), r.name, r.schedule, r.last_run_at, r.objective, r.role or "general", list(r.allowed_tools or []))
            for r in repo.list_scheduler_jobs(db)
            if r.enabled
        ]

    for job_id, name, schedule, last_run_at, objective, role, tools in snapshot:
        base = last_run_at
        if base is not None and base.tzinfo is None:
            base = base.replace(tzinfo=timezone.utc)
        # Никогда не запускавшийся job привязываем к моменту, когда планировщик
        # впервые его увидел. Раньше якорем было «сутки назад», и свежесозданный
        # «каждый день в 9:00» срабатывал на ближайшем тике, а не в 9:00.
        anchor = base or _FIRST_SEEN.setdefault(job_id, now)

        nxt = _next_run_after(schedule, anchor)
        if nxt is None or nxt > now:
            continue

        due.append(name)
        ok = False
        if name in _BUILTIN_RUNNERS:
            try:
                result = await run_scheduler_job_by_name(name, router=router)
                ok = bool(result.get("ok"))
            except Exception:
                ok = False
        elif runner is not None:
            try:
                task_id = _enqueue_scheduled_task(name, objective, role, tools, runner)
                ok = task_id is not None
                if task_id:
                    submitted.append(task_id)
            except Exception:
                ok = False

        with session_scope() as db:
            repo.update_scheduler_job(
                db,
                uuid.UUID(job_id),
                last_run_at=now,
                last_run_status="ok" if ok else "error",
            )

    return {"due": due, "submitted": submitted}

"""Minimal scheduler jobs module for v7.1 package completeness.

Это не полноценный scheduler runner, а явная точка интеграции для watchdog / TTL
jobs, чтобы релизная структура соответствовала ТЗ и кодовая ответственность была
очевидной.
"""
from __future__ import annotations

from sqlalchemy import text

from aria.db import repository as repo
from aria.db.base import session_scope


def expire_stale_attention_items_job() -> int:
    """Expire pending attention items whose TTL elapsed."""
    with session_scope() as db:
        return repo.expire_stale_attention_items(db)


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

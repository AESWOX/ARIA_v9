"""Cron routes: /api/cron/jobs, /api/cron/delivery-targets, /api/cron/blueprints.

Storage: ``scheduler_jobs`` table (name/schedule/objective/allowed_tools/enabled/
last_run_*). The table has no columns for ``profile``/``deliver``, so those
are kept in a module-level map keyed by job id — runtime-only metadata that
does not survive a backend restart (documented limitation until the schema
gains the columns). Triggering is best-effort: it records a run; there is no
background executor in this backend (scheduler/jobs.py is a stub).
"""
from __future__ import annotations

import os
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from aria.api.auth import require_runtime_token
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.http_utils import utc_now

router = APIRouter(tags=["cron"])

# job_id (str) -> {deliver, profile}; runtime-only, see module docstring.
_JOB_META: dict[str, dict[str, str]] = {}


def _serialize_job(row: m.SchedulerJob) -> dict[str, Any]:
    jid = str(row.job_id)
    meta = _JOB_META.get(jid, {})
    enabled = bool(row.enabled)
    return {
        "id": jid,
        "profile": meta.get("profile") or "default",
        "profile_name": "Default",
        "aria_home": None,
        "is_default_profile": True,
        "name": row.name,
        "prompt": row.objective,
        "script": None,
        "skills": list(row.allowed_tools or []),
        "schedule": {"kind": "cron", "expr": row.schedule, "display": row.schedule},
        "schedule_display": row.schedule,
        "enabled": enabled,
        "state": "disabled" if not enabled else "scheduled",
        "deliver": meta.get("deliver") or "local",
        "last_run_at": row.last_run_at.isoformat() if row.last_run_at else None,
        "next_run_at": None,
        "last_error": None,
    }


@router.get("/cron/jobs")
async def list_cron_jobs(
    profile: str = Query(default="all"),
    _: str = Depends(require_runtime_token),
) -> list[dict[str, Any]]:
    with session_scope() as db:
        rows = repo.list_scheduler_jobs(db)
    if profile not in ("all", "default"):
        rows = [r for r in rows if _JOB_META.get(str(r.job_id), {}).get("profile") == profile]
    return [_serialize_job(r) for r in rows]


@router.get("/cron/delivery-targets")
async def list_cron_delivery_targets(
    _: str = Depends(require_runtime_token),
) -> dict[str, list[dict[str, Any]]]:
    telegram_set = bool(os.environ.get("TELEGRAM_BOT_TOKEN"))
    return {
        "targets": [
            {"id": "local", "name": "Local execution", "home_target_set": False, "home_env_var": None},
            {"id": "telegram", "name": "Telegram", "home_target_set": telegram_set, "home_env_var": "TELEGRAM_BOT_TOKEN"},
            {"id": "discord", "name": "Discord", "home_target_set": bool(os.environ.get("DISCORD_BOT_TOKEN")), "home_env_var": "DISCORD_BOT_TOKEN"},
        ]
    }


@router.post("/cron/jobs")
async def create_cron_job(
    body: dict[str, Any],
    profile: str = Query(default="default"),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    prompt = (body.get("prompt") or "").strip()
    schedule = (body.get("schedule") or "").strip()
    if not prompt or not schedule:
        raise HTTPException(status_code=400, detail="prompt and schedule are required")
    name = (body.get("name") or "").strip() or f"Job {uuid.uuid4().hex[:6]}"
    skills = list(body.get("skills") or [])
    with session_scope() as db:
        row = repo.create_scheduler_job(
            db,
            name=name,
            schedule=schedule,
            objective=prompt,
            allowed_tools=skills,
        )
        jid = str(row.job_id)
    _JOB_META[jid] = {"deliver": body.get("deliver") or "local", "profile": profile}
    return _serialize_job(row)


@router.put("/cron/jobs/{job_id}")
async def update_cron_job(
    job_id: uuid.UUID,
    body: dict[str, Any],
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    updates_raw = body.get("updates") or {}
    fields: dict[str, Any] = {}
    if "name" in updates_raw:
        fields["name"] = (updates_raw.get("name") or "").strip() or None
    if "schedule" in updates_raw:
        fields["schedule"] = (updates_raw.get("schedule") or "").strip()
    if "prompt" in updates_raw:
        fields["objective"] = (updates_raw.get("prompt") or "").strip()
    if "skills" in updates_raw:
        fields["allowed_tools"] = list(updates_raw.get("skills") or [])
    with session_scope() as db:
        row = repo.update_scheduler_job(db, job_id, **fields)
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        jid = str(row.job_id)
    meta = _JOB_META.setdefault(jid, {})
    if "deliver" in updates_raw:
        meta["deliver"] = updates_raw.get("deliver") or "local"
    return _serialize_job(row)


@router.post("/cron/jobs/{job_id}/pause")
async def pause_cron_job(
    job_id: uuid.UUID,
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    with session_scope() as db:
        row = repo.update_scheduler_job(db, job_id, enabled=False)
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _serialize_job(row)


@router.post("/cron/jobs/{job_id}/resume")
async def resume_cron_job(
    job_id: uuid.UUID,
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    with session_scope() as db:
        row = repo.update_scheduler_job(db, job_id, enabled=True)
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _serialize_job(row)


@router.post("/cron/jobs/{job_id}/trigger")
async def trigger_cron_job(
    job_id: uuid.UUID,
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    """Best-effort trigger: records a run. No background executor exists yet."""
    with session_scope() as db:
        row = repo.update_scheduler_job(
            db, job_id, enabled=True, last_run_at=utc_now(), last_run_status="triggered"
        )
        if row is None:
            raise HTTPException(status_code=404, detail="job not found")
        return _serialize_job(row)


@router.delete("/cron/jobs/{job_id}")
async def delete_cron_job(
    job_id: uuid.UUID,
    _: str = Depends(require_runtime_token),
) -> dict[str, bool]:
    with session_scope() as db:
        ok = repo.delete_scheduler_job(db, job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="job not found")
    _JOB_META.pop(str(job_id), None)
    return {"ok": True}


@router.get("/cron/blueprints")
async def list_automation_blueprints(
    _: str = Depends(require_runtime_token),
) -> dict[str, list[Any]]:
    return {"blueprints": []}


@router.post("/cron/blueprints/instantiate")
async def instantiate_automation_blueprint(
    body: dict[str, Any],
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    raise HTTPException(status_code=404, detail="no automation blueprints available")

"""J1: минимальный API заказов (без отправок наружу)."""
from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from aria.api.auth import require_runtime_token
from aria.db import models as m
from aria.db.base import session_scope
from aria.jobs import ingest, store

router = APIRouter(prefix="/jobs", tags=["jobs"])


def serialize_lead(lead: m.Lead) -> dict[str, Any]:
    return {
        "id": str(lead.id),
        "source": lead.source,
        "url": lead.url,
        "title": lead.title,
        "description": lead.description,
        "budget_text": lead.budget_text,
        "budget_usd_est": lead.budget_usd_est,
        "currency": lead.currency,
        "posted_at": lead.posted_at.isoformat() if lead.posted_at else None,
        "language": lead.language,
        "status": lead.status,
        "score": lead.score,
        "effort_hours_est": lead.effort_hours_est,
        "token_cost_est_usd": lead.token_cost_est_usd,
        "scam_flags": list(lead.scam_flags or []),
        "sensitive": bool(lead.sensitive),
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
        "source_trust": "untrusted",
    }


@router.post("/manual")
async def create_manual(body: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    try:
        with session_scope() as db:
            lead = ingest.manual(
                db,
                url=body.get("url"),
                text=body.get("text"),
                title=body.get("title"),
                sensitive=body.get("sensitive") is True,
            )
            return serialize_lead(lead)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("")
async def list_jobs(
    status: str | None = Query(default=None),
    source: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _: str = Depends(require_runtime_token),
) -> list[dict[str, Any]]:
    with session_scope() as db:
        return [serialize_lead(lead) for lead in store.list_leads(db, status=status, source=source, limit=limit)]


@router.get("/{lead_id}")
async def get_job(lead_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    with session_scope() as db:
        lead = store.get_lead(db, lead_id)
        if lead is None:
            raise HTTPException(status_code=404, detail="lead not found")
        return serialize_lead(lead)


@router.post("/{lead_id}/status")
async def set_job_status(
    lead_id: uuid.UUID, body: dict[str, Any], _: str = Depends(require_runtime_token)
) -> dict[str, Any]:
    requested = str(body.get("status") or "").strip()
    try:
        with session_scope() as db:
            lead = store.update_status(db, lead_id, requested)
            if lead is None:
                raise HTTPException(status_code=404, detail="lead not found")
            return serialize_lead(lead)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

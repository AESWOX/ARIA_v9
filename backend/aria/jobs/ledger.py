"""J6: деньги против токенов. Считает только введённые владельцем числа."""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from aria.db import models as m


def _number(value, name: str, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def add_entry(
    db: OrmSession,
    *,
    lead_id: uuid.UUID,
    revenue: float,
    currency: str,
    platform_fee: float = 0,
    fx_to_usd: float = 1,
    llm_cost_usd: float | None = None,
    hours: float | None = None,
    notes: str = "",
    order_type: str | None = None,
    created_at: datetime | None = None,
) -> m.OrderLedger:
    if db.get(m.Lead, lead_id) is None:
        raise ValueError("lead not found")
    revenue_v = _number(revenue, "revenue")
    fee = _number(platform_fee, "platform_fee")
    fx = _number(fx_to_usd, "fx_to_usd")
    cost = _number(llm_cost_usd, "llm_cost_usd", allow_none=True)
    hours_v = _number(hours, "hours", allow_none=True)
    if revenue_v < 0 or fee < 0 or cost is not None and cost < 0 or hours_v is not None and hours_v < 0:
        raise ValueError("amounts must be non-negative")
    if fee > revenue_v:
        raise ValueError("platform_fee cannot exceed revenue")
    if fx <= 0:
        raise ValueError("fx_to_usd must be positive")
    if not str(currency or "").strip():
        raise ValueError("currency required")
    entry = m.OrderLedger(
        lead_id=lead_id,
        revenue_amount=revenue_v,
        currency=str(currency).strip().upper(),
        platform_fee=fee,
        fx_to_usd=fx,
        llm_cost_usd=cost,
        hours=hours_v,
        notes=str(notes or ""),
        order_type=order_type,
    )
    if created_at is not None:
        entry.created_at = (
            created_at.astimezone(timezone.utc)
            if created_at.tzinfo is not None
            else created_at.replace(tzinfo=timezone.utc)
        )
    db.add(entry)
    db.flush()
    return entry


def entry_report(entry: m.OrderLedger) -> dict:
    revenue_usd = round((float(entry.revenue_amount) - float(entry.platform_fee)) * float(entry.fx_to_usd), 6)
    cost = entry.llm_cost_usd
    profit = round(revenue_usd - float(cost), 6) if cost is not None else None
    hours = entry.hours
    per_hour = round(profit / float(hours), 2) if profit is not None and hours and float(hours) > 0 else None
    return_multiple = round(revenue_usd / float(cost), 2) if cost and float(cost) > 0 else None
    created = entry.created_at
    if created is not None:
        created = created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created.astimezone(timezone.utc)
    return {
        "id": str(entry.id),
        "lead_id": str(entry.lead_id),
        "currency": entry.currency,
        "revenue_usd": revenue_usd,
        "llm_cost_usd": float(cost) if cost is not None else None,
        "profit_usd": profit,
        "profit_per_hour": per_hour,
        "return_multiple": return_multiple,
        "hours": float(hours) if hours is not None else None,
        "order_type": entry.order_type,
        "created_at": created.isoformat() if created else None,
    }


def _entries(db: OrmSession, stmt) -> list[dict]:
    return [entry_report(row) for row in db.execute(stmt).scalars()]


def lead_report(db: OrmSession, lead_id: uuid.UUID) -> dict:
    reports = _entries(db, select(m.OrderLedger).where(m.OrderLedger.lead_id == lead_id).order_by(m.OrderLedger.created_at))
    known = [r for r in reports if r["llm_cost_usd"] is not None]
    return {
        "lead_id": str(lead_id),
        "entries": len(reports),
        "revenue_usd": round(sum(r["revenue_usd"] for r in reports), 6),
        "revenue_usd_known": round(sum(r["revenue_usd"] for r in known), 6),
        "llm_cost_usd": round(sum(r["llm_cost_usd"] for r in known), 6) if known else None,
        "profit_usd": round(sum(r["profit_usd"] for r in known), 6) if known else None,
        "complete": len(known) == len(reports) and bool(reports),
        "rows": reports,
    }


def monthly_report(db: OrmSession, year: int, month: int) -> dict:
    if not 1 <= int(year) <= 9999 or not 1 <= int(month) <= 12:
        raise ValueError("invalid year/month")
    start = datetime(int(year), int(month), 1, tzinfo=timezone.utc)
    end = (
        datetime(int(year) + 1, 1, 1, tzinfo=timezone.utc)
        if int(month) == 12
        else datetime(int(year), int(month) + 1, 1, tzinfo=timezone.utc)
    )
    reports = _entries(
        db,
        select(m.OrderLedger)
        .where(m.OrderLedger.created_at >= start, m.OrderLedger.created_at < end)
        .order_by(m.OrderLedger.created_at),
    )
    known = [r for r in reports if r["llm_cost_usd"] is not None]
    return {
        "year": int(year),
        "month": int(month),
        "entries": len(reports),
        "revenue_usd": round(sum(r["revenue_usd"] for r in reports), 6),
        "revenue_usd_known": round(sum(r["revenue_usd"] for r in known), 6),
        "llm_cost_usd": round(sum(r["llm_cost_usd"] for r in known), 6) if known else None,
        "profit_usd": round(sum(r["profit_usd"] for r in known), 6) if known else None,
        "unknown_cost_entries": len(reports) - len(known),
        "complete": len(known) == len(reports) and bool(reports),
        "rows": reports,
    }

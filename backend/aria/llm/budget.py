"""Spend estimate for the budget gate (router.budget_gate).

Message.content_json does not record the model, so spend is estimated from token counts at
DeepSeek Flash peak rates ($0.30 in / $1.20 out per 1M). Limits come from BUDGET_DAILY_USD /
BUDGET_WEEKLY_USD; 0 disables the corresponding limit.
"""
from __future__ import annotations

from datetime import timedelta

from aria.db import models as m
from aria.db.base import session_scope
from aria.http_utils import utc_now

IN_RATE_PER_1M = 0.30
OUT_RATE_PER_1M = 1.20


def _spend_since(db, since) -> float:
    cost = 0.0
    for role, tokens in db.query(m.Message.role, m.Message.token_estimate).filter(m.Message.created_at >= since).all():
        t = tokens or 0
        if role == "assistant":
            cost += t * OUT_RATE_PER_1M / 1_000_000
        elif role == "user":
            cost += t * IN_RATE_PER_1M / 1_000_000
    return cost


def compute_budget_status(daily_usd: float, weekly_usd: float) -> dict:
    now = utc_now()
    day_start = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as db:
        day = _spend_since(db, day_start)
        week = _spend_since(db, now - timedelta(days=7))
    return {
        "daily_pct": round(day * 100 / daily_usd, 1) if daily_usd > 0 else 0,
        "weekly_pct": round(week * 100 / weekly_usd, 1) if weekly_usd > 0 else 0,
        "daily_usd": round(day, 4),
        "weekly_usd": round(week, 4),
    }

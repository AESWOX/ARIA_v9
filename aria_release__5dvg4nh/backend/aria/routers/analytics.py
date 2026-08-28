"""Analytics routes: /api/analytics/usage, /api/analytics/models.

Aggregates usage over the last N days from the DB. Input/output token
split is derived from ``Message.token_estimate`` by role (user=prompt,
assistant=completion); cache-read and reasoning tokens are not tracked by
the current schema and are reported as 0. Costs are estimates at a flat
per-token rate; real billing is not tracked (``actual_cost=0``).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query

from aria.api.auth import require_runtime_token
from aria.db import models as m
from aria.db.base import session_scope
from aria.http_utils import utc_now

router = APIRouter(tags=["analytics"])

_INPUT_RATE_PER_1M = 2.5  # $ per 1M input tokens (estimate)
_OUTPUT_RATE_PER_1M = 10.0  # $ per 1M output tokens (estimate)


def _day_key(ts: datetime) -> str:
    return ts.astimezone().strftime("%Y-%m-%d")


def _day_range(days: int) -> tuple[datetime, datetime]:
    end = utc_now()
    start = end - timedelta(days=max(1, days) - 1)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, end


def _model_of(msg: m.Message) -> str:
    cj = msg.content_json
    if isinstance(cj, dict) and cj.get("model"):
        return str(cj["model"])
    return "unknown"


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    return round(input_tokens * _INPUT_RATE_PER_1M / 1_000_000 + output_tokens * _OUTPUT_RATE_PER_1M / 1_000_000, 6)


@router.get("/analytics/usage")
async def analytics_usage(
    days: int = Query(default=30, ge=1, le=365),
    profile: str | None = Query(default=None),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    start, end = _day_range(days)
    with session_scope() as db:
        messages = db.query(m.Message).filter(m.Message.created_at >= start).all()
        sessions = db.query(m.Session).filter(m.Session.created_at >= start).all()
        skills = db.query(m.SkillMeta).all()

    day_list = [(start + timedelta(days=i)).date().isoformat() for i in range((end.date() - start.date()).days + 1)]
    daily: dict[str, dict[str, Any]] = {
        d: {
            "day": d,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "reasoning_tokens": 0,
            "estimated_cost": 0.0,
            "actual_cost": 0.0,
            "sessions": 0,
            "api_calls": 0,
        }
        for d in day_list
    }
    by_model: dict[str, dict[str, Any]] = {}
    session_has_activity: set[str] = set()

    for msg in messages:
        key = _day_key(msg.created_at)
        bucket = daily.get(key)
        if bucket is None:
            continue
        sid = str(msg.session_id)
        session_has_activity.add(sid)
        tokens = msg.token_estimate or 0
        if msg.role == "assistant":
            bucket["output_tokens"] += tokens
            bucket["api_calls"] += 1
            model = _model_of(msg)
            mb = by_model.setdefault(model, {
                "model": model,
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost": 0.0,
                "sessions": 0,
                "api_calls": 0,
            })
            mb["output_tokens"] += tokens
            mb["api_calls"] += 1
        elif msg.role == "user":
            bucket["input_tokens"] += tokens
            mb = by_model.setdefault("(user prompts)", {
                "model": "(user prompts)",
                "input_tokens": 0,
                "output_tokens": 0,
                "estimated_cost": 0.0,
                "sessions": 0,
                "api_calls": 0,
            })
            mb["input_tokens"] += tokens

    for session in sessions:
        key = _day_key(session.created_at)
        bucket = daily.get(key)
        if bucket is None:
            continue
        bucket["sessions"] += 1

    # Model session counts: distinct sessions per model via assistant-message model
    model_sessions: dict[str, set[str]] = {}
    for msg in messages:
        if msg.role != "assistant":
            continue
        model = _model_of(msg)
        model_sessions.setdefault(model, set()).add(str(msg.session_id))
    for model, sids in model_sessions.items():
        mb = by_model.setdefault(model, {
            "model": model,
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost": 0.0,
            "sessions": 0,
            "api_calls": 0,
        })
        mb["sessions"] = len(sids)

    # Per-day session counts via activity (messages that day) merged with created
    for sid in session_has_activity:
        pass  # already counted by created_at; keep daily sessions = sessions created that day

    for bucket in daily.values():
        bucket["estimated_cost"] = _estimate_cost(bucket["input_tokens"], bucket["output_tokens"])
    for mb in by_model.values():
        mb["estimated_cost"] = _estimate_cost(mb["input_tokens"], mb["output_tokens"])

    totals = {
        "total_input": sum(b["input_tokens"] for b in daily.values()),
        "total_output": sum(b["output_tokens"] for b in daily.values()),
        "total_cache_read": 0,
        "total_reasoning": 0,
        "total_estimated_cost": round(sum(b["estimated_cost"] for b in daily.values()), 6),
        "total_actual_cost": 0.0,
        "total_sessions": len(sessions),
        "total_api_calls": sum(b["api_calls"] for b in daily.values()),
    }

    total_use = sum(s.use_count or 0 for s in skills)
    top_skills = []
    for s in sorted(skills, key=lambda x: x.use_count or 0, reverse=True):
        use = s.use_count or 0
        if not use:
            continue
        top_skills.append({
            "skill": s.skill_name,
            "view_count": use,
            "manage_count": 0,
            "total_count": use,
            "percentage": round(use * 100.0 / total_use, 2) if total_use else 0.0,
            "last_used_at": int(s.updated_at.timestamp()) if s.updated_at else None,
        })

    return {
        "daily": [daily[d] for d in day_list],
        "by_model": list(by_model.values()),
        "totals": totals,
        "skills": {
            "summary": {
                "total_skill_loads": sum(s.use_count or 0 for s in skills),
                "total_skill_edits": 0,
                "total_skill_actions": sum(s.use_count or 0 for s in skills),
                "distinct_skills_used": sum(1 for s in skills if (s.use_count or 0) > 0),
            },
            "top_skills": top_skills,
        },
    }


@router.get("/analytics/models")
async def analytics_models(
    days: int = Query(default=30, ge=1, le=365),
    profile: str | None = Query(default=None),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    start, end = _day_range(days)
    with session_scope() as db:
        messages = db.query(m.Message).filter(m.Message.created_at >= start).all()
        sessions = db.query(m.Session).filter(m.Session.created_at >= start).all()
        tool_calls = db.query(m.ToolCall).filter(m.ToolCall.started_at >= start).all()
        provider_models = db.query(m.ProviderModel).all()

    capabilities: dict[str, dict[str, Any]] = {}
    for pm in provider_models:
        caps = {
            "supports_tools": True,
            "supports_vision": False,
            "supports_reasoning": pm.provider_class in ("premium_reasoning",) if hasattr(pm, "provider_class") else False,
            "context_window": pm.context_window,
            "max_output_tokens": None,
            "model_family": getattr(pm, "provider_class", None),
        }
        capabilities.setdefault(pm.model_id, caps)

    agg: dict[str, dict[str, Any]] = {}
    model_sessions: dict[str, set[str]] = {}
    for msg in messages:
        model = _model_of(msg)
        tokens = msg.token_estimate or 0
        entry = agg.setdefault(model, {
            "model": model,
            "provider": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "reasoning_tokens": 0,
            "estimated_cost": 0.0,
            "actual_cost": 0.0,
            "sessions": 0,
            "api_calls": 0,
            "tool_calls": 0,
            "last_used_at": 0,
            "avg_tokens_per_session": 0.0,
            "capabilities": capabilities.get(model, {"supports_tools": True}),
        })
        if msg.role == "assistant":
            entry["output_tokens"] += tokens
            entry["api_calls"] += 1
            model_sessions.setdefault(model, set()).add(str(msg.session_id))
        elif msg.role == "user":
            entry["input_tokens"] += tokens
        if msg.created_at:
            ts = int(msg.created_at.timestamp())
            if ts > entry["last_used_at"]:
                entry["last_used_at"] = ts

    # Session-count and tool-call attribution per model via dominant model per session
    session_model: dict[str, str] = {}
    assistant_by_session: dict[str, list[str]] = {}
    for msg in messages:
        if msg.role != "assistant":
            continue
        sid = str(msg.session_id)
        assistant_by_session.setdefault(sid, []).append(_model_of(msg))
    for sid, models in assistant_by_session.items():
        if models:
            session_model[sid] = max(set(models), key=models.count)
    for sid, model in session_model.items():
        entry = agg.setdefault(model, {
            "model": model,
            "provider": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "reasoning_tokens": 0,
            "estimated_cost": 0.0,
            "actual_cost": 0.0,
            "sessions": 0,
            "api_calls": 0,
            "tool_calls": 0,
            "last_used_at": 0,
            "avg_tokens_per_session": 0.0,
            "capabilities": capabilities.get(model, {"supports_tools": True}),
        })
        entry["sessions"] += 1

    tool_model = session_model  # attribute tool calls to the session's dominant model
    for tc in tool_calls:
        model = tool_model.get(str(tc.session_id), "unknown")
        entry = agg.setdefault(model, {
            "model": model,
            "provider": None,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "reasoning_tokens": 0,
            "estimated_cost": 0.0,
            "actual_cost": 0.0,
            "sessions": 0,
            "api_calls": 0,
            "tool_calls": 0,
            "last_used_at": 0,
            "avg_tokens_per_session": 0.0,
            "capabilities": capabilities.get(model, {"supports_tools": True}),
        })
        entry["tool_calls"] += 1

    for model, entry in agg.items():
        entry["estimated_cost"] = _estimate_cost(entry["input_tokens"], entry["output_tokens"])
        if entry["sessions"]:
            entry["avg_tokens_per_session"] = round(
                (entry["input_tokens"] + entry["output_tokens"]) / entry["sessions"], 1
            )

    models = sorted(agg.values(), key=lambda e: e["input_tokens"] + e["output_tokens"], reverse=True)
    totals = {
        "distinct_models": len(models),
        "total_input": sum(e["input_tokens"] for e in models),
        "total_output": sum(e["output_tokens"] for e in models),
        "total_cache_read": 0,
        "total_reasoning": 0,
        "total_estimated_cost": round(sum(e["estimated_cost"] for e in models), 6),
        "total_actual_cost": 0.0,
        "total_sessions": len(sessions),
        "total_api_calls": sum(e["api_calls"] for e in models),
    }
    return {"models": models, "totals": totals, "period_days": days}

"""J2: локальный фильтр, пакетная оценка (одним вызовом Flash) и флаги обмана."""
from __future__ import annotations

import json
import logging
import math
import re

from sqlalchemy.orm import Session as OrmSession

from aria.config import get_settings
from aria.db import models as m
from aria.jobs import scamcheck, store
from aria.llm.providers.base import ChatMessage

logger = logging.getLogger("local_agent.jobs.screening")

TOKEN_COST_EST_USD = 0.0005  # верхняя оценка Flash на заказ (COST_MODEL §3)
BATCH_SIZE = 20

_SYSTEM = (
    "Ты отбираешь фриланс-заказы. Верни ТОЛЬКО JSON-массив объектов "
    '{"id": "s1", "fit": 0-10, "effort_hours": число, "can_agent_do_it": true|false, '
    '"red_flags": ["короткая причина"]}. Без текста вокруг JSON.'
)


def local_filter(lead, profile: dict | None = None) -> tuple[bool, str]:
    data = profile if profile is not None else get_settings().jobs_profile_dict
    text = store.normalize_text(f"{lead.title or ''} {lead.description or ''}")
    for phrase in data.get("blacklist") or []:
        if phrase and store.normalize_text(str(phrase)) in text:
            return False, f"blacklist: {phrase}"
    skills = [store.normalize_text(str(s)) for s in (data.get("skills") or []) if s]
    if skills and not any(skill in text for skill in skills):
        return False, "skills mismatch"
    languages = [str(lang).lower() for lang in (data.get("languages") or []) if lang]
    if languages and lead.language and str(lead.language).lower() not in languages:
        return False, "language mismatch"
    minimum = data.get("min_budget_usd")
    if minimum is not None and lead.budget_usd_est is not None and float(lead.budget_usd_est) < float(minimum):
        return False, "budget below minimum"
    return True, ""


def _parse_batch(text: str) -> dict[str, dict] | None:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.IGNORECASE).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    parsed: dict[str, dict] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        index = str(item.get("id") or "")
        try:
            fit = int(item.get("fit"))
            effort = float(item.get("effort_hours"))
        except (TypeError, ValueError):
            continue
        red = item.get("red_flags")
        if not index or not 0 <= fit <= 10 or not math.isfinite(effort) or effort < 0:
            continue
        if red is not None and not isinstance(red, list):
            continue
        flags = [str(flag) for flag in (red or []) if flag]
        parsed[index] = {"fit": fit, "effort_hours": effort, "red_flags": flags}
    return parsed or None


async def _score(router, batch: list[m.Lead]) -> dict[str, dict] | None:
    payload = [
        {
            "id": f"s{index}",
            "title": (lead.title or "")[:200],
            "description": (lead.description or "")[:1500],
            "budget": lead.budget_usd_est,
        }
        for index, lead in enumerate(batch, start=1)
    ]
    messages = [
        ChatMessage(role="system", content=_SYSTEM),
        ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
    ]
    for attempt in range(2):
        result = await router.route_chat(
            "free_tier_reasoning", messages, tools=[],
            allow_degrade=True, resilient=True,
            fallback_classes=("standard_reasoning",), purpose="other",
        )
        parsed = _parse_batch(result.response.text)
        if parsed is not None:
            return parsed
        logger.warning("screening: invalid JSON from model (attempt %s)", attempt + 1)
    return None


async def screen_batch(
    db: OrmSession, leads: list[m.Lead], router, profile: dict | None = None, limit: int = BATCH_SIZE,
) -> list[dict]:
    profile = profile if profile is not None else get_settings().jobs_profile_dict
    eligible: list[m.Lead] = []
    for lead in leads:
        ok, reason = local_filter(lead, profile)
        if not ok:
            lead.status = "skipped"
            store.add_event(db, lead, "screened_out", {"reason": reason})
            continue
        eligible.append(lead)
        if len(eligible) >= limit:
            break
    if not eligible:
        return []
    scores = await _score(router, eligible)
    if scores is None:
        return []
    results: list[dict] = []
    for index, lead in enumerate(eligible, start=1):
        item = scores.get(f"s{index}")
        if item is None:
            continue
        text_flags = scamcheck.scan_text(f"{lead.title or ''}\n{lead.description or ''}")
        flags = list(item["red_flags"]) + [f["reason"] for f in text_flags]
        lead.score = item["fit"]
        lead.effort_hours_est = item["effort_hours"]
        lead.token_cost_est_usd = TOKEN_COST_EST_USD
        lead.scam_flags = flags
        lead.status = "suspect" if flags else "screened"
        store.add_event(db, lead, "scored", {"score": lead.score, "flags": flags})
        profit = (
            round(float(lead.budget_usd_est) - TOKEN_COST_EST_USD, 6)
            if lead.budget_usd_est is not None else None
        )
        results.append({
            "lead_id": str(lead.id),
            "score": lead.score,
            "effort_hours": lead.effort_hours_est,
            "flags": flags,
            "token_cost_est_usd": TOKEN_COST_EST_USD,
            "expected_profit_usd": profit,
        })
    return results

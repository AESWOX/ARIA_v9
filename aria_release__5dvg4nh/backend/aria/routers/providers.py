from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from aria.api.auth import token_store
from aria.db import repository as repo
from aria.db.base import session_scope

router = APIRouter(tags=["providers"])

# provider_class (router.py) -> UI tier vocabulary (types.ts ProviderModelDescriptor.tier)
PROVIDER_CLASS_TO_TIER = {
    "free_tier_reasoning": "fast",
    "standard_reasoning": "balanced",
    "premium_reasoning": "reasoning",
}

TIER_HINTS = {
    "flagship": "Maximum quality and strongest multimodal capability",
    "fast": "Lower latency and lower cost for routine tasks",
    "reasoning": "Best suited for complex reasoning and longer analytical chains",
    "balanced": "Balanced quality, speed, and cost",
    "local": "Runs locally when available and can work as an offline-friendly fallback",
}


async def require_runtime_token(x_local_agent_token: str | None = Header(default=None)) -> str:
    if not token_store.verify_token(x_local_agent_token):
        raise HTTPException(status_code=401, detail="invalid runtime token")
    return x_local_agent_token or ""


@router.get("/providers/models")
async def get_provider_models(request: Request, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    llm_router = request.app.state.router  # без циклического импорта aria.main

    provider_class_by_id: dict[str, str] = {}
    for provider_class, providers in llm_router.providers_by_class.items():
        for p in providers:
            provider_class_by_id.setdefault(p.provider_id, provider_class)

    with session_scope() as db:
        rows = repo.list_provider_models(db)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        tier = PROVIDER_CLASS_TO_TIER.get(provider_class_by_id.get(row.provider_id, ""), "balanced")
        grouped.setdefault(row.provider_id, []).append(
            {"id": row.model_id, "name": row.model_id, "tier": tier}
        )

    providers_payload = [
        {"id": pid, "name": pid, "models": models}
        for pid, models in grouped.items()
    ]

    return {"providers": providers_payload, "provider_tier_hints": TIER_HINTS}

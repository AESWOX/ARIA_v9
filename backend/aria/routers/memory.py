"""Долговременная память (H3): /memory/* поверх :mod:`aria.memory.store` (SQLite FTS5).

Формы ответов ``GET /memory``, ``PUT /memory/provider``, ``POST /memory/reset`` совместимы со
страницей System (``builtin_files.memory`` = эпизоды + факты, ``builtin_files.user`` = предпочтения).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from aria.api.auth import require_runtime_token
from aria.memory import store

router = APIRouter(tags=["memory"])

# Цели сброса на странице System → слои хранилища.
_RESET_TARGETS: dict[str, tuple[str, ...]] = {
    "all": ("all",),
    "memory": ("episode", "fact"),
    "user": ("preference",),
    "episode": ("episode",),
    "fact": ("fact",),
    "preference": ("preference",),
}


@router.get("/memory")
async def memory_status(profile: str | None = Query(None), _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    counts = store.stats(profile)
    return {
        "active": store.get_provider(),
        "providers": [
            {"name": "local", "description": "Local memory (SQLite FTS5, works offline)", "configured": True},
            {"name": "none", "description": "Memory off: nothing is saved or recalled", "configured": True},
        ],
        "builtin_files": {"memory": counts["episode"] + counts["fact"], "user": counts["preference"]},
        "counts": counts,
        "fts5": store.fts_available(),
        "layers": list(store.LAYERS),
    }


@router.put("/memory/provider")
async def memory_set_provider(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    provider = str(payload.get("provider", ""))
    if provider not in store.PROVIDERS:
        raise HTTPException(status_code=400, detail="unknown memory provider")
    return {"ok": True, "active": store.set_provider(provider)}


@router.get("/memory/search")
async def memory_search(
    q: str = Query("", max_length=500),
    layer: str | None = Query(None),
    profile: str | None = Query(None),
    limit: int = Query(5, ge=1, le=50),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    try:
        items = store.search(q, layer=layer, profile=profile, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"query": q, "items": items}


@router.get("/memory/items")
async def memory_items(
    layer: str | None = Query(None),
    profile: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    try:
        return {"items": store.list_items(layer=layer, profile=profile, limit=limit, offset=offset)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/memory/items")
async def memory_add(
    payload: dict[str, Any],
    profile: str | None = Query(None),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    try:
        item = store.add(
            str(payload.get("layer") or "fact"),
            str(payload.get("content") or ""),
            profile=profile or payload.get("profile"),
            source="user",
        )
    except store.MemoryDisabled as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "item": item}


@router.delete("/memory/items/{item_id}")
async def memory_delete(item_id: str, profile: str | None = Query(None), _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    if not store.delete(item_id, profile=profile):
        raise HTTPException(status_code=404, detail="memory item not found")
    return {"ok": True}


@router.post("/memory/reset")
async def memory_reset(
    payload: dict[str, Any] | None = None,
    profile: str | None = Query(None),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    body = payload or {}
    target = str(body.get("target") or "all")
    layers = _RESET_TARGETS.get(target)
    if layers is None:
        raise HTTPException(status_code=400, detail=f"target must be one of {sorted(_RESET_TARGETS)}")
    older = body.get("older_than_days")
    try:
        older_days = float(older) if older is not None else None
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="older_than_days must be a number") from exc
    deleted = sum(store.reset(layer, profile=profile, older_than_days=older_days) for layer in layers)
    return {"ok": True, "deleted": list(layers), "count": deleted}

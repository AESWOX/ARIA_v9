"""Хуки и чекпоинты (H4): ``/ops/hooks*`` и ``/ops/checkpoints*`` поверх :mod:`aria.hooks` и :mod:`aria.checkpoints`.

Формы ответов ``GET /ops/hooks`` и ``GET /ops/checkpoints`` совместимы с ``HooksResponse`` /
``CheckpointsResponse`` из api.ts (``checkpoints`` — дополнительное поле со списком).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from aria.api.auth import require_runtime_token
from aria.checkpoints import store as checkpoints
from aria.hooks import EVENTS, load_hooks, save_hooks
from aria.hooks.engine import MAX_TIMEOUT_SEC

router = APIRouter(tags=["ops-hooks"])


@router.get("/ops/hooks")
async def ops_hooks(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"hooks": load_hooks(), "valid_events": list(EVENTS)}


@router.post("/ops/hooks")
async def ops_hooks_create(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    event = str(payload.get("event", "")).strip()
    command = str(payload.get("command", "")).strip()
    if not event or not command:
        raise HTTPException(status_code=400, detail="event and command required")
    if event not in EVENTS:
        raise HTTPException(status_code=400, detail=f"unknown event; valid: {', '.join(EVENTS)}")
    matcher = payload.get("matcher") or None
    if matcher is not None:
        try:
            re.compile(str(matcher))
        except re.error:
            raise HTTPException(status_code=400, detail="matcher is not a valid regular expression")
    timeout = payload.get("timeout")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= MAX_TIMEOUT_SEC:
            raise HTTPException(status_code=400, detail=f"timeout must be an integer 1..{MAX_TIMEOUT_SEC}")
    hooks = load_hooks()
    if any(h.get("event") == event and h.get("command") == command for h in hooks):
        raise HTTPException(status_code=409, detail="hook already exists")
    approved = bool(payload.get("approve", False))
    hooks.append({
        "event": event,
        "matcher": matcher,
        "command": command,
        "timeout": timeout,
        "allowed": approved,
        "approved_at": datetime.now(timezone.utc).isoformat() if approved else None,
        "executable": approved,
    })
    save_hooks(hooks)
    return {"ok": True, "event": event, "command": command, "approved": approved}


@router.delete("/ops/hooks")
async def ops_hooks_delete(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    event, command = payload.get("event"), payload.get("command")
    hooks = load_hooks()
    remaining = [h for h in hooks if not (h.get("event") == event and h.get("command") == command)]
    save_hooks(remaining)
    return {"ok": True, "removed": len(hooks) - len(remaining)}


@router.get("/ops/checkpoints")
async def ops_checkpoints(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return checkpoints.summary()


@router.post("/ops/checkpoints/prune")
async def ops_checkpoints_prune(
    payload: dict[str, Any] | None = Body(None), _: str = Depends(require_runtime_token)
) -> dict[str, Any]:
    payload = payload or {}
    days, max_bytes = payload.get("older_than_days"), payload.get("max_bytes")
    for name, value in (("older_than_days", days), ("max_bytes", max_bytes)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0):
            raise HTTPException(status_code=400, detail=f"{name} must be a non-negative number")
    result = checkpoints.prune(older_than_days=days, max_bytes=int(max_bytes) if max_bytes is not None else None)
    return {
        "name": "ops-checkpoints-prune", "ok": True, "pid": None,
        "message": f"removed {result['removed_count']} checkpoint(s), freed {result['freed_bytes']} bytes",
        **result,
    }


@router.post("/ops/checkpoints/restore-task/{task_id}")
async def ops_checkpoints_restore_task(task_id: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    results = checkpoints.restore_task(task_id)
    if not results:
        raise HTTPException(status_code=404, detail="no checkpoints for this task")
    return {"ok": True, "task_id": task_id, "restored": results}


@router.post("/ops/checkpoints/{cp_id}/restore")
async def ops_checkpoints_restore(cp_id: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    try:
        result = checkpoints.restore(cp_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="checkpoint not found")
    return {"ok": True, **result}

"""Action status routes: GET /api/actions/{name}/status.

Long-running backend actions (e.g. ``aria update``, hub skill sync) report
their outcome into an in-memory registry; the UI polls this endpoint until
``running`` is false. In-memory only — results do not survive a restart.
"""
from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, Depends, Query

from aria.api.auth import require_runtime_token

router = APIRouter(tags=["actions"])

_lock = threading.Lock()
# name -> {exit_code, lines, running, pid}
_RESULTS: dict[str, dict[str, Any]] = {}


def record(name: str, exit_code: int = 0, lines: list[str] | None = None,
           running: bool = False, pid: int | None = None) -> None:
    with _lock:
        _RESULTS[name] = {
            "exit_code": exit_code,
            "lines": lines or [],
            "running": running,
            "pid": pid,
        }


@router.get("/actions/{name}/status")
async def action_status(
    name: str,
    lines: int = Query(default=200),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    with _lock:
        entry = _RESULTS.get(name)
    if entry is None:
        return {"exit_code": None, "lines": [], "name": name, "pid": None, "running": False}
    return {
        "exit_code": entry.get("exit_code"),
        "lines": entry.get("lines", [])[:max(0, lines)],
        "name": name,
        "pid": entry.get("pid"),
        "running": bool(entry.get("running")),
    }

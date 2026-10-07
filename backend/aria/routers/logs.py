"""Log tailing routes: /api/logs.

Serves the tail of the backend's file logs. Logical log names map to files
under ``backend/logs`` (where ``aria/main.py`` writes with a
``RotatingFileHandler``): ``agent`` -> backend.log, ``gateway`` -> gateway.log.
Unknown logical names fall back to backend.log. Lines are filtered by level
(cascading: ERROR < WARNING < INFO < DEBUG) and by component keyword.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from aria import paths as _paths
from aria.api.auth import require_runtime_token

router = APIRouter(tags=["logs"])

_LOG_DIR = _paths.log_dir()

_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40}


def _classify_line(line: str) -> str:
    upper = line.upper()
    if "ERROR" in upper or "CRITICAL" in upper or "FATAL" in upper:
        return "ERROR"
    if "WARNING" in upper or "WARN" in upper:
        return "WARNING"
    if "DEBUG" in upper:
        return "DEBUG"
    return "INFO"


def _resolve_file(name: str) -> Path:
    if not name:
        name = "backend.log"
    safe = Path(name).name  # strip any directory components
    candidates: list[Path] = []
    if safe.lower() in ("backend.log", "agent", "errors"):
        candidates.append(_LOG_DIR / "backend.log")
    else:
        candidates.append(_LOG_DIR / safe)
    candidates.append(_LOG_DIR / "backend.log")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return _LOG_DIR / "backend.log"


def _tail(path: Path, lines: int) -> list[str]:
    chunk = 8192
    read = b""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    pos = max(0, size - chunk)
    with open(path, "rb") as fh:
        while pos > 0:
            fh.seek(pos)
            read = fh.read() + read
            if read.count(b"\n") >= lines * 2:
                break
            pos = max(0, pos - chunk)
            if pos == 0:
                break
        if pos == 0:
            fh.seek(0)
            read = fh.read() + read
    text = read.decode("utf-8", errors="replace")
    parts = text.splitlines()
    return parts[-lines:] if len(parts) > lines else parts


@router.get("/logs")
async def get_logs(
    file: str = Query(default="agent"),
    lines: int = Query(default=100, ge=1, le=2000),
    level: str = Query(default="ALL"),
    component: str = Query(default="all"),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    path = _resolve_file(file)
    raw = _tail(path, max(1, min(lines, 2000)))
    min_rank = _LEVEL_ORDER.get(level.upper(), 0) if level and level.upper() != "ALL" else 0
    comp = component.strip().lower()
    result: list[str] = []
    for line in raw:
        if min_rank and _LEVEL_ORDER.get(_classify_line(line), 0) < min_rank:
            continue
        if comp and comp != "all" and comp not in line.lower():
            continue
        result.append(line)
    return {"file": path.name, "lines": result[-lines:] if len(result) > lines else result}

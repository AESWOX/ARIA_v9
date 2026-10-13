"""core/delegate.py — §7.2/§7.3.

Один основной event loop (принцип §4.2): саб-агент — не отдельный процесс,
а рекурсивный вызов той же loop-функции с урезанным role/tool_whitelist и
запретом повышать бюджетный класс модели (§7.2).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aria.config import get_settings
from aria.core.roles import get_role
from aria.llm.router import ProviderRouter

# Значения по умолчанию (§7.2). Реальные лимиты читаются из Settings (.env) через max_depth()/max_parallel().
MAX_DEPTH = 1
MAX_PARALLEL = 5
DEFAULT_TIMEOUT_SEC = 60
RETRY_COUNT = 1


def max_depth() -> int:
    """Максимальная глубина делегирования: settings.delegate_max_depth (D2)."""
    return int(get_settings().delegate_max_depth)


def max_parallel() -> int:
    """Максимум параллельных саб-агентов: settings.delegate_max_parallel (D2)."""
    return int(get_settings().delegate_max_parallel)


@dataclass
class DelegateRequest:
    session_id: str
    parent_task_id: str
    role: str
    objective: str
    allowed_tools: list[str]
    allow_paid: bool = False
    hard_timeout_sec: int = DEFAULT_TIMEOUT_SEC
    depth: int = 0


@dataclass
class DelegateResult:
    status: str  # ok|failed|timeout|cancelled|exhausted_free_tier
    role: str
    summary: str
    artifacts: list[str]
    tool_calls_count: int
    error: str | None = None


async def delegate_task(request: DelegateRequest, run_subtask_fn) -> DelegateResult:
    """run_subtask_fn — callable(role, objective, allowed_tools, allow_paid) -> DelegateResult,
    внедряется вызывающим кодом (core/loop.py), чтобы избежать циклического импорта
    и явно провести depth/budget инварианты."""
    limit = max_depth()
    if request.depth >= limit:
        return DelegateResult(status="failed", role=request.role, summary="", artifacts=[], tool_calls_count=0, error=f"max delegation depth ({limit}) exceeded, §7.2")

    try:
        get_role(request.role)
    except (KeyError, PermissionError) as exc:  # D4: get_role бросает, а не возвращает None
        return DelegateResult(status="failed", role=request.role, summary="", artifacts=[], tool_calls_count=0, error=f"unknown or disabled role: {request.role} ({exc})")

    last_error: str | None = None
    for attempt in range(RETRY_COUNT + 1):
        try:
            coro = run_subtask_fn(request.role, request.objective, request.allowed_tools, request.allow_paid)
            result: DelegateResult = await asyncio.wait_for(coro, timeout=request.hard_timeout_sec)
            return result
        except asyncio.TimeoutError:
            last_error = "timeout"
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)

    status = "timeout" if last_error == "timeout" else "failed"
    return DelegateResult(status=status, role=request.role, summary="", artifacts=[], tool_calls_count=0, error=last_error)


async def run_parallel(requests: list[DelegateRequest], run_subtask_fn) -> list[DelegateResult]:
    limit = max_parallel()
    if len(requests) > limit:
        raise ValueError(f"§7.2: max {limit} parallel sub-agents per orchestration cycle")
    return await asyncio.gather(*(delegate_task(r, run_subtask_fn) for r in requests))

"""core/timing.py — замер времени по этапам ответа (p50/p95 в памяти, без новой таблицы).

Зачем: по логам нельзя ответить, куда уходит время ответа (ожидание слота, проверка связи, сжатие, вызов
модели, аудит). Каждый этап пишет одну запись (имя, миллисекунды, ok) в кольцевой буфер; ``summary()`` считает
перцентили, эндпоинт ``GET /system/timings`` отдаёт их. Буфер живёт до перезапуска бэкенда и ограничен
``MAX_SAMPLES`` записями, поэтому не растёт и ничего не пишет на диск.
"""
from __future__ import annotations

import functools
import inspect
import logging
import math
import threading
import time
from collections import deque
from contextlib import contextmanager

logger = logging.getLogger("local_agent.timing")

MAX_SAMPLES = 5000
SLOW_STAGE_MS = 2000.0  # дольше — строка INFO в логе: по ней легко найти медленные запросы

_lock = threading.Lock()
_samples: deque[tuple[str, float, bool, float]] = deque(maxlen=MAX_SAMPLES)  # (этап, мс, ok, время записи)


def _wall() -> float:
    return time.time()


def record(stage_name: str, ms: float, ok: bool = True) -> None:
    with _lock:
        _samples.append((stage_name, float(ms), ok, _wall()))
    if ms >= SLOW_STAGE_MS:
        logger.info("slow stage=%s ms=%.0f ok=%s", stage_name, ms, ok)


@contextmanager
def stage(name: str):
    """``with timing.stage("x"):`` — работает и внутри async-кода (не держит блокировку между await)."""
    t0 = time.monotonic()
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        record(name, (time.monotonic() - t0) * 1000.0, ok)


def timed(name: str):
    """Декоратор для sync- и async-функций."""

    def deco(fn):
        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                with stage(name):
                    return await fn(*args, **kwargs)

            return awrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with stage(name):
                return fn(*args, **kwargs)

        return wrapper

    return deco


def percentile(sorted_values: list[float], p: float) -> float | None:
    """Nearest-rank по отсортированному списку; пустой список -> None."""
    if not sorted_values:
        return None
    rank = max(1, math.ceil(p / 100.0 * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]


def summary(window_sec: float | None = None) -> dict[str, dict]:
    cutoff = None if window_sec is None else _wall() - window_sec
    with _lock:
        rows = [s for s in _samples if cutoff is None or s[3] >= cutoff]
    by_stage: dict[str, list[tuple[float, bool]]] = {}
    for name, ms, ok, _ts in rows:
        by_stage.setdefault(name, []).append((ms, ok))
    out: dict[str, dict] = {}
    for name, vals in sorted(by_stage.items()):
        ms_sorted = sorted(v[0] for v in vals)
        out[name] = {
            "count": len(vals),
            "errors": sum(1 for _, ok in vals if not ok),
            "p50_ms": round(percentile(ms_sorted, 50), 1),
            "p95_ms": round(percentile(ms_sorted, 95), 1),
            "max_ms": round(ms_sorted[-1], 1),
        }
    return out


def reset() -> None:
    with _lock:
        _samples.clear()

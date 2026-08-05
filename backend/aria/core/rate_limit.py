from __future__ import annotations

import time


class SimpleRateLimiter:
    def __init__(self, min_interval_sec: float):
        self.min_interval_sec = min_interval_sec
        self._last_call: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        last = self._last_call.get(key, 0)
        if now - last < self.min_interval_sec:
            return False
        self._last_call[key] = now
        return True


shutdown_limiter = SimpleRateLimiter(min_interval_sec=10)
feedback_limiter = SimpleRateLimiter(min_interval_sec=2)

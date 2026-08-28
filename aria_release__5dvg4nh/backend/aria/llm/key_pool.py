"""app/llm/key_pool.py

Простая ротация API-ключей round-robin (1 -> 2 -> 3 -> ... -> N -> 1 -> ...).

Два разных исхода ошибки:
  - 429 (rate limit)      -> ключ уходит в cooldown на `cooldown_sec`,
                              пробуем следующий по кругу.
  - 401/403 (auth/invalid)-> ключ помечается dead навсегда (в рамках
                              текущего процесса), больше не выбирается.

Один KeyPool = один независимый курсор ротации. Компрессия и основной
ответ агента используют РАЗНЫЕ инстансы KeyPool (см. router.py и
compression.py), поэтому они не делят состояние друг с другом даже если
физически указывают на один и тот же список ключей.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time

logger = logging.getLogger("local_agent.key_pool")


class NoAvailableKeys(Exception):
    pass


class KeyPool:
    def __init__(self, keys: list[str], name: str = "key_pool", cooldown_sec: float = 60.0):
        if not keys:
            raise ValueError(f"KeyPool '{name}' created with an empty key list")
        self.name = name
        self.cooldown_sec = cooldown_sec
        self._keys = list(keys)
        self._dead: set[str] = set()
        self._cooldown_until: dict[str, float] = {}
        self._cycle = itertools.cycle(range(len(self._keys)))
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._keys)

    def _is_usable(self, key: str) -> bool:
        if key in self._dead:
            return False
        until = self._cooldown_until.get(key)
        if until and until > time.monotonic():
            return False
        return True

    def next_key(self) -> str:
        """Возвращает следующий доступный ключ по кругу. Бросает
        NoAvailableKeys если все ключи мертвы или в cooldown."""
        with self._lock:
            for _ in range(len(self._keys)):
                idx = next(self._cycle)
                key = self._keys[idx]
                if self._is_usable(key):
                    return key
            raise NoAvailableKeys(f"pool '{self.name}': all {len(self._keys)} keys dead/cooling down")

    def mark_rate_limited(self, key: str) -> None:
        with self._lock:
            self._cooldown_until[key] = time.monotonic() + self.cooldown_sec
        logger.warning("pool '%s': key ...%s rate-limited, cooldown %.0fs", self.name, key[-4:], self.cooldown_sec)

    def mark_dead(self, key: str) -> None:
        with self._lock:
            self._dead.add(key)
        logger.error("pool '%s': key ...%s marked dead (auth error)", self.name, key[-4:])

    def status(self) -> dict:
        with self._lock:
            now = time.monotonic()
            return {
                "name": self.name,
                "total": len(self._keys),
                "dead": len(self._dead),
                "cooling_down": sum(1 for until in self._cooldown_until.values() if until > now),
            }

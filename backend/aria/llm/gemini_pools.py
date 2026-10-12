"""llm/gemini_pools.py — общий реестр пулов ключей Gemini: один пул на (набор ключей, модель).

Квота Gemini free tier считается на проект И МОДЕЛЬ (0020). Поэтому состояние cooldown должно быть
одно на пару (ключи, модель) для всех, кто ходит в эту модель этими ключами: чат, агент, запасной
класс, vision и сжатие контекста. Раньше у каждого был свой пул, и исчерпанная квота перебиралась
заново каждым из них. Разные модели получают РАЗНЫЕ пулы: исчерпанная модель A не блокирует модель B.

Реестр живёт в процессе. `reset()` вызывается при сборке роутера (старт и смена .env): состояние
cooldown начинается с нуля, как и у самих провайдеров.
"""
from __future__ import annotations

import threading

from aria.llm.key_pool import KeyPool

_lock = threading.Lock()
_pools: dict[tuple[tuple[str, ...], str], KeyPool] = {}


def pool_for(keys: list[str], model: str) -> KeyPool:
    ident = (tuple(keys), model)
    with _lock:
        pool = _pools.get(ident)
        if pool is None:
            pool = _pools[ident] = KeyPool(keys, name=f"gemini-{model}")
        return pool


def reset() -> None:
    with _lock:
        _pools.clear()


def snapshot() -> list[dict]:
    """Состояние всех пулов (для диагностики): имя, ключей, мёртвых, в cooldown, секунд до первого свободного."""
    with _lock:
        pools = list(_pools.values())
    return [p.status() for p in pools]

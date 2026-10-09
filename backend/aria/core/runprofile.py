"""core/runprofile.py — профиль запуска: кто работает (стиль), на какой модели, как проверяется результат.

Профиль хранится на сессию (``agent_state``, ключ ``run_profile:<session_id>``); последний сохранённый
становится значением по умолчанию для новых сессий (``run_profile:default``). Нет профиля — прежнее
поведение (модель по роли, аудит как раньше), поэтому существующие задачи не меняются.

Места («seats»):
  * ``main``    — основная модель: в стиле «solo» делает всё сама, в стиле «boss» это Босс
                  (роль orchestrator): планирует и раздаёт подзадачи через ``delegate_task``;
  * ``workers`` — исполнители подзадач босса (задачи с ``delegation_depth > 0``);
  * ``auditor`` — модель, проверяющая результат в конце цикла.

У места: ``tier`` (класс premium/standard/free/fast) и/или ``model`` (конкретный provider_id,
перекрывает tier). Недоступная конкретная модель не ломает задачу: роутер идёт цепочкой класса.

Правило §7.2: исполнители не дороже босса — проверяется при сохранении и при исполнении.
Стиль «рой» (H5) не входит: он появится вместе с H5 (решение «ни одной заглушки»).
"""
from __future__ import annotations

import logging
from typing import Any

from aria.db import repository as repo

logger = logging.getLogger("local_agent.runprofile")

TIERS: dict[str, str] = {
    "premium": "premium_reasoning",
    "standard": "standard_reasoning",
    "free": "free_tier_reasoning",
    "fast": "subagent_execution",
}
STYLES = ("solo", "boss")
AUDIT_LEVELS = ("off", "light", "strict")
THINKING_LEVELS = ("off", "medium", "high")
SEATS = ("main", "workers", "auditor")
DEFAULT_KEY = "run_profile:default"
_CLASS_RANK = {"premium_reasoning": 2, "standard_reasoning": 1}  # остальные классы — 0
_THINKING_HINTS = {
    "medium": "Перед ответом коротко продумай решение по шагам и проверь его.",
    "high": "Перед ответом тщательно продумай задачу по шагам, рассмотри альтернативы и проверь результат; не торопись.",
}


class ProfileError(ValueError):
    """Профиль не прошёл проверку (HTTP 400)."""


def builtin_profile() -> dict:
    return {"style": "solo", "audit": "strict", "thinking": "off", "main": {}, "workers": {}, "auditor": {}}


def class_rank(provider_class: str | None) -> int:
    return _CLASS_RANK.get(provider_class or "", 0)


def provider_class_of(router: Any, provider_id: str) -> str | None:
    for cls, providers in getattr(router, "providers_by_class", {}).items():
        if any(p.provider_id == provider_id for p in providers):
            return cls
    return None


def _seat(raw: Any, name: str, router: Any) -> dict:
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        raise ProfileError(f"'{name}' must be an object")
    out: dict[str, str] = {}
    tier = raw.get("tier")
    if tier not in (None, ""):
        if tier not in TIERS:
            raise ProfileError(f"'{name}.tier' must be one of {sorted(TIERS)}")
        out["tier"] = str(tier)
    model = raw.get("model")
    if model not in (None, ""):
        if not isinstance(model, str):
            raise ProfileError(f"'{name}.model' must be a string")
        if router is not None and provider_class_of(router, model) is None:
            raise ProfileError(f"'{name}.model' '{model}' is not an available model")
        out["model"] = model
    return out


def _seat_rank(seat: dict, router: Any) -> int | None:
    if seat.get("model") and router is not None:
        return class_rank(provider_class_of(router, seat["model"]))
    if seat.get("tier"):
        return class_rank(TIERS[seat["tier"]])
    return None


def normalize(raw: Any, router: Any = None) -> tuple[dict, list[str]]:
    """Проверить и привести профиль к каноническому виду. Возвращает (профиль, заметки о правках)."""
    if not isinstance(raw, dict):
        raise ProfileError("profile must be an object")
    style = raw.get("style", "solo")
    if style not in STYLES:
        raise ProfileError(f"'style' must be one of {list(STYLES)}")
    audit = raw.get("audit", "strict")
    if audit not in AUDIT_LEVELS:
        raise ProfileError(f"'audit' must be one of {list(AUDIT_LEVELS)}")
    thinking = raw.get("thinking", "off")
    if thinking not in THINKING_LEVELS:
        raise ProfileError(f"'thinking' must be one of {list(THINKING_LEVELS)}")
    profile: dict[str, Any] = {"style": style, "audit": audit, "thinking": thinking}
    for name in SEATS:
        profile[name] = _seat(raw.get(name), name, router)
    notes: list[str] = []
    main_rank, workers_rank = _seat_rank(profile["main"], router), _seat_rank(profile["workers"], router)
    if main_rank is not None and workers_rank is not None and workers_rank > main_rank:
        profile["workers"] = dict(profile["main"])
        notes.append("workers cannot use a more expensive model than the main one (§7.2): they use the main model")
    return profile, notes


# ── хранение ─────────────────────────────────────────────────────────────

def get_profile(db: Any, session_id: Any) -> dict | None:
    """Профиль сессии, иначе последний сохранённый по умолчанию, иначе None (прежнее поведение)."""
    for key in (f"run_profile:{session_id}", DEFAULT_KEY):
        value = repo.get_agent_state(db, key)
        if isinstance(value, dict) and value:
            return value
    return None


def save_profile(db: Any, session_id: Any, profile: dict) -> None:
    repo.set_agent_state(db, f"run_profile:{session_id}", profile, source="user")
    repo.set_agent_state(db, DEFAULT_KEY, profile, source="user")


# ── разрешение в «класс + желаемая модель» ───────────────────────────────

def _target(seat: dict, default_class: str, router: Any) -> tuple[str, str | None]:
    model = seat.get("model")
    if model:
        return (provider_class_of(router, model) or default_class), model
    if seat.get("tier"):
        return TIERS[seat["tier"]], None
    return default_class, None


def resolve_for_task(profile: dict | None, role_default_class: str, delegation_depth: int, router: Any) -> tuple[str, str | None]:
    """(provider_class, prefer_provider_id) для очередного вызова модели в цикле задачи."""
    if not profile:
        return role_default_class, None
    main = profile.get("main") or {}
    if delegation_depth > 0:
        cls, prefer = _target(profile.get("workers") or {}, role_default_class, router)
        main_rank = _seat_rank(main, router)
        if main_rank is not None:
            worker_rank = class_rank(provider_class_of(router, prefer) if prefer else cls)
            if worker_rank > main_rank:  # §7.2: не дороже босса
                return _target(main, role_default_class, router)
        return cls, prefer
    return _target(main, role_default_class, router)


def audit_settings(profile: dict | None, router: Any) -> tuple[str, str, str | None]:
    """(уровень аудита, класс модели аудитора, желаемая модель)."""
    if not profile:
        return "strict", "standard_reasoning", None
    cls, prefer = _target(profile.get("auditor") or {}, "standard_reasoning", router)
    return str(profile.get("audit") or "strict"), cls, prefer


def chat_target(profile: dict | None, router: Any, default_class: str, default_fallback: tuple[str, ...]) -> tuple[str, tuple[str, ...], str | None]:
    """Для обычного чата: (класс, запасные классы, желаемая модель)."""
    if not profile or not (profile.get("main") or {}):
        return default_class, default_fallback, None
    cls, prefer = _target(profile["main"], default_class, router)
    fallback = tuple(c for c in ("free_tier_reasoning", "standard_reasoning") if c != cls)
    return cls, fallback, prefer


def root_role(profile: dict | None) -> str:
    """Роль корневой задачи: босс-стиль — orchestrator (делегирует), иначе general."""
    return "orchestrator" if profile and profile.get("style") == "boss" else "general"


def thinking_hint(profile: dict | None) -> str:
    """Подсказка о глубине мышления для системного промпта (работает у любого провайдера)."""
    return _THINKING_HINTS.get((profile or {}).get("thinking", "off"), "")

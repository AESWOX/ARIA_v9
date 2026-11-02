"""Static owner map ``role -> provider class / provider id`` (L6).

Read from ``ROLE_MODELS`` (JSON) in ``config.py``::

    ROLE_MODELS={"content_creative": "standard_reasoning", "coder": "premium_reasoning"}

A value is either a provider class (``premium_reasoning`` …) or a concrete
provider/model id registered in the router. The map is static: it never switches
the model after a refusal, and the existing 5xx fallback in the router is
untouched. An invalid map or unknown value falls back to the role default.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("local_agent.role_models")

PROVIDER_CLASSES = frozenset({
    "premium_reasoning",
    "standard_reasoning",
    "free_tier_reasoning",
    "subagent_execution",
    "plan_flash_reasoning",
    "vision_multimodal",
})


def role_models_map(settings=None) -> dict[str, str]:
    from aria.config import get_settings

    raw = getattr(settings or get_settings(), "role_models", "") or ""
    if not str(raw).strip():
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("ROLE_MODELS is not valid JSON; using role defaults")
        return {}
    if not isinstance(data, dict):
        logger.warning("ROLE_MODELS must be a JSON object; using role defaults")
        return {}
    out: dict[str, str] = {}
    for role, value in data.items():
        if isinstance(role, str) and isinstance(value, str) and role.strip() and value.strip():
            out[role.strip()] = value.strip()
    return out


def resolve_role_model(role_id: str, default_class: str, router=None) -> tuple[str, str | None]:
    """(provider class, preferred provider id) for a role; default when unmapped."""
    value = role_models_map().get(str(role_id))
    if not value:
        return default_class, None
    if value in PROVIDER_CLASSES:
        return value, None
    if router is not None:
        from aria.core.runprofile import provider_class_of

        provider_class = provider_class_of(router, value)
        if provider_class:
            return provider_class, value
    logger.warning("ROLE_MODELS: unknown class or model %r for role %r; using %s", value, role_id, default_class)
    return default_class, None

"""
notebook/tool.py — Tool registration for research notes bridge.

Registered in TOOL_REGISTRY when notebook_enabled=True in config.
Every call degrades gracefully via NotebookClient (never crashes agent).
"""

from __future__ import annotations

import logging
from typing import Any

from aria.config import get_settings
from aria.integrations.notebooklm.client import NotebookClient

logger = logging.getLogger(__name__)


def notebook_query(
    prompt: str,
    source_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Query a research notebook.

    Args:
        prompt: The research question / query text.
        source_ids: Optional list of source document IDs to scope the query.

    Returns:
        Dict with keys: success, data, error, elapsed_sec.
    """
    settings = get_settings()
    if not settings.notebook_enabled:
        return {"success": False, "error": "notebook disabled (notebook_enabled=False)"}

    client = NotebookClient(
        auth_path=settings.notebook_auth_path,
        timeout_sec=settings.notebook_timeout_sec,
    )
    result = client.query(prompt=prompt, source_ids=source_ids)
    return {
        "success": result.success,
        "data": result.data,
        "error": result.error,
        "elapsed_sec": result.elapsed_sec,
    }


def notebook_health() -> dict[str, Any]:
    """Health check for the notebook provider. Returns {'ok': bool, 'detail': str}."""
    settings = get_settings()
    if not settings.notebook_enabled:
        return {"ok": False, "detail": "notebook disabled"}

    client = NotebookClient(
        auth_path=settings.notebook_auth_path,
        timeout_sec=settings.notebook_timeout_sec,
    )
    return client.health()


# ToolSpec metadata for registration (see tools/registry.py)
NOTEBOOK_TOOL_SPEC = {
    "notebook_query": {
        "tool_name": "notebook_query",
        "description": "Запрашивает исследовательский notebook по вопросу. "
        "Требует cookies Google-аккаунта. Неофициальный API — может сломаться без предупреждения.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Исследовательский вопрос"},
                "source_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "ID источников для сужения области поиска (опционально)",
                },
            },
            "required": ["prompt"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "data": {"type": "string"},
                "error": {"type": "string"},
            },
        },
        "timeout_sec": 60,
        "risk_level": "low",
        "null_output_allowed": True,
        "requires_approval": False,
        "allowed_roles": ("general", "research", "orchestrator"),
        "idempotency_class": "safe_read",
    }
}

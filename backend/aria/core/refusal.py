"""Refusal diagnostics for model answers (TZ_COMPLIANCE_V2 §4).

Detection only: no retries, no second provider call, no change to the stored
answer. Log lines name session/task/provider/model and never include the text.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("local_agent.refusal")

# Minimal list: explicit refusal phrases only. Generic "i can't" / "не могу найти"
# are ordinary tool/answer failures and must not be counted as refusals.
REFUSAL_PREFIXES: tuple[str, ...] = (
    "i cannot fulfill",
    "i can't fulfill",
    "i cannot assist",
    "i can't assist",
    "i can't help with that",
    "i cannot help with that",
    "i must refuse",
    "i'm not able to help",
    "as an ai",
    "я не могу выполнить",
    "я не могу помочь",
    "к сожалению, я не могу",
    "извините, но я не могу",
    "не могу выполнить этот запрос",
    "я не можу виконати",
    "я не можу допомогти",
    "на жаль, я не можу",
)

SAFETY_FINISH_REASONS: frozenset[str] = frozenset(
    {"content_filter", "safety", "prohibited_content", "blocklist"}
)


def is_model_refusal(
    text: str | None,
    *,
    has_tool_calls: bool = False,
    finish_reason: str | None = None,
) -> bool:
    """True only for an explicit refusal, never for a normal "no" or a tool error."""
    reason = str(finish_reason or "").strip().lower()
    stripped = text.strip() if isinstance(text, str) else ""

    if reason in SAFETY_FINISH_REASONS:
        return True
    if not stripped:
        return not has_tool_calls and reason != "tool_calls"
    head = stripped.lower()
    return any(head.startswith(prefix) for prefix in REFUSAL_PREFIXES)


def finish_reason_from_response(response: Any) -> str | None:
    """OpenAI-compatible ``choices[0].finish_reason`` if the provider returned it."""
    try:
        reason = response.raw["choices"][0].get("finish_reason")
    except Exception:  # noqa: BLE001 — raw may be absent or shaped differently
        return None
    return str(reason) if reason else None


def log_refusal(
    *,
    session_id: Any = None,
    task_id: Any = None,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    """One diagnostic line; never includes the message text or secrets."""
    logger.info(
        "refusal_detected session_id=%s task_id=%s provider=%s model=%s",
        str(session_id)[:8] if session_id else "-",
        str(task_id)[:8] if task_id else "-",
        provider or "-",
        model or "-",
    )

"""Refusal detection shared by the chat path and preflight.

The module is deliberately conservative: a refusal is either an empty answer, an
explicit provider policy signal, or a short allow-listed prefix at the start of
the text. Anything else (including a plain "Нет, …") is treated as a real answer.
"""
from __future__ import annotations

REFUSAL_PREFIXES: tuple[str, ...] = (
    "i cannot fulfill",
    "i can't fulfill",
    "i cannot assist",
    "i must refuse",
    "as an ai",
)

POLICY_HTTP_STATUSES: frozenset[int] = frozenset({451})

SAFETY_FINISH_REASONS: frozenset[str] = frozenset(
    {"content_filter", "safety", "refusal", "blocked", "content_policy_violation"}
)


def is_model_refusal(
    text: str | None,
    *,
    http_status: int | None = None,
    provider_flags: dict | None = None,
) -> bool:
    """True only for an explicit refusal, never for a bare "no" answer."""
    if text is None or not str(text).strip():
        return True
    if http_status is not None and http_status in POLICY_HTTP_STATUSES:
        return True
    flags = provider_flags or {}
    if any(bool(flags.get(key)) for key in ("refusal", "safety", "blocked")):
        return True
    finish = str(flags.get("finish_reason") or "").strip().lower()
    if finish in SAFETY_FINISH_REASONS:
        return True
    head = str(text).strip().lower()
    return any(head.startswith(prefix) for prefix in REFUSAL_PREFIXES)

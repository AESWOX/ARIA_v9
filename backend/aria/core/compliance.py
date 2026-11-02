"""Loader for ``prompts/compliance.md`` (TZ_COMPLIANCE_V2 §3).

The text is read once at import: the chat system prompt stays a stable byte
prefix for the provider prompt cache (PR-1), and no LLM call is added.
"""
from __future__ import annotations

from pathlib import Path

_COMPLIANCE_PATH = Path(__file__).resolve().parent.parent / "prompts" / "compliance.md"


def load_compliance_prompt() -> str:
    try:
        return _COMPLIANCE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


COMPLIANCE_TEXT = load_compliance_prompt()

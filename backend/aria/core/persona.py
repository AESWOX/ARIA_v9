"""Loader for ``prompts/persona.md`` (TZ_PRODUCTION_LINE, L1).

Read once at import: the chat system prompt stays a stable byte prefix (PR-1)
and no LLM call is added.
"""
from __future__ import annotations

from pathlib import Path

_PERSONA_PATH = Path(__file__).resolve().parent.parent / "prompts" / "persona.md"


def load_persona_prompt() -> str:
    try:
        return _PERSONA_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


PERSONA_TEXT = load_persona_prompt()

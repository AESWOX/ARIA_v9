"""Preflight rewrite layer (TZ PREFLIGHT §6).

The user's raw message is the source of truth: the DB and the UI keep it
untouched. Only the outgoing model prompt gets the rewritten text. Every model
call goes through the existing ProviderRouter, so usage accounting treats the
rewriter exactly like any other purpose ("preflight").
"""
from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aria.config import get_settings

logger = logging.getLogger("local_agent.preflight")

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_REWRITE_PROMPT_PATH = _PROMPTS_DIR / "preflight_rewrite.md"
_COMPLIANCE_PROMPT_PATH = _PROMPTS_DIR / "compliance.md"

_CACHE: OrderedDict[str, str] = OrderedDict()
_CACHE_MAX = 256

CLI_PREFIXES: tuple[str, ...] = (
    "git ", "npm ", "npx ", "pytest ", "docker ", "cargo ",
    "pip ", "pip3 ", "rm ", "cd ", "ls ", "python ", "python3 ", "uv ",
)


@dataclass(frozen=True)
class PreflightResult:
    raw: str
    rewritten: str
    applied: bool
    reason: str


def reset_cache() -> None:
    """Process-local cache reset (tests and diagnostics)."""
    _CACHE.clear()


def load_rewrite_prompt() -> str:
    return _REWRITE_PROMPT_PATH.read_text(encoding="utf-8")


def load_compliance_prompt() -> str:
    try:
        return _COMPLIANCE_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _cache_get(key: str) -> str | None:
    if key not in _CACHE:
        return None
    _CACHE.move_to_end(key)
    return _CACHE[key]


def _cache_put(key: str, value: str) -> None:
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


def _build_cache_key(raw: str, context_blob: str, model_class: str) -> str:
    digest = hashlib.sha256()
    digest.update(raw.encode("utf-8"))
    digest.update(b"\0")
    digest.update(context_blob.encode("utf-8"))
    digest.update(b"\0")
    digest.update(model_class.encode("utf-8"))
    return digest.hexdigest()


def _is_pure_cli_command(text: str) -> bool:
    trimmed = text.strip()
    if not trimmed:
        return False
    if trimmed.startswith("#!"):
        return True
    if trimmed.startswith("```"):
        first_line = trimmed.split("\n", 1)[0].lower()
        if "bash" in first_line or first_line.endswith("sh"):
            return True
    if "\n\n" in trimmed:
        return False
    return any(trimmed.startswith(prefix) for prefix in CLI_PREFIXES)


def _message_field(message: Any, name: str) -> str:
    value = getattr(message, name, None)
    if value is None and isinstance(message, dict):
        value = message.get(name)
    return str(value or "")


def _context_entries(context_history: list[Any] | None, turns: int) -> list[tuple[str, str]]:
    if not context_history or turns <= 0:
        return []
    entries: list[tuple[str, str]] = []
    for message in context_history:
        role = _message_field(message, "role")
        content = _message_field(message, "content")
        if role in ("user", "assistant") and content:
            entries.append((role, content))
    return entries[-turns * 2:]


def _context_blob(entries: list[tuple[str, str]]) -> str:
    return "\n".join(f"{role}:{content}" for role, content in entries)


def _fallback_classes(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, (tuple, list)):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    return tuple(item.strip() for item in str(raw or "").split(",") if item.strip())


async def preflight_user_message(
    content: str | None,
    *,
    router: Any,
    mode: str,
    session_id: Any = None,
    context_history: list[Any] | None = None,
    sensitive: bool = False,
) -> PreflightResult:
    """Rewrite the user message for the model; never raise on LLM/network errors."""
    settings = get_settings()
    raw = content or ""

    if not raw.strip():
        return PreflightResult(raw, raw, False, "empty")
    if not bool(getattr(settings, "preflight_rewrite_enabled", True)):
        return PreflightResult(raw, raw, False, "disabled")
    if sensitive:
        # E9: содержимое с секретами не уходит в лишний вызов (тем более на free-tier).
        return PreflightResult(raw, raw, False, "skipped_sensitive")

    from aria.core.clarifier import is_small_talk

    if is_small_talk(raw) and not context_history:
        return PreflightResult(raw, raw, False, "skipped_small_talk")

    max_chars = int(getattr(settings, "preflight_max_chars", 8000))
    if len(raw) > max_chars:
        return PreflightResult(raw, raw, False, "too_long")

    if _is_pure_cli_command(raw):
        return PreflightResult(raw, raw, False, "skipped_cli_passthrough")

    turns = int(getattr(settings, "preflight_context_turns", 2))
    entries = _context_entries(context_history, turns)
    blob = _context_blob(entries)
    model_class = str(getattr(settings, "preflight_rewrite_model_class", "free_tier_reasoning"))
    key = _build_cache_key(raw, blob, model_class)
    cached = _cache_get(key)
    if cached is not None:
        return PreflightResult(raw, cached, True, "cached")

    try:
        from aria.llm.providers.base import ChatMessage

        messages = [ChatMessage(role="system", content=load_rewrite_prompt())]
        messages += [ChatMessage(role=role, content=text) for role, text in entries]
        messages.append(ChatMessage(role="user", content=raw))
        result = await router.route_chat(
            model_class,
            messages,
            [],
            allow_degrade=True,
            resilient=True,
            purpose="preflight",
            session_id=session_id,
        )
        rewritten = str(getattr(result.response, "text", "") or "").strip()
        if not rewritten:
            return PreflightResult(raw, raw, False, "error_fallback")
        _cache_put(key, rewritten)
        logger.info(
            "preflight_rewrite mode=%s session_id=%s chars=%d->%d",
            mode,
            str(session_id)[:8] if session_id else "-",
            len(raw),
            len(rewritten),
        )
        return PreflightResult(raw, rewritten, True, "rewritten")
    except Exception:  # noqa: BLE001 — preflight must never break the chat
        logger.exception("preflight failed mode=%s session_id=%s", mode, session_id)
        return PreflightResult(raw, raw, False, "error_fallback")

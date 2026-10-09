"""Env routes: /api/env, /api/env/reveal.

Lists a curated registry of env vars the backend cares about, redacting
secrets. PUT/DELETE mutate os.environ and persist to the backend .env file
so changes survive restarts. Values are never returned in plaintext by
GET; POST /api/env/reveal returns the raw value under runtime-token auth.
"""
from __future__ import annotations

from aria import paths

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from aria.api.auth import require_runtime_token

router = APIRouter(tags=["env"])

_ENV_FILE = paths.env_file()


def _mask(value: str | None) -> str | None:
    if not value:
        return None
    if len(value) <= 8:
        return "***"
    return f"{value[:3]}...{value[-3:]}"


def _entry(
    name: str,
    description: str,
    category: str,
    url: str | None = None,
    is_password: bool | None = None,
    tools: list[str] | None = None,
    advanced: bool = False,
    channel_managed: bool = False,
) -> dict[str, Any]:
    if is_password is None:
        is_password = any(marker in name for marker in ("KEY", "TOKEN", "PIN", "SECRET", "PASSWORD"))
    return {
        "description": description,
        "url": url,
        "category": category,
        "is_password": is_password,
        "tools": tools or [],
        "advanced": advanced,
        "channel_managed": channel_managed,
    }


def _live(name: str, meta: dict[str, Any]) -> dict[str, Any]:
    """Snapshot of a registry entry with the *current* process env (read per request)."""
    actual = os.environ.get(name) or None
    return {
        **meta,
        "is_set": bool(actual),
        "redacted_value": _mask(actual),
    }


# Registry of env vars surfaced in the UI. provider prefixes match the
# grouping in desktop/src/pages/EnvPage.tsx PROVIDER_GROUPS.
_REGISTRY: dict[str, dict[str, Any]] = {
    # --- OAuth/SSO section ---
    "LOCAL_AGENT_UI_PIN": _entry(
        "LOCAL_AGENT_UI_PIN", "UI idle-lock PIN (auto-generated if empty)", "oauth", advanced=True,
    ),
    "LOCAL_AGENT_RUNTIME_TOKEN": _entry(
        "LOCAL_AGENT_RUNTIME_TOKEN", "Runtime auth token used by the shell/UI", "oauth", advanced=True,
    ),
    # --- Providers ---
    "OPENAI_API_KEY": _entry("OPENAI_API_KEY", "OpenAI API key", "provider", url="https://platform.openai.com/api-keys"),
    "ANTHROPIC_API_KEY": _entry("ANTHROPIC_API_KEY", "Anthropic API key", "provider", url="https://console.anthropic.com/settings/keys"),
    "DEEPSEEK_API_KEY": _entry("DEEPSEEK_API_KEY", "DeepSeek API key", "provider", url="https://platform.deepseek.com/api_keys"),
    "GEMINI_API_KEY": _entry("GEMINI_API_KEY", "Gemini API key", "provider", url="https://aistudio.google.com/app/apikey"),
    "GEMINI_API_KEYS": _entry("GEMINI_API_KEYS", "Gemini key rotation pool (comma-separated)", "provider", url="https://aistudio.google.com/app/apikey"),
    "GEMINI_FLASH_MODEL": _entry("GEMINI_FLASH_MODEL", "Gemini model for fast/free answers (default gemini-3.8-flash)", "provider", advanced=True),
    "GEMINI_PRO_MODEL": _entry("GEMINI_PRO_MODEL", "Gemini model for the 'pro' class; keep a Flash model on the free tier", "provider", advanced=True),
    "GROQ_API_KEY": _entry("GROQ_API_KEY", "Groq API key", "provider", url="https://console.groq.com/keys"),
    "GROQ_API_KEYS": _entry("GROQ_API_KEYS", "Groq key rotation pool (comma-separated)", "provider", url="https://console.groq.com/keys"),
    "OPENROUTER_API_KEY": _entry("OPENROUTER_API_KEY", "OpenRouter API key", "provider", url="https://openrouter.ai/keys"),
    "NOUS_API_KEY": _entry("NOUS_API_KEY", "Nous Portal API key", "provider", url="https://nousresearch.com"),
    "NOUS_PORTAL_API_KEY": _entry("NOUS_PORTAL_API_KEY", "Nous Portal API key", "provider", url="https://nousresearch.com"),
    "GLM_API_KEY": _entry("GLM_API_KEY", "GLM / Z.AI API key", "provider", url="https://open.bigmodel.cn"),
    "ZAI_API_KEY": _entry("ZAI_API_KEY", "GLM / Z.AI API key", "provider", url="https://open.bigmodel.cn"),
    "KIMI_API_KEY": _entry("KIMI_API_KEY", "Kimi / Moonshot API key", "provider", url="https://platform.moonshot.ai"),
    "HF_TOKEN": _entry("HF_TOKEN", "Hugging Face token", "provider", url="https://huggingface.co/settings/tokens"),
    # --- Tools ---
    "OBSIDIAN_VAULT_PATH": _entry("OBSIDIAN_VAULT_PATH", "Path to the Obsidian vault", "tool", advanced=True),
    "B2_BUCKET": _entry("B2_BUCKET", "Backblaze B2 bucket name", "tool", url="https://secure.backblaze.com/b2_buckets.htm", advanced=True),
    "B2_KEY_ID": _entry("B2_KEY_ID", "Backblaze B2 key ID", "tool", url="https://secure.backblaze.com/b2_buckets.htm", advanced=True),
    "B2_APPLICATION_KEY": _entry("B2_APPLICATION_KEY", "Backblaze B2 application key", "tool", url="https://secure.backblaze.com/b2_buckets.htm", advanced=True),
    # --- Messaging (owned by Channels page) ---
    "TELEGRAM_BOT_TOKEN": _entry("TELEGRAM_BOT_TOKEN", "Telegram bot token (managed on Channels page)", "messaging", channel_managed=True),
    "DISCORD_BOT_TOKEN": _entry("DISCORD_BOT_TOKEN", "Discord bot token (managed on Channels page)", "messaging", channel_managed=True),
    # --- Settings ---
    "POSTGRES_DSN": _entry("POSTGRES_DSN", "Database DSN (sqlite:/// or postgres://)", "setting", advanced=True),
    "REDIS_URL": _entry("REDIS_URL", "Redis connection URL", "setting", advanced=True),
    "ARIA_SERVER_MODE": _entry("ARIA_SERVER_MODE", "Run in server/multi-user mode (1)", "setting", advanced=True),
    "ARIA_DEV_CORS": _entry("ARIA_DEV_CORS", "Enable dev CORS origins (1)", "setting", advanced=True),
    "LOCAL_AGENT_DISABLE_BOOTSTRAP_WRITE": _entry(
        "LOCAL_AGENT_DISABLE_BOOTSTRAP_WRITE", "Skip writing bootstrap.json (1)", "setting", advanced=True,
    ),
    "LOCK_TTL_SECONDS": _entry("LOCK_TTL_SECONDS", "Optimistic lock TTL (seconds)", "setting", advanced=True),
}

# Env vars actually present but not in the registry still surface, so the
# UI can edit them (grouped by provider prefix or as settings).
_PROVIDER_PREFIXES = (
    "OPENAI_", "ANTHROPIC_", "DEEPSEEK_", "GEMINI_", "GROQ_", "OPENROUTER_",
    "NOUS_", "GLM_", "ZAI_", "Z_AI_", "KIMI_", "MINIMAX_", "HF_", "DASHSCOPE_",
    "ARIA_QWEN_", "OPENCODE_", "XIAOMI_",
)


def _load_env_file() -> dict[str, str]:
    if not _ENV_FILE.exists():
        return {}
    result: dict[str, str] = {}
    for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def _persist_env_var(key: str, value: str | None) -> None:
    """Update backend/.env in place, preserving unrelated lines/comments."""
    entries = _load_env_file()
    entries[key] = value or ""
    lines = _ENV_FILE.read_text(encoding="utf-8").splitlines() if _ENV_FILE.exists() else []
    seen = False
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.partition("=")[0].strip()
            if k == key:
                new_lines.append(f"{key}={value or ''}")
                seen = True
                continue
        new_lines.append(line)
    if not seen:
        new_lines.append(f"{key}={value or ''}")
    _ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


def _refresh_settings(request: Request | None = None) -> None:
    """Settings are lru_cached; without this a changed OBSIDIAN_VAULT_PATH (or
    any other setting) is saved to .env but ignored until the app restarts.

    The LLM provider router is built from the keys at startup, so it is rebuilt
    too: a Gemini key pasted on the Keys page works for the very next chat
    message. (Rebuilding resets key cooldown state, which is what you want
    after changing keys.)"""
    from aria.config import get_settings

    get_settings.cache_clear()
    if request is not None and hasattr(request.app.state, "router"):
        try:
            from aria.llm.router import build_default_router

            request.app.state.router = build_default_router()
        except Exception:  # noqa: BLE001 - never fail saving a key because of this
            logging.getLogger("local_agent.env").exception("could not rebuild LLM router")


@router.get("/env")
async def list_env(_: str = Depends(require_runtime_token)) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name, meta in _REGISTRY.items():
        result[name] = _live(name, meta)
    present = set(os.environ.keys())
    for key in sorted(present):
        if key in result:
            continue
        if key.startswith(_PROVIDER_PREFIXES):
            result[key] = _live(key, _entry(key, f"Provider credential {key}", "provider", advanced=True))
        elif key.startswith(("ARIA_", "LOCAL_AGENT_")):
            result[key] = _live(key, _entry(key, f"Runtime setting {key}", "setting", advanced=True))
    return result


@router.put("/env")
async def set_env_var(
    body: dict[str, str],
    request: Request,
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    key = (body.get("key") or "").strip()
    value = body.get("value") or ""
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    os.environ[key] = value
    _persist_env_var(key, value)
    _refresh_settings(request)
    return {"ok": True}


@router.delete("/env")
async def delete_env_var(
    body: dict[str, str],
    request: Request,
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    key = (body.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    os.environ.pop(key, None)
    _persist_env_var(key, None)
    _refresh_settings(request)
    return {"ok": True}


@router.post("/env/reveal")
async def reveal_env_var(
    body: dict[str, str],
    _: str = Depends(require_runtime_token),
) -> dict[str, str]:
    key = (body.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="key is required")
    value = os.environ.get(key)
    if value is None:
        raise HTTPException(status_code=404, detail=f"{key} is not set")
    return {"key": key, "value": value}

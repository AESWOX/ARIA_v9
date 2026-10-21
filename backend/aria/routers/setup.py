"""Setup routes for the first-run wizard: /setup/validate-key, /setup/apply.

The wizard lets a brand-new user pick a provider, paste a key, see which
models that key can actually use and save the choice, without editing .env
by hand. Both routes are runtime-token protected. Provider ids and base URLs
come from a fixed allowlist, so the key is only ever sent to the provider's
own API (no user-supplied URLs, no SSRF). Keys are never logged or echoed back.
"""
from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.llm import gemini_discovery
from aria.routers import env as env_mod

router = APIRouter(prefix="/setup", tags=["setup"])
logger = logging.getLogger("local_agent.setup")

# provider -> (label, key env var, model env var, key page)
PROVIDERS: dict[str, dict[str, str]] = {
    "gemini": {
        "label": "Google Gemini (free tier available)",
        "key_env": "GEMINI_API_KEYS",
        "model_env": "GEMINI_FLASH_MODELS",
        "url": "https://aistudio.google.com/app/apikey",
    },
    "groq": {
        "label": "Groq (fast Llama / gpt-oss)",
        "key_env": "GROQ_API_KEYS",
        "model_env": "GROQ_MODELS",
        "url": "https://console.groq.com/keys",
    },
    "deepseek": {
        "label": "DeepSeek",
        "key_env": "DEEPSEEK_API_KEY",
        "model_env": "DEEPSEEK_CHAT_MODEL",
        "url": "https://platform.deepseek.com/api_keys",
    },
}

_KEY_RE = re.compile(r"^[A-Za-z0-9_\-\.]{8,300}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9_\-\./:]{1,120}$")
_NON_CHAT = re.compile(r"whisper|guard|moderation|tts|playai|orpheus|embed|transcri|speech|imagen|veo|aqa")


class ValidateBody(BaseModel):
    provider: str
    key: str = Field(max_length=400)


class ApplyBody(ValidateBody):
    model: str = Field(max_length=200)


def _base_url(provider: str) -> str:
    s = get_settings()
    return {"gemini": s.gemini_base_url, "groq": s.groq_base_url, "deepseek": s.deepseek_base_url}[provider]


def _check(provider: str, key: str) -> str:
    key = (key or "").strip()
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail="unknown provider")
    if not _KEY_RE.match(key):
        raise HTTPException(status_code=400, detail="that does not look like an API key")
    return key


def _list_models(provider: str, key: str) -> list[str]:
    """Models the key can see. Raises httpx errors; callers translate them."""
    models = gemini_discovery.list_models(_base_url(provider), key)
    seen: set[str] = set()
    out: list[str] = []
    for m in models:
        if m and m not in seen and not _NON_CHAT.search(m.lower()):
            seen.add(m)
            out.append(m)
    return out


@router.get("/providers")
async def list_providers(_: str = Depends(require_runtime_token)) -> list[dict[str, str]]:
    return [{"id": pid, "label": meta["label"], "url": meta["url"]} for pid, meta in PROVIDERS.items()]


@router.post("/validate-key")
async def validate_key(body: ValidateBody, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    key = _check(body.provider, body.key)
    try:
        models = _list_models(body.provider, key)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (400, 401, 403):
            return {"ok": False, "reason": "invalid_key", "models": []}
        if code == 429:
            return {"ok": False, "reason": "rate_limited", "models": []}
        return {"ok": False, "reason": "provider_error", "models": []}
    except httpx.HTTPError:
        return {"ok": False, "reason": "network", "models": []}
    if not models:
        return {"ok": False, "reason": "no_models", "models": []}
    return {"ok": True, "reason": None, "models": models}


@router.post("/apply")
async def apply_choice(body: ApplyBody, request: Request, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    key = _check(body.provider, body.key)
    model = (body.model or "").strip()
    if not _MODEL_RE.match(model):
        raise HTTPException(status_code=400, detail="invalid model id")
    meta = PROVIDERS[body.provider]
    import os

    for name, value in ((meta["key_env"], key), (meta["model_env"], model)):
        os.environ[name] = value
        env_mod._persist_env_var(name, value)
    env_mod._refresh_settings(request)
    return {"ok": True, "provider": body.provider, "model": model}

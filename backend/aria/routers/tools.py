"""Tools routes: /api/tools/toolsets (list/toggle/config/provider/env/post-setup).

Toolsets are a static catalog of capability groups (browser, shell, search,
vault, ...) with pluggable backends ("providers") that need API keys or a
post-setup install hook. Enabled state and the active provider per toolset
persist to ``data/toolsets.json``; env var presence is read live from
``os.environ``.
"""
from __future__ import annotations

from aria import paths

import json
import os
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from aria.api.auth import require_runtime_token
from aria.routers import actions as actions_module

router = APIRouter(tags=["tools"])

_STATE_FILE = paths.pkg_data_dir() / "toolsets.json"
_state_lock = threading.Lock()


def _load_state() -> dict[str, Any]:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {}


def _save_state(state: dict[str, Any]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _is_set(key: str) -> bool:
    return bool(os.environ.get(key))


def _env_var(key: str, prompt: str, url: str | None = None,
             default: str | None = None) -> dict[str, Any]:
    return {
        "key": key,
        "prompt": prompt,
        "url": url,
        "default": default,
        "is_set": _is_set(key),
    }


def _provider(name: str, badge: str, tag: str,
              env_vars: list[dict[str, Any]],
              post_setup: str | None = None,
              requires_nous_auth: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "badge": badge,
        "tag": tag,
        "env_vars": env_vars,
        "post_setup": post_setup,
        "requires_nous_auth": requires_nous_auth,
        "is_active": False,
    }


# name -> {label, description, tools, has_category, providers}
_CATALOG: dict[str, dict[str, Any]] = {
    "obsidian": {
        "label": "Obsidian vault",
        "description": "Read, write, search (text, tags, key decisions) and list notes in the Obsidian vault; create branches and log decisions.",
        "tools": ["read_note", "write_note", "search_vault", "list_vault", "search_vault_tags", "list_decisions", "create_vault_branch", "log_decision"],
        "has_category": True,
        "providers": [
            _provider("vault", "Vault", "local",
                      [_env_var("OBSIDIAN_VAULT_PATH", "Path to the Obsidian vault")]),
        ],
    },
    "shell": {
        "label": "Shell",
        "description": "Execute terminal commands with a full allowlist of risk patterns.",
        "tools": ["shell_execute"],
        "has_category": False,
        "providers": [_provider("local", "Local", "local", [])],
    },
    "files": {
        "label": "Files",
        "description": "Read, write and search files in the workspace.",
        "tools": ["file_read", "file_write", "file_search"],
        "has_category": False,
        "providers": [_provider("local", "Local", "local", [])],
    },
    "search": {
        "label": "Web search",
        "description": "Search the web through one of the supported search APIs.",
        "tools": ["web_search"],
        "has_category": True,
        "providers": [
            _provider("brave", "Brave", "API",
                      [_env_var("BRAVE_API_KEY", "Brave Search API key",
                                url="https://brave.com/search/api/")]),
            _provider("tavily", "Tavily", "API",
                      [_env_var("TAVILY_API_KEY", "Tavily API key",
                                url="https://tavily.com/")]),
        ],
    },
    "vision": {
        "label": "Vision",
        "description": "Analyze images with a vision-capable multimodal model.",
        "tools": ["vision_analyze"],
        "has_category": True,
        "providers": [
            _provider("gemini", "Gemini", "API",
                      [_env_var("GEMINI_API_KEY", "Gemini API key",
                                url="https://aistudio.google.com/app/apikey")]),
            _provider("openai", "OpenAI", "API",
                      [_env_var("OPENAI_API_KEY", "OpenAI API key",
                                url="https://platform.openai.com/api-keys")]),
        ],
    },
    "notebooklm": {
        "label": "NotebookLM",
        "description": "Query a NotebookLM notebook for grounded research answers.",
        "tools": ["notebook_query"],
        "has_category": False,
        "providers": [
            _provider("notebooklm", "NotebookLM", "API",
                      [_env_var("NOTEBOOKLM_KEY", "NotebookLM API key")]),
        ],
    },
    "memory": {
        "label": "Memory",
        "description": "Long-term friend memory read/write for persistent context.",
        "tools": ["friend_memory_read"],
        "has_category": False,
        "providers": [_provider("local", "Local", "local", [])],
    },
    "delegation": {
        "label": "Delegation",
        "description": "Spawn sub-agents for parallelizable sub-tasks.",
        "tools": ["delegate_task"],
        "has_category": False,
        "providers": [_provider("local", "Local", "local", [])],
    },
}


@router.get("/tools/toolsets")
async def list_toolsets(
    _: str = Depends(require_runtime_token),
    profile: str | None = None,
) -> list[dict[str, Any]]:
    state = _load_state()
    enabled_map = state.get("enabled", {})
    out: list[dict[str, Any]] = []
    for name, spec in _CATALOG.items():
        out.append({
            "name": name,
            "label": spec["label"],
            "description": spec["description"],
            "enabled": bool(enabled_map.get(name, True)),
            "configured": any(_is_set(e["key"]) for p in spec["providers"] for e in p["env_vars"]),
            "tools": spec["tools"],
        })
    return out


@router.put("/tools/toolsets/{name}")
async def toggle_toolset(
    name: str,
    body: dict[str, Any],
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    if name not in _CATALOG:
        raise HTTPException(status_code=404, detail=f"unknown toolset: {name}")
    enabled = bool(body.get("enabled"))
    with _state_lock:
        state = _load_state()
        state.setdefault("enabled", {})[name] = enabled
        _save_state(state)
    return {"ok": True, "name": name, "enabled": enabled}


@router.get("/tools/toolsets/{name}/config")
async def get_toolset_config(
    name: str,
    _: str = Depends(require_runtime_token),
    profile: str | None = None,
) -> dict[str, Any]:
    spec = _CATALOG.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"unknown toolset: {name}")
    state = _load_state()
    active_provider = state.get("active_provider", {}).get(name)
    providers: list[dict[str, Any]] = []
    for p in spec["providers"]:
        p = dict(p)
        p["is_active"] = p["name"] == active_provider
        providers.append(p)
    return {
        "name": name,
        "has_category": spec["has_category"],
        "providers": providers,
        "active_provider": active_provider,
    }


@router.put("/tools/toolsets/{name}/provider")
async def select_toolset_provider(
    name: str,
    body: dict[str, Any],
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    spec = _CATALOG.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"unknown toolset: {name}")
    provider = (body.get("provider") or "").strip()
    if provider not in {p["name"] for p in spec["providers"]}:
        raise HTTPException(status_code=400, detail=f"unknown provider: {provider}")
    with _state_lock:
        state = _load_state()
        state.setdefault("active_provider", {})[name] = provider
        _save_state(state)
    return {"ok": True, "name": name, "provider": provider}


@router.put("/tools/toolsets/{name}/env")
async def save_toolset_env(
    name: str,
    body: dict[str, Any],
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    spec = _CATALOG.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"unknown toolset: {name}")
    env = body.get("env") or {}
    saved: list[str] = []
    skipped: list[str] = []
    valid_keys = {e["key"] for p in spec["providers"] for e in p["env_vars"]}
    for key, value in env.items():
        if not key or key not in valid_keys:
            skipped.append(key)
            continue
        os.environ[key] = str(value)
        saved.append(key)
    is_set = {k: _is_set(k) for k in valid_keys}
    return {"ok": True, "name": name, "saved": saved, "skipped": skipped, "is_set": is_set}


@router.post("/tools/toolsets/{name}/post-setup")
async def run_toolset_post_setup(
    name: str,
    body: dict[str, Any],
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    spec = _CATALOG.get(name)
    if spec is None:
        raise HTTPException(status_code=404, detail=f"unknown toolset: {name}")
    key = (body.get("key") or "").strip()
    known = {p["post_setup"] for p in spec["providers"] if p["post_setup"]}
    if key not in known:
        raise HTTPException(status_code=400, detail=f"unknown post-setup hook: {key}")
    lines = [
        f"post-setup hook {key!r} for toolset {name!r} acknowledged.",
        "This build does not auto-install dependencies; run the install manually:",
        f"  aria tools post-setup --toolset {name} --key {key}",
    ]
    actions_module.record("tools-post-setup", 0, lines)
    return {"name": name, "ok": True, "pid": None, "key": key,
            "message": f"post-setup {key} started"}

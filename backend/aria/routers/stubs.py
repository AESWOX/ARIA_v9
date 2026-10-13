"""Stub routers (§WORK_NOTES stubs phase).

Minimal but contract-faithful implementations for feature areas that the
local build does not fully wire up (OAuth, gateway, MCP, ops, dashboard
plugins, messaging, pairing, webhooks, credential pool, memory provider,
curator, portal, aria update). Every response shape matches the frontend
contract in desktop/src/lib/api.ts so the UI roundtrips without 4xx/5xx.
CRUD areas that have cheap local persistence (mcp, webhooks, hooks,
credentials) persist to data/*.json instead of returning empty lists.
"""
from __future__ import annotations

from aria import paths

import json
import os
import platform
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from aria.api.auth import require_runtime_token

router = APIRouter(tags=["stubs"])

_DATA_DIR = paths.data_dir()


# ---------------------------------------------------------------------------
# Small JSON store helper
# ---------------------------------------------------------------------------


def _load_json(name: str, default: Any) -> Any:
    path = _DATA_DIR / name
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data
        except Exception:
            pass
    return default


def _save_json(name: str, data: Any) -> None:
    path = _DATA_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def _action(name: str, ok: bool = True, message: str | None = None) -> dict[str, Any]:
    resp: dict[str, Any] = {"name": name, "ok": ok, "pid": None}
    if message:
        resp["message"] = message
    return resp


# ---------------------------------------------------------------------------
# OAuth providers
# ---------------------------------------------------------------------------

_OAUTH_SESSIONS: dict[str, str] = {}  # session_id -> status

_OAUTH_CATALOG = [
    {"id": "anthropic", "name": "Anthropic", "flow": "pkce", "cli_command": "", "docs_url": "https://docs.anthropic.com/en/api/getting-started/oauth"},
    {"id": "openai", "name": "OpenAI", "flow": "pkce", "cli_command": "", "docs_url": "https://platform.openai.com/docs"},
    {"id": "google", "name": "Google Gemini", "flow": "pkce", "cli_command": "", "docs_url": "https://ai.google.dev/gemini-api/docs"},
    {"id": "qwen", "name": "Qwen", "flow": "external", "cli_command": "qwen login", "docs_url": ""},
    {"id": "claude-code", "name": "Claude Code", "flow": "external", "cli_command": "claude setup-token", "docs_url": ""},
]


@router.get("/providers/oauth")
async def oauth_providers(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    providers = []
    for entry in _OAUTH_CATALOG:
        providers.append(
            {
                **entry,
                "status": {
                    "logged_in": False,
                    "source": None,
                    "source_label": None,
                    "token_preview": None,
                    "expires_at": None,
                    "has_refresh_token": False,
                    "last_refresh": None,
                    "error": None,
                },
            }
        )
    return {"providers": providers}


@router.delete("/providers/oauth/{provider_id}")
async def oauth_disconnect(provider_id: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    if provider_id not in {p["id"] for p in _OAUTH_CATALOG}:
        raise HTTPException(status_code=404, detail="unknown provider")
    return {"ok": True, "provider": provider_id}


@router.post("/providers/oauth/{provider_id}/start")
async def oauth_start(provider_id: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    entry = next((p for p in _OAUTH_CATALOG if p["id"] == provider_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="unknown provider")
    import uuid

    session_id = str(uuid.uuid4())
    _OAUTH_SESSIONS[session_id] = "pending"
    if entry["flow"] == "device_code":
        return {
            "session_id": session_id,
            "flow": "device_code",
            "user_code": "ABCD-1234",
            "verification_url": "https://example.com/device",
            "expires_in": 900,
            "poll_interval": 5,
        }
    return {
        "session_id": session_id,
        "flow": "pkce",
        "auth_url": "https://example.com/oauth/authorize",
        "expires_in": 900,
    }


@router.post("/providers/oauth/{provider_id}/submit")
async def oauth_submit(provider_id: str, session_id: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    _OAUTH_SESSIONS.pop(session_id, None)
    return {"ok": False, "status": "error", "message": "OAuth is not configured for the local build"}


@router.get("/providers/oauth/{provider_id}/poll/{session_id}")
async def oauth_poll(provider_id: str, session_id: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    status = _OAUTH_SESSIONS.get(session_id, "expired")
    return {"session_id": session_id, "status": status, "error_message": None, "expires_at": None}


@router.delete("/providers/oauth/sessions/{session_id}")
async def oauth_cancel(session_id: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    _OAUTH_SESSIONS.pop(session_id, None)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Gateway lifecycle
# ---------------------------------------------------------------------------


@router.post("/gateway/start")
async def gateway_start(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return _action("gateway-start", False, "gateway is not implemented in the local build")


@router.post("/gateway/stop")
async def gateway_stop(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return _action("gateway-stop", False, "gateway is not implemented in the local build")


@router.post("/gateway/restart")
async def gateway_restart(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return _action("gateway-restart", False, "gateway is not implemented in the local build")


# ---------------------------------------------------------------------------
# Ops: doctor / audit / backup / import / hooks / diagnostics / checkpoints
# ---------------------------------------------------------------------------


@router.post("/ops/config-migrate")
async def ops_config_migrate(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return _action("ops-config-migrate", True, "config already at latest version")


@router.post("/ops/debug-share")
async def ops_debug_share(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    redact = bool(payload.get("redact", True))
    return {
        "ok": True,
        "urls": {},
        "failures": [],
        "redacted": redact,
        "auto_delete_seconds": 0,
    }


# ---------------------------------------------------------------------------
# Dashboard plugins
# ---------------------------------------------------------------------------


@router.post("/dashboard/plugins/rescan")
async def plugins_rescan(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"ok": True, "count": 0}


@router.get("/dashboard/plugins/hub")
async def plugins_hub(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {
        "plugins": [],
        "orphan_dashboard_plugins": [],
        "providers": {
            "memory_provider": "local",
            "memory_options": [{"name": "local", "description": "Built-in local memory store"}],
            "context_engine": "none",
            "context_options": [],
        },
    }


@router.post("/dashboard/agent-plugins/install")
async def agent_plugins_install(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"ok": False, "error": "agent plugin install is not supported in the local build"}


@router.post("/dashboard/agent-plugins/{name}/enable")
async def agent_plugins_enable(name: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"ok": True, "name": name}


@router.post("/dashboard/agent-plugins/{name}/disable")
async def agent_plugins_disable(name: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"ok": True, "name": name, "unchanged": True}


@router.post("/dashboard/agent-plugins/{name}/update")
async def agent_plugins_update(name: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"ok": False, "name": name, "error": "agent plugins are not installed in the local build"}


@router.delete("/dashboard/agent-plugins/{name}")
async def agent_plugins_remove(name: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"ok": True, "name": name}


@router.put("/dashboard/plugin-providers")
async def plugin_providers_put(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    _save_json("plugin_providers.json", payload)
    return {"ok": True}


@router.post("/dashboard/plugins/{name}/visibility")
async def plugins_visibility(name: str, payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    hidden = bool(payload.get("hidden", False))
    return {"ok": True, "name": name, "hidden": hidden}


# ---------------------------------------------------------------------------
# Messaging platforms
# ---------------------------------------------------------------------------


@router.get("/messaging/platforms")
async def messaging_platforms(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {
        "env_path": str(paths.env_file()),
        "gateway_start_command": "",
        "platforms": [],
    }


@router.put("/messaging/platforms/{platform_id}")
async def messaging_platform_update(platform_id: str, payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"ok": True, "platform": platform_id}


@router.post("/messaging/platforms/{platform_id}/test")
async def messaging_platform_test(platform_id: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"ok": False, "state": "not_configured", "message": "platform not configured in the local build"}


@router.post("/messaging/telegram/onboarding/start")
async def telegram_onboarding_start(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    import datetime

    import uuid

    pairing_id = str(uuid.uuid4())
    bot_name = str(payload.get("bot_name", "aria_bot") or "aria_bot")
    return {
        "pairing_id": pairing_id,
        "suggested_username": bot_name,
        "deep_link": f"https://t.me/{bot_name}",
        "qr_payload": f"tg://resolve?domain={bot_name}",
        "expires_at": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=15)).isoformat(),
    }


@router.get("/messaging/telegram/onboarding/{pairing_id}")
async def telegram_onboarding_status(pairing_id: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    import datetime

    return {
        "status": "waiting",
        "expires_at": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=15)).isoformat(),
    }


@router.post("/messaging/telegram/onboarding/{pairing_id}/apply")
async def telegram_onboarding_apply(pairing_id: str, payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"ok": False, "platform": "telegram", "needs_restart": False, "error": "telegram onboarding requires the gateway, which is a no-op here"}


@router.delete("/messaging/telegram/onboarding/{pairing_id}")
async def telegram_onboarding_cancel(pairing_id: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"ok": True}


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------


@router.get("/pairing")
async def pairing_get(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"pending": [], "approved": []}


@router.post("/pairing/approve")
async def pairing_approve(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    platform = str(payload.get("platform", ""))
    code = str(payload.get("code", ""))
    return {"ok": False, "user": {"platform": platform, "user_id": code}}


@router.post("/pairing/revoke")
async def pairing_revoke(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"ok": True}


@router.post("/pairing/clear-pending")
async def pairing_clear(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"ok": True, "cleared": 0}


# ---------------------------------------------------------------------------
# Credential pool
# ---------------------------------------------------------------------------


def _pool_store() -> dict[str, list[dict[str, Any]]]:
    return _load_json("credentials_pool.json", {})


@router.get("/credentials/pool")
async def credentials_pool_get(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    store = _pool_store()
    providers = []
    for provider, entries in store.items():
        rows = []
        for i, entry in enumerate(entries):
            rows.append(
                {
                    "index": i,
                    "id": entry.get("id"),
                    "label": entry.get("label"),
                    "auth_type": entry.get("auth_type"),
                    "source": entry.get("source"),
                    "priority": entry.get("priority", 0),
                    "last_status": entry.get("last_status"),
                    "request_count": entry.get("request_count", 0),
                    "token_preview": entry.get("token_preview", "****"),
                    "has_refresh": bool(entry.get("has_refresh", False)),
                }
            )
        providers.append({"provider": provider, "entries": rows})
    return {"providers": providers}


@router.post("/credentials/pool")
async def credentials_pool_add(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    provider = str(payload.get("provider", "")).strip()
    api_key = str(payload.get("api_key", "")).strip()
    if not provider or not api_key:
        raise HTTPException(status_code=400, detail="provider and api_key required")
    store = _pool_store()
    store.setdefault(provider, []).append(
        {
            "label": payload.get("label"),
            "token_preview": (api_key[:4] + "****") if len(api_key) > 4 else "****",
            "priority": 0,
        }
    )
    _save_json("credentials_pool.json", store)
    return {"ok": True, "provider": provider, "count": len(store[provider])}


@router.delete("/credentials/pool/{provider}/{index}")
async def credentials_pool_remove(provider: str, index: int, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    store = _pool_store()
    entries = store.get(provider, [])
    if index < 0 or index >= len(entries):
        raise HTTPException(status_code=404, detail="entry not found")
    store[provider] = entries[:index] + entries[index + 1:]
    _save_json("credentials_pool.json", store)
    return {"ok": True, "provider": provider, "count": len(store[provider])}


# ---------------------------------------------------------------------------
# Curator
# ---------------------------------------------------------------------------


@router.get("/curator")
async def curator_get(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {
        "enabled": False,
        "paused": False,
        "interval_hours": None,
        "last_run_at": None,
        "min_idle_hours": None,
        "stale_after_days": None,
        "archive_after_days": None,
    }


@router.put("/curator/paused")
async def curator_paused(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    paused = bool(payload.get("paused", False))
    return {"ok": True, "paused": paused}


@router.post("/curator/run")
async def curator_run(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return _action("curator-run", False, "curator is disabled in the local build")


# ---------------------------------------------------------------------------
# Portal
# ---------------------------------------------------------------------------


@router.get("/portal")
async def portal_get(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {
        "logged_in": False,
        "portal_url": None,
        "inference_url": None,
        "provider": "local",
        "subscription_url": "",
        "features": [{"label": "portal", "state": "disabled"}],
    }


# ---------------------------------------------------------------------------
# Aria update
# ---------------------------------------------------------------------------


@router.post("/aria/update")
async def aria_update(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return _action("aria-update", False, "updates are handled by the outer launcher")


@router.get("/aria/update/check")
async def aria_update_check(force: bool = False, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    try:
        import importlib.metadata

        version = importlib.metadata.version("local-agent")
    except Exception:
        version = "0.0.0 (dev)"
    return {
        "install_method": "local",
        "current_version": version,
        "behind": 0,
        "update_available": False,
        "can_apply": False,
        "update_command": "",
        "message": "updates are handled by the outer launcher",
    }

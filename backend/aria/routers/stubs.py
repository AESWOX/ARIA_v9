"""Stub routers (§WORK_NOTES stubs phase).

Feature areas the local build does not wire up (OAuth, gateway, ops, dashboard
plugins, messaging, pairing, credential pool, curator, portal, aria update).

Rules (enforced by tests/test_stubs_honesty.py):
* GET routes return the contract-shaped empty/disabled structure.
* A mutating route either does the real thing, answers ok:false with a reason
  (HONEST_MUTATORS), or raises 501 via _not_implemented(). It never returns a
  success that did not happen.
"""
from __future__ import annotations

from aria import paths

import json
import os
import platform
from pathlib import Path
from typing import Any, NoReturn

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


def _not_implemented(feature: str) -> NoReturn:
    """Honest answer for a stub that has no real backend behind it.

    The UI used to show "saved" / "added" for these calls although nothing
    happened (and, for the credential pool, the API key was thrown away).
    501 makes fetchJSON throw, so the existing catch-blocks show a real error.
    """
    raise HTTPException(
        status_code=501,
        detail={
            "code": "not_implemented",
            "feature": feature,
            "message": f"{feature} is not implemented in this build",
        },
    )


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
    _not_implemented("OAuth disconnect")


@router.post("/providers/oauth/{provider_id}/start")
async def oauth_start(provider_id: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    _not_implemented("OAuth login")


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
    _not_implemented("config migration")


@router.post("/ops/debug-share")
async def ops_debug_share(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    _not_implemented("debug share")


# ---------------------------------------------------------------------------
# Dashboard plugins
# ---------------------------------------------------------------------------


@router.post("/dashboard/plugins/rescan")
async def plugins_rescan(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    _not_implemented("dashboard plugin rescan")


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
    _not_implemented("agent plugin enable")


@router.post("/dashboard/agent-plugins/{name}/disable")
async def agent_plugins_disable(name: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    _not_implemented("agent plugin disable")


@router.post("/dashboard/agent-plugins/{name}/update")
async def agent_plugins_update(name: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"ok": False, "name": name, "error": "agent plugins are not installed in the local build"}


@router.delete("/dashboard/agent-plugins/{name}")
async def agent_plugins_remove(name: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    _not_implemented("agent plugin remove")


@router.put("/dashboard/plugin-providers")
async def plugin_providers_put(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    _not_implemented("plugin provider selection")


@router.post("/dashboard/plugins/{name}/visibility")
async def plugins_visibility(name: str, payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    _not_implemented("plugin visibility")


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
    _not_implemented("messaging platform settings")


@router.post("/messaging/platforms/{platform_id}/test")
async def messaging_platform_test(platform_id: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"ok": False, "state": "not_configured", "message": "platform not configured in the local build"}


@router.post("/messaging/telegram/onboarding/start")
async def telegram_onboarding_start(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    _not_implemented("Telegram onboarding")


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
    _not_implemented("pairing approve")


@router.post("/pairing/revoke")
async def pairing_revoke(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    _not_implemented("pairing revoke")


@router.post("/pairing/clear-pending")
async def pairing_clear(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    _not_implemented("pairing clear")


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
    _not_implemented("credential pool add")


@router.delete("/credentials/pool/{provider}/{index}")
async def credentials_pool_remove(provider: str, index: int, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    _not_implemented("credential pool remove")


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
    _not_implemented("curator pause")


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

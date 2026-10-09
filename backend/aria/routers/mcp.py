"""routers/mcp.py — /mcp/servers, OAuth 2.1 (0009) и каталог (0010).

``GET /mcp/oauth/callback`` открыт без токена намеренно: в него приходит браузер после входа у
сервера авторизации. Защита — одноразовый ``state`` (32 байта, живёт 10 минут) и PKCE.
"""
from __future__ import annotations

import html
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from aria.api.auth import require_runtime_token
from aria.mcp import oauth
from aria.mcp.catalog import catalog_view, entry_to_server, find_entry
from aria.mcp.manager import get_manager, load_servers, save_servers, validate_server

router = APIRouter(tags=["mcp"])


@router.get("/mcp/servers")
async def mcp_list(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"servers": get_manager().view()}


@router.post("/mcp/servers")
async def mcp_create(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    try:
        entry = validate_server(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    servers = load_servers()
    if any(s.get("name") == entry["name"] for s in servers):
        raise HTTPException(status_code=409, detail="server already exists")
    servers.append(entry)
    save_servers(servers)
    return next(s for s in get_manager().view() if s["name"] == entry["name"])


@router.delete("/mcp/servers/{name}")
async def mcp_delete(name: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    await get_manager().disconnect(name)
    save_servers([s for s in load_servers() if s.get("name") != name])
    oauth.drop_record(name)  # токены удалённого сервера не должны пережить его удаление
    return {"ok": True}


@router.post("/mcp/servers/{name}/test")
async def mcp_test(name: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    mgr = get_manager()
    if mgr.get_config(name) is None:
        raise HTTPException(status_code=404, detail="server not found")
    return await mgr.test(name)


@router.put("/mcp/servers/{name}/enabled")
async def mcp_enabled(name: str, payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    servers = load_servers()
    server = next((s for s in servers if s.get("name") == name), None)
    if server is None:
        raise HTTPException(status_code=404, detail="server not found")
    server["enabled"] = bool(payload.get("enabled", True))
    save_servers(servers)
    if not server["enabled"]:
        await get_manager().disconnect(name)
    return {"ok": True, "name": name, "enabled": server["enabled"]}


# ── OAuth 2.1 (0009) ─────────────────────────────────────────────────────

def _page(title: str, message: str, ok: bool) -> HTMLResponse:
    body = (
        "<!doctype html><meta charset=utf-8><title>ARIA</title>"
        "<body style='font-family:system-ui;max-width:32rem;margin:4rem auto;padding:0 1rem'>"
        f"<h2>{html.escape(title)}</h2><p>{html.escape(message)}</p>"
        "<p>You can close this tab and return to ARIA.</p></body>"
    )
    return HTMLResponse(body, status_code=200 if ok else 400, headers={"Cache-Control": "no-store"})


@router.post("/mcp/servers/{name}/oauth/start")
async def mcp_oauth_start(name: str, request: Request, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    mgr = get_manager()
    cfg = mgr.get_config(name)
    if cfg is None:
        raise HTTPException(status_code=404, detail="server not found")
    if not cfg.get("url"):
        raise HTTPException(status_code=400, detail="OAuth applies to HTTP servers only")
    redirect_uri = f"{request.url.scheme}://{request.url.netloc}/mcp/oauth/callback"
    try:
        auth_url = await oauth.start_flow(name, cfg["url"], redirect_uri, mgr.challenge(name))
    except oauth.OAuthError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"auth_url": auth_url}


@router.get("/mcp/oauth/callback")
async def mcp_oauth_callback(
    state: str = "", code: str = "", error: str = "", error_description: str = ""
) -> HTMLResponse:
    if error:
        oauth.reject_flow(state)
        return _page("Authorization was not completed", error_description or error, ok=False)
    try:
        server = await oauth.complete_flow(state, code)
    except oauth.OAuthError as exc:
        return _page("Authorization failed", str(exc), ok=False)
    await get_manager().disconnect(server)  # следующее подключение возьмёт новый токен
    return _page("Connected", f"Server '{server}' is authorized.", ok=True)


@router.get("/mcp/servers/{name}/oauth")
async def mcp_oauth_status(name: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    if get_manager().get_config(name) is None:
        raise HTTPException(status_code=404, detail="server not found")
    return {"name": name, "status": oauth.status(name)}


@router.delete("/mcp/servers/{name}/oauth")
async def mcp_oauth_revoke(name: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    if get_manager().get_config(name) is None:
        raise HTTPException(status_code=404, detail="server not found")
    await get_manager().disconnect(name)
    return {"ok": True, **await oauth.revoke(name)}


# ── каталог (0010) ───────────────────────────────────────────────────────

@router.get("/mcp/catalog")
async def mcp_catalog(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"entries": catalog_view(load_servers()), "diagnostics": []}


@router.post("/mcp/catalog/install")
async def mcp_catalog_install(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    name = str(payload.get("name") or "")
    entry = find_entry(name)
    if entry is None:
        raise HTTPException(status_code=404, detail="catalog entry not found")
    env = payload.get("env") if isinstance(payload.get("env"), dict) else {}
    missing = [e["name"] for e in entry["required_env"] if e["required"] and not str(env.get(e["name"], "")).strip()]
    if missing:
        raise HTTPException(status_code=400, detail=f"missing required value: {', '.join(missing)}")
    servers = load_servers()
    if any(s.get("name") == name for s in servers):
        raise HTTPException(status_code=409, detail="server already exists")
    try:
        server = validate_server(entry_to_server(entry, env, enable=bool(payload.get("enable", True))))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    servers.append(server)
    save_servers(servers)
    return {"ok": True, "name": name, "background": False, "action": "catalog-install", "auth_type": entry["auth_type"]}

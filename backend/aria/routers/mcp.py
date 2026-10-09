"""routers/mcp.py — /mcp/servers: реальный клиент MCP вместо заглушек (H7).

Каталог (/mcp/catalog*) пока остаётся в stubs.py (патч 0009).
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from aria.api.auth import require_runtime_token
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

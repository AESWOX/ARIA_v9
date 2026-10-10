"""H8: /plugins — предпросмотр, импорт и удаление Agent Plugins (skills + MCP-серверы)."""
from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from aria import paths
from aria.api.auth import require_runtime_token
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import SkillStatus
from aria.mcp import oauth
from aria.mcp.manager import get_manager, load_servers, save_servers
from aria.plugins import importer
from aria.plugins.importer import PluginConflict, PluginError

router = APIRouter(tags=["plugins"])


def _load(source: Any) -> tuple[dict, Path]:
    """Синхронная часть (файлы/сеть), вызывается в потоке: разобрать плагин и вынести скиллы
    из временной папки в staging-каталог (его удаляет вызывающий)."""
    existing = {s.get("name") for s in load_servers()}
    base = Path(paths.data_dir())
    base.mkdir(parents=True, exist_ok=True)
    with importer.open_source(source) as root:
        parsed = importer.parse_plugin(root, existing_server_names=existing)
        staging = Path(tempfile.mkdtemp(prefix=".plugin_staging_", dir=base))
        try:
            for sk in parsed["skills"]:
                dest = staging / sk["name"]
                dest.mkdir()
                importer.stage_tree(Path(sk["_dir"]), dest)
                sk["_dir"] = str(dest)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return parsed, staging


def _remove_plugin(entry: dict) -> None:
    skills_root = paths.skills_dir()
    for name in entry.get("skills", []):
        d = skills_root / Path(name).name
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
        with session_scope() as db:
            row = repo.get_skill(db, name)
            if row is not None:
                row.status = SkillStatus.archived
    names = set(entry.get("servers", []))
    if names:
        save_servers([s for s in load_servers() if s.get("name") not in names])
        for n in names:
            oauth.drop_record(n)


@router.post("/plugins/preview")
async def plugins_preview(body: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    try:
        parsed, staging = await asyncio.to_thread(_load, body.get("source"))
    except PluginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    shutil.rmtree(staging, ignore_errors=True)
    registry = {e["name"] for e in importer.load_registry()}
    view = importer.public_view(parsed)
    view["already_installed"] = parsed["name"] in registry
    return view


@router.post("/plugins/import")
async def plugins_import(body: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    replace = bool(body.get("replace", False))
    source = body.get("source")
    registry = importer.load_registry()
    skills_root = paths.skills_dir()
    staging: Path | None = None
    try:
        parsed, staging = await asyncio.to_thread(_load, source)
        existing = next((e for e in registry if e["name"] == parsed["name"]), None)
        if existing is not None and not replace:
            raise PluginConflict("plugin already installed (pass replace=true to reinstall)")
        if existing is not None:
            # снять прежнюю установку и разобрать заново: имена её серверов теперь свободны
            _remove_plugin(existing)
            registry = [e for e in registry if e["name"] != parsed["name"]]
            shutil.rmtree(staging, ignore_errors=True)
            parsed, staging = await asyncio.to_thread(_load, source)
        servers_cfg = load_servers()
        taken = {s.get("name") for s in servers_cfg}
        for s in parsed["skills"]:
            if (skills_root / s["name"]).exists():
                raise PluginConflict(f"skill folder already exists: {s['name']}")
        for srv in parsed["_servers"]:
            if srv["name"] in taken:
                raise PluginConflict(f"MCP server name already exists: {srv['name']}")
    except PluginConflict as exc:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PluginError as exc:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        skills_root.mkdir(parents=True, exist_ok=True)
        for s in parsed["skills"]:
            importer.copy_skill(Path(s["_dir"]), skills_root, s["name"])
            with session_scope() as db:
                repo.upsert_skill(
                    db, s["name"], status=SkillStatus.active,
                    source_origin="plugin", created_by=f"plugin:{parsed['name']}",
                )
        if parsed["_servers"]:
            save_servers(servers_cfg + parsed["_servers"])
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    entry = {
        "name": parsed["name"], "title": parsed["title"], "version": parsed["version"],
        "license": parsed["license"], "source": importer.describe_source(source),
        "skills": [s["name"] for s in parsed["skills"]],
        "servers": [s["name"] for s in parsed["_servers"]],
        "installed_at": importer.now_iso(),
    }
    importer.save_registry(registry + [entry])
    return {"ok": True, **importer.public_view(parsed), "installed": entry}


@router.get("/plugins")
async def plugins_list(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"plugins": importer.load_registry()}


@router.delete("/plugins/{name}")
async def plugins_delete(name: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    registry = importer.load_registry()
    entry = next((e for e in registry if e["name"] == name), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="plugin not installed")
    for srv in entry.get("servers", []):
        await get_manager().disconnect(srv)
    _remove_plugin(entry)
    importer.save_registry([e for e in registry if e["name"] != name])
    return {"ok": True}

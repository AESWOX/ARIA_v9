"""Obsidian vault routes (moved from aria.main)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from aria.api.auth import require_runtime_token
from aria.storage import obsidian_vault, vault_index

router = APIRouter(tags=["vault"])


@router.get("/vault/tree")
async def get_vault_tree(subdir: str = Query(default=""), _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    payload = obsidian_vault.list_vault_tree(subdir=subdir)
    if "error" in payload:
        raise HTTPException(status_code=400, detail=payload["error"])
    return payload


@router.get("/vault/notes/{note_path:path}")
async def get_vault_note(note_path: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    try:
        payload = obsidian_vault.read_note_by_path(note_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not payload.get("found"):
        raise HTTPException(status_code=404, detail="note not found")
    return payload


@router.put("/vault/notes/{note_path:path}")
async def put_vault_note(note_path: str, payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    content = payload.get("content")
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="content must be a string")
    try:
        write_result = obsidian_vault.write_note_by_path(note_path, content)
        read_back = obsidian_vault.read_note_by_path(write_result["path"])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {**write_result, **{k: v for k, v in read_back.items() if k != "content"}}


@router.get("/vault/search")
async def search_vault(
    q: str = Query(..., min_length=1),
    max_results: int = Query(default=20, ge=1, le=100),
    tags: str = Query(default="", description="comma-separated; note must have ALL of them (nested tags match)"),
    folder: str = Query(default=""),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    try:
        only = vault_index.paths_for_tags(tags, folder=folder) if tags.strip() else None
        return obsidian_vault.search_vault(q, max_results=max_results, folder=folder, only_paths=only)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/vault/status")
async def vault_status(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return vault_index.status()


@router.get("/vault/discover")
async def vault_discover(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    """Vaults known to Obsidian itself (obsidian.json) + folders containing .obsidian."""
    return vault_index.discover()


@router.post("/vault/connect")
async def vault_connect(payload: dict[str, Any], request: Request, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    import os

    from aria.routers.env import _persist_env_var, _refresh_settings

    try:
        path = vault_index.validate_vault_path(str(payload.get("path") or ""))
    except ValueError as exc:
        raise _bad(exc) from exc
    if not path.exists():
        if not payload.get("create"):
            raise HTTPException(status_code=400, detail=f"folder not found: {path}")
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise HTTPException(status_code=400, detail=f"not a folder: {path}")
    initialized = vault_index.init_obsidian_dir(path) if payload.get("init_obsidian") else False
    os.environ["OBSIDIAN_VAULT_PATH"] = str(path)
    _persist_env_var("OBSIDIAN_VAULT_PATH", str(path))
    _refresh_settings(request)
    return {"ok": True, "initialized_obsidian": initialized, **vault_index.status()}


@router.get("/vault/structure")
async def vault_structure(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return vault_index.structure()


@router.get("/vault/tags")
async def vault_tags(folder: str = Query(default=""), _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    try:
        tags = vault_index.list_tags(folder)
    except ValueError as exc:
        raise _bad(exc) from exc
    return {"tags": tags, "total": len(tags)}


@router.get("/vault/tag-search")
async def vault_tag_search(
    tags: str = Query(..., min_length=1),
    mode: str = Query(default="any", pattern="^(any|all)$"),
    folder: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    try:
        return vault_index.search_by_tags(tags, mode=mode, folder=folder, limit=limit)
    except ValueError as exc:
        raise _bad(exc) from exc


@router.get("/vault/decisions")
async def vault_decisions(
    q: str = Query(default=""),
    folder: str = Query(default=""),
    tags: str = Query(default=""),
    limit: int = Query(default=50, ge=1, le=500),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    try:
        return vault_index.find_decisions(q, folder=folder, tags=tags, limit=limit)
    except ValueError as exc:
        raise _bad(exc) from exc


@router.post("/vault/decisions")
async def vault_log_decision(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    try:
        return vault_index.log_decision(
            str(payload.get("title") or ""), str(payload.get("decision") or ""),
            context=str(payload.get("context") or ""), branch=str(payload.get("branch") or ""),
            tags=payload.get("tags"), status=str(payload.get("status") or "accepted"),
        )
    except ValueError as exc:
        raise _bad(exc) from exc


@router.get("/vault/branches")
async def vault_branches(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    branches = vault_index.list_branches()
    return {"branches": branches, "total": len(branches)}


@router.post("/vault/branches")
async def vault_create_branch(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    try:
        return vault_index.create_branch(
            str(payload.get("name") or ""), description=str(payload.get("description") or ""), tags=payload.get("tags"),
        )
    except ValueError as exc:
        raise _bad(exc) from exc

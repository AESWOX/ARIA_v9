"""Obsidian vault routes (moved from aria.main)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from aria.api.auth import require_runtime_token
from aria.storage import obsidian_vault

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
async def search_vault(q: str = Query(..., min_length=1), max_results: int = Query(default=20, ge=1, le=100), _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return obsidian_vault.search_vault(q, max_results=max_results)

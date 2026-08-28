"""Managed-files routes: /api/files, /api/files/read, /api/files/mkdir, /api/files/upload-stream.

Serves a sandboxed file browser rooted at ``settings.agent_sandbox_root``
(default ``./data/sandbox``). All paths are relative POSIX-style and are
resolved against the root; traversal outside the root is rejected.
"""
from __future__ import annotations

import base64
import mimetypes
import os
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from aria.api.auth import require_runtime_token
from aria.config import get_settings

router = APIRouter(tags=["files"])


def _root() -> str:
    settings = get_settings()
    root = os.path.abspath(settings.agent_sandbox_root)
    os.makedirs(root, exist_ok=True)
    return root


def _resolve(rel_path: str) -> tuple[str, str]:
    """Return (absolute_path, normalized_rel_path) for a sandbox-relative path."""
    clean = rel_path.replace("\\", "/").strip("/")
    parts = [p for p in clean.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise HTTPException(status_code=400, detail="path traversal is not allowed")
    root = _root()
    abs_path = os.path.realpath(os.path.join(root, *parts))
    if not (abs_path == root or abs_path.startswith(root + os.sep)):
        raise HTTPException(status_code=400, detail="path escapes managed root")
    rel = "/".join(parts)
    return abs_path, rel


def _entry(abs_path: str, rel_path: str) -> dict[str, Any]:
    is_dir = os.path.isdir(abs_path)
    name = os.path.basename(rel_path) or os.path.basename(abs_path.rstrip("/")) or ""
    size: int | None = None
    mime_type: str | None = None
    if not is_dir:
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            size = None
        mime_type = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
    try:
        mtime = os.path.getmtime(abs_path)
    except OSError:
        mtime = 0.0
    return {
        "name": name,
        "path": rel_path,
        "is_directory": is_dir,
        "size": size,
        "mtime": mtime,
        "mime_type": mime_type,
    }


def _listing_context() -> dict[str, Any]:
    return {
        "root": _root(),
        "locked_root": None,
        "can_change_path": False,
    }


@router.get("/files")
async def list_files(path: str = Query(default=""), _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    abs_path, rel = _resolve(path)
    if not os.path.isdir(abs_path):
        raise HTTPException(status_code=404, detail=f"no such directory: {rel or '/'}")
    entries: list[dict[str, Any]] = []
    try:
        for name in sorted(os.listdir(abs_path)):
            child_abs = os.path.join(abs_path, name)
            child_rel = f"{rel}/{name}" if rel else name
            entries.append(_entry(child_abs, child_rel))
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot list directory: {exc}") from exc
    parent = rel.rsplit("/", 1)[0] if rel else None
    return {
        **_listing_context(),
        "path": rel,
        "parent": parent,
        "entries": entries,
    }


@router.get("/files/read")
async def read_file(path: str = Query(...), _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    abs_path, rel = _resolve(path)
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=404, detail=f"no such file: {rel}")
    try:
        with open(abs_path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot read file: {exc}") from exc
    mime = mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
    return {
        **_listing_context(),
        "name": os.path.basename(abs_path),
        "path": rel,
        "size": len(data),
        "mime_type": mime,
        "data_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
    }


@router.post("/files/mkdir")
async def create_directory(body: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    path = (body.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=422, detail="path is required")
    abs_path, rel = _resolve(path)
    if os.path.exists(abs_path):
        raise HTTPException(status_code=409, detail=f"already exists: {rel}")
    try:
        os.makedirs(abs_path, exist_ok=False)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot create directory: {exc}") from exc
    return {
        "ok": True,
        **_listing_context(),
        "path": rel,
        "entry": _entry(abs_path, rel),
    }


@router.post("/files/upload-stream")
async def upload_stream(
    file: UploadFile = File(...),
    path: str = Form(default=""),
    overwrite: str = Form(default="true"),
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    target = path.strip()
    if not target:
        raise HTTPException(status_code=422, detail="path is required")
    abs_path, rel = _resolve(target)
    if os.path.isdir(abs_path):
        raise HTTPException(status_code=409, detail=f"target is a directory: {rel}")
    if os.path.exists(abs_path) and overwrite.lower() not in ("1", "true", "yes", "on"):
        raise HTTPException(status_code=409, detail=f"already exists: {rel}")
    content = await file.read()
    try:
        os.makedirs(os.path.dirname(abs_path) or abs_path, exist_ok=True)
        with open(abs_path, "wb") as fh:
            fh.write(content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot write file: {exc}") from exc
    return {
        "ok": True,
        **_listing_context(),
        "path": rel,
        "entry": _entry(abs_path, rel),
    }


@router.delete("/files")
async def delete_file(body: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    path = (body.get("path") or "").strip()
    if not path:
        raise HTTPException(status_code=422, detail="path is required")
    recursive = bool(body.get("recursive", False))
    abs_path, rel = _resolve(path)
    if not os.path.lexists(abs_path):
        raise HTTPException(status_code=404, detail=f"no such path: {rel}")
    try:
        if os.path.isdir(abs_path):
            if not recursive:
                raise HTTPException(status_code=409, detail="directory not empty; pass recursive=true")
            import shutil
            shutil.rmtree(abs_path)
        else:
            os.remove(abs_path)
    except HTTPException:
        raise
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"cannot delete: {exc}") from exc
    return {"ok": True, "path": rel}

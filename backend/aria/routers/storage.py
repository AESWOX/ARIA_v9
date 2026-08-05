from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile

from aria.api.auth import token_store
from aria.storage import obsidian_vault
from aria.storage.b2_client import B2ConfigError, BackblazeB2Client

router = APIRouter(tags=["storage"])


async def require_runtime_token(x_local_agent_token: str | None = Header(default=None)) -> str:
    if not token_store.verify_token(x_local_agent_token):
        raise HTTPException(status_code=401, detail="invalid runtime token")
    return x_local_agent_token or ""


def save_to_vault_fallback(file_name: str, content: bytes) -> dict[str, Any]:
    asset = obsidian_vault.save_binary_asset(file_name=file_name, content=content, subdir=".assets")
    return {
        "storage": "vault",
        "file_name": asset["file_name"],
        "relative_url": asset["relative_url"],
        "url": asset["relative_url"],
        "path": asset["path"],
        "size": asset["size"],
    }


@router.post("/storage/b2/upload")
async def upload_to_b2(file: UploadFile = File(...), _: str = Header(default=""), x_local_agent_token: str | None = Header(default=None)) -> dict[str, Any]:
    if not token_store.verify_token(x_local_agent_token):
        raise HTTPException(status_code=401, detail="invalid runtime token")

    client = BackblazeB2Client()
    if not client.is_configured():
        raise HTTPException(status_code=503, detail="B2 storage is not configured")

    content = await file.read()
    try:
        uploaded = await client.upload_bytes(
            file_name=file.filename or "upload.bin",
            content=content,
            content_type=file.content_type or "application/octet-stream",
        )
    except B2ConfigError as exc:
        if str(exc) == "B2 storage is not configured":
            raise HTTPException(status_code=503, detail="B2 storage is not configured") from exc
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"b2 upload failed: {exc}") from exc

    return {
        "storage": "b2",
        "file_name": uploaded["file_name"],
        "key": uploaded["key"],
        "bucket_name": uploaded["bucket_name"],
        "public_url": uploaded["public_url"],
        "url": uploaded["public_url"],
        "size": uploaded["size"],
        "content_type": uploaded["content_type"],
    }


@router.post("/storage/vault/upload")
async def upload_to_vault(file: UploadFile = File(...), x_local_agent_token: str | None = Header(default=None)) -> dict[str, Any]:
    if not token_store.verify_token(x_local_agent_token):
        raise HTTPException(status_code=401, detail="invalid runtime token")

    content = await file.read()
    return save_to_vault_fallback(file.filename or "upload.bin", content)


# --- B2 read routes (moved from aria.main) ---

@router.get("/storage/b2/buckets")
async def get_b2_buckets(bucket_name: str | None = Query(default=None), _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    client = BackblazeB2Client(get_settings())
    try:
        buckets = await client.list_buckets(bucket_name=bucket_name)
    except B2ConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"b2 request failed: {exc}") from exc
    return {"items": buckets}


@router.get("/storage/b2/objects")
async def list_b2_objects(prefix: str = Query(default=""), bucket: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=1000), _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    client = BackblazeB2Client(get_settings())
    try:
        items = await client.list_objects(prefix=prefix, bucket_name=bucket, max_file_count=limit)
    except B2ConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"b2 request failed: {exc}") from exc
    return {"items": items, "prefix": prefix}


@router.get("/storage/b2/objects/{key:path}/meta")
async def get_b2_object_meta(key: str, bucket: str | None = Query(default=None), _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    client = BackblazeB2Client(get_settings())
    try:
        return await client.get_object_meta(key=key, bucket_name=bucket)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except B2ConfigError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"b2 request failed: {exc}") from exc

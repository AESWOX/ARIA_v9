from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

from aria.config import Settings, get_settings


class B2ConfigError(RuntimeError):
    pass


@dataclass(slots=True)
class B2Authorization:
    account_id: str
    api_url: str
    authorization_token: str
    download_url: str


class BackblazeB2Client:
    authorize_url = "https://api.backblazeb2.com/b2api/v2/b2_authorize_account"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def _require_credentials(self) -> tuple[str, str]:
        key_id = self.settings.B2_KEY_ID.strip()
        app_key = self.settings.B2_APPLICATION_KEY.strip()
        if not key_id or not app_key:
            raise B2ConfigError("B2 storage is not configured")
        return key_id, app_key

    def is_configured(self) -> bool:
        key_id = self.settings.B2_KEY_ID.strip()
        app_key = self.settings.B2_APPLICATION_KEY.strip()
        bucket = self.settings.B2_BUCKET.strip()
        return bool(key_id and app_key and bucket)

    async def _authorize(self) -> B2Authorization:
        key_id, app_key = self._require_credentials()
        raw = f"{key_id}:{app_key}".encode("utf-8")
        token = base64.b64encode(raw).decode("ascii")
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(self.authorize_url, headers={"Authorization": f"Basic {token}"})
            response.raise_for_status()
            data = response.json()
        return B2Authorization(
            account_id=data["accountId"],
            api_url=data["apiUrl"],
            authorization_token=data["authorizationToken"],
            download_url=data.get("downloadUrl", ""),
        )

    async def _call_api(self, auth: B2Authorization, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{auth.api_url}/b2api/v2/{endpoint}"
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(url, headers={"Authorization": auth.authorization_token}, json=payload)
            response.raise_for_status()
            return response.json()

    async def list_buckets(self, bucket_name: str | None = None) -> list[dict[str, Any]]:
        auth = await self._authorize()
        data = await self._call_api(auth, "b2_list_buckets", {"accountId": auth.account_id})
        target_name = (bucket_name or self.settings.B2_BUCKET or "").strip()
        rows: list[dict[str, Any]] = []
        for bucket in data.get("buckets", []):
            if target_name and bucket.get("bucketName") != target_name:
                continue
            rows.append(
                {
                    "bucket_id": bucket.get("bucketId"),
                    "bucket_name": bucket.get("bucketName"),
                    "bucket_type": bucket.get("bucketType"),
                    "revision": bucket.get("revision"),
                }
            )
        return rows

    async def _resolve_bucket(self, bucket_name: str | None = None) -> tuple[B2Authorization, dict[str, Any]]:
        target = (bucket_name or self.settings.B2_BUCKET or "").strip()
        if not target:
            raise B2ConfigError("B2 storage is not configured")
        auth = await self._authorize()
        data = await self._call_api(auth, "b2_list_buckets", {"accountId": auth.account_id})
        for bucket in data.get("buckets", []):
            if bucket.get("bucketName") == target:
                return auth, {
                    "bucket_id": bucket.get("bucketId"),
                    "bucket_name": bucket.get("bucketName"),
                    "bucket_type": bucket.get("bucketType"),
                    "revision": bucket.get("revision"),
                }
        raise B2ConfigError(f"B2 bucket not found: {target}")

    async def list_objects(self, prefix: str = "", bucket_name: str | None = None, max_file_count: int = 100) -> list[dict[str, Any]]:
        auth, bucket = await self._resolve_bucket(bucket_name=bucket_name)
        payload = {
            "bucketId": bucket["bucket_id"],
            "maxFileCount": max_file_count,
        }
        if prefix:
            payload["prefix"] = prefix
        data = await self._call_api(auth, "b2_list_file_names", payload)
        rows: list[dict[str, Any]] = []
        for item in data.get("files", []):
            rows.append(
                {
                    "bucket_id": bucket["bucket_id"],
                    "bucket_name": bucket["bucket_name"],
                    "key": item.get("fileName"),
                    "file_id": item.get("fileId"),
                    "size": item.get("contentLength"),
                    "content_type": item.get("contentType"),
                    "upload_timestamp": item.get("uploadTimestamp"),
                    "action": item.get("action"),
                }
            )
        return rows

    async def get_object_meta(self, key: str, bucket_name: str | None = None) -> dict[str, Any]:
        objects = await self.list_objects(prefix=key, bucket_name=bucket_name, max_file_count=10)
        for item in objects:
            if item.get("key") == key:
                return item
        raise FileNotFoundError(f"B2 object not found: {key}")

    async def upload_bytes(
        self,
        *,
        file_name: str,
        content: bytes,
        content_type: str = "application/octet-stream",
        bucket_name: str | None = None,
        prefix: str = ".assets",
    ) -> dict[str, Any]:
        auth, bucket = await self._resolve_bucket(bucket_name=bucket_name)
        upload_meta = await self._call_api(auth, "b2_get_upload_url", {"bucketId": bucket["bucket_id"]})

        normalized_prefix = (prefix or "").strip().strip("/")
        normalized_name = PurePosixPath(file_name).name
        object_key = f"{normalized_prefix}/{normalized_name}" if normalized_prefix else normalized_name
        sha1 = hashlib.sha1(content).hexdigest()

        headers = {
            "Authorization": upload_meta["authorizationToken"],
            "X-Bz-File-Name": quote(object_key, safe="/._-()"),
            "Content-Type": content_type or "application/octet-stream",
            "X-Bz-Content-Sha1": sha1,
        }

        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(upload_meta["uploadUrl"], headers=headers, content=content)
            response.raise_for_status()
            data = response.json()

        download_url = auth.download_url.rstrip("/")
        public_url = f"{download_url}/file/{bucket['bucket_name']}/{quote(object_key, safe='/._-()')}" if download_url else ""

        return {
            "bucket_id": bucket["bucket_id"],
            "bucket_name": bucket["bucket_name"],
            "key": object_key,
            "file_id": data.get("fileId"),
            "file_name": normalized_name,
            "content_type": content_type or "application/octet-stream",
            "size": len(content),
            "public_url": public_url,
        }

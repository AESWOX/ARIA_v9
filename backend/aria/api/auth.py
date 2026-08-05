"""api/auth.py — runtime handshake между Tauri shell и backend sidecar."""
from __future__ import annotations

import json
import os
import secrets
import stat
from pathlib import Path

from fastapi import Header, HTTPException

from aria.config import get_settings


RUNTIME_TOKEN_ENV = "LOCAL_AGENT_RUNTIME_TOKEN"
DISABLE_BOOTSTRAP_WRITE_ENV = "LOCAL_AGENT_DISABLE_BOOTSTRAP_WRITE"


def generate_runtime_token() -> str:
    return secrets.token_urlsafe(32)


def generate_pin() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def write_bootstrap_file(runtime_token: str) -> Path:
    settings = get_settings()
    path = Path(settings.runtime_token_path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "backendBaseUrl": f"http://{settings.http_host}:{settings.http_port}",
        "runtimeToken": runtime_token,
        "pinRequired": True,
        "idleLockMinutes": settings.security_auto_lock_minutes,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


class RuntimeTokenStore:
    def __init__(self):
        self._token: str | None = None
        self._pin: str | None = None
        self._failed_attempts = 0

    def issue(self) -> tuple[str, str]:
        settings = get_settings()
        env_token = os.getenv(RUNTIME_TOKEN_ENV, "").strip()
        self._token = env_token or generate_runtime_token()
        self._pin = settings.LOCAL_AGENT_UI_PIN.strip() or generate_pin()
        self._failed_attempts = 0
        if os.getenv(DISABLE_BOOTSTRAP_WRITE_ENV, "0") != "1":
            write_bootstrap_file(self._token)
        return self._token, self._pin

    def verify_token(self, presented: str | None) -> bool:
        if self._token is None or presented is None:
            return False
        return secrets.compare_digest(self._token, presented)

    def current_token(self) -> str:
        """Expose the active runtime token so the backend can embed it into the
        served index.html (same-origin SPA mode). Called only after issue()."""
        return self._token or ""

    def verify_pin(self, presented: str | None) -> bool:
        if self._pin is None or presented is None:
            return False
        if self._failed_attempts >= 10:
            return False
        ok = secrets.compare_digest(self._pin, presented)
        self._failed_attempts = 0 if ok else self._failed_attempts + 1
        return ok


token_store = RuntimeTokenStore()


async def require_runtime_token(x_local_agent_token: str | None = Header(default=None)) -> str:
    """Shared runtime-token dependency for API routers (moved from aria.main)."""
    settings = get_settings()
    if settings.aria_dev_mode:
        if x_local_agent_token is None and settings.http_host in ("127.0.0.1", "localhost", "0.0.0.0"):
            return ""
    if not token_store.verify_token(x_local_agent_token):
        raise HTTPException(status_code=401, detail="invalid runtime token")
    return x_local_agent_token or ""

"""
notebook/client.py — Thin wrapper over notebooklm-py v0.7+.

Uses the new AuthTokens + NotebookClient API.
Every external call is wrapped in try/except that maps to provider_health
"degraded" status — never raises unhandled exceptions up the stack.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class NotebookResult:
    success: bool
    data: str = ""
    error: str = ""
    elapsed_sec: float = 0.0


class NotebookClient:
    """Minimal wrapper around notebooklm-py v0.7+ library.

    Uses the new AuthTokens + NotebookClient API.
    The underlying library is loaded lazily (import inside method).
    """

    def __init__(
        self,
        auth_path: str = "",
        auth_json_env: str = "NOTEBOOK_AUTH_JSON",
        timeout_sec: int = 30,
    ) -> None:
        self.auth_path = auth_path
        self.auth_json_env = auth_json_env
        self.timeout_sec = timeout_sec
        self._client: Any = None

    def _lazy_init(self) -> bool:
        """Import notebooklm-py v0.7+ and create a client instance."""
        try:
            import notebooklm
            import notebooklm.auth as nbauth

            if self.auth_path and Path(self.auth_path).exists():
                tokens = nbauth.AuthTokens.from_file(self.auth_path)
            else:
                import os
                json_str = os.environ.get(self.auth_json_env, "{}")
                import json
                tokens = nbauth.AuthTokens.from_dict(json.loads(json_str))

            self._client = notebooklm.NotebookLMClient(auth=tokens)
            return True
        except Exception as exc:
            logger.warning("NotebookClient lazy_init failed: %s", exc)
            return False

    def query(
        self, prompt: str, source_ids: list[str] | None = None
    ) -> NotebookResult:
        """Send a research query to the notebook service."""
        t0 = time.time()
        try:
            if self._client is None:
                ok = self._lazy_init()
                if not ok:
                    return NotebookResult(
                        success=False,
                        error="Client initialization failed",
                        elapsed_sec=time.time() - t0,
                    )

            kwargs: dict[str, Any] = {"prompt": prompt}
            if source_ids:
                kwargs["source_ids"] = source_ids

            resp = self._client.query(**kwargs)
            elapsed = time.time() - t0
            return NotebookResult(
                success=True,
                data=str(resp),
                elapsed_sec=elapsed,
            )
        except Exception as exc:
            elapsed = time.time() - t0
            logger.error("NotebookClient.query failed in %.1fs: %s", elapsed, exc)
            return NotebookResult(
                success=False,
                error=str(exc),
                elapsed_sec=elapsed,
            )

    def health(self) -> dict[str, Any]:
        """Lightweight health check."""
        try:
            if self._client is None:
                ok = self._lazy_init()
                if not ok:
                    return {"ok": False, "detail": "client init failed"}
            return {"ok": True, "detail": "healthy"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

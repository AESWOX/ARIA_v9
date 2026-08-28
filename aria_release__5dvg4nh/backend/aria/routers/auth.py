"""Auth routes: WebSocket upgrade tickets (gated-mode WS auth bridge).

POST /api/auth/ws-ticket mints a single-use, short-TTL ticket that the
frontend swaps for a WS upgrade (?ticket=) where cookie auth is impossible
on a WebSocket handshake. The ticket is consumed once by /api/ws.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from aria.api.auth import require_runtime_token, ticket_store

router = APIRouter(tags=["auth"])


@router.post("/auth/ws-ticket")
async def ws_ticket(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    ticket, ttl = ticket_store.issue()
    return {"ticket": ticket, "ttl_seconds": ttl}

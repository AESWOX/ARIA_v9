"""SEC-1: every non-public HTTP route requires the runtime token, proven per route.

The public list is explicit and tiny. Any new route without the shared dependency
fails the test with its method and path, before a request is sent to it.
"""
from __future__ import annotations

import asyncio
import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token, ticket_store, token_store
from aria.main import app
from aria.routers import webhooks as webhooks_mod

PROBE_ID = "00000000-0000-4000-8000-000000000000"

PUBLIC: dict[tuple[str, str], str] = {
    ("GET", "/health"): "liveness probe; health_payload carries no secrets",
    ("GET", "/status"): "same health_payload; used before the token handshake",
    ("GET", "/mcp/oauth/callback"): "provider OAuth redirect target (state + PKCE validate it)",
    ("POST", "/webhooks/in/{name}"): "the HMAC-signed body is the credential",
    ("GET", "/{full_path:path}"): "SPA shell",
}


def _http_routes(routes=None) -> list[APIRoute]:
    """FastAPI may keep included routers nested (``_IncludedRouter``) instead of flattening."""
    found: list[APIRoute] = []
    for route in app.routes if routes is None else routes:
        if isinstance(route, APIRoute):
            found.append(route)
        elif type(route).__name__ == "_IncludedRouter":
            found.extend(_http_routes(route.original_router.routes))
    return found


def _methods(route: APIRoute) -> list[str]:
    return sorted(route.methods - {"HEAD", "OPTIONS"})


def _dependencies(dependant):
    for dependency in dependant.dependencies:
        yield dependency.call
        yield from _dependencies(dependency)


def _is_protected(route: APIRoute) -> bool:
    return any(call is require_runtime_token for call in _dependencies(route.dependant))


def _probe_path(route: APIRoute) -> str:
    return re.sub(r"\{[^}]+\}", PROBE_ID, route.path)


def test_public_list_is_small_and_unchanged():
    assert set(PUBLIC) == {
        ("GET", "/health"),
        ("GET", "/status"),
        ("GET", "/mcp/oauth/callback"),
        ("POST", "/webhooks/in/{name}"),
        ("GET", "/{full_path:path}"),
    }


def test_every_non_public_route_has_the_shared_token_dependency():
    routes = _http_routes()
    assert len(routes) > 200, f"route walker saw only {len(routes)} routes"
    unprotected = [
        f"{method} {route.path}"
        for route in routes
        for method in _methods(route)
        if (method, route.path) not in PUBLIC and not _is_protected(route)
    ]
    assert unprotected == [], (
        "routes without the shared runtime-token dependency (SEC-1 S5, do not fix silently): "
        + ", ".join(unprotected)
    )


@pytest.mark.parametrize("headers", [{}, {"X-Local-Agent-Token": "wrong-token"}])
def test_non_public_routes_reject_missing_and_wrong_token(headers):
    client = TestClient(app)
    failures: list[str] = []
    for route in _http_routes():
        for method in _methods(route):
            if (method, route.path) in PUBLIC:
                continue
            if not _is_protected(route):
                continue
            kwargs = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
            response = client.request(method, _probe_path(route), headers=headers, **kwargs)
            if response.status_code not in (401, 403):
                failures.append(f"{method} {route.path} -> {response.status_code}")
    assert failures == [], "routes that did not reject the request: " + ", ".join(failures)


def test_public_routes_answer_without_a_token():
    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 200, health.text
    status = client.get("/status")
    assert status.status_code == 200, status.text
    callback = client.get("/mcp/oauth/callback")
    # Public by design: an unknown state gives an explanatory HTML page, not a token prompt.
    assert callback.status_code in (200, 400), callback.text
    assert "invalid runtime token" not in callback.text
    spa = client.get("/some-spa-route")
    assert spa.status_code == 200, spa.text


def test_shutdown_rejects_before_reaching_the_handler():
    assert TestClient(app).post("/system/shutdown").status_code == 401


def test_webhook_needs_a_valid_signature():
    webhooks_mod._save(
        {
            "enabled": True,
            "subscriptions": [
                {"name": "sec1-probe", "secret": "fake-secret", "events": ["ping"], "enabled": True}
            ],
        }
    )
    client = TestClient(app)
    body = b'{"event":"not-ping"}'

    assert client.post("/webhooks/in/sec1-probe", content=body).status_code == 401
    assert (
        client.post(
            "/webhooks/in/sec1-probe",
            content=body,
            headers={"X-Aria-Signature": webhooks_mod.sign("wrong-secret", body)},
        ).status_code
        == 401
    )
    accepted = client.post(
        "/webhooks/in/sec1-probe",
        content=body,
        headers={"X-Aria-Signature": webhooks_mod.sign("fake-secret", body)},
    )
    assert accepted.status_code == 202
    assert accepted.json() == {"ok": True, "ignored": True, "event": "not-ping"}


def _ws_messages(path_qs: str) -> list[dict]:
    path, _, query = path_qs.partition("?")
    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "http_version": "1.1",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query.encode(),
        "headers": [(b"host", b"testserver")],
        "client": ["testclient", 50000],
        "server": ["testserver", 80],
        "subprotocols": [],
    }
    sent: list[dict] = []

    async def run() -> None:
        first = [True]

        async def receive():
            if first[0]:
                first[0] = False
                return {"type": "websocket.connect"}
            return {"type": "websocket.disconnect", "code": 1000}

        async def send(message):
            sent.append(message)
            if message["type"] == "websocket.send":
                raise OSError("client disconnected")

        await app(scope, receive, send)

    asyncio.run(run())
    return sent


def _assert_rejected(sent: list[dict]) -> None:
    types = [message["type"] for message in sent]
    assert "websocket.accept" not in types, f"connection was accepted: {types}"
    closes = [message for message in sent if message["type"] == "websocket.close"]
    assert closes and closes[0].get("code") == 1008, sent


def test_websocket_without_ticket_is_rejected():
    _assert_rejected(_ws_messages("/ws"))


def test_websocket_ticket_is_single_use():
    ticket, _ttl = ticket_store.issue()
    first = [message["type"] for message in _ws_messages(f"/ws?ticket={ticket}")]
    assert "websocket.accept" in first, first
    _assert_rejected(_ws_messages(f"/ws?ticket={ticket}"))


def test_websocket_expired_ticket_is_rejected():
    original = ticket_store._ttl_seconds
    ticket_store._ttl_seconds = -1
    try:
        ticket, _ttl = ticket_store.issue()
    finally:
        ticket_store._ttl_seconds = original
    _assert_rejected(_ws_messages(f"/ws?ticket={ticket}"))


def test_storage_and_providers_use_the_shared_dependency():
    from aria.routers import providers as providers_mod
    from aria.routers import storage as storage_mod

    assert storage_mod.require_runtime_token is require_runtime_token
    assert providers_mod.require_runtime_token is require_runtime_token
    assert token_store.current_token() == token_store.current_token()

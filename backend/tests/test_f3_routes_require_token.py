"""F3: every route except an explicit allowlist rejects a request with no runtime token.

/health and /status are public on purpose: the Tauri shell polls /status before it holds a token.
They must not leak local paths or credentials (see test_public_health_does_not_leak_dsn).
"""
import io
import re

from fastapi.testclient import TestClient

from aria.main import app

METHODS = ("get", "post", "put", "delete", "patch")
PUBLIC = {("GET", "/health"), ("GET", "/status"), ("GET", "/mcp/oauth/callback")}  # callback: браузер без токена, защита — одноразовый state
MULTIPART = {("POST", "/storage/b2/upload"), ("POST", "/storage/vault/upload")}


def _routes():
    out = []
    for path, item in app.openapi()["paths"].items():
        out.extend((m.upper(), path) for m in METHODS if m in item)
    return sorted(out)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000000", path)


def _call(c, method, path, headers=None):
    url = _concrete(path)
    if (method, path) in MULTIPART:
        return c.request(method, url, files={"file": ("x.txt", io.BytesIO(b"x"))}, headers=headers)
    return c.request(method, url, json={} if method != "GET" else None, headers=headers)


def test_all_routes_require_token_except_allowlist():
    c = TestClient(app)
    routes = _routes()
    assert len(routes) > 150  # guard: the walk really covered the API
    open_routes = []
    for method, path in routes:
        if (method, path) in PUBLIC:
            continue
        if _call(c, method, path).status_code not in (401, 403):
            open_routes.append((method, path))
    assert not open_routes, f"routes reachable without a token: {open_routes}"


def test_bad_token_is_rejected_too():
    c = TestClient(app)
    for method, path in [("GET", "/system/self-test"), ("GET", "/chat/status"), ("POST", "/storage/vault/upload")]:
        r = _call(c, method, path, headers={"X-Local-Agent-Token": "wrong"})
        assert r.status_code in (401, 403), (method, path, r.status_code)


def test_public_health_does_not_leak_dsn():
    c = TestClient(app)
    for path in ("/health", "/status"):
        body = c.get(path).text
        assert "local_agent.db" not in body and "sqlite:///" not in body and "/tmp" not in body, path

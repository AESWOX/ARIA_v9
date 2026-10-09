"""0009/0010: OAuth 2.1 для MCP (discovery, DCR, PKCE, refresh, revoke) и встроенный каталог.

Серверы авторизации и MCP имитирует ``httpx.MockTransport``; живых сервисов тесты не трогают.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import stat
import sys
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria import paths
from aria.api.auth import require_runtime_token
from aria.mcp import manager as mcp_manager
from aria.mcp import oauth
from aria.mcp.client import HttpTransport, McpClient
from aria.mcp.manager import McpManager, validate_server
from aria.routers import mcp as mcp_router

MCP_URL = "https://mcp.example/mcp"
AS = "https://auth.example"


def _run(coro):
    return asyncio.run(coro)


class FakeAuthServer:
    """PRM + метаданные AS + DCR + token + revoke + MCP за Bearer."""

    def __init__(self, *, with_dcr=True, issuer=AS, expires_in=3600, rotate=True):
        self.with_dcr, self.issuer, self.expires_in, self.rotate = with_dcr, issuer, expires_in, rotate
        self.registrations: list[dict] = []
        self.token_forms: list[dict] = []
        self.revoked: list[dict] = []
        self.codes: dict[str, dict] = {}
        self.valid_access: set[str] = set()
        self.refresh_ok = True
        self.counter = 0
        self.mcp_auth_headers: list[str | None] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle), follow_redirects=False)

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith("https://mcp.example/.well-known/oauth-protected-resource"):
            return httpx.Response(200, json={"resource": MCP_URL, "authorization_servers": [AS]})
        if url == f"{AS}/.well-known/oauth-authorization-server":
            meta = {
                "issuer": self.issuer, "authorization_endpoint": f"{AS}/authorize", "token_endpoint": f"{AS}/token",
                "revocation_endpoint": f"{AS}/revoke", "code_challenge_methods_supported": ["S256"],
            }
            if self.with_dcr:
                meta["registration_endpoint"] = f"{AS}/register"
            return httpx.Response(200, json=meta)
        if url == f"{AS}/register":
            body = json.loads(request.content)
            self.registrations.append(body)
            return httpx.Response(201, json={"client_id": f"cid-{len(self.registrations)}"})
        if url == f"{AS}/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_forms.append(form)
            if form["grant_type"] == "authorization_code":
                info = self.codes.pop(form["code"], None)
                if info is None:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=").decode()
                if challenge != info["challenge"]:
                    return httpx.Response(400, json={"error": "invalid_grant"})
            elif form["grant_type"] == "refresh_token":
                if not self.refresh_ok:
                    return httpx.Response(400, json={"error": "invalid_grant"})
            self.counter += 1
            access = f"access-{self.counter}"
            self.valid_access = {access}
            out = {"access_token": access, "token_type": "Bearer", "expires_in": self.expires_in}
            if form["grant_type"] == "authorization_code" or self.rotate:
                out["refresh_token"] = f"refresh-{self.counter}"
            return httpx.Response(200, json=out)
        if url == f"{AS}/revoke":
            self.revoked.append({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
            return httpx.Response(200)
        if url == MCP_URL:
            auth = request.headers.get("authorization")
            self.mcp_auth_headers.append(auth)
            if not auth or auth.removeprefix("Bearer ") not in self.valid_access:
                return httpx.Response(401, headers={"www-authenticate": f'Bearer resource_metadata="https://mcp.example/.well-known/oauth-protected-resource"'})
            body = json.loads(request.content) if request.content else {}
            method = body.get("method")
            if method == "initialize":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "up"}}})
            if method == "tools/list":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": "find_jobs", "inputSchema": {"type": "object"}}]}})
            if method == "tools/call":
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": "ok"}]}})
            return httpx.Response(202)
        return httpx.Response(404)

    def authorize(self, auth_url: str) -> tuple[str, str]:
        """Вместо браузера: принять запрос, вернуть (state, code)."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(auth_url).query).items()}
        code = f"code-{len(self.codes) + 1}"
        self.codes[code] = {"challenge": q["code_challenge"]}
        return q["state"], code


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    fake = FakeAuthServer()
    monkeypatch.setattr(oauth, "_client_factory", fake.client)
    oauth._pending.clear()
    oauth._refresh_locks.clear()
    from aria.tools.registry import TOOL_REGISTRY
    for key in [k for k in TOOL_REGISTRY if k.startswith("mcp__up__")]:
        TOOL_REGISTRY.pop(key)
    return tmp_path, fake


def _connect(fake: FakeAuthServer, server="up") -> str:
    url = _run(oauth.start_flow(server, MCP_URL, "http://127.0.0.1:8765/mcp/oauth/callback"))
    state, code = fake.authorize(url)
    assert _run(oauth.complete_flow(state, code)) == server
    return url


# ── поток авторизации ────────────────────────────────────────────────────

def test_full_flow_registers_client_uses_pkce_and_resource_and_stores_tokens(env):
    tmp, fake = env
    url = _run(oauth.start_flow("up", MCP_URL, "http://127.0.0.1:8765/mcp/oauth/callback"))
    q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    assert url.startswith(f"{AS}/authorize?")
    assert q["response_type"] == "code" and q["code_challenge_method"] == "S256"
    assert q["resource"] == MCP_URL and q["client_id"] == "cid-1"
    assert q["redirect_uri"] == "http://127.0.0.1:8765/mcp/oauth/callback"
    assert fake.registrations[0]["token_endpoint_auth_method"] == "none"

    state, code = fake.authorize(url)
    assert _run(oauth.complete_flow(state, code)) == "up"
    form = fake.token_forms[0]
    assert form["client_id"] == "cid-1" and form["resource"] == MCP_URL and form["code_verifier"]
    assert oauth.access_token("up") == "access-1" and oauth.status("up") == "authorized"
    if os.name != "nt":
        assert stat.S_IMODE((tmp / "mcp_oauth.json").stat().st_mode) == 0o600


def test_state_is_single_use_and_unknown_state_rejected(env):
    _, fake = env
    url = _run(oauth.start_flow("up", MCP_URL, "http://127.0.0.1:8765/mcp/oauth/callback"))
    state, code = fake.authorize(url)
    _run(oauth.complete_flow(state, code))
    with pytest.raises(oauth.OAuthError):
        _run(oauth.complete_flow(state, code))
    with pytest.raises(oauth.OAuthError):
        _run(oauth.complete_flow("forged", "x"))


def test_expired_state_rejected(env):
    _, fake = env
    url = _run(oauth.start_flow("up", MCP_URL, "http://127.0.0.1:8765/mcp/oauth/callback"))
    state, code = fake.authorize(url)
    oauth._pending[state]["created"] -= oauth.STATE_TTL_SEC + 5
    with pytest.raises(oauth.OAuthError):
        _run(oauth.complete_flow(state, code))


def test_second_start_reuses_registered_client(env):
    _, fake = env
    _connect(fake)
    _run(oauth.start_flow("up", MCP_URL, "http://127.0.0.1:8765/mcp/oauth/callback"))
    assert len(fake.registrations) == 1


def test_discovery_uses_www_authenticate_resource_metadata(env):
    _, fake = env
    header = 'Bearer resource_metadata="https://mcp.example/.well-known/oauth-protected-resource"'
    info = _run(oauth.discover(MCP_URL, header, fake.client()))
    assert info["meta"]["token_endpoint"] == f"{AS}/token" and info["resource"] == MCP_URL


def test_no_dynamic_registration_is_a_clear_error(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    fake = FakeAuthServer(with_dcr=False)
    monkeypatch.setattr(oauth, "_client_factory", fake.client)
    with pytest.raises(oauth.OAuthError, match="dynamic client registration"):
        _run(oauth.start_flow("up", MCP_URL, "http://127.0.0.1:1/mcp/oauth/callback"))


def test_issuer_mismatch_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    fake = FakeAuthServer(issuer="https://evil.example")
    monkeypatch.setattr(oauth, "_client_factory", fake.client)
    with pytest.raises(oauth.OAuthError, match="issuer"):
        _run(oauth.start_flow("up", MCP_URL, "http://127.0.0.1:1/mcp/oauth/callback"))


def test_plain_http_endpoints_rejected_except_loopback():
    with pytest.raises(oauth.OAuthError):
        oauth._check_url("http://auth.example/token")
    with pytest.raises(oauth.OAuthError):
        oauth._check_url("https://user:pw@auth.example/token")
    assert oauth._check_url("http://127.0.0.1:9/cb")


# ── обновление и отзыв ───────────────────────────────────────────────────

def test_refresh_rotates_tokens_and_keeps_refresh_when_not_rotated(env):
    _, fake = env
    _connect(fake)
    assert _run(oauth.refresh("up")) is True
    assert oauth.access_token("up") == "access-2"
    assert fake.token_forms[-1]["grant_type"] == "refresh_token" and fake.token_forms[-1]["refresh_token"] == "refresh-1"
    fake.rotate = False
    assert _run(oauth.refresh("up")) is True
    assert get_refresh("up") == "refresh-2", "refresh_token сохраняется, если сервер новый не выдал"


def get_refresh(server):
    return (oauth.get_record(server).get("tokens") or {}).get("refresh_token")


def test_invalid_grant_wipes_tokens(env):
    _, fake = env
    _connect(fake)
    fake.refresh_ok = False
    assert _run(oauth.refresh("up")) is False
    assert oauth.status("up") == "none"


def test_ensure_fresh_refreshes_only_near_expiry(env):
    _, fake = env
    _connect(fake)
    _run(oauth.ensure_fresh("up"))
    assert len(fake.token_forms) == 1, "токен свежий — запросов нет"
    rec = oauth.get_record("up")
    rec["tokens"]["expires_at"] = time.time() + 5
    oauth._update_record("up", tokens=rec["tokens"])
    _run(oauth.ensure_fresh("up"))
    assert fake.token_forms[-1]["grant_type"] == "refresh_token"


def test_expired_token_is_not_returned(env):
    _, fake = env
    _connect(fake)
    rec = oauth.get_record("up")
    rec["tokens"]["expires_at"] = time.time() - 1
    oauth._update_record("up", tokens=rec["tokens"])
    assert oauth.access_token("up") is None and oauth.status("up") == "authorized"


def test_revoke_calls_endpoint_and_erases_everything(env):
    tmp, fake = env
    _connect(fake)
    res = _run(oauth.revoke("up"))
    assert res == {"revoked_remotely": True, "erased_locally": True}
    assert fake.revoked[0]["token"] == "refresh-1"
    assert oauth.get_record("up") == {} and oauth.status("up") == "none"
    assert "access-1" not in (tmp / "mcp_oauth.json").read_text(encoding="utf-8")


# ── транспорт и менеджер ─────────────────────────────────────────────────

def _patch_build(monkeypatch, fake):
    def build(cfg):
        name = cfg["name"]
        return McpClient(HttpTransport(cfg["url"], client=fake.client(), token_provider=lambda: oauth.access_token(name)))
    monkeypatch.setattr(McpManager, "_build_client", staticmethod(build))


def test_manager_without_token_reports_auth_required_then_works_after_authorize(env, monkeypatch):
    tmp, fake = env
    _patch_build(monkeypatch, fake)
    mgr = McpManager()
    mcp_manager.save_servers([validate_server({"name": "up", "url": MCP_URL, "auth": "oauth"})])
    res = _run(mgr.test("up"))
    assert res["ok"] is False and res["auth_required"] is True
    assert "resource_metadata" in mgr.challenge("up")
    _connect(fake)
    res = _run(mgr.test("up"))
    assert res["ok"] is True and res["tools"] == ["find_jobs"]
    assert fake.mcp_auth_headers[-1].startswith("Bearer access-")


def test_call_refreshes_once_on_401_and_retries(env, monkeypatch):
    _, fake = env
    _patch_build(monkeypatch, fake)
    mgr = McpManager()
    mcp_manager.save_servers([validate_server({"name": "up", "url": MCP_URL, "auth": "oauth", "read_tools": ["find_jobs"]})])
    _connect(fake)
    _run(mgr.refresh("up"))
    fake.valid_access = {"access-other"}  # сервер «забыл» выданный токен: нужен refresh
    out = _run(mgr.call("up", "find_jobs", {}))
    assert out["text"] == "ok"
    assert oauth.access_token("up") == "access-2"
    assert "mcp__up__find_jobs" in mgr._registered["up"], "тулы остаются в реестре после переподключения"


def test_call_without_refresh_possible_gives_actionable_error(env, monkeypatch):
    _, fake = env
    _patch_build(monkeypatch, fake)
    mgr = McpManager()
    mcp_manager.save_servers([validate_server({"name": "up", "url": MCP_URL, "auth": "oauth"})])
    _connect(fake)
    _run(mgr.refresh("up"))
    fake.valid_access = set()
    fake.refresh_ok = False
    with pytest.raises(Exception, match="Authorize"):
        _run(mgr.call("up", "find_jobs", {}))


# ── роутер ───────────────────────────────────────────────────────────────

def _app(monkeypatch, fake):
    monkeypatch.setattr(mcp_manager, "_manager", McpManager())
    app = FastAPI()
    app.include_router(mcp_router.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_router_oauth_roundtrip_hides_tokens_and_cleans_up_on_delete(env, monkeypatch):
    tmp, fake = env
    _patch_build(monkeypatch, fake)
    with _app(monkeypatch, fake) as c:
        assert c.post("/mcp/servers", json={"name": "up", "url": MCP_URL, "auth": "oauth"}).status_code == 200
        start = c.post("/mcp/servers/up/oauth/start")
        assert start.status_code == 200, start.text
        auth_url = start.json()["auth_url"]
        assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Fmcp%2Foauth%2Fcallback" in auth_url
        state, code = fake.authorize(auth_url)
        cb = c.get("/mcp/oauth/callback", params={"state": state, "code": code})
        assert cb.status_code == 200 and "authorized" in cb.text
        assert c.get("/mcp/servers/up/oauth").json() == {"name": "up", "status": "authorized"}
        listing = c.get("/mcp/servers").text
        assert "access-1" not in listing and "refresh-1" not in listing
        assert c.get("/mcp/servers").json()["servers"][0]["oauth"] == "authorized"
        assert c.delete("/mcp/servers/up").status_code == 200
        assert oauth.get_record("up") == {}


def test_router_callback_rejects_bad_state_and_escapes_html(env, monkeypatch):
    _, fake = env
    with _app(monkeypatch, fake) as c:
        r = c.get("/mcp/oauth/callback", params={"state": "nope", "code": "x"})
        assert r.status_code == 400
        r = c.get("/mcp/oauth/callback", params={"error": "access_denied", "error_description": "<script>alert(1)</script>"})
        assert r.status_code == 400 and "<script>" not in r.text and "&lt;script&gt;" in r.text


def test_router_oauth_errors(env, monkeypatch):
    _, fake = env
    with _app(monkeypatch, fake) as c:
        assert c.post("/mcp/servers/none/oauth/start").status_code == 404
        c.post("/mcp/servers", json={"name": "loc", "command": sys.executable})
        assert c.post("/mcp/servers/loc/oauth/start").status_code == 400
        assert c.delete("/mcp/servers/none/oauth").status_code == 404


def test_router_revoke(env, monkeypatch):
    _, fake = env
    with _app(monkeypatch, fake) as c:
        c.post("/mcp/servers", json={"name": "up", "url": MCP_URL, "auth": "oauth"})
        state, code = fake.authorize(c.post("/mcp/servers/up/oauth/start").json()["auth_url"])
        c.get("/mcp/oauth/callback", params={"state": state, "code": code})
        r = c.delete("/mcp/servers/up/oauth")
        assert r.json() == {"ok": True, "revoked_remotely": True, "erased_locally": True}
        assert c.get("/mcp/servers/up/oauth").json()["status"] == "none"


# ── каталог (0010) ───────────────────────────────────────────────────────

def test_catalog_lists_upwork_and_install_creates_oauth_server(env, monkeypatch):
    tmp, fake = env
    with _app(monkeypatch, fake) as c:
        entries = {e["name"]: e for e in c.get("/mcp/catalog").json()["entries"]}
        assert entries["upwork"]["auth_type"] == "oauth" and entries["upwork"]["url"] == "https://mcp.upwork.com/mcp"
        assert entries["upwork"]["installed"] is False
        r = c.post("/mcp/catalog/install", json={"name": "upwork", "env": {"EVIL": "x"}, "enable": True})
        assert r.status_code == 200 and r.json()["auth_type"] == "oauth"
        saved = json.loads((tmp / "mcp_servers.json").read_text(encoding="utf-8"))[0]
        assert saved["url"] == "https://mcp.upwork.com/mcp" and saved["auth"] == "oauth"
        assert "find_jobs" in saved["read_tools"] and saved["env"] == {}, "env вне required_env отбрасывается"
        assert "manage_proposals" not in saved["read_tools"], "пишущие тулы остаются под Approve"
        assert c.get("/mcp/catalog").json()["entries"][0]["installed"] is True
        assert c.post("/mcp/catalog/install", json={"name": "upwork"}).status_code == 409
        assert c.post("/mcp/catalog/install", json={"name": "nope"}).status_code == 404


def test_catalog_stdio_entry_installs_without_read_tools(env, monkeypatch):
    tmp, fake = env
    with _app(monkeypatch, fake) as c:
        assert c.post("/mcp/catalog/install", json={"name": "fetch", "enable": False}).status_code == 200
        saved = json.loads((tmp / "mcp_servers.json").read_text(encoding="utf-8"))[0]
        assert saved["command"] == "uvx" and saved["enabled"] is False and saved["read_tools"] == []


def test_router_refuses_non_loopback_redirect(env, monkeypatch):
    """Callback принимаем только на loopback: redirect_uri на чужой хост не должен уйти серверу авторизации."""
    _, fake = env
    monkeypatch.setattr(mcp_manager, "_manager", McpManager())
    app = FastAPI()
    app.include_router(mcp_router.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    with TestClient(app, base_url="http://evil.example") as c:
        c.post("/mcp/servers", json={"name": "up", "url": MCP_URL, "auth": "oauth"})
        assert c.post("/mcp/servers/up/oauth/start").status_code == 502

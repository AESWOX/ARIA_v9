"""POST /system/open-external: кнопки со ссылками в десктопе (window.open в WebView2 не работает)."""
from __future__ import annotations

import webbrowser

import pytest
from fastapi.testclient import TestClient

from aria.api.auth import token_store
from aria.main import app
from aria.routers.system import _external_url_ok


def _h() -> dict[str, str]:
    token, _pin = token_store.issue()
    return {"X-Local-Agent-Token": token}


@pytest.mark.parametrize("url", ["https://example.com/a?b=1", "http://127.0.0.1:8765/x", "https://mcp.upwork.com/oauth"])
def test_http_urls_are_allowed(url):
    assert _external_url_ok(url)


@pytest.mark.parametrize(
    "url",
    ["file:///C:/Windows/system32/calc.exe", "javascript:alert(1)", "calc.exe", "\\\\host\\share", "https://", "ms-msdt:/id", "", None, "https://a.com/\r\nx", "https://a.com/" + "x" * 5000],
)
def test_other_schemes_and_junk_are_rejected(url):
    assert not _external_url_ok(url)


def test_requires_token():
    c = TestClient(app)
    assert c.post("/system/open-external", json={"url": "https://example.com"}).status_code == 401


def test_opens_url_in_default_browser(monkeypatch):
    seen = []
    monkeypatch.setattr(webbrowser, "open", lambda url, new=0, autoraise=True: seen.append((url, new)) or True)
    monkeypatch.delenv("ARIA_SERVER_MODE", raising=False)
    r = TestClient(app).post("/system/open-external", json={"url": "https://example.com/pay"}, headers=_h())
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert seen == [("https://example.com/pay", 2)]


def test_rejects_non_web_url_without_launching(monkeypatch):
    seen = []
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: seen.append(a) or True)
    r = TestClient(app).post("/system/open-external", json={"url": "file:///etc/passwd"}, headers=_h())
    assert r.status_code == 400 and seen == []


def test_server_mode_has_no_local_browser(monkeypatch):
    seen = []
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: seen.append(a) or True)
    monkeypatch.setenv("ARIA_SERVER_MODE", "1")
    r = TestClient(app).post("/system/open-external", json={"url": "https://example.com"}, headers=_h())
    assert r.status_code == 409 and seen == []

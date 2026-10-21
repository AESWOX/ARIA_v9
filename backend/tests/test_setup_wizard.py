"""First-run wizard backend: key validation, model list, apply (no real network)."""
import os

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.llm import gemini_discovery
from aria.routers import env as env_mod
from aria.routers import setup as setup_mod

KEY = "AIzaSyTESTKEY1234567890"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(env_mod, "_ENV_FILE", tmp_path / ".env")
    app = FastAPI()
    app.include_router(setup_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "test"
    yield TestClient(app)
    for name in ("GEMINI_API_KEYS", "GEMINI_FLASH_MODELS", "GROQ_API_KEYS", "GROQ_MODELS", "DEEPSEEK_API_KEY", "DEEPSEEK_CHAT_MODEL"):
        os.environ.pop(name, None)
    get_settings.cache_clear()


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://x/models")
    return httpx.HTTPStatusError("err", request=req, response=httpx.Response(code, request=req))


def test_providers_listed(client):
    ids = {p["id"] for p in client.get("/setup/providers").json()}
    assert ids == {"gemini", "groq", "deepseek"}


def test_validate_ok_filters_non_chat_models(client, monkeypatch):
    monkeypatch.setattr(
        gemini_discovery, "list_models",
        lambda base, key, **kw: ["gemini-2.5-flash", "text-embedding-004", "whisper-large-v3", "gemini-2.5-flash"],
    )
    r = client.post("/setup/validate-key", json={"provider": "gemini", "key": KEY}).json()
    assert r == {"ok": True, "reason": None, "models": ["gemini-2.5-flash"]}


@pytest.mark.parametrize("code,reason", [(401, "invalid_key"), (403, "invalid_key"), (429, "rate_limited"), (500, "provider_error")])
def test_validate_maps_http_errors(client, monkeypatch, code, reason):
    def boom(*a, **kw):
        raise _status_error(code)

    monkeypatch.setattr(gemini_discovery, "list_models", boom)
    r = client.post("/setup/validate-key", json={"provider": "groq", "key": KEY}).json()
    assert r["ok"] is False and r["reason"] == reason and r["models"] == []


def test_validate_network_error(client, monkeypatch):
    def boom(*a, **kw):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(gemini_discovery, "list_models", boom)
    assert client.post("/setup/validate-key", json={"provider": "deepseek", "key": KEY}).json()["reason"] == "network"


def test_validate_rejects_bad_input(client):
    assert client.post("/setup/validate-key", json={"provider": "grok", "key": KEY}).status_code == 400
    assert client.post("/setup/validate-key", json={"provider": "gemini", "key": "short"}).status_code == 400
    assert client.post("/setup/validate-key", json={"provider": "gemini", "key": "bad key with spaces!!"}).status_code == 400


def test_key_never_goes_to_user_supplied_url(client, monkeypatch):
    seen = {}

    def fake(base, key, **kw):
        seen["base"] = base
        return ["m1"]

    monkeypatch.setattr(gemini_discovery, "list_models", fake)
    client.post("/setup/validate-key", json={"provider": "groq", "key": KEY, "base_url": "https://evil.example"})
    assert seen["base"] == get_settings().groq_base_url


def test_apply_persists_and_sets_env(client, tmp_path):
    r = client.post("/setup/apply", json={"provider": "groq", "key": KEY, "model": "openai/gpt-oss-20b"})
    assert r.json() == {"ok": True, "provider": "groq", "model": "openai/gpt-oss-20b"}
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert f"GROQ_API_KEYS={KEY}" in text and "GROQ_MODELS=openai/gpt-oss-20b" in text
    assert os.environ["GROQ_API_KEYS"] == KEY


def test_apply_rejects_bad_model_id(client):
    assert client.post("/setup/apply", json={"provider": "gemini", "key": KEY, "model": "bad model; rm -rf"}).status_code == 400


def test_response_never_echoes_key(client, monkeypatch):
    monkeypatch.setattr(gemini_discovery, "list_models", lambda *a, **k: ["m1"])
    assert KEY not in client.post("/setup/validate-key", json={"provider": "gemini", "key": KEY}).text
    assert KEY not in client.post("/setup/apply", json={"provider": "gemini", "key": KEY, "model": "m1"}).text

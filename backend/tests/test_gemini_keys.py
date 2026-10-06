"""Gemini free-tier behaviour: error classification, cooldowns, model config."""
import asyncio

import httpx
import pytest

from aria.llm.key_pool import KeyPool, NoAvailableKeys
from aria.llm.providers import openai_compatible as oc
from aria.llm.providers.base import ChatMessage

_REAL_CLIENT = httpx.AsyncClient  # captured before any monkeypatching
OK = {"choices": [{"message": {"content": "pong"}}], "usage": {}}


def _provider(keys, handler, monkeypatch):
    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return _REAL_CLIENT(*a, **kw)

    monkeypatch.setattr(oc.httpx, "AsyncClient", factory)
    pool = KeyPool(keys, name="t", cooldown_sec=60)
    prov = oc.OpenAICompatibleProvider(
        provider_id="g", provider_class="x", base_url="https://example.test/v1", model="m", key_pool=pool
    )
    return prov, pool


def _chat(prov):
    return asyncio.run(prov.chat([ChatMessage(role="user", content="ping")], [], 5))


def _by_key(responses):
    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["authorization"].removeprefix("Bearer ")
        status, body, headers = responses[key]
        return httpx.Response(status, json=body, headers=headers)

    return handler


def test_403_about_model_access_does_not_kill_the_key(monkeypatch):
    perm = {"error": {"code": 403, "message": "Model gemini-3.1-pro is not available on your plan", "status": "PERMISSION_DENIED"}}
    prov, pool = _provider(["k1", "k2"], _by_key({"k1": (403, perm, {}), "k2": (200, OK, {})}), monkeypatch)
    assert _chat(prov).text == "pong"
    st = pool.status()
    assert st["dead"] == 0 and st["cooling_down"] == 1  # k1 backs off, is NOT dead


def test_invalid_key_is_dead_on_400_401_403(monkeypatch):
    bad400 = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "details": [{"reason": "API_KEY_INVALID"}]}}
    bad403 = {"error": {"code": 403, "message": "Your API key was reported as leaked."}}
    for status, body in ((400, bad400), (403, bad403), (401, {"error": "unauthorized"})):
        prov, pool = _provider(["bad", "good"], _by_key({"bad": (status, body, {}), "good": (200, OK, {})}), monkeypatch)
        assert _chat(prov).text == "pong"
        assert pool.status()["dead"] == 1, status


def test_all_keys_forbidden_recovers_after_cooldown(monkeypatch):
    perm = {"error": {"message": "model not enabled", "status": "PERMISSION_DENIED"}}
    state = {"ok": False}

    def handler(request):
        return httpx.Response(200, json=OK) if state["ok"] else httpx.Response(403, json=perm)

    prov, pool = _provider(["k1", "k2"], handler, monkeypatch)
    with pytest.raises((NoAvailableKeys, httpx.HTTPStatusError)):
        _chat(prov)
    assert pool.status()["dead"] == 0  # nothing permanently lost
    state["ok"] = True
    for k in list(pool._cooldown_until):
        pool._cooldown_until[k] = 0.0  # cooldown elapsed
    assert _chat(prov).text == "pong"


def test_429_backoff_honours_retry_after_and_daily_quota(monkeypatch):
    prov, pool = _provider(["k1", "k2"], _by_key({"k1": (429, {"error": {"message": "slow down"}}, {"retry-after": "300"}), "k2": (200, OK, {})}), monkeypatch)
    _chat(prov)
    assert pool._cooldown_until["k1"] - __import__("time").monotonic() > 250

    daily = {"error": {"status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded for metric GenerateRequestsPerDayPerProjectPerModel-FreeTier"}}
    prov, pool = _provider(["k1", "k2"], _by_key({"k1": (429, daily, {}), "k2": (200, OK, {})}), monkeypatch)
    _chat(prov)
    assert pool._cooldown_until["k1"] - __import__("time").monotonic() > oc.DAILY_QUOTA_COOLDOWN_SEC - 5

    plain = {"error": {"status": "RESOURCE_EXHAUSTED", "message": "rate limit"}}
    prov, pool = _provider(["k1", "k2"], _by_key({"k1": (429, plain, {}), "k2": (200, OK, {})}), monkeypatch)
    _chat(prov)
    assert 50 < pool._cooldown_until["k1"] - __import__("time").monotonic() <= 60


def test_cooldown_never_shortens(monkeypatch):
    pool = KeyPool(["a"], name="t", cooldown_sec=60)
    pool.mark_rate_limited("a", 1000)
    long_until = pool._cooldown_until["a"]
    pool.mark_rate_limited("a", 5)
    assert pool._cooldown_until["a"] == long_until


def test_router_uses_free_tier_safe_models_and_overrides(monkeypatch):
    from aria import config
    from aria.llm.router import build_default_router

    def models():
        config.get_settings.cache_clear()
        r = build_default_router()
        return {p.provider_id: p.model for cls in r.providers_by_class.values() for p in cls if p.provider_id.startswith("gemini")}

    monkeypatch.setenv("GEMINI_API_KEYS", "k1,k2")
    monkeypatch.delenv("GEMINI_PRO_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_FLASH_MODEL", raising=False)
    try:
        default = models()
        assert default and set(default.values()) == {"gemini-2.5-flash"}  # no paid-only model by default
        monkeypatch.setenv("GEMINI_PRO_MODEL", "gemini-2.5-pro")
        monkeypatch.setenv("GEMINI_FLASH_MODEL", "gemini-3.5-flash")
        over = models()
        assert over["gemini-pro"] == "gemini-2.5-pro"
        assert over["gemini-pro-premium-fallback"] == "gemini-2.5-pro"
        assert over["gemini-flash"] == "gemini-3.5-flash"
    finally:
        monkeypatch.undo()
        config.get_settings.cache_clear()


def test_check_script_reports_per_key_status():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("check_gemini_keys", Path(__file__).parent.parent / "scripts" / "check_gemini_keys.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def handler(request: httpx.Request) -> httpx.Response:
        key = request.headers["authorization"].removeprefix("Bearer ")
        if key == "good-key-1234":
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "models/gemini-2.5-flash"}]})
            return httpx.Response(200, json=OK)
        return httpx.Response(403, json={"error": "nope"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        rep = mod.check(["good-key-1234", "bad-key-9999"], "https://example.test/v1", {"flash": "f", "pro": "p"}, c)
    assert rep["...1234"]["chat"] == {"flash": "OK", "pro": "OK"}
    assert rep["...1234"]["visible_models"] == ["gemini-2.5-flash"]
    assert rep["...9999"]["chat"]["flash"].startswith("HTTP 403")

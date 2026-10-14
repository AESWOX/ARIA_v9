"""P1 (Groq): автоподбор цепочек по списку моделей пула ключей вместо зашитого ID (был 404 на qwen/qwen3.6-27b)."""
from __future__ import annotations

import json
import time

import httpx
import pytest

from aria import config
from aria.config import Settings
from aria.llm import groq_discovery as gq
from aria.llm.router import build_default_router

MODELS = [
    "llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-120b", "openai/gpt-oss-20b",
    "qwen/qwen3-32b", "moonshotai/kimi-k2-instruct", "meta-llama/llama-4-scout-17b-16e-instruct",
    "whisper-large-v3", "whisper-large-v3-turbo", "meta-llama/llama-guard-4-12b", "playai-tts",
    "canopylabs/orpheus-v1-english", "groq/compound", "groq/compound-mini", "openai/gpt-oss-safeguard-20b", "allam-2-7b",
]


class Resp:
    def __init__(self, code=200, payload=None, text=""):
        self.status_code, self._p, self.text = code, payload or {}, text or json.dumps(payload or {})

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=httpx.Request("GET", "http://x"), response=httpx.Response(self.status_code))


class FakeHttp:
    def __init__(self, models=MODELS, bad_keys=(), probe=None):
        self.models, self.bad_keys, self.probe, self.calls = models, set(bad_keys), probe or {}, []

    def get(self, url, headers=None, timeout=None):
        key = headers["Authorization"].split()[-1]
        self.calls.append(("get", key))
        if key in self.bad_keys:
            return Resp(401)
        return Resp(200, {"data": [{"id": m} for m in self.models]})

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(("post", json["model"]))
        code, body = self.probe.get(json["model"], (200, ""))
        return Resp(code, {}, body)


def test_only_text_chat_models_are_kept():
    kept = {c.model for c in map(gq.classify, MODELS) if c}
    assert {"llama-3.3-70b-versatile", "llama-3.1-8b-instant", "openai/gpt-oss-120b", "qwen/qwen3-32b"} <= kept
    for bad in ("whisper", "guard", "tts", "orpheus", "compound", "safeguard", "allam"):
        assert not any(bad in m for m in kept), f"{bad} не должно попасть в цепочки"


def test_main_goes_strong_to_weak_and_fast_the_opposite():
    chains = gq.build_chains(MODELS)
    assert chains["main"][0] == "openai/gpt-oss-120b" and chains["main"][1] == "llama-3.3-70b-versatile"
    assert chains["fast"][0] == "llama-3.1-8b-instant" and chains["fast"][1] == "meta-llama/llama-4-scout-17b-16e-instruct" and chains["fast"][2] == "openai/gpt-oss-20b"
    assert len(chains["main"]) <= gq.MAX_CHAIN and len(chains["fast"]) <= gq.MAX_CHAIN


def test_size_parsing_does_not_confuse_versions():
    assert gq.classify("llama-3.3-70b-versatile").size == 70
    assert gq.classify("meta-llama/llama-4-scout-17b-16e-instruct").size == 17
    assert gq.classify("moonshotai/kimi-k2-instruct").size == gq.UNKNOWN_SIZE


@pytest.mark.parametrize("code,body,expected", [
    (200, "", "ok"),
    (404, "", "not_found"),
    (400, '{"error":{"code":"model_decommissioned"}}', "not_found"),
    (401, "", "forbidden"),
    (403, "", "forbidden"),
    (429, '{"error":"rate limit reached, try again in 2s"}', "rate_limited"),
    (503, "", "overloaded"),
])
def test_probe_classification(code, body, expected):
    assert gq.classify_probe(code, body) == expected


def test_dead_models_dropped_and_busy_models_moved_to_the_end():
    status = {"openai/gpt-oss-120b": "not_found", "llama-3.3-70b-versatile": "rate_limited", "qwen/qwen3-32b": "ok"}
    chains = gq.build_chains(MODELS, status)
    assert "openai/gpt-oss-120b" not in chains["main"] and "openai/gpt-oss-120b" not in chains["fast"]
    assert chains["main"].index("llama-3.3-70b-versatile") > chains["main"].index("qwen/qwen3-32b")
    assert chains["fast"].index("llama-3.3-70b-versatile") > chains["fast"].index("llama-3.1-8b-instant")


def test_discover_skips_bad_key_and_probes_with_good_one():
    http = FakeHttp(bad_keys={"k1"}, probe={"openai/gpt-oss-20b": (404, "")})
    result = gq.discover(["k1", "k2"], http=http, probe=True)
    assert ("get", "k1") in http.calls and ("get", "k2") in http.calls
    assert result["status"]["openai/gpt-oss-20b"] == "not_found"
    assert "openai/gpt-oss-20b" not in result["chains"]["fast"]


def test_discover_raises_when_every_key_fails():
    with pytest.raises(RuntimeError):
        gq.discover(["k1", "k2"], http=FakeHttp(bad_keys={"k1", "k2"}))


def test_refresh_caches_and_respects_age_and_key_change(tmp_path):
    http = FakeHttp()
    assert gq.refresh_if_stale(["k1"], data_dir=tmp_path, http=http) is True
    assert gq.cached_chain("main", tmp_path)[0] == "openai/gpt-oss-120b"
    n = len(http.calls)
    assert gq.refresh_if_stale(["k1"], data_dir=tmp_path, http=http) is False and len(http.calls) == n
    assert gq.refresh_if_stale(["other"], data_dir=tmp_path, http=http) is True, "смена ключей = обновить"
    cache = gq.load_cache(tmp_path)
    cache["fetched_at"] = time.time() - 13 * 3600
    gq.save_cache(cache, tmp_path)
    assert gq.refresh_if_stale(["other"], data_dir=tmp_path, http=http) is True


def test_refresh_never_raises_and_keeps_old_cache(tmp_path):
    gq.refresh_if_stale(["k1"], data_dir=tmp_path, http=FakeHttp())
    old = gq.cached_chain("main", tmp_path)
    cache = gq.load_cache(tmp_path)
    cache["fetched_at"] = 0
    gq.save_cache(cache, tmp_path)
    assert gq.refresh_if_stale(["k1"], data_dir=tmp_path, http=FakeHttp(bad_keys={"k1"})) is False
    assert gq.cached_chain("main", tmp_path) == old


def test_refresh_keeps_manual_probe_results(tmp_path):
    gq.save_cache(gq.discover(["k1"], probe=True, http=FakeHttp(probe={"openai/gpt-oss-120b": (404, "")})), tmp_path)
    cache = gq.load_cache(tmp_path)
    cache["fetched_at"] = 0
    gq.save_cache(cache, tmp_path)
    gq.refresh_if_stale(["k1"], data_dir=tmp_path, http=FakeHttp())
    assert "openai/gpt-oss-120b" not in gq.cached_chain("main", tmp_path)


@pytest.fixture()
def cache_in_data_dir():
    path = gq.cache_path()
    yield path
    path.unlink(missing_ok=True)


def test_settings_priority_explicit_then_cache_then_single(cache_in_data_dir):
    s = Settings(groq_models_auto=True, groq_models="")
    assert s.groq_main_chain == [s.groq_model], "нет кэша = одна запасная модель"
    gq.save_cache(gq.discover(["k1"], http=FakeHttp()))
    auto = Settings(groq_models_auto=True, groq_models="")
    assert auto.groq_main_chain[0] == "openai/gpt-oss-120b" and auto.groq_fast_chain[0] == "llama-3.1-8b-instant"
    explicit = Settings(groq_models_auto=True, groq_models="my-a,my-b")
    assert explicit.groq_main_chain == ["my-a", "my-b"] == explicit.groq_fast_chain
    off = Settings(groq_models_auto=False, groq_models="")
    assert off.groq_main_chain == [off.groq_model]


def _router(monkeypatch, **env):
    monkeypatch.setenv("GROQ_API_KEYS", "g1,g2")
    monkeypatch.delenv("GROQ_MODELS", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()
    try:
        return build_default_router()
    finally:
        config.get_settings.cache_clear()


def _groq(router):
    return {p.provider_id: p for cls in router.providers_by_class.values() for p in cls if p.provider_id.startswith("groq")}


def test_router_has_no_hardcoded_model_and_keeps_first_link_ids(monkeypatch):
    prov = _groq(_router(monkeypatch, GROQ_MODELS="m-a,m-b"))
    assert {"groq-llama-fast", "groq-llama-versatile", "groq-llama-versatile-premium-fallback", "groq-subagent-fast"} <= set(prov)
    assert all(p.model in ("m-a", "m-b") for p in prov.values())
    assert prov["groq-llama-fast"].model == "m-a" and prov["groq-llama-fast-m-b"].model == "m-b"
    assert not any("qwen3.6" in p.model for p in prov.values())


def test_router_one_pool_per_model_shared_between_classes(monkeypatch):
    prov = _groq(_router(monkeypatch, GROQ_MODELS="m-a,m-b"))
    assert prov["groq-llama-fast"].key_pool is prov["groq-llama-versatile"].key_pool is prov["groq-subagent-fast"].key_pool
    assert prov["groq-llama-fast"].key_pool is not prov["groq-llama-fast-m-b"].key_pool


def test_router_without_chain_uses_single_fallback_model(monkeypatch):
    prov = _groq(_router(monkeypatch, GROQ_MODEL="only-one"))
    assert {p.model for p in prov.values()} == {"only-one"}


def test_arabic_specialist_never_leads_the_fast_chain():
    assert gq.build_chains(MODELS)["fast"][0] != "allam-2-7b"
    assert gq.classify("allam-2-7b") is None

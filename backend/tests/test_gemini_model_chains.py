"""0021: цепочки моделей Gemini. У каждой модели своя квота на тех же ключах, исчерпанная модель отдаёт запрос следующей."""
import asyncio

import httpx
import pytest

from aria import config
from aria.llm import gemini_pools
from aria.llm.key_pool import KeyPool, NoAvailableKeys
from aria.llm.router import ProviderRouter, build_default_router


def _build(monkeypatch, **env):
    monkeypatch.setenv("GEMINI_API_KEYS", "k1,k2,k3")
    for name in ("GEMINI_FLASH_MODEL", "GEMINI_PRO_MODEL", "GEMINI_FLASH_MODELS", "GEMINI_PRO_MODELS", "COMPRESSION_MODELS", "COMPRESSION_MODEL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()
    try:
        return build_default_router()
    finally:
        config.get_settings.cache_clear()


def _gemini(router):
    return {p.provider_id: p for cls in router.providers_by_class.values() for p in cls if p.provider_id.startswith("gemini")}


def test_chain_parsing_dedups_keeps_order_and_falls_back_to_single_model():
    chain = config.Settings._model_chain
    assert chain(" a , b,a,,c ", "x") == ["a", "b", "c"]
    assert chain("", "x") == ["x"]
    assert chain(None, " x ") == ["x"]
    assert chain("", "") == []


def test_no_chain_vars_keeps_the_old_provider_set(monkeypatch):
    prov = _gemini(_build(monkeypatch, GEMINI_FLASH_MODEL="gemini-3.5-flash-lite", GEMINI_PRO_MODEL="gemini-3.1-flash-lite"))
    assert {k: v.model for k, v in prov.items() if k != "gemini-vision"} == {
        "gemini-flash": "gemini-3.5-flash-lite",
        "gemini-pro": "gemini-3.1-flash-lite",
        "gemini-pro-premium-fallback": "gemini-3.1-flash-lite",
    }


def test_chain_registers_providers_in_order_with_stable_first_ids(monkeypatch):
    r = _build(
        monkeypatch,
        GEMINI_FLASH_MODELS="gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash-lite",
        GEMINI_PRO_MODELS="gemini-3.1-flash-lite,gemini-2.5-flash",
    )
    free = [(p.provider_id, p.model) for p in r.providers_by_class["free_tier_reasoning"]]
    assert free == [
        ("gemini-flash", "gemini-3.5-flash-lite"),
        ("gemini-flash-3.1-flash-lite", "gemini-3.1-flash-lite"),
        ("gemini-flash-2.5-flash-lite", "gemini-2.5-flash-lite"),
    ]
    std = [(p.provider_id, p.model) for p in r.providers_by_class["standard_reasoning"]]
    assert std == [("gemini-pro", "gemini-3.1-flash-lite"), ("gemini-pro-2.5-flash", "gemini-2.5-flash")]
    prem = [p.model for p in r.providers_by_class["premium_reasoning"]]
    assert prem == ["gemini-3.1-flash-lite", "gemini-2.5-flash"]
    ids = [p.provider_id for cls in r.providers_by_class.values() for p in cls]
    assert len(ids) == len(set(ids)), "provider_id должны быть уникальны (cooldown и выбор модели идут по id)"


def test_one_pool_per_model_across_classes_and_vision(monkeypatch):
    monkeypatch.setenv("VISION_GEMINI_API_KEYS", "")
    r = _build(monkeypatch, GEMINI_FLASH_MODELS="m-a,m-b", GEMINI_PRO_MODELS="m-b,m-c")
    by_model: dict[str, set[int]] = {}
    for cls in r.providers_by_class.values():
        for p in cls:
            if p.provider_id.startswith("gemini"):
                by_model.setdefault(p.model, set()).add(id(p.key_pool))
    assert set(by_model) == {"m-a", "m-b", "m-c"}
    assert all(len(pools) == 1 for pools in by_model.values()), "одна модель = один пул на все классы и vision"
    assert len({next(iter(v)) for v in by_model.values()}) == 3, "разные модели = разные пулы"


def test_separate_vision_keys_get_their_own_pool(monkeypatch):
    monkeypatch.setenv("VISION_GEMINI_API_KEYS", "v1,v2")
    r = _build(monkeypatch, GEMINI_FLASH_MODEL="m-a")
    vision = r.providers_by_class["vision_multimodal"][0]
    flash = r.providers_by_class["free_tier_reasoning"][0]
    assert vision.model == flash.model == "m-a"
    assert vision.key_pool is not flash.key_pool


def test_rebuilding_the_router_resets_pool_state(monkeypatch):
    r1 = _build(monkeypatch, GEMINI_FLASH_MODEL="m-a")
    for k in ("k1", "k2", "k3"):
        r1.providers_by_class["free_tier_reasoning"][0].key_pool.mark_rate_limited(k, 3600)
    r2 = _build(monkeypatch, GEMINI_FLASH_MODEL="m-a")
    assert r2.providers_by_class["free_tier_reasoning"][0].key_pool.next_key() in ("k1", "k2", "k3")


class _Stub:
    """Провайдер-пустышка: отвечает по сценарию, чтобы проверить переход по цепочке без сети."""

    def __init__(self, pid, cls, pool, behaviour):
        self.provider_id, self.provider_class, self.key_pool, self.behaviour, self.calls = pid, cls, pool, behaviour, 0

    async def check_connectivity(self, timeout):
        try:
            self.key_pool.next_key()
        except NoAvailableKeys:
            return False
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.calls += 1
        return self.behaviour(self)


def _http(status):
    return httpx.HTTPStatusError("x", request=httpx.Request("POST", "http://x"), response=httpx.Response(status, request=httpx.Request("POST", "http://x")))


def test_exhausted_model_hands_over_to_next_model_in_chain(monkeypatch):
    class R(ProviderRouter):
        def _record_provider_status(self, *a, **k):
            pass

        def budget_gate(self, cls):
            return True, False

    router = R()
    pool_a, pool_b = KeyPool(["k1", "k2"], name="a"), KeyPool(["k1", "k2"], name="b")

    def exhausted(p):
        for k in ("k1", "k2"):
            p.key_pool.mark_rate_limited(k, 3600)
        raise _http(429)

    a = _Stub("gemini-flash", "free_tier_reasoning", pool_a, exhausted)
    b = _Stub("gemini-flash-b", "free_tier_reasoning", pool_b, lambda p: "answer-from-b")
    router.register(a)
    router.register(b)

    class S:
        providers_connectivity_timeout_sec = 1

    monkeypatch.setattr("aria.llm.router.get_settings", lambda: S())
    res = asyncio.run(router.route_chat("free_tier_reasoning", [], [], resilient=True))
    assert res.response == "answer-from-b" and res.provider_id == "gemini-flash-b" and res.fallback
    # следующий запрос не тратит ни одного обращения к исчерпанной модели
    res2 = asyncio.run(router.route_chat("free_tier_reasoning", [], [], resilient=True))
    assert res2.provider_id == "gemini-flash-b" and a.calls == 1


def test_pool_error_tells_when_the_first_key_frees_up():
    pool = KeyPool(["k1", "k2"], name="t")
    pool.mark_rate_limited("k1", 3600)
    pool.mark_rate_limited("k2", 600)
    with pytest.raises(NoAvailableKeys, match=r"frees up in ~6\d\d?s"):
        pool.next_key()
    assert 590 <= pool.status()["retry_in_sec"] <= 600
    pool.mark_dead("k2")
    pool.mark_dead("k1")
    with pytest.raises(NoAvailableKeys, match="all dead until restart"):
        pool.next_key()


def test_compression_walks_the_model_chain_and_uses_retry_delay(monkeypatch):
    from unittest.mock import patch

    from aria.llm import compression

    monkeypatch.setenv("GEMINI_API_KEYS", "k1,k2")
    monkeypatch.setenv("COMPRESSION_MODELS", "m-a,m-b")
    config.get_settings.cache_clear()
    gemini_pools.reset()
    body = '{"error":{"details":[{"retryDelay":"64600s"}]}}'
    seen = []

    def fake_post(url, headers=None, json=None):
        seen.append(json["model"])
        req = httpx.Request("POST", url)
        if json["model"] == "m-a":
            return httpx.Response(429, text=body, request=req)
        return httpx.Response(200, json={"choices": [{"message": {"content": "sum"}}]}, request=req)

    async def post(url, headers=None, json=None):
        return fake_post(url, headers, json)

    try:
        with patch("httpx.AsyncClient") as cls:
            cls.return_value.__aenter__.return_value.post = post
            assert asyncio.run(compression._summarize("dialog")) == "sum"
            assert asyncio.run(compression._summarize("dialog")) == "sum"
        # во втором вызове исчерпанная m-a уже не трогается: cooldown по retryDelay, а не 60 с
        assert seen == ["m-a", "m-a", "m-b", "m-b"]
        pool_a = compression._get_compression_pool("m-a")
        assert pool_a.status()["retry_in_sec"] > 64000
    finally:
        config.get_settings.cache_clear()
        gemini_pools.reset()


# ── 0022: любая ошибка ведёт дальше по цепочке, пока не погаснет всё ──

def _chain_router(*providers):
    class R(ProviderRouter):
        def _record_provider_status(self, *a, **k):
            pass

        def budget_gate(self, cls):
            return True, False

    r = R()
    for p in providers:
        r.register(p)
    return r


@pytest.fixture
def _fast_settings(monkeypatch):
    class S:
        providers_connectivity_timeout_sec = 1

    monkeypatch.setattr("aria.llm.router.get_settings", lambda: S())


def test_every_kind_of_failure_goes_on_to_the_next_model(_fast_settings):
    steps = [
        lambda p: (_ for _ in ()).throw(_http(429)),
        lambda p: (_ for _ in ()).throw(_http(400)),
        lambda p: (_ for _ in ()).throw(_http(401)),
        lambda p: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),
        lambda p: (_ for _ in ()).throw(KeyError("choices")),  # пустой/битый ответ
        lambda p: "alive",
    ]
    provs = [_Stub(f"m{i}", "free_tier_reasoning", KeyPool(["k"], name=f"p{i}"), fn) for i, fn in enumerate(steps)]
    res = asyncio.run(_chain_router(*provs).route_chat("free_tier_reasoning", [], [], resilient=True))
    assert res.response == "alive" and res.provider_id == "m5" and res.fallback
    assert [p.calls for p in provs] == [1] * 6


def test_when_everything_is_down_error_lists_all_and_a_bad_response_becomes_unavailable(_fast_settings):
    from aria.llm.router import ProviderUnavailable, tried_suffix

    a = _Stub("m0", "free_tier_reasoning", KeyPool(["k"], name="a"), lambda p: (_ for _ in ()).throw(_http(429)))
    b = _Stub("m1", "free_tier_reasoning", KeyPool(["k"], name="b"), lambda p: (_ for _ in ()).throw(KeyError("choices")))
    with pytest.raises(ProviderUnavailable) as ei:
        asyncio.run(_chain_router(a, b).route_chat("free_tier_reasoning", [], [], resilient=True))
    text = str(ei.value)
    assert "m0 HTTP 429" in text and "m1 bad response (KeyError)" in text
    assert tried_suffix(ei.value).startswith(" (tried:")


def test_pool_exhausted_providers_are_named_with_wait_time_in_the_final_error(_fast_settings):
    from aria.llm.router import ProviderUnavailable

    pool = KeyPool(["k1", "k2"], name="x")
    pool.mark_rate_limited("k1", 64600)
    pool.mark_rate_limited("k2", 64600)
    dead = _Stub("gemini-flash", "free_tier_reasoning", pool, lambda p: "never")
    with pytest.raises(ProviderUnavailable, match=r"gemini-flash no usable key \(first frees up in ~646\d\ds\)"):
        asyncio.run(_chain_router(dead).route_chat("free_tier_reasoning", [], [], resilient=True))
    assert dead.calls == 0


def test_a_provider_that_failed_with_400_goes_to_the_back_of_the_queue(_fast_settings):
    from aria.llm.router import OTHER_ERROR_COOLDOWN_SEC

    bad = _Stub("m0", "free_tier_reasoning", KeyPool(["k"], name="a"), lambda p: (_ for _ in ()).throw(_http(400)))
    good = _Stub("m1", "free_tier_reasoning", KeyPool(["k"], name="b"), lambda p: "ok")
    r = _chain_router(bad, good)
    now = [100.0]
    r.clock = lambda: now[0]
    asyncio.run(r.route_chat("free_tier_reasoning", [], [], resilient=True))
    asyncio.run(r.route_chat("free_tier_reasoning", [], [], resilient=True))
    assert bad.calls == 1 and good.calls == 2
    now[0] += OTHER_ERROR_COOLDOWN_SEC + 1
    asyncio.run(r.route_chat("free_tier_reasoning", [], [], resilient=True))
    assert bad.calls == 2

"""PERF-1: the chat must fail over fast instead of walking dead providers and keys."""
from __future__ import annotations

import types

import httpx

from aria.core.runprofile import chat_target
from aria.llm.key_pool import KeyPool
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.providers.openai_compatible import (
    MAX_404_RETRIES,
    MAX_KEY_ATTEMPTS_PER_CALL,
    OpenAICompatibleProvider,
)
from aria.llm.router import (
    BREAKER_COOLDOWN_SEC,
    BREAKER_THRESHOLD,
    RETRY_ATTEMPTS_PER_PROVIDER,
    ProviderRouter,
)
from aria.routers import chat as chat_router


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://x/chat/completions")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


class Fake(LlmProvider):
    def __init__(self, pid, cls, script):
        self.provider_id, self.provider_class = pid, cls
        self.script = list(script)
        self.calls = 0
        self.checks = 0

    async def check_connectivity(self, timeout_sec):
        self.checks += 1
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.calls += 1
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return LlmResponse(text=item)


def _router(*providers):
    r = ProviderRouter()
    now = [1000.0]
    r.clock = lambda: now[0]
    r._now = now
    for p in providers:
        r.register(p)
    return r


async def _route(r):
    return await r.route_chat(
        "free_tier_reasoning", [], [], resilient=True,
        fallback_classes=("standard_reasoning",), retry_backoff_sec=0,
    )


async def test_a_failing_provider_is_called_once_then_the_next_one_answers():
    assert RETRY_ATTEMPTS_PER_PROVIDER == 1
    g = Fake("gemini", "free_tier_reasoning", [_status_error(503)])
    q = Fake("groq", "free_tier_reasoning", ["q-ok"])
    r = _router(g, q)
    result = await _route(r)
    assert result.provider_id == "groq"
    assert g.calls == 1
    assert q.calls == 1


async def test_breaker_opens_after_three_consecutive_failures():
    a = Fake("a", "free_tier_reasoning", [_status_error(503)] * 6)
    b = Fake("b", "standard_reasoning", [_status_error(503)] * 6)
    r = _router(a, b)
    for _ in range(BREAKER_THRESHOLD):
        try:
            await _route(r)
        except Exception:
            pass
    assert r.cooldown_until["a"] - r.clock() >= BREAKER_COOLDOWN_SEC


async def test_key_pool_walk_is_capped_per_call():
    provider = OpenAICompatibleProvider(
        provider_id="gemini",
        provider_class="free_tier_reasoning",
        base_url="https://example.invalid/v1",
        model="gemini-flash",
        key_pool=KeyPool([f"key-{i}" for i in range(10)]),
    )
    assert provider._max_attempts() == MAX_KEY_ATTEMPTS_PER_CALL == 3

    single = OpenAICompatibleProvider(
        provider_id="deepseek",
        provider_class="standard_reasoning",
        base_url="https://example.invalid/v1",
        model="deepseek-chat",
        api_key="k",
    )
    assert single._max_attempts() == 1
    assert MAX_404_RETRIES == 1


async def test_connectivity_is_checked_once_per_provider():
    g = Fake("gemini", "free_tier_reasoning", ["ok"])
    r = _router(g)
    await _route(r)
    assert g.checks == 1


async def test_stream_picks_a_healthy_provider_over_a_cooling_one():
    cooling = Fake("gemini", "free_tier_reasoning", ["never"])
    healthy = Fake("groq", "free_tier_reasoning", ["ok"])
    router = _router(cooling, healthy)
    router.cooldown_until["gemini"] = router.clock() + 60
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(router=router)))
    prepared = types.SimpleNamespace(
        chat_prefer=None,
        chat_class="free_tier_reasoning",
        chat_fallback=("standard_reasoning",),
    )
    picked = chat_router._pick_stream_provider(request, prepared)
    assert picked is healthy


def test_mark_failed_puts_the_stream_provider_on_cooldown():
    g = Fake("gemini", "free_tier_reasoning", ["ok"])
    r = _router(g)
    r.mark_failed(g, 60)
    assert r.is_cooling(g)


def test_chat_timeouts_fit_the_first_token_budget():
    assert chat_router.CHAT_STREAM_TIMEOUT_SEC <= 8
    assert chat_router.CHAT_CALL_TIMEOUT_SEC <= 20


def test_default_chat_target_is_deepseek_first():
    assert chat_router.CHAT_PROVIDER_CLASS == "standard_reasoning"
    assert chat_router.CHAT_FALLBACK_CLASSES == ("free_tier_reasoning",)
    target = chat_target(
        None,
        None,
        chat_router.CHAT_PROVIDER_CLASS,
        chat_router.CHAT_FALLBACK_CLASSES,
    )
    assert target == ("standard_reasoning", ("free_tier_reasoning",), None)


def test_an_explicit_free_seat_still_wins_over_deepseek_first():
    cls, fallback, prefer = chat_target(
        {"main": {"tier": "free"}},
        None,
        chat_router.CHAT_PROVIDER_CLASS,
        chat_router.CHAT_FALLBACK_CLASSES,
    )
    assert (cls, prefer) == ("free_tier_reasoning", None)
    assert fallback == ("standard_reasoning",)


async def test_preferred_failure_is_flagged_as_fallback():
    preferred = Fake("deepseek-chat", "standard_reasoning", [_status_error(503)])
    backup = Fake("gemini-flash", "standard_reasoning", ["backup-ok"])
    r = _router(preferred, backup)
    result = await r.route_chat(
        "standard_reasoning", [], [], resilient=True,
        prefer_provider_id="deepseek-chat", retry_backoff_sec=0,
    )
    assert result.provider_id == "gemini-flash"
    assert result.response.text == "backup-ok"
    assert result.fallback is True
    assert backup.calls == 1
    assert preferred.calls >= 1


def test_a_cooling_provider_is_retried_after_the_cooldown():
    a = Fake("a", "free_tier_reasoning", ["ok"])
    b = Fake("b", "free_tier_reasoning", ["ok"])
    r = _router(a, b)
    r.mark_failed(a, 60)
    assert r.is_cooling(a)
    assert r._ordered([a, b]) == [b, a]
    r._now[0] += 61
    assert not r.is_cooling(a)
    assert r._ordered([a, b]) == [a, b]


def test_the_preferred_model_leads_the_stream_candidates():
    preferred = Fake("deepseek-chat", "standard_reasoning", ["ok"])
    other = Fake("gemini-flash", "free_tier_reasoning", ["ok"])
    router = _router(other, preferred)
    request = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(router=router)))
    prepared = types.SimpleNamespace(
        chat_prefer="deepseek-chat",
        chat_class="standard_reasoning",
        chat_fallback=("free_tier_reasoning",),
    )
    assert chat_router._stream_candidates(request, prepared)[0] is preferred

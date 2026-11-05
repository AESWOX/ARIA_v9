"""PERF-1: the chat must fail over fast instead of walking dead providers and keys."""
from __future__ import annotations

import types

import httpx

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


def test_chat_timeouts_fit_the_first_token_budget():
    assert chat_router.CHAT_STREAM_TIMEOUT_SEC <= 12
    assert chat_router.CHAT_CALL_TIMEOUT_SEC <= 20

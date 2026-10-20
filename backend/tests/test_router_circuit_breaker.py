"""Предохранитель провайдера: после 429 / 5xx / нет ключей / таймаута провайдер уходит в конец очереди
на короткое время, а не получает новую заведомо провальную попытку на каждый запрос (по логам: сотни
«HTTP N, retry N» и «switching provider» за три дня)."""
import httpx

from aria.llm.key_pool import NoAvailableKeys
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import (
    NO_KEYS_COOLDOWN_SEC,
    RATE_LIMIT_COOLDOWN_SEC,
    TIMEOUT_COOLDOWN_SEC,
    TRANSIENT_COOLDOWN_SEC,
    ProviderRouter,
)


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://x/chat/completions")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


class Fake(LlmProvider):
    def __init__(self, pid, cls, script):
        self.provider_id, self.provider_class = pid, cls
        self.script = list(script)
        self.calls = 0

    async def check_connectivity(self, timeout_sec):
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


async def test_after_429_next_request_skips_the_provider():
    g = Fake("gemini", "free_tier_reasoning", [_status_error(429), "g-ok"])
    q = Fake("groq", "free_tier_reasoning", ["q1", "q2"])
    r = _router(g, q)
    assert (await _route(r)).provider_id == "groq"
    res = await _route(r)
    assert res.provider_id == "groq" and res.fallback is False
    assert g.calls == 1, "gemini bit again right after a 429"
    assert r.cooldown_until["gemini"] == 1000.0 + RATE_LIMIT_COOLDOWN_SEC


async def test_after_persistent_503_next_request_skips_the_provider():
    g = Fake("gemini", "free_tier_reasoning", [_status_error(503)] * 2 + ["g-ok"])
    q = Fake("groq", "free_tier_reasoning", ["q1", "q2"])
    r = _router(g, q)
    await _route(r)
    await _route(r)
    assert g.calls == 2, "the 503 retries must not be repeated on every request"
    assert r.cooldown_until["gemini"] == 1000.0 + TRANSIENT_COOLDOWN_SEC


async def test_recovered_503_does_not_trigger_cooldown():
    g = Fake("gemini", "free_tier_reasoning", [_status_error(503), "ok"])
    r = _router(g)
    assert (await _route(r)).response.text == "ok"
    assert "gemini" not in r.cooldown_until


async def test_after_no_keys_next_request_skips_the_provider():
    g = Fake("gemini", "free_tier_reasoning", [NoAvailableKeys("all cooling")])
    q = Fake("groq", "free_tier_reasoning", ["q1", "q2"])
    r = _router(g, q)
    await _route(r)
    await _route(r)
    assert g.calls == 1
    assert r.cooldown_until["gemini"] == 1000.0 + NO_KEYS_COOLDOWN_SEC


async def test_after_timeout_next_request_skips_the_provider():
    g = Fake("deepseek-reasoner", "free_tier_reasoning", [httpx.ReadTimeout("slow")])
    q = Fake("groq", "free_tier_reasoning", ["q1", "q2"])
    r = _router(g, q)
    await _route(r)
    await _route(r)
    assert g.calls == 1, "a 60 s timeout must not be paid on every request"
    assert r.cooldown_until["deepseek-reasoner"] == 1000.0 + TIMEOUT_COOLDOWN_SEC


async def test_cooldown_expires():
    g = Fake("gemini", "free_tier_reasoning", [_status_error(429), "g-ok"])
    q = Fake("groq", "free_tier_reasoning", ["q1"])
    r = _router(g, q)
    await _route(r)
    r._now[0] += RATE_LIMIT_COOLDOWN_SEC + 1
    res = await _route(r)
    assert res.provider_id == "gemini" and g.calls == 2


async def test_lone_provider_is_still_tried_after_cooldown():
    """Карантин меняет порядок, а не отказывает: единственный провайдер пробуется снова."""
    g = Fake("gemini", "free_tier_reasoning", [_status_error(429), "ok"])
    r = _router(g)
    try:
        await _route(r)
    except httpx.HTTPStatusError:
        pass
    assert (await _route(r)).response.text == "ok"


async def test_success_clears_cooldown():
    g = Fake("gemini", "free_tier_reasoning", ["ok"])
    r = _router(g)
    r.cooldown_until["gemini"] = r.clock() + 500
    await _route(r)
    assert r.cooldown_until.get("gemini", 0.0) <= r.clock()

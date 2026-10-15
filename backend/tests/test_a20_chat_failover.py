"""A20: chat must survive a provider 503 (retry, then fail over to another provider)."""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.llm.key_pool import NoAvailableKeys
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter, ProviderUnavailable
from aria.routers import chat as chat_mod


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "https://x/chat/completions")
    return httpx.HTTPStatusError("boom", request=req, response=httpx.Response(code, request=req))


class Fake(LlmProvider):
    def __init__(self, pid, cls, script):
        self.provider_id, self.provider_class = pid, cls
        self.script = list(script)  # items: Exception or str
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
    for p in providers:
        r.register(p)
    return r


async def _route(r, **kw):
    return await r.route_chat(
        "free_tier_reasoning", [], [], resilient=True,
        fallback_classes=("standard_reasoning",), retry_backoff_sec=0, **kw,
    )


async def test_503_is_retried_on_same_provider():
    g = Fake("gemini", "free_tier_reasoning", [_status_error(503), "ok"])
    res = await _route(_router(g))
    assert res.response.text == "ok" and g.calls == 2 and res.provider_id == "gemini"


async def test_persistent_503_fails_over_to_next_provider_in_class():
    g = Fake("gemini", "free_tier_reasoning", [_status_error(503)] * 2)
    q = Fake("groq", "free_tier_reasoning", ["from groq"])
    res = await _route(_router(g, q))
    assert res.provider_id == "groq" and res.fallback is True and g.calls == 2


async def test_falls_back_to_deepseek_class():
    g = Fake("gemini", "free_tier_reasoning", [_status_error(503)] * 2)
    d = Fake("deepseek-chat", "standard_reasoning", ["from deepseek"])
    res = await _route(_router(g, d))
    assert res.provider_id == "deepseek-chat" and res.fallback is True


async def test_429_and_no_keys_switch_without_retry():
    g = Fake("gemini", "free_tier_reasoning", [_status_error(429)])
    k = Fake("gemini2", "free_tier_reasoning", [NoAvailableKeys("cooldown")])
    d = Fake("deepseek-chat", "standard_reasoning", ["ok"])
    res = await _route(_router(g, k, d))
    assert res.provider_id == "deepseek-chat" and g.calls == 1 and k.calls == 1


async def test_non_transient_error_moves_on_instead_of_stopping_the_chain():
    """0022: 400 у одного провайдера (схема тулов, параметры) не останавливает перебор: пробуем следующего."""
    g = Fake("gemini", "free_tier_reasoning", [_status_error(400)])
    d = Fake("deepseek-chat", "standard_reasoning", ["from deepseek"])
    res = await _route(_router(g, d))
    assert res.provider_id == "deepseek-chat" and res.fallback is True and g.calls == 1


async def test_when_every_provider_fails_the_last_error_is_raised_with_all_attempts():
    from aria.llm.router import tried_suffix

    g = Fake("gemini", "free_tier_reasoning", [_status_error(400)])
    d = Fake("deepseek-chat", "standard_reasoning", [_status_error(401)])
    with pytest.raises(httpx.HTTPStatusError) as ei:
        await _route(_router(g, d))
    assert "gemini HTTP 400" in tried_suffix(ei.value) and "deepseek-chat HTTP 401" in tried_suffix(ei.value)


async def test_everything_down_raises_last_error():
    g = Fake("gemini", "free_tier_reasoning", [_status_error(503)] * 2)
    d = Fake("deepseek-chat", "standard_reasoning", [_status_error(502)] * 2)
    with pytest.raises(httpx.HTTPStatusError):
        await _route(_router(g, d))


async def test_no_providers_raises_provider_unavailable():
    with pytest.raises(ProviderUnavailable):
        await _route(_router())


def test_chat_endpoint_survives_gemini_503(monkeypatch):
    """The exact 09.10 log: Gemini 503 -> POST /chat/.../send must not return 502."""
    g = Fake("gemini-flash", "free_tier_reasoning", [_status_error(503)] * 2)
    d = Fake("deepseek-chat", "standard_reasoning", ["answer from deepseek"])
    app = FastAPI()
    app.include_router(chat_mod.router)
    app.state.router = _router(g, d)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    monkeypatch.setattr("aria.llm.router.RETRY_BACKOFF_SEC", 0)
    # default backoff is bound at def time, so patch the instance method's default via asyncio.sleep
    async def _nosleep(_):
        return None
    monkeypatch.setattr("aria.llm.router.asyncio.sleep", _nosleep)
    c = TestClient(app)
    sid = c.post("/chat/sessions", json={}).json()["session_id"]
    r = c.post(f"/chat/sessions/{sid}/send", json={"content": "hi"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assistant"]["content"] == "answer from deepseek"
    assert body["provider_id"] == "deepseek-chat" and body["fallback"] is True


# ---- пустой ответ (200 OK без текста и без тулов) = отказ звена, а не успех (15.10, ctx 502) ----

async def test_empty_reply_fails_over_when_reject_empty():
    g = Fake("gemini", "free_tier_reasoning", [""])
    q = Fake("groq", "free_tier_reasoning", ["from groq"])
    res = await _route(_router(g, q), reject_empty=True)
    assert res.provider_id == "groq" and res.response.text == "from groq" and res.fallback is True and g.calls == 1


async def test_whitespace_reply_counts_as_empty():
    g = Fake("gemini", "free_tier_reasoning", ["  \n "])
    d = Fake("deepseek-chat", "standard_reasoning", ["from deepseek"])
    res = await _route(_router(g, d), reject_empty=True)
    assert res.provider_id == "deepseek-chat"


async def test_all_empty_raises_with_tried_list():
    g = Fake("gemini", "free_tier_reasoning", [""])
    q = Fake("groq", "free_tier_reasoning", [""])
    with pytest.raises(ProviderUnavailable) as ei:
        await _route(_router(g, q), reject_empty=True)
    from aria.llm.router import tried_suffix

    shown = tried_suffix(ei.value)
    assert "gemini empty reply" in shown and "groq empty reply" in shown


async def test_empty_reply_still_returned_by_default():
    # агент/цикл тулов зовёт без reject_empty: там пустой текст бывает законным концом хода
    g = Fake("gemini", "free_tier_reasoning", [""])
    res = await _route(_router(g))
    assert res.provider_id == "gemini" and res.response.text == ""


async def test_tool_call_without_text_is_not_empty():
    from aria.llm.providers.base import ToolCallRequest

    class ToolOnly(Fake):
        async def chat(self, messages, tools, timeout_sec):
            self.calls += 1
            return LlmResponse(text=None, tool_calls=[ToolCallRequest(tool_name="x", arguments={})])

    g = ToolOnly("gemini", "free_tier_reasoning", [])
    res = await _route(_router(g), reject_empty=True)
    assert res.provider_id == "gemini" and res.response.tool_calls

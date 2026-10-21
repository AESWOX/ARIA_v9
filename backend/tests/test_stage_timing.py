"""Замер времени по этапам ответа: без него ускорение идёт вслепую.

Этапы: provider_slot_wait, route_chat, provider_call, connectivity_check, compression_check/compression_run, audit.
Перцентили считаются по кольцевому буферу в памяти; эндпоинт GET /system/timings отдаёт p50/p95/max.
"""
import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from aria.api.auth import token_store
from aria.core import timing
from aria.llm import compression
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter
from aria.main import app


@pytest.fixture(autouse=True)
def _clean():
    timing.reset()
    yield
    timing.reset()


def test_percentile_nearest_rank():
    vals = list(range(1, 101))  # 1..100
    assert timing.percentile(vals, 50) == 50
    assert timing.percentile(vals, 95) == 95
    assert timing.percentile(vals, 100) == 100
    assert timing.percentile([7], 95) == 7
    assert timing.percentile([], 50) is None


def test_stage_records_duration_and_ok():
    with timing.stage("x"):
        pass
    s = timing.summary()["x"]
    assert s["count"] == 1 and s["errors"] == 0 and s["max_ms"] >= 0


def test_stage_records_error_and_reraises():
    with pytest.raises(ValueError):
        with timing.stage("boom"):
            raise ValueError("no")
    s = timing.summary()["boom"]
    assert s["count"] == 1 and s["errors"] == 1


def test_summary_percentiles_from_records():
    for ms in (10, 20, 30, 40, 1000):
        timing.record("m", ms)
    s = timing.summary()["m"]
    assert s["count"] == 5 and s["p50_ms"] == 30 and s["p95_ms"] == 1000 and s["max_ms"] == 1000


def test_buffer_is_bounded():
    for _ in range(timing.MAX_SAMPLES + 50):
        timing.record("b", 1)
    assert timing.summary()["b"]["count"] == timing.MAX_SAMPLES


def test_summary_window_filters_old_samples(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(timing, "_wall", lambda: now[0])
    timing.record("w", 5)
    now[0] += 600
    timing.record("w", 5)
    assert timing.summary()["w"]["count"] == 2
    assert timing.summary(window_sec=60)["w"]["count"] == 1


async def test_timed_decorator_async():
    @timing.timed("dec")
    async def f(a):
        return a + 1

    assert await f(1) == 2
    assert timing.summary()["dec"]["count"] == 1
    assert f.__name__ == "f"


# ── инструментирование ───────────────────────────────────────────────────

class Fake(LlmProvider):
    provider_id, provider_class = "fake", "free_tier_reasoning"

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        return LlmResponse(text="ok")


async def test_router_records_route_provider_call_and_connectivity():
    r = ProviderRouter()
    r.register(Fake())
    await r.route_chat("free_tier_reasoning", [], [], resilient=True, retry_backoff_sec=0)
    st = timing.summary()
    for name in ("provider_slot_wait", "route_chat", "provider_call", "connectivity_check"):
        assert name in st, f"stage {name} is not measured: {sorted(st)}"


async def test_failed_provider_call_is_counted_as_error():
    req = httpx.Request("POST", "https://x")

    class Bad(Fake):
        async def chat(self, messages, tools, timeout_sec):
            raise httpx.HTTPStatusError("x", request=req, response=httpx.Response(429, request=req))

    r = ProviderRouter()
    r.register(Bad())
    with pytest.raises(httpx.HTTPStatusError):
        await r.route_chat("free_tier_reasoning", [], [], resilient=True, retry_backoff_sec=0)
    assert timing.summary()["provider_call"]["errors"] == 1


async def test_compression_stages(monkeypatch):
    async def did(_sid):
        return True

    monkeypatch.setattr(compression, "_maybe_compress_impl", did)
    assert await compression.maybe_compress("s") is True
    st = timing.summary()
    assert st["compression_check"]["count"] == 1 and st["compression_run"]["count"] == 1

    async def nothing(_sid):
        return False

    monkeypatch.setattr(compression, "_maybe_compress_impl", nothing)
    assert await compression.maybe_compress("s") is False
    st = timing.summary()
    assert st["compression_check"]["count"] == 2 and st["compression_run"]["count"] == 1


def test_run_audit_is_measured():
    from aria.core.audit import run_audit

    assert asyncio.iscoroutinefunction(run_audit)
    assert hasattr(run_audit, "__wrapped__"), "run_audit is not wrapped by timing.timed('audit')"


# ── эндпоинт ─────────────────────────────────────────────────────────────

def test_timings_endpoint_requires_token_and_returns_summary():
    token, _ = token_store.issue()
    c = TestClient(app)
    assert c.get("/system/timings").status_code in (401, 403)
    timing.record("probe", 12)
    body = c.get("/system/timings", headers={"x-local-agent-token": token}).json()
    assert body["stages"]["probe"]["count"] == 1
    body = c.get("/system/timings?window_sec=60", headers={"x-local-agent-token": token}).json()
    assert body["window_sec"] == 60


# ── чат и аудитор: общее время и подэтапы ────────────────────────────────

def test_chat_send_records_total_prepare_store_and_provider():
    from tests.test_chat_api import FakeLlm, _cleanup, _client, _new_session

    c = _client(FakeLlm(answers=["ok"]))
    sid = _new_session(c)
    try:
        assert c.post(f"/chat/sessions/{sid}/send", json={"content": "ping"}).status_code == 200
    finally:
        _cleanup(sid)
    st = timing.summary()
    for name in ("chat_total", "chat_prepare", "chat_store", "provider_call", "route_chat"):
        assert st[name]["count"] == 1, name
    assert st["chat_total"]["errors"] == 0
    # общее время не меньше его частей
    assert st["chat_total"]["max_ms"] >= st["chat_prepare"]["max_ms"]
    assert st["chat_total"]["max_ms"] >= st["provider_call"]["max_ms"]


def test_chat_send_rejected_request_counts_as_total_error():
    from tests.test_chat_api import FakeLlm, _client

    c = _client(FakeLlm())
    r = c.post("/chat/sessions/00000000-0000-0000-0000-000000000000/send", json={"content": ""})
    assert r.status_code == 400
    st = timing.summary()
    assert st["chat_total"]["count"] == 1 and st["chat_total"]["errors"] == 1
    assert "chat_prepare" not in st  # до вызова модели дело не дошло


def test_audit_model_call_is_a_separate_stage():
    import inspect

    from aria.core import audit

    src = inspect.getsource(audit.run_audit)
    assert 'timing.stage("audit_model_call")' in src

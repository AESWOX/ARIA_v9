"""D-38: цена в шапке чата — алиасы старых id, ledger-сумма за сегодня, частичная цена сессии."""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.db import models as m
from aria.db.base import session_scope
from aria.llm import pricing
from aria.llm.usage import record_usage
from aria.routers import analytics as analytics_mod
from aria.routers import chat as chat_mod

OFF_PEAK = datetime(2026, 11, 7, 2, 0, tzinfo=timezone.utc)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(analytics_mod.router)
    app.include_router(chat_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    app.state.router = None
    return TestClient(app)


def _add_usage(session_id: str, cost: float | None, provider_id: str, model: str) -> None:
    with session_scope() as db:
        db.add(
            m.LlmUsage(
                session_id=uuid.UUID(session_id),
                provider_id=provider_id,
                model=model,
                purpose="chat",
                prompt_total=1000,
                completion=500,
                cost_usd=cost,
                tier="off" if cost is not None else "n/a",
            )
        )


def test_d0a_retired_model_ids_are_priced_like_flash():
    for model in ("deepseek-chat", "deepseek-reasoner"):
        cost, tier = pricing.cost_usd(model, 0, 1_000_000, 0, OFF_PEAK)
        assert tier == "off"
        assert cost == pytest.approx(0.15, abs=1e-9)


def test_d0b_analytics_today_sums_the_ledger(monkeypatch):
    monkeypatch.setattr(pricing, "_utcnow", lambda: OFF_PEAK)
    record_usage(
        provider_id="deepseek-chat",
        model="deepseek-chat",
        usage={
            "prompt_tokens": 16_000,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 16_000,
            "completion_tokens": 0,
        },
        purpose="chat",
    )

    body = _client().get("/analytics/usage", params={"days": 1}).json()

    today = body["daily"][-1]
    assert today["ledger_cost_usd"] == pytest.approx(0.0024, abs=1e-9)
    assert today["unpriced_calls"] == 0
    assert body["totals"]["ledger_cost_usd"] == pytest.approx(0.0024, abs=1e-9)
    assert body["totals"]["unpriced_calls"] == 0
    assert "total_estimated_cost" in body["totals"]


def test_d0b_unpriced_calls_are_counted_not_silently_zeroed():
    record_usage(
        provider_id="gemini-flash",
        model="gemini-2.5-flash",
        usage={"prompt_tokens": 100, "completion_tokens": 10},
        purpose="chat",
    )

    totals = _client().get("/analytics/usage", params={"days": 1}).json()["totals"]

    assert totals["ledger_cost_usd"] == 0.0
    assert totals["unpriced_calls"] == 1


def test_d0c_partial_session_cost_is_reported():
    client = _client()
    sid = client.post("/chat/sessions", json={}).json()["session_id"]
    _add_usage(sid, 0.001, "deepseek-chat", "deepseek-flash")
    _add_usage(sid, None, "gemini-flash", "gemini-2.5-flash")

    body = client.get(f"/chat/sessions/{sid}/usage").json()

    assert body["session_cost"] == pytest.approx(0.001)
    assert body["unpriced_messages"] == 1
    assert body["session_cost_partial"] is True


def test_d5_prices_are_read_from_the_frozen_bundle(monkeypatch, tmp_path):
    root = tmp_path / "bundle"
    target = root / "aria" / "llm" / "prices.json"
    target.parent.mkdir(parents=True)
    target.write_text(
        json.dumps({"models": {}, "aliases": {"probe-model": "deepseek-flash"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "_MEIPASS", str(root), raising=False)
    pricing.load_prices.cache_clear()
    try:
        assert pricing._prices_path() == target
        assert pricing.normalize_model("probe-model") == "deepseek-flash"
    finally:
        pricing.load_prices.cache_clear()


def test_d5_backend_spec_bundles_prices():
    spec = Path(__file__).resolve().parents[1] / "backend.spec"
    assert "aria/llm/prices.json" in spec.read_text(encoding="utf-8")

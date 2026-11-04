"""UI-2: /chat/sessions/{id}/usage carries the aggregates the session inspector shows."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from aria.api.auth import token_store
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.main import app


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _h():
    if not token_store.current_token():
        token_store.issue()
    return {"X-Local-Agent-Token": token_store.current_token()}


def _new_session() -> uuid.UUID:
    with session_scope() as db:
        return repo.create_session(db, "ui2 inspector").id


def _add_usage(
    sid: uuid.UUID,
    provider_id: str,
    model: str,
    prompt: int,
    completion: int,
    cost: float | None,
    hit: int | None = None,
    miss: int | None = None,
    reasoning: int | None = None,
) -> None:
    with session_scope() as db:
        db.add(
            m.LlmUsage(
                session_id=sid,
                provider_id=provider_id,
                model=model,
                purpose="chat",
                prompt_total=prompt,
                completion=completion,
                reasoning=reasoning,
                cost_usd=cost,
                prompt_hit=hit,
                prompt_miss=miss,
            )
        )


def test_usage_aggregates_tokens_calls_and_models(client):
    sid = _new_session()
    _add_usage(sid, "prov-a", "m-a", 100, 20, 0.01, hit=80, miss=20, reasoning=5)
    _add_usage(sid, "prov-a", "m-a", 50, 10, None, hit=40, miss=10)
    _add_usage(sid, "prov-b", "m-b", 30, 5, 0.002)

    body = client.get(f"/chat/sessions/{sid}/usage", headers=_h()).json()

    assert body["total_prompt_tokens"] == 180
    assert body["total_completion_tokens"] == 35
    assert body["total_reasoning_tokens"] == 5
    assert body["total_tokens"] == 215
    assert body["calls"] == 3
    assert body["session_cost"] == pytest.approx(0.012)
    assert body["session_cost_partial"] is True
    assert body["first_call_at"] is not None and body["last_call_at"] is not None

    by_model = {item["provider_id"]: item for item in body["by_model"]}
    assert by_model["prov-a"]["calls"] == 2
    assert by_model["prov-a"]["prompt_tokens"] == 150
    assert by_model["prov-a"]["completion_tokens"] == 30
    assert by_model["prov-a"]["priced"] is False
    assert by_model["prov-b"]["priced"] is True
    assert by_model["prov-b"]["cost_usd"] == pytest.approx(0.002)


def test_context_tab_counts_messages_and_reports_budget(client):
    sid = _new_session()
    with session_scope() as db:
        session = repo.get_session(db, sid)
        repo.append_message(db, session, "user", "hello there")
        repo.append_message(db, session, "assistant", "hi")

    body = client.get(f"/chat/sessions/{sid}/usage", headers=_h()).json()

    assert body["message_count"] == 2
    assert body["context_tokens"] > 0
    assert "context_budget" in body


def test_empty_session_aggregates_are_zero(client):
    sid = _new_session()
    body = client.get(f"/chat/sessions/{sid}/usage", headers=_h()).json()

    assert body["total_tokens"] == 0
    assert body["calls"] == 0
    assert body["by_model"] == []
    assert body["first_call_at"] is None and body["last_call_at"] is None

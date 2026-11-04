"""GET /chat/sessions/{id}/usage: контекст считается, стоимость честно None."""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from aria.api.auth import token_store
from aria.core import runprofile
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.main import app

KEYS = {
    "context_tokens",
    "context_window",
    "context_pct",
    "context_budget",
    "session_cost",
    "session_cost_complete",
    "session_cost_partial",
    "priced",
    "priced_messages",
    "unpriced_messages",
    "cache_hit_ratio",
    "total_prompt_tokens",
    "total_completion_tokens",
    "total_reasoning_tokens",
    "total_tokens",
    "calls",
    "first_call_at",
    "last_call_at",
    "by_model",
    "message_count",
}


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _h():
    if not token_store.current_token():
        token_store.issue()
    return {"X-Local-Agent-Token": token_store.current_token()}


def _new_session() -> uuid.UUID:
    with session_scope() as db:
        return repo.create_session(db, "usage test").id


def _add(sid: uuid.UUID, role: str, text: str) -> None:
    with session_scope() as db:
        sess = repo.get_session(db, sid)
        repo.append_message(db, sess, role, text)


def test_empty_session_has_zero_tokens_and_expected_shape(client):
    sid = _new_session()
    r = client.get(f"/chat/sessions/{sid}/usage", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == KEYS
    assert body["context_tokens"] == 0
    assert body["session_cost"] is None
    assert body["priced"] is False
    assert body["context_window"] is None and body["context_pct"] is None


def test_tokens_positive_and_grow_with_messages(client):
    sid = _new_session()
    _add(sid, "user", "hello there, please summarise this document " * 5)
    _add(sid, "assistant", "sure, here is a summary " * 5)
    first = client.get(f"/chat/sessions/{sid}/usage", headers=_h()).json()["context_tokens"]
    assert first > 0
    _add(sid, "user", "and one more question with some extra words " * 5)
    second = client.get(f"/chat/sessions/{sid}/usage", headers=_h()).json()["context_tokens"]
    assert second > first


def test_context_window_and_pct_from_session_profile(client):
    sid = _new_session()
    _add(sid, "user", "x" * 4000)
    with session_scope() as db:
        db.add(
            m.ProviderModel(
                id="usage-test-model", provider_id="usage-test-prov", model_id="m1", context_window=10000
            )
        )
        profile = runprofile.builtin_profile()
        profile["main"] = {"model": "usage-test-prov"}
        runprofile.save_profile(db, sid, profile)
    body = client.get(f"/chat/sessions/{sid}/usage", headers=_h()).json()
    assert body["context_window"] == 10000
    assert body["context_pct"] == round(body["context_tokens"] / 10000 * 100, 1)
    assert body["session_cost"] is None


def test_unknown_session_is_404(client):
    r = client.get(f"/chat/sessions/{uuid.uuid4()}/usage", headers=_h())
    assert r.status_code == 404


def test_usage_requires_token(client):
    sid = _new_session()
    r = client.get(f"/chat/sessions/{sid}/usage")
    assert r.status_code in (401, 403)

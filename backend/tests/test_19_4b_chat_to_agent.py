"""19.4b B2: a Chat request that needs a tool switches the session to Agent."""
from __future__ import annotations

import json
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter
from aria.routers import chat as chat_mod
from aria.routers import sessions as sessions_mod
from tests.test_wave1_runner_and_policy import _RecordingRunner


class _EchoLlm(LlmProvider):
    def __init__(self):
        self.provider_id = "fake-chat"
        self.provider_class = "free_tier_reasoning"
        self.calls = 0

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.calls += 1
        return LlmResponse(text="pong")


def _client():
    app = FastAPI()
    app.include_router(chat_mod.router)
    app.include_router(sessions_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    rec = _RecordingRunner()
    app.state.task_runner = rec
    llm = ProviderRouter()
    provider = _EchoLlm()
    llm.register(provider)
    app.state.router = llm
    return TestClient(app), rec, provider


def _session(title: str = "chat") -> str:
    with session_scope() as db:
        return str(repo.create_session(db, title, active_role="general").id)


@pytest.mark.parametrize("text", [
    "открой https://example.com и скажи заголовок",
    "открой example.com",
    r"прочитай C:\temp\logs\app.log",
    "запусти pytest в backend",
])
def test_tool_request_in_chat_switches_to_agent(text):
    client, rec, provider = _client()
    sid = _session()

    r = client.post(f"/chat/sessions/{sid}/send", json={"content": text})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "agent" and body["queued"] is True and body["task_id"]
    assert len(rec.submitted) == 1
    assert rec.submitted[0][0] == body["task_id"] and rec.submitted[0][1] == "agent"
    assert provider.calls == 0  # no chat answer from memory; the agent runs the tool
    with session_scope() as db:
        session = repo.get_session(db, uuid.UUID(sid))
        assert str(session.current_task_id) == body["task_id"]
        roles = [msg.role for msg in repo.list_messages(db, uuid.UUID(sid), limit=10)]
    assert roles[-1] == "user"


@pytest.mark.parametrize("text", ["что такое DNS?", "объясни разницу между TCP и UDP", "привет"])
def test_plain_chat_request_stays_in_chat(text):
    client, rec, provider = _client()
    sid = _session()

    r = client.post(f"/chat/sessions/{sid}/send", json={"content": text})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("mode") != "agent"
    assert rec.submitted == []
    assert body["assistant"]["content"] == "pong" and provider.calls == 1


def test_streaming_chat_switch_reports_the_mode():
    client, rec, _ = _client()
    sid = _session()

    r = client.post(f"/chat/sessions/{sid}/send", json={"content": "открой https://example.com", "stream": True})

    assert r.status_code == 200, r.text
    events = [json.loads(line) for line in r.text.splitlines() if line.strip()]
    done = [e for e in events if e.get("type") == "done"]
    assert len(done) == 1
    result = done[0]["result"]
    assert result["mode"] == "agent" and result["queued"] is True
    assert len(rec.submitted) == 1 and rec.submitted[0][1] == "agent"

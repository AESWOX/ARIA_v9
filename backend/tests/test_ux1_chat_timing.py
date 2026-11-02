"""UX-1: chat_timing (first token + total) for stream and non-stream answers, no message text."""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.llm.providers.base import LlmProvider, LlmResponse, StreamChunk
from aria.llm.router import ProviderRouter
from aria.routers import chat as chat_mod

CONTENT = "ux1-marker-secret"


class Clock:
    def __init__(self, values: list[float]):
        self.values = list(values)
        self.calls = 0

    def __call__(self) -> float:
        value = self.values[self.calls]
        self.calls += 1
        return value


class FakeProvider(LlmProvider):
    provider_id = "fake-gemini"
    provider_class = "free_tier_reasoning"

    def __init__(self, chunks: tuple[str, ...] = ("Hel", "lo")):
        self.chunks = list(chunks)

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        return LlmResponse(text="".join(self.chunks), usage={"prompt_tokens": 3, "completion_tokens": 2})

    async def stream(self, messages, tools, timeout_sec):
        for chunk in self.chunks:
            yield StreamChunk(text=chunk, final=False)
        yield StreamChunk(text="", final=True, usage={"prompt_tokens": 3, "completion_tokens": 2})


def _client() -> TestClient:
    llm = ProviderRouter()
    llm.register(FakeProvider())
    app = FastAPI()
    app.include_router(chat_mod.router)
    app.state.router = llm
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    return TestClient(app)


def _session_id(client: TestClient) -> str:
    return client.post("/chat/sessions", json={}).json()["session_id"]


def _timing_lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if "chat_timing" in record.getMessage()]


def test_stream_answer_logs_first_token_and_total(monkeypatch, caplog):
    monkeypatch.setattr(chat_mod, "_monotonic", Clock([100.0, 100.25, 102.0]))
    client = _client()
    sid = _session_id(client)

    with caplog.at_level(logging.INFO, logger="local_agent.chat"):
        response = client.post(f"/chat/sessions/{sid}/send", json={"content": CONTENT, "stream": True})

    assert response.status_code == 200, response.text
    lines = _timing_lines(caplog)
    assert len(lines) == 1, lines
    assert f"first_token_ms=250 total_ms=2000 session_id={sid[:8]}" in lines[0]
    assert CONTENT not in lines[0]


def test_non_stream_answer_logs_full_time(monkeypatch, caplog):
    monkeypatch.setattr(chat_mod, "_monotonic", Clock([200.0, 202.5]))
    client = _client()
    sid = _session_id(client)

    with caplog.at_level(logging.INFO, logger="local_agent.chat"):
        response = client.post(f"/chat/sessions/{sid}/send", json={"content": CONTENT})

    assert response.status_code == 200, response.text
    lines = _timing_lines(caplog)
    assert len(lines) == 1, lines
    assert f"first_token_ms=2500 total_ms=2500 session_id={sid[:8]}" in lines[0]
    assert CONTENT not in lines[0]

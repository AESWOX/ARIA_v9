"""L0 (TZ_COMPLIANCE_V2 v2.1): compliance prompt + refusal diagnostics, no extra calls."""
from __future__ import annotations

import asyncio
import logging
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.core import loop as loop_mod
from aria.core.refusal import finish_reason_from_response, is_model_refusal
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus
from aria.llm.providers.base import LlmProvider, LlmResponse, StreamChunk
from aria.llm.router import ProviderRouter
from aria.routers import chat as chat_mod
from aria.routers.chat import SYSTEM_PROMPT, SYSTEM_PROMPT_BASE

CONTRACT_MARKER = "ARIA execution contract"
SECRET = "SECRETMARK-please-never-log-me"
REFUSAL = f"I cannot fulfill this request. {SECRET}"


class FakeProvider(LlmProvider):
    provider_class = "standard_reasoning"

    def __init__(self, text: str = "pong", raw: dict | None = None, provider_id: str = "fake-std"):
        self.provider_id = provider_id
        self.text = text
        self.raw = raw
        self.calls = 0
        self.seen: list[list] = []
        self.stream_calls = 0

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.calls += 1
        self.seen.append(list(messages))
        return LlmResponse(text=self.text, raw=self.raw, usage={"prompt_tokens": 3, "completion_tokens": 2})

    async def stream(self, messages, tools, timeout_sec):
        self.stream_calls += 1
        yield StreamChunk(text=self.text, final=False)
        yield StreamChunk(text="", final=True, usage={"prompt_tokens": 3, "completion_tokens": 2})


def _client(provider) -> TestClient:
    llm = ProviderRouter()
    llm.register(provider)
    app = FastAPI()
    app.include_router(chat_mod.router)
    app.state.router = llm
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    return TestClient(app)


def _new_session(client: TestClient) -> str:
    return client.post("/chat/sessions", json={}).json()["session_id"]


def _cleanup(*ids: str) -> None:
    with session_scope() as db:
        for sid in ids:
            db.query(m.Message).filter(m.Message.session_id == uuid.UUID(sid)).delete()
            db.query(m.Session).filter(m.Session.id == uuid.UUID(sid)).delete()


# --- T1 / T2: compliance prompt -------------------------------------------


def test_chat_prompt_contains_contract_and_is_byte_stable():
    first = chat_mod._system_prompt()
    second = chat_mod._system_prompt()
    assert CONTRACT_MARKER in first
    assert first == second == SYSTEM_PROMPT
    assert chat_mod._build_prompt([("user", "hi")])[0].content == first


def test_chat_prompt_returns_to_old_text_when_flag_is_off(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "compliance_prompt_enabled", False)
    prompt = chat_mod._build_prompt([("user", "hi")])
    assert prompt[0].content == SYSTEM_PROMPT_BASE
    assert CONTRACT_MARKER not in prompt[0].content


def test_defaults_are_on():
    settings = get_settings()
    assert settings.compliance_prompt_enabled is True
    assert settings.refusal_detection_enabled is True


# --- T3 / T4 / T10 / T11: refusal detector --------------------------------


def test_plain_no_is_not_a_refusal():
    assert is_model_refusal("Нет, на Марсе не доказано") is False


def test_explicit_refusals_in_english_and_russian():
    assert is_model_refusal("I cannot fulfill this request.") is True
    assert is_model_refusal("Я не могу выполнить этот запрос.") is True


def test_ordinary_failures_are_not_refusals():
    assert is_model_refusal("I can't find the file") is False
    assert is_model_refusal("Не могу найти файл") is False
    assert is_model_refusal("Нет, на Марсе не доказано") is False


def test_safety_finish_reason_wins_even_with_text():
    assert is_model_refusal("Here is a partial answer", finish_reason="content_filter") is True


def test_finish_reason_is_read_from_raw_response():
    from types import SimpleNamespace

    response = SimpleNamespace(raw={"choices": [{"finish_reason": "content_filter"}]})
    assert finish_reason_from_response(response) == "content_filter"


# --- T5 / T6: empty text vs tool calls ------------------------------------


def test_empty_text_with_tool_calls_is_not_a_refusal():
    assert is_model_refusal("", has_tool_calls=True) is False
    assert is_model_refusal(None, has_tool_calls=True, finish_reason="tool_calls") is False


def test_empty_text_without_tool_calls_is_a_refusal():
    assert is_model_refusal("") is True
    assert is_model_refusal("   ") is True


# --- T7 / T8: detection logs, exactly one provider call -------------------


def test_nonstream_refusal_is_logged_without_text_and_with_one_call(caplog):
    provider = FakeProvider(text=REFUSAL)
    client = _client(provider)
    sid = _new_session(client)
    try:
        with caplog.at_level(logging.INFO, logger="local_agent.refusal"):
            response = client.post(f"/chat/sessions/{sid}/send", json={"content": "do the task"})
        assert response.status_code == 200, response.text
        lines = [r.getMessage() for r in caplog.records if "refusal_detected" in r.getMessage()]
        assert len(lines) == 1, lines
        assert "provider=fake-std" in lines[0]
        assert SECRET not in lines[0]
        assert provider.calls == 1
    finally:
        _cleanup(sid)


def test_detection_off_keeps_chat_silent(caplog, monkeypatch):
    provider = FakeProvider(text=REFUSAL)
    settings = get_settings()
    monkeypatch.setattr(settings, "refusal_detection_enabled", False)
    client = _client(provider)
    sid = _new_session(client)
    try:
        with caplog.at_level(logging.INFO, logger="local_agent.refusal"):
            assert client.post(f"/chat/sessions/{sid}/send", json={"content": "do the task"}).status_code == 200
        assert not [r for r in caplog.records if "refusal_detected" in r.getMessage()]
        assert provider.calls == 1
    finally:
        _cleanup(sid)


# --- T12: stream detection ------------------------------------------------


def test_stream_refusal_is_detected_from_assembled_text(caplog):
    provider = FakeProvider(text=REFUSAL)
    client = _client(provider)
    sid = _new_session(client)
    try:
        with caplog.at_level(logging.INFO, logger="local_agent.refusal"):
            response = client.post(f"/chat/sessions/{sid}/send", json={"content": "do the task", "stream": True})
        assert response.status_code == 200
        lines = [r.getMessage() for r in caplog.records if "refusal_detected" in r.getMessage()]
        assert len(lines) == 1, lines
        assert SECRET not in lines[0]
        assert provider.stream_calls == 1
    finally:
        _cleanup(sid)


# --- agent: contract in role prompt + detection, one call per step --------


async def _run_agent_once(text: str) -> FakeProvider:
    provider = FakeProvider(text=text)
    router = ProviderRouter()
    router.register(provider)
    sandbox = "C:\\pt\\l0-sandbox"

    async def _no_finalize(task_id, router_arg):
        return None

    original = loop_mod.finalize_task
    loop_mod.finalize_task = _no_finalize
    try:
        with session_scope() as db:
            session = repo.create_session(db, "l0-agent")
            task = repo.create_task(db, session, role="general", objective="do the task")
            repo.set_task_status(db, task, TaskStatus.approved)
            task_id = task.id
            session_id = session.id
        try:
            await loop_mod.execute_agent_loop(task_id, router, sandbox)
        finally:
            with session_scope() as db:
                db.query(m.Task).filter(m.Task.id == task_id).delete()
                db.query(m.Session).filter(m.Session.id == session_id).delete()
    finally:
        loop_mod.finalize_task = original
    return provider


def test_agent_prompt_contains_contract_and_one_call_per_step(caplog):
    with caplog.at_level(logging.INFO, logger="local_agent.refusal"):
        provider = asyncio.run(_run_agent_once(REFUSAL))
    assert provider.calls == 1
    system_prompt = provider.seen[0][0].content
    assert CONTRACT_MARKER in system_prompt
    lines = [r.getMessage() for r in caplog.records if "refusal_detected" in r.getMessage()]
    assert len(lines) == 1, lines
    assert "provider=fake-std" in lines[0]
    assert SECRET not in lines[0]


def test_agent_prompt_has_no_contract_when_flag_is_off(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "compliance_prompt_enabled", False)
    provider = asyncio.run(_run_agent_once("pong"))
    assert CONTRACT_MARKER not in provider.seen[0][0].content
    assert provider.calls == 1

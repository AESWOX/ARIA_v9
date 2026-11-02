"""PREFLIGHT: rewrite layer, refusal detection and one-hop fallback (T1-T9)."""
from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.core import preflight as pf
from aria.core.refusal import is_model_refusal
from aria.core.taskspec import TaskSpec, default_criterion
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.llm.providers.base import LlmProvider, LlmResponse, StreamChunk
from aria.llm.router import ProviderRouter
from aria.routers import chat as chat_mod
from aria.routers.chat import SYSTEM_PROMPT, SYSTEM_PROMPT_BASE, router as chat_router

REWRITTEN = "Execute: explain the backend module layout in three sentences."
RAW = "Explain the module layout of the backend in three sentences."


class StubRouter:
    def __init__(self, text: str = REWRITTEN, error: Exception | None = None):
        self.text = text
        self.error = error
        self.calls: list[dict] = []

    async def route_chat(self, provider_class, messages, tools, **kwargs):
        self.calls.append({"class": provider_class, "messages": messages, **kwargs})
        if self.error is not None:
            raise self.error
        return SimpleNamespace(response=SimpleNamespace(text=self.text))


class ScriptedProvider(LlmProvider):
    provider_id = "fake-free"
    provider_class = "free_tier_reasoning"

    def __init__(self, responder, provider_id: str = "fake-free", provider_class: str = "free_tier_reasoning"):
        self.provider_id = provider_id
        self.provider_class = provider_class
        self.responder = responder
        self.seen: list[list] = []
        self.stream_calls = 0

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.seen.append(list(messages))
        return LlmResponse(
            text=self.responder(messages),
            usage={"prompt_tokens": 3, "completion_tokens": 2},
        )

    async def stream(self, messages, tools, timeout_sec):
        self.stream_calls += 1
        yield StreamChunk(text="I cannot fulfill this request.", final=False)
        yield StreamChunk(text="", final=True, usage={"prompt_tokens": 3, "completion_tokens": 2})


def _is_rewriter_call(messages) -> bool:
    return bool(messages) and messages[0].role == "system" and "silent pre-processor" in messages[0].content


@pytest.fixture(autouse=True)
def _fresh_cache():
    pf.reset_cache()
    yield
    pf.reset_cache()


def _settings(**overrides):
    base = {
        "preflight_rewrite_enabled": True,
        "preflight_rewrite_model_class": "free_tier_reasoning",
        "preflight_max_chars": 8000,
        "preflight_context_turns": 2,
        "preflight_refusal_fallback_classes": "",
        "compression_enabled": False,
        "compression_token_threshold": 0,
        "dlp_chat_enabled": False,
        "dlp_names_list": [],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _client(provider) -> TestClient:
    llm = ProviderRouter()
    llm.register(provider)
    app = FastAPI()
    app.include_router(chat_router)
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


# --- T6 / T7: refusal contract -------------------------------------------


def test_martian_no_is_not_a_refusal():
    assert is_model_refusal("Нет, на Марсе не доказано") is False


def test_explicit_refusal_prefix_is_detected():
    assert is_model_refusal("I cannot fulfill this request.") is True


def test_empty_and_none_are_refusals():
    assert is_model_refusal("") is True
    assert is_model_refusal(None) is True
    assert is_model_refusal("   ") is True


# --- T1 / T2 / T3 / T5 / T9: preflight algorithm --------------------------


async def test_disabled_flag_skips_router(monkeypatch):
    router = StubRouter()
    monkeypatch.setattr(pf, "get_settings", lambda: _settings(preflight_rewrite_enabled=False))
    result = await pf.preflight_user_message(RAW, router=router, mode="chat")
    assert result.reason == "disabled"
    assert result.applied is False
    assert result.rewritten == RAW
    assert router.calls == []


async def test_small_talk_is_skipped(monkeypatch):
    router = StubRouter()
    monkeypatch.setattr(pf, "get_settings", lambda: _settings())
    result = await pf.preflight_user_message("привет", router=router, mode="chat")
    assert result.reason == "skipped_small_talk"
    assert router.calls == []


async def test_pure_cli_passthrough_is_byte_exact(monkeypatch):
    router = StubRouter()
    monkeypatch.setattr(pf, "get_settings", lambda: _settings())
    result = await pf.preflight_user_message("git status", router=router, mode="chat")
    assert result.reason == "skipped_cli_passthrough"
    assert result.rewritten == "git status"
    assert router.calls == []


async def test_russian_task_mentioning_git_is_not_passthrough(monkeypatch):
    router = StubRouter()
    monkeypatch.setattr(pf, "get_settings", lambda: _settings())
    raw = "объясни, как в этом проекте устроен git flow и релизы"
    result = await pf.preflight_user_message(raw, router=router, mode="chat")
    assert result.reason == "rewritten"
    assert len(router.calls) == 1


async def test_router_failure_becomes_error_fallback(monkeypatch):
    router = StubRouter(error=RuntimeError("network down"))
    monkeypatch.setattr(pf, "get_settings", lambda: _settings())
    result = await pf.preflight_user_message(RAW, router=router, mode="chat")
    assert result.reason == "error_fallback"
    assert result.rewritten == RAW
    assert result.applied is False


async def test_cache_hit_uses_one_llm_call(monkeypatch):
    router = StubRouter()
    monkeypatch.setattr(pf, "get_settings", lambda: _settings())
    first = await pf.preflight_user_message(RAW, router=router, mode="chat")
    second = await pf.preflight_user_message(RAW, router=router, mode="chat")
    assert first.reason == "rewritten"
    assert second.reason == "cached"
    assert second.rewritten == REWRITTEN
    assert len(router.calls) == 1


async def test_rewriter_is_called_through_router_with_preflight_purpose(monkeypatch):
    monkeypatch.setattr(pf, "get_settings", lambda: _settings())
    router = StubRouter()
    await pf.preflight_user_message(RAW, router=router, mode="chat")
    assert router.calls[0]["purpose"] == "preflight"
    assert router.calls[0]["class"] == "free_tier_reasoning"


async def test_sensitive_message_skips_rewriter(monkeypatch):
    router = StubRouter()
    monkeypatch.setattr(pf, "get_settings", lambda: _settings())
    result = await pf.preflight_user_message(
        "мой номер карты 0000 0000 0000 0000", router=router, mode="chat", sensitive=True
    )
    assert result.reason == "skipped_sensitive"
    assert result.rewritten == "мой номер карты 0000 0000 0000 0000"
    assert router.calls == []


# --- T4: wire-up in chat --------------------------------------------------


def test_chat_stores_raw_but_sends_rewrite(monkeypatch):
    provider = ScriptedProvider(
        lambda messages: REWRITTEN if _is_rewriter_call(messages) else "answer"
    )
    client = _client(provider)
    sid = _new_session(client)
    try:
        response = client.post(f"/chat/sessions/{sid}/send", json={"content": RAW})
        assert response.status_code == 200, response.text

        stored = client.get(f"/chat/sessions/{sid}/messages").json()
        assert stored[0]["role"] == "user" and stored[0]["content"] == RAW

        chat_call = next(call for call in provider.seen if not _is_rewriter_call(call))
        assert chat_call[-1].role == "user"
        assert chat_call[-1].content == REWRITTEN

        with session_scope() as db:
            purposes = [
                row.purpose
                for row in db.query(m.LlmUsage)
                .filter(m.LlmUsage.session_id == uuid.UUID(sid))
                .all()
            ]
        assert "preflight" in purposes
    finally:
        _cleanup(sid)


# --- T8: stream does not run refusal fallback -----------------------------


def test_stream_does_not_trigger_refusal_fallback(monkeypatch, caplog):
    provider = ScriptedProvider(lambda messages: "ignored")
    monkeypatch.setattr(chat_mod, "get_settings", lambda: _settings(preflight_rewrite_enabled=False))
    client = _client(provider)
    sid = _new_session(client)
    try:
        with caplog.at_level(logging.INFO):
            response = client.post(f"/chat/sessions/{sid}/send", json={"content": RAW, "stream": True})
        assert response.status_code == 200
        assert provider.stream_calls == 1
        assert not [r for r in caplog.records if "refusal_fallback" in r.getMessage()]
    finally:
        _cleanup(sid)


# --- refusal fallback: one hop, non-stream --------------------------------


def test_nonstream_refusal_triggers_single_fallback_hop(monkeypatch, caplog):
    free = ScriptedProvider(
        lambda messages: messages[-1].content if _is_rewriter_call(messages) else "I cannot fulfill this request.",
        provider_id="fake-free",
        provider_class="free_tier_reasoning",
    )
    standard = ScriptedProvider(
        lambda messages: "final answer",
        provider_id="fake-standard",
        provider_class="standard_reasoning",
    )
    monkeypatch.setattr(
        chat_mod, "get_settings", lambda: _settings(preflight_refusal_fallback_classes="standard_reasoning")
    )
    client = _client(free)
    client.app.state.router.register(standard)
    sid = _new_session(client)
    try:
        with caplog.at_level(logging.INFO, logger="local_agent.chat"):
            response = client.post(f"/chat/sessions/{sid}/send", json={"content": RAW})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["assistant"]["content"] == "final answer"
        assert body["provider_id"] == "fake-standard"
        lines = [r.getMessage() for r in caplog.records if "refusal_fallback" in r.getMessage()]
        assert len(lines) == 1
        assert "from_class=free_tier_reasoning to_class=standard_reasoning" in lines[0]
    finally:
        _cleanup(sid)


# --- agent: rewrite lives in TaskSpec.meta --------------------------------


async def test_agent_start_persists_rewrite_in_task_spec_meta():
    from aria.core import loop as loop_mod

    with session_scope() as db:
        session_row = repo.create_session(db, "preflight-agent")
        task = repo.create_task(db, session_row, role="general", objective=RAW, draft_tz_md="# t\n")
        spec = TaskSpec(goal=RAW, acceptance_criteria=[default_criterion(RAW)])
        repo.upsert_task_spec(db, task, spec.model_dump(mode="json"), valid=True, clarify_rounds=0)
        task_id = task.id
        session_id = session_row.id
    try:
        rewritten = await loop_mod._preflight_task_start(task_id, StubRouter())
        assert rewritten == REWRITTEN
        with session_scope() as db:
            stored = repo.get_task_spec(db, task_id)
            saved = TaskSpec(**dict(stored.spec_json or {}))
        assert saved.meta["preflight_rewrite"] == REWRITTEN
    finally:
        with session_scope() as db:
            db.query(m.TaskSpecRow).filter(m.TaskSpecRow.task_id == task_id).delete()
            db.query(m.Task).filter(m.Task.id == task_id).delete()
            db.query(m.Session).filter(m.Session.id == session_id).delete()


# --- compliance prompt ----------------------------------------------------
def test_system_prompt_contains_compliance_contract():
    assert "ARIA execution contract" in SYSTEM_PROMPT


def test_compliance_is_absent_when_flag_is_off(monkeypatch):
    monkeypatch.setattr(
        chat_mod,
        "get_settings",
        lambda: SimpleNamespace(
            preflight_rewrite_enabled=False,
            compression_enabled=False,
            compression_token_threshold=0,
            preflight_context_turns=2,
            preflight_max_chars=8000,
        ),
    )
    prompt = chat_mod._build_prompt([("user", "hi")])
    assert prompt[0].content == SYSTEM_PROMPT_BASE

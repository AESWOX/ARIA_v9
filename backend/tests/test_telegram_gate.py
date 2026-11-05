"""Telegram gate: personal account questions get a card, public channels still work."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.core import telegram_gate
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter
from aria.routers.chat import router as chat_router

PERSONAL = "Что мне писали в телеграм за сегодня?"
PUBLIC = "Прочитай публичный канал t.me/durov и перескажи последний пост"


class FakeLlm(LlmProvider):
    def __init__(self):
        self.provider_id = "fake-gemini"
        self.provider_class = "free_tier_reasoning"
        self.seen: list[list] = []

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.seen.append(list(messages))
        return LlmResponse(text="model answer")


def _client(provider):
    llm = ProviderRouter()
    llm.register(provider)
    app = FastAPI()
    app.include_router(chat_router)
    app.state.router = llm
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    return TestClient(app)


def _new_session(c):
    return c.post("/chat/sessions", json={}).json()["session_id"]


def _unlinked(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "")
    monkeypatch.setattr(settings, "telegram_chat_id", "")


def _cleanup(sid):
    import uuid
    from aria.db import models as m
    from aria.db.base import session_scope
    from aria.db.models import Session as SessionRow

    with session_scope() as db:
        db.query(m.Message).filter(m.Message.session_id == uuid.UUID(sid)).delete()
        db.query(SessionRow).filter(SessionRow.id == uuid.UUID(sid)).delete()


def test_personal_request_is_detected_without_a_link(monkeypatch):
    _unlinked(monkeypatch)
    assert telegram_gate.is_personal_request(PERSONAL)
    assert telegram_gate.blocked_notice(PERSONAL, get_settings()) is not None


def test_public_channel_is_not_gated(monkeypatch):
    _unlinked(monkeypatch)
    assert not telegram_gate.is_personal_request(PUBLIC)
    assert telegram_gate.blocked_notice(PUBLIC, get_settings()) is None


def test_linked_bot_disables_the_gate(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "telegram_bot_token", "test-token")
    monkeypatch.setattr(settings, "telegram_chat_id", "42")
    assert telegram_gate.blocked_notice(PERSONAL, get_settings()) is None


def test_personal_question_returns_a_card_and_never_calls_the_model(monkeypatch):
    _unlinked(monkeypatch)
    fake = FakeLlm()
    c = _client(fake)
    sid = _new_session(c)
    try:
        r = c.post(f"/chat/sessions/{sid}/send", json={"content": PERSONAL})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "telegram_not_connected"
        assert body["assistant"]["content_json"]["notice"] == "telegram_not_connected"
        assert body["assistant"]["content_json"]["action_path"] == "/channels"
        assert fake.seen == []
    finally:
        _cleanup(sid)


def test_public_channel_message_reaches_the_model(monkeypatch):
    _unlinked(monkeypatch)
    fake = FakeLlm()
    c = _client(fake)
    sid = _new_session(c)
    try:
        r = c.post(f"/chat/sessions/{sid}/send", json={"content": PUBLIC})
        assert r.status_code == 200
        assert r.json().get("mode") != "telegram_not_connected"
        assert len(fake.seen) == 1
    finally:
        _cleanup(sid)

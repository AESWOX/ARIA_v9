"""Chat: key -> message -> answer -> session saved. LLM is faked at provider level."""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.db.base import session_scope
from aria.db.models import Session as SessionRow
from aria.llm.providers.base import ChatMessage, LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter
from aria.routers.chat import NOT_CONFIGURED_HINT, SYSTEM_PROMPT, router as chat_router


class FakeLlm(LlmProvider):
    def __init__(self, provider_id="fake-gemini", answers=None, error=None):
        self.provider_id = provider_id
        self.provider_class = "free_tier_reasoning"
        self.answers = list(answers or ["pong"])
        self.error = error
        self.seen: list[list[ChatMessage]] = []

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.seen.append(list(messages))
        if self.error:
            raise self.error
        return LlmResponse(text=self.answers.pop(0) if self.answers else "pong")


def _client(provider):
    llm = ProviderRouter()
    if provider is not None:
        llm.register(provider)
    app = FastAPI()
    app.include_router(chat_router)
    app.state.router = llm
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    return TestClient(app)


def _new_session(c):
    return c.post("/chat/sessions", json={}).json()["session_id"]


def _cleanup(*ids):
    import uuid
    from aria.db import models as m
    with session_scope() as db:
        for i in ids:
            db.query(m.Message).filter(m.Message.session_id == uuid.UUID(i)).delete()
            db.query(SessionRow).filter(SessionRow.id == uuid.UUID(i)).delete()


def test_round_trip_history_title_and_persistence():
    fake = FakeLlm(answers=["Hi there", "Fine, thanks"])
    c = _client(fake)
    sid = _new_session(c)
    try:
        assert c.get("/chat/status").json()["configured"] is True
        r1 = c.post(f"/chat/sessions/{sid}/send", json={"content": "Hello ARIA"})
        assert r1.status_code == 200
        assert r1.json()["assistant"]["content"] == "Hi there" and r1.json()["provider_id"] == "fake-gemini"
        r2 = c.post(f"/chat/sessions/{sid}/send", json={"content": "How are you?"})
        assert r2.json()["assistant"]["content"] == "Fine, thanks"

        # second call carried the whole conversation, system prompt first
        sent = [(m.role, m.content) for m in fake.seen[1]]
        assert sent == [("system", SYSTEM_PROMPT), ("user", "Hello ARIA"), ("assistant", "Hi there"), ("user", "How are you?")]

        # persisted, ordered, retrievable after "restart" (fresh client, same DB)
        c2 = _client(FakeLlm())
        msgs = c2.get(f"/chat/sessions/{sid}/messages").json()
        assert [(m["role"], m["content"]) for m in msgs] == [
            ("user", "Hello ARIA"), ("assistant", "Hi there"), ("user", "How are you?"), ("assistant", "Fine, thanks")
        ]
        row = next(s for s in c2.get("/chat/sessions").json() if s["id"] == sid)
        assert row["title"] == "Hello ARIA" and row["message_count"] == 4
    finally:
        _cleanup(sid)


def test_no_real_model_gives_a_clear_hint_not_a_fake_answer():
    c = _client(None)
    sid = _new_session(c)
    try:
        assert c.get("/chat/status").json() == {"configured": False, "providers": [], "hint": NOT_CONFIGURED_HINT}
        r = c.post(f"/chat/sessions/{sid}/send", json={"content": "hi"})
        assert r.status_code == 503 and "GEMINI_API_KEYS" in r.json()["detail"]
        assert c.get(f"/chat/sessions/{sid}/messages").json() == []  # nothing stored when we could not even try
    finally:
        _cleanup(sid)


@pytest.mark.parametrize(
    "error,status",
    [
        (httpx.HTTPStatusError("x", request=httpx.Request("POST", "http://x"), response=httpx.Response(429)), 429),
        (httpx.HTTPStatusError("x", request=httpx.Request("POST", "http://x"), response=httpx.Response(500)), 502),
        (httpx.ReadTimeout("slow"), 504),
    ],
)
def test_model_errors_become_readable_http_errors(error, status):
    c = _client(FakeLlm(error=error))
    sid = _new_session(c)
    try:
        r = c.post(f"/chat/sessions/{sid}/send", json={"content": "hi"})
        assert r.status_code == status and r.json()["detail"]
        # the user's message is kept so they can retry without retyping
        assert [m["role"] for m in c.get(f"/chat/sessions/{sid}/messages").json()] == ["user"]
    finally:
        _cleanup(sid)


def test_retry_reuses_the_stored_user_message_without_duplicating_it():
    fake = FakeLlm(answers=["recovered"])
    fake.error = httpx.ReadTimeout("slow")
    c = _client(fake)
    sid = _new_session(c)
    try:
        assert c.post(f"/chat/sessions/{sid}/send", json={"content": "question"}).status_code == 504
        fake.error = None
        r = c.post(f"/chat/sessions/{sid}/send", json={"retry": True})
        assert r.status_code == 200 and r.json()["assistant"]["content"] == "recovered"
        msgs = c.get(f"/chat/sessions/{sid}/messages").json()
        assert [(m["role"], m["content"]) for m in msgs] == [("user", "question"), ("assistant", "recovered")]
        # nothing left to retry now (last message is the assistant's)
        assert c.post(f"/chat/sessions/{sid}/send", json={"retry": True}).status_code == 400
    finally:
        _cleanup(sid)


def test_validation():
    c = _client(FakeLlm())
    sid = _new_session(c)
    try:
        assert c.post(f"/chat/sessions/{sid}/send", json={"content": "   "}).status_code == 400
        assert c.post(f"/chat/sessions/{sid}/send", json={"content": "x" * 20_001}).status_code == 413
        import uuid
        assert c.post(f"/chat/sessions/{uuid.uuid4()}/send", json={"content": "hi"}).status_code == 404
        assert c.get(f"/chat/sessions/{uuid.uuid4()}/messages").status_code == 404
    finally:
        _cleanup(sid)


def test_saving_a_key_on_the_keys_page_enables_chat_without_restart(tmp_path, monkeypatch):
    import os
    from aria import config
    from aria.routers import env as env_mod

    monkeypatch.setattr(env_mod, "_ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("GEMINI_API_KEYS", raising=False)
    app = FastAPI()
    app.include_router(chat_router)
    app.include_router(env_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    app.state.router = ProviderRouter()  # nothing configured yet
    c = TestClient(app)
    try:
        assert c.get("/chat/status").json()["configured"] is False
        assert c.put("/env", json={"key": "GEMINI_API_KEYS", "value": "AIzaA111,AIzaB222"}).status_code == 200
        st = c.get("/chat/status").json()
        assert st["configured"] is True and "gemini-flash" in st["providers"]
        assert c.request("DELETE", "/env", json={"key": "GEMINI_API_KEYS"}).status_code == 200
        assert c.get("/chat/status").json()["configured"] is False
    finally:
        os.environ.pop("GEMINI_API_KEYS", None)
        config.get_settings.cache_clear()

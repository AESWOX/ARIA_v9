"""Run-profile scope: a session save never silently changes the default."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.core import runprofile
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter
from aria.routers.chat import router as chat_router


class FakeLlm(LlmProvider):
    provider_id = "fake-gemini"
    provider_class = "free_tier_reasoning"

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        return LlmResponse(text="ok")


def _client():
    llm = ProviderRouter()
    llm.register(FakeLlm())
    app = FastAPI()
    app.include_router(chat_router)
    app.state.router = llm
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    return TestClient(app)


def _new_session(c):
    return c.post("/chat/sessions", json={}).json()["session_id"]


def _profile(tier: str) -> dict:
    return {"style": "solo", "audit": "off", "thinking": "off",
            "main": {"tier": tier}, "workers": {}, "auditor": {}}


def _cleanup(*ids):
    import uuid
    from aria.db import models as m
    from aria.db.models import Session as SessionRow

    with session_scope() as db:
        for sid in ids:
            db.query(m.Message).filter(m.Message.session_id == uuid.UUID(sid)).delete()
            db.query(SessionRow).filter(SessionRow.id == uuid.UUID(sid)).delete()
        repo.set_agent_state(db, runprofile.DEFAULT_KEY, {}, source="test")


def test_session_save_does_not_change_the_default():
    c = _client()
    a = _new_session(c)
    b = _new_session(c)
    try:
        assert c.put(f"/chat/sessions/{a}/profile", json=_profile("free")).status_code == 200
        assert c.get(f"/chat/sessions/{a}/profile").json()["profile"]["main"]["tier"] == "free"
        assert c.get(f"/chat/sessions/{b}/profile").json()["profile"]["main"] == {}
        assert c.get("/chat/profile").json()["profile"]["main"] == {}
    finally:
        _cleanup(a, b)


def test_explicit_use_for_new_updates_the_default():
    c = _client()
    a = _new_session(c)
    b = _new_session(c)
    try:
        r = c.put(f"/chat/sessions/{a}/profile?use_for_new=true", json=_profile("premium"))
        assert r.status_code == 200
        assert c.get("/chat/profile").json()["profile"]["main"]["tier"] == "premium"
        assert c.get(f"/chat/sessions/{b}/profile").json()["profile"]["main"]["tier"] == "premium"
    finally:
        _cleanup(a, b)


def test_default_profile_endpoint_works_without_a_session():
    c = _client()
    try:
        r = c.put("/chat/profile", json=_profile("standard"))
        assert r.status_code == 200
        assert c.get("/chat/profile").json()["profile"]["main"]["tier"] == "standard"
    finally:
        _cleanup()


def test_explicit_false_keeps_the_default_untouched():
    c = _client()
    a = _new_session(c)
    try:
        r = c.put(f"/chat/sessions/{a}/profile?use_for_new=false", json=_profile("free"))
        assert r.status_code == 200
        assert c.get(f"/chat/sessions/{a}/profile").json()["profile"]["main"]["tier"] == "free"
        assert c.get("/chat/profile").json()["profile"]["main"] == {}
    finally:
        _cleanup(a)


def test_two_sessions_keep_independent_profiles():
    c = _client()
    a = _new_session(c)
    b = _new_session(c)
    try:
        c.put(f"/chat/sessions/{a}/profile", json=_profile("free"))
        c.put(f"/chat/sessions/{b}/profile", json=_profile("premium"))
        assert c.get(f"/chat/sessions/{a}/profile").json()["profile"]["main"]["tier"] == "free"
        assert c.get(f"/chat/sessions/{b}/profile").json()["profile"]["main"]["tier"] == "premium"
        assert c.get("/chat/profile").json()["profile"]["main"] == {}
    finally:
        _cleanup(a, b)

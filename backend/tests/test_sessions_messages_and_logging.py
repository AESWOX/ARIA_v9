"""Regression: Sessions page loop (GET /sessions/{id}/messages was 404), log duplication, provider error logging."""
import logging
import uuid

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.db import models as m
from aria.db.base import session_scope
from aria.routers.chat import router as chat_router
from aria.routers.sessions import router as sessions_router


def _client():
    app = FastAPI()
    app.include_router(chat_router)
    app.include_router(sessions_router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    return TestClient(app)


def test_sessions_messages_get_shape_and_404():
    c = _client()
    sid = c.post("/chat/sessions", json={}).json()["session_id"]
    try:
        r = c.get(f"/sessions/{sid}/messages")
        assert r.status_code == 200
        assert r.json() == {"session_id": sid, "messages": []}
        assert c.get(f"/sessions/{uuid.uuid4()}/messages").status_code == 404
    finally:
        with session_scope() as db:
            db.query(m.Session).filter(m.Session.id == uuid.UUID(sid)).delete()


def test_file_handler_not_duplicated_on_uvicorn_error():
    import aria.main as main
    assert main._file_handler not in logging.getLogger("uvicorn.error").handlers
    assert main._file_handler in logging.getLogger("uvicorn").handlers


def test_provider_error_log_has_status_and_redacted_body(caplog):
    from aria.llm.providers.openai_compatible import _log_http_error
    req = httpx.Request("POST", "https://x/chat/completions?key=AIzaSECRET")
    resp = httpx.Response(400, request=req, text='{"error":"bad AIzaSyA1234567890123456789012345 field"}')
    with caplog.at_level(logging.WARNING):
        _log_http_error("gemini", "gemini-2.5-flash", resp)
    out = caplog.text
    assert "HTTP 400" in out and "bad" in out and "AIzaSy" not in out

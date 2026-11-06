"""Telegram connect: the chat card can link a bot without the dead Channels stub."""
from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.core import telegram_gate
from aria.routers import env as env_router


def _client():
    app = FastAPI()
    app.include_router(env_router.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    return TestClient(app)


def _cleanup():
    for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        os.environ.pop(key, None)
        env_router._persist_env_var(key, None)
    get_settings.cache_clear()


def test_bad_token_is_rejected():
    c = _client()
    try:
        assert c.post("/messaging/telegram/connect", json={"token": "bad", "channel": "@chan"}).status_code == 400
    finally:
        _cleanup()


def test_connect_saves_token_and_channel_and_lifts_the_gate():
    c = _client()
    try:
        token = "123456789:" + "A" * 35
        r = c.post("/messaging/telegram/connect", json={"token": token, "channel": "my_channel"})
        assert r.status_code == 200, r.text
        assert r.json()["channel"] == "@my_channel"
        assert r.json()["token_saved"] is True

        saved = env_router._load_env_file()
        assert saved.get("TELEGRAM_BOT_TOKEN") == token
        assert saved.get("TELEGRAM_CHAT_ID") == "@my_channel"
        assert telegram_gate.is_linked(get_settings()) is True
    finally:
        _cleanup()


def test_connect_requires_a_channel():
    c = _client()
    try:
        r = c.post("/messaging/telegram/connect", json={"token": "123456789:" + "A" * 35, "channel": ""})
        assert r.status_code == 400
    finally:
        _cleanup()


@pytest.mark.parametrize("token", [
    "",
    "bad",
    "12345:" + "A" * 35,
    "123456789:" + "A" * 29,
    "abcdefghi:" + "A" * 35,
])
def test_malformed_tokens_are_rejected(token):
    c = _client()
    try:
        r = c.post("/messaging/telegram/connect", json={"token": token, "channel": "@chan"})
        assert r.status_code == 400
    finally:
        _cleanup()


@pytest.mark.parametrize("channel", ["", "ab", "bad channel!"])
def test_malformed_channels_are_rejected(channel):
    c = _client()
    try:
        r = c.post("/messaging/telegram/connect", json={"token": "123456789:" + "A" * 35, "channel": channel})
        assert r.status_code == 400
    finally:
        _cleanup()


def test_the_response_never_contains_the_token():
    c = _client()
    token = "123456789:" + "A" * 35
    try:
        r = c.post("/messaging/telegram/connect", json={"token": token, "channel": "chan"})
        assert r.status_code == 200, r.text
        assert token not in r.text
        assert r.json()["channel"] == "@chan"
    finally:
        _cleanup()

"""Public Telegram channel reading is exposed as a safe read tool."""
from __future__ import annotations

from aria.core.roles import ROLE_REGISTRY
from aria.integrations.telegram_public import reader
from aria.tools.handlers.web import telegram_channel
from aria.tools.registry import get_tool

POSTS = [
    {
        "channel": "test",
        "message_id": "1",
        "date": "2026-11-04T10:00:00+00:00",
        "text": "hello flux",
        "views": "10",
        "links": [],
        "url": "https://t.me/test/1",
    },
    {
        "channel": "test",
        "message_id": "2",
        "date": "2026-11-04T10:01:00+00:00",
        "text": "other post",
        "views": "20",
        "links": [],
        "url": "https://t.me/test/2",
    },
]


def test_registered_and_allowed_for_general():
    spec = get_tool("telegram_channel")
    assert spec.handler is not None
    assert spec.requires_approval is False
    assert "telegram_channel" in ROLE_REGISTRY["general"].tool_whitelist


async def test_filters_by_query_and_honours_limit(monkeypatch):
    async def fake_read(channel, pages=1):
        return list(POSTS)

    monkeypatch.setattr(reader, "read_channel", fake_read)
    out = await telegram_channel({"channel": "@test", "query": "flux", "limit": 5})

    assert out["channel"] == "test"
    assert out["count"] == 1
    assert out["posts"][0]["message_id"] == "1"
    assert out["source_trust"] == "untrusted"


async def test_reports_public_channel_errors(monkeypatch):
    async def fail(channel, pages=1):
        raise reader.TelegramPublicError("channel unavailable without an account")

    monkeypatch.setattr(reader, "read_channel", fail)
    out = await telegram_channel({"channel": "test"})

    assert "unavailable" in out["error"]
    assert out["source_trust"] == "untrusted"

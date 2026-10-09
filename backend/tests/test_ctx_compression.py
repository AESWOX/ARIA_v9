"""CTX: сжатие по порогу токенов, порядок сводки, защита хвоста/тулов, подключение к чату."""
from __future__ import annotations

import uuid

import pytest

from aria.config import get_settings
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import SourceTrust, TaskStatus
from aria.llm import compression
from aria.routers.chat import SYSTEM_PROMPT, _build_prompt
from tests.test_chat_api import FakeLlm, _cleanup, _client, _new_session


@pytest.fixture
def ctx(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "compression_enabled", True)
    monkeypatch.setattr(s, "compression_token_threshold", 1000)  # ~3000 символов
    monkeypatch.setattr(s, "compression_hard_message_limit", 0)
    monkeypatch.setattr(s, "compression_protect_first_n", 2)
    monkeypatch.setattr(s, "compression_protect_last_n", 4)
    calls: list[str] = []

    async def fake_summarize(block: str) -> str:
        calls.append(block)
        return "SUMMARY-TEXT"

    monkeypatch.setattr(compression, "_summarize", fake_summarize)
    return calls


def _make_session(n_pairs: int, size: int = 400, with_tool: bool = False) -> uuid.UUID:
    with session_scope() as db:
        sess = repo.create_session(db, "ctx-test")
        for i in range(n_pairs):
            repo.append_message(db, sess, role="user", content=f"U{i} " + "x" * size)
            if with_tool and i == 2:
                repo.append_message(
                    db, sess, role="tool", content="read_file -> ok",
                    content_json={"tool_name": "read_file", "output": {"text": "TOOLBODY"}, "status": "ok"},
                )
            repo.append_message(db, sess, role="assistant", content=f"A{i} " + "y" * size)
        return sess.id


def _prompt(sid):
    with session_scope() as db:
        return [(x.role, x.content, x.seq_no) for x in repo.list_messages_for_prompt(db, sid)]


async def test_below_threshold_not_compressed(ctx):
    sid = _make_session(2, size=50)
    try:
        assert await compression.maybe_compress(sid) is False
        assert ctx == []
    finally:
        _cleanup(str(sid))


async def test_over_token_threshold_compresses_middle_and_keeps_order(ctx):
    sid = _make_session(8)  # 16 сообщений ≈ 6400 символов ≈ 2100 токенов > 1000
    try:
        assert await compression.maybe_compress(sid) is True
        rows = _prompt(sid)
        texts = [r[1] for r in rows]
        # первые 2 и последние 4 дословно, между ними — сводка (а не в конце!)
        assert texts[0].startswith("U0") and texts[1].startswith("A0")
        assert texts[2].startswith(compression.SUMMARY_PREFIX) and "SUMMARY-TEXT" in texts[2]
        assert texts[-1].startswith("A7") and texts[-4].startswith("U6")
        assert len(rows) == 2 + 1 + 4
        # оригиналы остаются в БД
        with session_scope() as db:
            assert len(repo.list_messages(db, sid, limit=100)) == 17
    finally:
        _cleanup(str(sid))


async def test_tool_results_reach_summarizer_and_current_turn_stays_verbatim(ctx):
    sid = _make_session(8, with_tool=True)
    with session_scope() as db:
        sess = repo.get_session(db, sid)
        repo.append_message(db, sess, role="user", content="LAST-USER " + "z" * 400)
        repo.append_message(
            db, sess, role="tool", content="fetch -> ok",
            content_json={"tool_name": "fetch", "output": {"text": "FRESH-TOOL"}, "status": "ok"},
        )
    try:
        assert await compression.maybe_compress(sid) is True
        assert "TOOLBODY" in ctx[0]  # результат тула попал в суммаризацию, а не только «tool -> ok»
        texts = [r[1] for r in _prompt(sid)]
        assert any(t.startswith("LAST-USER") for t in texts) and "fetch -> ok" in texts  # текущий ход цел
    finally:
        _cleanup(str(sid))


async def test_pending_approval_blocks_compression(ctx):
    sid = _make_session(8)
    try:
        with session_scope() as db:
            sess = repo.get_session(db, sid)
            task = repo.create_task(db, sess, "general", "wait for approve")
            task.status = TaskStatus.awaiting_attention
        assert await compression.maybe_compress(sid) is False
        assert ctx == []
        with session_scope() as db:
            db.query(m.Task).filter(m.Task.session_id == sid).update({"status": TaskStatus.done})
        assert await compression.maybe_compress(sid) is True  # после Approve сжатие снова работает
    finally:
        with session_scope() as db:
            db.query(m.Task).filter(m.Task.session_id == sid).delete()
        _cleanup(str(sid))


async def test_summarizer_failure_leaves_history_intact(ctx, monkeypatch):
    async def boom(_):
        raise RuntimeError("down")

    monkeypatch.setattr(compression, "_summarize", boom)
    sid = _make_session(8)
    try:
        assert await compression.maybe_compress(sid) is False
        assert len(_prompt(sid)) == 16
    finally:
        _cleanup(str(sid))


def test_prompt_limit_takes_newest_messages():
    sid = _make_session(5, size=5)
    try:
        with session_scope() as db:
            rows = repo.list_messages_for_prompt(db, sid, limit=3)
        assert [r.content[:2] for r in rows] == ["A3", "U4", "A4"]
    finally:
        _cleanup(str(sid))


def test_build_prompt_puts_summary_into_system_and_budgets_by_tokens(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "compression_enabled", True)
    monkeypatch.setattr(s, "compression_token_threshold", 100)  # 300 символов
    history = [("system", f"{compression.SUMMARY_PREFIX} of 9 messages]\nOLD-FACTS")] + [
        ("user" if i % 2 == 0 else "assistant", f"m{i} " + "q" * 100) for i in range(10)
    ]
    prompt = _build_prompt(history)
    assert prompt[0].content.startswith(SYSTEM_PROMPT) and "OLD-FACTS" in prompt[0].content
    kept = [x.content[:3] for x in prompt[1:]]
    assert kept and kept[-1] == "m9 "  # самые свежие остаются, старые отброшены по токенному бюджету
    assert len(kept) < 10


def test_chat_send_triggers_compression_and_sends_summary(ctx):
    fake = FakeLlm(answers=["ok"])
    c = _client(fake)
    sid = _new_session(c)
    with session_scope() as db:
        sess = repo.get_session(db, uuid.UUID(sid))
        for i in range(8):
            repo.append_message(db, sess, role="user", content=f"U{i} " + "x" * 400)
            repo.append_message(db, sess, role="assistant", content=f"A{i} " + "y" * 400)
    try:
        r = c.post(f"/chat/sessions/{sid}/send", json={"content": "new question"})
        assert r.status_code == 200
        system = fake.seen[0][0].content
        assert "SUMMARY-TEXT" in system
        assert fake.seen[0][-1].content == "new question"
        assert len(ctx) == 1
    finally:
        _cleanup(sid)

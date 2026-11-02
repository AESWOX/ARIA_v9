"""PRD-3: open clarification questions come back with the run state (card survives a reload)."""
from __future__ import annotations

import asyncio

import pytest

from aria.core import clarifier
from aria.db import base as db_base
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus
from aria.routers import sessions as sessions_router


@pytest.fixture()
def env(tmp_path):
    saved = (db_base._engine, db_base._SessionLocal)
    db_base.init_db(f"sqlite:///{(tmp_path / 'run.db').as_posix()}", create_all=True)
    yield
    db_base._engine, db_base._SessionLocal = saved


def _task_awaiting_questions(content: str) -> tuple[str, str]:
    with session_scope() as db:
        s = repo.create_session(db, "q")
        t = repo.create_task(db, s, role="general", objective=content, draft_tz_md="# x\n")
        repo.set_task_status(db, t, TaskStatus.draft)
        sid, tid = s.id, t.id
    out = asyncio.run(clarifier.run_gate(tid, content, router=None))
    assert out.status == "awaiting_clarification"
    return str(sid), str(tid)


def _run(sid: str) -> dict:
    return asyncio.run(sessions_router.get_session_run(__import__("uuid").UUID(sid), "tok"))


def test_run_state_returns_the_open_questions_while_awaiting_clarification(env):
    sid, tid = _task_awaiting_questions("write a short poem about spring")
    state = _run(sid)
    assert state["status"] == "awaiting_clarification" and state["task_id"] == tid
    qs = state["questions"]
    assert 1 <= len(qs) <= 2
    assert all(q["id"] and q["text"] and len(q["options"]) == 5 and q["default_value"] in q["options"] for q in qs)


def test_run_state_has_no_questions_once_the_task_stops_waiting(env):
    sid, tid = _task_awaiting_questions("сделай отчёт")
    import uuid
    with session_scope() as db:
        task = repo.get_task(db, uuid.UUID(tid))
        repo.set_task_status(db, task, TaskStatus.cancelled)
    state = _run(sid)
    assert state["status"] == "cancelled" and state["questions"] == []


def test_run_state_without_a_task_has_empty_questions(env):
    with session_scope() as db:
        sid = str(repo.create_session(db, "empty").id)
    assert _run(sid)["questions"] == []

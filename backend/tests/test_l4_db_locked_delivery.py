"""L4: the only logged ``database is locked`` queries were event inserts while posting
the result/failure message (backend.log 2755, 3062, 3564).

The delivery is best-effort: it must survive a transient writer-lock by retrying in a
fresh transaction, and must not be turned into a second permanent failure.
"""
from __future__ import annotations

import sqlite3

from aria.core import executor as ex
from aria.db import repository as repo
from aria.db.base import session_scope


def _make_session():
    with session_scope() as db:
        session = repo.create_session(db, "l4-db-locked")
        task = repo.create_task(db, session, role="general", objective="open example.com")
        return session.id, task.id


def _flaky(monkeypatch, *, failures: int, exc: Exception):
    attempts = {"n": 0}
    real = repo.append_message

    def wrapper(db, session, role, content, **kwargs):
        attempts["n"] += 1
        if attempts["n"] <= failures:
            raise exc
        return real(db, session, role, content, **kwargs)

    monkeypatch.setattr(repo, "append_message", wrapper)
    return attempts


def test_result_message_retries_a_locked_database_in_a_fresh_transaction(monkeypatch):
    sid, tid = _make_session()
    attempts = _flaky(monkeypatch, failures=2, exc=sqlite3.OperationalError("database is locked"))

    with session_scope() as db:
        task = repo.get_task(db, tid)
        ex._post_result_message(db, task, None, [], None)

    assert attempts["n"] == ex.MAX_DB_RETRIES
    with session_scope() as db:
        roles = [m.role for m in repo.list_messages(db, sid)]
    assert roles == ["assistant"]


def test_result_message_does_not_retry_a_permanent_error(monkeypatch):
    sid, tid = _make_session()
    attempts = _flaky(monkeypatch, failures=1, exc=RuntimeError("schema mismatch"))

    with session_scope() as db:
        task = repo.get_task(db, tid)
        ex._post_result_message(db, task, None, [], None)

    assert attempts["n"] == 1
    with session_scope() as db:
        assert repo.list_messages(db, sid) == []

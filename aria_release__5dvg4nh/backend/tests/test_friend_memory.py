"""test_friend_memory.py — verify friend_memory write point fires during session finalization."""

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as OrmSession, sessionmaker

from aria.core.loop import _observe_user_patterns
from aria.db import models as m
from aria.db import repository as repo
from aria.db.enums import TaskStatus


@pytest.fixture
def db_session():
    """In-memory SQLite session with all tables created."""
    engine = create_engine("sqlite://", echo=False)
    m.Base.metadata.create_all(engine)
    session_local = sessionmaker(bind=engine)
    session = session_local()
    yield session
    session.close()


def make_session(db: OrmSession, title: str = "Test Session") -> m.Session:
    s = repo.create_session(db, title=title, user_label="test_user")
    db.flush()
    return s


def make_task(db: OrmSession, sess: m.Session, objective: str = "test task") -> m.Task:
    t = repo.create_task(
        db=db,
        session=sess,
        objective=objective,
        role="general",
    )
    t.status = TaskStatus.in_progress
    db.flush()
    return t


def test_observe_user_patterns_writes_friend_memory(db_session):
    """_observe_user_patterns should write at least one friend_memory entry after finalizing a task."""
    sess = make_session(db_session)
    task = make_task(db_session, sess, "build a weather app")
    task.status = TaskStatus.done

    # Add some user messages
    repo.append_message(db_session, sess, role="user", content="Make the UI clean and minimal")
    repo.append_message(db_session, sess, role="user", content="Use dark theme please")
    repo.append_message(db_session, sess, role="assistant", content="Sure, here's a clean dark UI")

    db_session.commit()

    # Call the observer
    _observe_user_patterns(db_session, sess, task)

    # Read back what was written
    entries = repo.list_friend_memory_entries(db_session, category="session")
    assert len(entries) >= 1, "Expected at least one friend_memory entry"

    entry = entries[0]
    assert entry.category == "session"
    assert entry.key == str(sess.id)
    assert entry.value_json["task_objective"] == "build a weather app"
    assert entry.value_json["user_messages"] == 2
    assert entry.value_json["assistant_messages"] == 1
    assert entry.value_json["outcome"] == "done"
    assert entry.source == "agent"
    assert entry.session_id == sess.id


def test_observe_user_patterns_no_messages_still_writes(db_session):
    """Even a task with no user messages should produce a friend_memory entry."""
    sess = make_session(db_session, title="Empty Session")
    task = make_task(db_session, sess, "empty task")
    task.status = TaskStatus.under_audit

    _observe_user_patterns(db_session, sess, task)

    entries = repo.list_friend_memory_entries(db_session)
    assert len(entries) == 1
    assert entries[0].value_json["user_messages"] == 0


def test_observe_user_patterns_upsert_idempotent(db_session):
    """Calling _observe_user_patterns twice on same session should upsert, not duplicate."""
    sess = make_session(db_session)
    task = make_task(db_session, sess, "idempotent test")
    task.status = TaskStatus.done
    repo.append_message(db_session, sess, role="user", content="Hello")
    db_session.commit()

    _observe_user_patterns(db_session, sess, task)
    _observe_user_patterns(db_session, sess, task)

    entries = repo.list_friend_memory_entries(db_session, category="session")
    assert len(entries) == 1, "Upsert should not create duplicate entries"

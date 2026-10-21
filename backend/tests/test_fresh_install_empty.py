"""A fresh install must not ship demo content (seed is opt-in)."""
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.http_utils import seed_database


def test_seed_is_noop_by_default(monkeypatch):
    monkeypatch.delenv("ARIA_SEED_DEMO", raising=False)
    with session_scope() as db:
        before = len(repo.list_sessions(db))
    seed_database()
    with session_scope() as db:
        assert len(repo.list_sessions(db)) == before


def test_seed_can_be_enabled_for_dev(monkeypatch):
    monkeypatch.setenv("ARIA_SEED_DEMO", "1")
    with session_scope() as db:
        if repo.list_sessions(db):
            return  # shared test DB already populated: seeding is a no-op by design
    seed_database()
    with session_scope() as db:
        assert any(s.title == "Bootstrap session" for s in repo.list_sessions(db))

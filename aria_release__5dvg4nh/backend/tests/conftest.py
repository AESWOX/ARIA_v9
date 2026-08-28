"""pytest configuration — enables anyio for async test functions.

DB isolation: tests MUST NOT touch the real backend database
(backend/data/local_agent.db). POSTGRES_DSN is pointed at a THROWAWAY COPY
of the real DB (via sqlite3 backup API, WAL-safe) BEFORE any `aria` module
is imported, so Settings() picks up the test DSN on first instantiation
(config.get_settings is lru_cached).

Why a copy instead of a fresh empty DB: the integration tests in this suite
were designed against a populated DB (skills_meta imported via
import_skills.py, friend_memory, existing sessions). A fresh DB would break
them; a copy keeps all green while the real DB stays untouched.
"""
import os
import sqlite3
import tempfile
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_SRC_DB = _BACKEND_ROOT / "data" / "local_agent.db"
_TEST_DB_PATH = os.path.join(tempfile.gettempdir(), "aria_test_local_agent.db")

if _SRC_DB.exists():
    # WAL-safe consistent snapshot: sqlite3 backup API, not a raw file copy.
    src = sqlite3.connect(str(_SRC_DB))
    dst = sqlite3.connect(_TEST_DB_PATH)
    with dst:
        src.backup(dst)
    dst.close()
    src.close()

os.environ["POSTGRES_DSN"] = f"sqlite:///{_TEST_DB_PATH}"

import pytest


def pytest_configure(config):
    """Ensure schema + skills exist even on a clean machine (no dev-DB).

    When local_agent.db is absent we still need skills_meta populated,
    otherwise integration tests fail with 'no such table: skills_meta'.
    init_db(create_all=True) is a no-op on the cloned dev-DB, and
    seed_skills() only inserts missing rows (idempotent).
    """
    from aria.db.base import init_db, run_migrations
    from aria.db.skills_seed import seed_skills

    init_db()
    run_migrations()
    n = seed_skills()
    if n:
        print(f"[conftest] seeded {n} skills")


def pytest_collection_modifyitems(items):
    """Auto-apply anyio marker to all async test functions."""
    for item in items:
        if item.get_closest_marker("anyio") is None:
            # Check if it's an async function
            if hasattr(item, "obj") and item.obj and hasattr(item.obj, "__code__"):
                try:
                    import inspect
                    if inspect.iscoroutinefunction(item.obj):
                        item.add_marker(pytest.mark.anyio)
                except Exception:
                    pass

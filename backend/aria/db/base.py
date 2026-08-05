from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from aria.config import get_settings
from aria.db.models import Base


def make_engine(dsn: str | None = None):
    settings = get_settings()
    dsn = dsn or settings.POSTGRES_DSN
    is_sqlite = dsn.startswith("sqlite")
    if is_sqlite:
        from pathlib import Path
        db_path = dsn.replace("sqlite:///", "", 1)
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connect_args = {"check_same_thread": False} if is_sqlite else {}
    engine = create_engine(dsn, connect_args=connect_args, future=True)
    if is_sqlite:
        from sqlalchemy import event

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine


_engine = None
_SessionLocal = None


def init_db(dsn: str | None = None, create_all: bool = False):
    """Инициализация engine. create_all=True используется только для
    dev/sqlite-smoke-режима — в production схему создаёт Alembic (§26 п.7)."""
    global _engine, _SessionLocal
    _engine = make_engine(dsn)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
    if create_all:
        Base.metadata.create_all(_engine)
    return _engine


def get_engine():
    if _engine is None:
        init_db()
    return _engine


@contextmanager
def session_scope() -> Iterator[OrmSession]:
    if _SessionLocal is None:
        init_db()
    db = _SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

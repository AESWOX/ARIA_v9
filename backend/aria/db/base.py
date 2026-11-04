from __future__ import annotations

import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar

T = TypeVar("T")

_LOCK_MARKERS = ("database is locked", "database table is locked", "database is busy")

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
            cursor.execute("PRAGMA busy_timeout=10000")
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


def run_migrations(dsn: str | None = None) -> None:
    """Применить Alembic-миграции к схеме.

    Adopt-логика для существующих БД, созданных через create_all:
      - БД пустая (нет таблиц)              -> alembic upgrade head
      - БД с таблицами, но без alembic_version -> alembic stamp head (adopt)
      - alembic_version указывает на ревизию, которой нет в дереве
        (старая история / create_all-snapshot) -> alembic stamp head
      - alembic_version актуальная          -> no-op (upgrade head ничего не делает)
    """
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    from sqlalchemy import inspect, text

    settings = get_settings()
    dsn = dsn or settings.POSTGRES_DSN
    engine = make_engine(dsn)

    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    with engine.connect() as conn:
        has_version = conn.dialect.has_table(conn, "alembic_version")
        version_num = None
        if has_version:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
            version_num = row[0] if row else None
    engine.dispose()

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent.parent.parent / "alembic"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    cfg.attributes["dsn_explicit"] = dsn
    script = ScriptDirectory.from_config(cfg)
    known_revs = {s.revision for s in script.walk_revisions()}

    if not existing:
        command.upgrade(cfg, "head")
    elif version_num is not None and version_num in known_revs:
        command.upgrade(cfg, "head")
    else:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        command.stamp(cfg, "head")


def get_engine():
    if _engine is None:
        init_db()
    return _engine


def is_locked_error(exc: BaseException) -> bool:
    """True for SQLite writer-lock errors (including the table-level variant)."""
    message = str(exc).lower()
    return any(marker in message for marker in _LOCK_MARKERS)


def run_with_db_retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    delays: tuple[float, ...] = (0.2, 0.5, 1.0),
) -> T:
    """Run a short DB operation again when SQLite reports a writer lock.

    The operation must open its own transaction: a failed commit rolls the whole
    scope back, so re-running the callable is safe. Non-lock errors propagate at
    once. This is a bounded retry, not a queue: after ``attempts`` the last error
    is raised to the caller.
    """
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - only lock errors are retried
            if not is_locked_error(exc) or attempt == attempts - 1:
                raise
            last_exc = exc
            time.sleep(delays[min(attempt, len(delays) - 1)])
    raise last_exc  # pragma: no cover - loop always returns or raises


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


def release_write_lock(db) -> None:
    """Коммитит накопленные записи перед долгим вызовом модели (BUG-5).

    SQLite допускает одного писателя: сессия, сделавшая flush и ушедшая ждать ответ модели на минуту,
    держит блокировку записи, и остальные запросы стоят до busy_timeout («database is locked»).
    Вызывать прямо перед `await router.route_chat(...)`.
    """
    if db is None:
        return
    try:
        db.commit()
    except Exception:  # noqa: BLE001 - блокировка не должна ронять сам вызов модели
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        import logging

        logging.getLogger("local_agent.db").error("release_write_lock: commit failed", exc_info=True)

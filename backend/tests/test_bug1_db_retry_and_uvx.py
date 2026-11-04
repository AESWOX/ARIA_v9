"""BUG-1: bounded retry for SQLite writer locks and the installed uv/uvx path."""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from aria.core import events as events_mod
from aria.db.base import is_locked_error, run_with_db_retry
from aria.mcp import executables


def test_run_with_db_retry_retries_locked_then_succeeds():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("database is locked")
        return "ok"

    assert run_with_db_retry(flaky, delays=(0, 0, 0)) == "ok"
    assert calls["n"] == 3


def test_run_with_db_retry_propagates_non_lock_errors_immediately():
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise ValueError("not a lock")

    try:
        run_with_db_retry(broken, delays=(0, 0, 0))
    except ValueError as exc:
        assert "not a lock" in str(exc)
    else:
        raise AssertionError("non-lock error must propagate")
    assert calls["n"] == 1


def test_table_level_lock_is_recognized():
    assert is_locked_error(RuntimeError("database table is locked: messages")) is True
    assert is_locked_error(RuntimeError("database is busy")) is True
    assert is_locked_error(RuntimeError("disk I/O error")) is False


def test_event_bus_retries_a_locked_persist(monkeypatch):
    calls = {"n": 0}
    fake_event = SimpleNamespace(
        event_id=uuid.uuid4(),
        event_type="ui.retry",
        ts=datetime.now(timezone.utc),
    )

    def persist(_db, *_args, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return fake_event

    monkeypatch.setattr(events_mod.repo, "persist_event", persist)
    envelope = events_mod.event_bus.emit("ui.retry", {"n": 1})

    assert calls["n"] == 2
    assert envelope["event_type"] == "ui.retry"
    assert envelope["payload"] == {"n": 1}


def test_bundled_uvx_is_found_next_to_resources(tmp_path, monkeypatch):
    install = tmp_path
    resources = install / "resources"
    resources.mkdir()
    uv_dir = install / "uv"
    uv_dir.mkdir()
    uvx = uv_dir / "uvx.exe"
    uvx.write_bytes(b"")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(resources / "backend.exe"))

    assert Path(executables.resolve_executable("uvx")) == uvx


def test_portable_uvx_next_to_backend_is_found(tmp_path, monkeypatch):
    resources = tmp_path / "resources"
    uv_dir = resources / "uv"
    uv_dir.mkdir(parents=True)
    uvx = uv_dir / "uvx.exe"
    uvx.write_bytes(b"")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(resources / "backend.exe"))

    assert Path(executables.resolve_executable("uvx")) == uvx


def test_uvx_falls_back_to_path(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(executables.shutil, "which", lambda name: f"C:/tools/{name}.exe")

    assert executables.resolve_executable("uvx") == "C:/tools/uvx.exe"

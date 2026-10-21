"""Дочерние процессы на Windows не должны открывать видимое консольное окно (uvx/npx/shell/hooks/codex)."""
from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

import pytest

from aria.core import winproc


def test_flag_is_create_no_window_on_windows(monkeypatch):
    monkeypatch.setattr("os.name", "nt")
    reloaded = importlib.reload(winproc)
    try:
        assert reloaded.NO_WINDOW == {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    finally:
        monkeypatch.undo()
        importlib.reload(winproc)


def test_flag_is_empty_elsewhere():
    importlib.reload(winproc)
    import os
    if os.name != "nt":
        assert winproc.NO_WINDOW == {}


@pytest.mark.parametrize(
    "rel",
    ["aria/mcp/client.py", "aria/tools/handlers/shell.py", "aria/hooks/engine.py", "aria/agents/codex_exec_bridge.py"],
)
def test_every_spawn_site_uses_no_window(rel):
    src = (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
    assert "**NO_WINDOW" in src and "from aria.core.winproc import NO_WINDOW" in src

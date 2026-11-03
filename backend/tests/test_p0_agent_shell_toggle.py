"""P0 A2: тумблер «Shell в Agent» (вариант C).

general остаётся read-only, пока agent_shell_enabled=False; при включении shell доступен
только роли general, всегда через Approve и только внутри agent_project_root.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aria.core import loop
from aria.tools import registry


def _settings(enabled: bool, root: str = ""):
    return SimpleNamespace(agent_shell_enabled=enabled, agent_project_root=root)


def test_general_off_has_no_agent_shell():
    assert loop._agent_shell_target("general", _settings(False, "/tmp/proj")) == (False, None)


def test_general_on_with_root_enables_agent_shell():
    assert loop._agent_shell_target("general", _settings(True, "/tmp/proj")) == (True, "/tmp/proj")


def test_general_on_without_root_reports_missing_root():
    assert loop._agent_shell_target("general", _settings(True, "  ")) == (True, None)


def test_other_roles_never_get_agent_shell():
    assert loop._agent_shell_target("coder", _settings(True, "/tmp/proj")) == (False, None)


def test_agent_shell_without_root_denies_with_message(tmp_path):
    out = asyncio.run(
        registry._shell_execute(
            {"command": "echo hi"}, 5, str(tmp_path), approved=True,
            agent_shell=True, shell_root=None,
        )
    )
    assert "project root" in out["error"].lower()


def test_agent_shell_outside_root_denies(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    out = asyncio.run(
        registry._shell_execute(
            {"command": r"dir C:\Windows"}, 5, str(tmp_path), approved=True,
            agent_shell=True, shell_root=str(root),
        )
    )
    assert "outside project root" in out["error"].lower()


def test_agent_shell_traversal_denies(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    out = asyncio.run(
        registry._shell_execute(
            {"command": "cat ../secret.txt"}, 5, str(tmp_path), approved=True,
            agent_shell=True, shell_root=str(root),
        )
    )
    assert "outside project root" in out["error"].lower()


def test_outside_root_reason_allows_inside(tmp_path):
    from aria.tools.handlers.shell import outside_root_reason

    root = (tmp_path / "proj").resolve()
    root.mkdir()
    assert outside_root_reason("echo hi", root) is None
    assert outside_root_reason(r"dir C:\Windows", root) is not None
    assert outside_root_reason("cat ../x", root) is not None
    assert outside_root_reason("echo %USERPROFILE%", root) is not None

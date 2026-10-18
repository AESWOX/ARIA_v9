"""scripts/check_authorship.py и .githooks/commit-msg: в историю попадает только разрешённый автор, без Co-authored-by."""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location("check_authorship", REPO / "scripts" / "check_authorship.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> None:
    full = {**os.environ, **(env or {})}
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env=full)


def _commit(cwd: Path, name: str, email: str, msg: str, fname: str) -> None:
    (cwd / fname).write_text(fname, encoding="utf-8")
    _git(cwd, "add", fname)
    env = {
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email,
    }
    _git(cwd, "commit", "-q", "-m", msg, env=env)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git не найден")
    _git(tmp_path, "init", "-q")
    return tmp_path


OK = {"author": {"owner@example.test"}, "committer": {"owner@example.test"}}


def test_clean_history_passes(repo: Path):
    mod = _load()
    _commit(repo, "Owner", "owner@example.test", "feat: a", "a.txt")
    assert mod.check(repo, "HEAD", OK) == []


def test_foreign_author_and_trailer_are_reported(repo: Path):
    mod = _load()
    _commit(repo, "Owner", "owner@example.test", "feat: a", "a.txt")
    _commit(repo, "Someone", "someone@example.test", "fix: b\n\nCo-Authored-By: X <x@example.test>", "b.txt")
    bad = mod.check(repo, "HEAD", OK)
    assert any("автор someone@example.test" in b for b in bad)
    assert any("коммиттер someone@example.test" in b for b in bad)
    assert any("Co-authored-by" in b for b in bad)


def test_trailer_alone_is_reported(repo: Path):
    mod = _load()
    _commit(repo, "Owner", "owner@example.test", "fix: c\n\nco-authored-by: X <x@example.test>", "c.txt")
    assert len(mod.check(repo, "HEAD", OK)) == 1


@pytest.mark.skipif(shutil.which("sh") is None, reason="нет sh")
def test_commit_msg_hook_strips_trailers(tmp_path: Path):
    msg = tmp_path / "MSG"
    msg.write_text("fix: x\n\nCo-Authored-By: Y <y@example.test>\nGenerated with Some Tool\nнужная строка\n", encoding="utf-8")
    subprocess.run(["sh", str(REPO / ".githooks" / "commit-msg"), str(msg)], check=True)
    text = msg.read_text(encoding="utf-8")
    assert "Co-Authored-By" not in text and "Generated with" not in text
    assert "нужная строка" in text and text.startswith("fix: x")


def test_allowlist_file_has_owner_only():
    ok = _load().allowed(REPO)
    assert ok["author"] and all("@" in e for e in ok["author"])
    assert "noreply@github.com" not in ok["author"]  # служебный адрес GitHub допустим только как коммиттер

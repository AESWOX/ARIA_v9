"""aria.paths: dev defaults unchanged, installed/override mode is persistent."""
import os
import sys
from pathlib import Path

import pytest

from aria import paths


@pytest.fixture
def dev_mode(monkeypatch):
    monkeypatch.delenv("ARIA_DATA_DIR", raising=False)
    monkeypatch.delenv("ARIA_SKILLS_DIR", raising=False)
    monkeypatch.delattr(sys, "frozen", raising=False)


def test_dev_defaults_are_the_legacy_ones(dev_mode):
    assert not paths.uses_user_dir()
    assert paths.env_file_setting() == ".env"
    assert paths.default_dsn() == "sqlite:///./data/local_agent.db"
    assert paths.default_vault_path() == "./data/vault"
    assert paths.default_sandbox_path() == "./data/sandbox"
    assert paths.data_dir().name == "data" and paths.data_dir().parent.name == "backend"
    assert paths.env_file().name == ".env" and paths.env_file().parent.name == "backend"


def test_override_moves_everything_into_one_dir(dev_mode, monkeypatch, tmp_path):
    monkeypatch.setenv("ARIA_DATA_DIR", str(tmp_path))
    assert paths.uses_user_dir()
    assert paths.data_dir() == tmp_path
    assert paths.pkg_data_dir() == tmp_path
    assert paths.env_file() == tmp_path / ".env"
    assert paths.skills_dir() == tmp_path / "skills"
    assert paths.default_vault_path() == str(tmp_path / "vault")
    dsn = paths.default_dsn()
    assert dsn.startswith("sqlite:///") and dsn.endswith("/local_agent.db")
    assert Path(dsn.removeprefix("sqlite:///")).parent == tmp_path


def test_frozen_uses_persistent_user_dir_not_meipass(dev_mode, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "meipass"), raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    expected = tmp_path / ("appdata" if os.name == "nt" else "xdg") / ("ARIA" if os.name == "nt" else "aria")
    assert paths.user_data_dir() == expected
    assert "meipass" not in str(paths.data_dir())
    assert paths.env_file() == expected / ".env"


def test_skills_env_override_wins(dev_mode, monkeypatch, tmp_path):
    monkeypatch.setenv("ARIA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ARIA_SKILLS_DIR", str(tmp_path / "myskills"))
    assert paths.skills_dir() == tmp_path / "myskills"


def test_ensure_copies_bundled_skills_but_never_overwrites(dev_mode, monkeypatch, tmp_path):
    bundle = tmp_path / "meipass"
    for name, text in (("foo", "bundled foo"), ("bar", "bundled bar")):
        d = bundle / "data" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(text, encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    user = tmp_path / "user"
    monkeypatch.setenv("ARIA_DATA_DIR", str(user))

    paths.ensure_user_data()
    assert (user / "skills" / "bar" / "SKILL.md").read_text(encoding="utf-8") == "bundled bar"
    for sub in ("vault", "logs", "skills"):
        assert (user / sub).is_dir()

    # user edits one skill; next start (upgrade) must not clobber it
    (user / "skills" / "foo" / "SKILL.md").write_text("MY EDIT", encoding="utf-8")
    paths.ensure_user_data()
    assert (user / "skills" / "foo" / "SKILL.md").read_text(encoding="utf-8") == "MY EDIT"


def test_blank_env_values_mean_default(dev_mode, monkeypatch, tmp_path):
    from aria.config import Settings

    monkeypatch.setenv("ARIA_DATA_DIR", str(tmp_path))
    s = Settings(_env_file=None, OBSIDIAN_VAULT_PATH="", POSTGRES_DSN="  ", agent_sandbox_root="")
    assert s.OBSIDIAN_VAULT_PATH == str(tmp_path / "vault")
    assert s.POSTGRES_DSN.endswith("/local_agent.db")
    assert s.agent_sandbox_root == str(tmp_path / "sandbox")

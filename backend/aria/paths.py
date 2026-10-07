"""Single source of truth for where ARIA keeps user data.

Dev run (from sources): everything stays where it always was
(backend/data, backend/.env, backend/aria/data) - nothing changes.

Installed app (PyInstaller, ``sys.frozen``) or explicit ``ARIA_DATA_DIR``:
everything lives in one persistent user directory
(``%APPDATA%\\ARIA`` on Windows, ``~/.local/share/aria`` elsewhere) instead of
the temporary ``_MEIPASS`` folder, which is wiped on exit.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _override() -> Path | None:
    raw = os.environ.get("ARIA_DATA_DIR", "").strip()
    return Path(raw).expanduser() if raw else None


def uses_user_dir() -> bool:
    return is_frozen() or _override() is not None


def user_data_dir() -> Path:
    override = _override()
    if override is not None:
        return override
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "ARIA"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "aria"


def data_dir() -> Path:
    """Runtime state (db, json state files, profiles)."""
    return user_data_dir() if uses_user_dir() else _BACKEND_ROOT / "data"


def pkg_data_dir() -> Path:
    """Legacy ``aria/data`` (toolsets.json). Same as data_dir() in user mode."""
    return user_data_dir() if uses_user_dir() else _BACKEND_ROOT / "aria" / "data"


def env_file() -> Path:
    return user_data_dir() / ".env" if uses_user_dir() else _BACKEND_ROOT / ".env"


def env_file_setting() -> str:
    """Value for pydantic ``env_file``: legacy cwd-relative in dev."""
    return str(env_file()) if uses_user_dir() else ".env"


def log_dir() -> Path:
    return user_data_dir() / "logs" if uses_user_dir() else _BACKEND_ROOT / "logs"


def skills_dir() -> Path:
    env = os.environ.get("ARIA_SKILLS_DIR", "").strip()
    if env:
        return Path(env)
    if uses_user_dir():
        return user_data_dir() / "skills"
    primary = _BACKEND_ROOT / "data" / "skills"
    legacy = _BACKEND_ROOT / "aria" / "data" / "skills"
    return legacy if (not primary.exists() and legacy.exists()) else primary


def bundled_skills_dir() -> Path | None:
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / "data" / "skills"
        if candidate.is_dir():
            return candidate
    return None


def default_dsn() -> str:
    if uses_user_dir():
        return "sqlite:///" + (user_data_dir() / "local_agent.db").as_posix()
    return "sqlite:///./data/local_agent.db"


def default_sandbox_path() -> str:
    if uses_user_dir():
        return str(user_data_dir() / "sandbox")
    return "./data/sandbox"


def default_vault_path() -> str:
    if uses_user_dir():
        return str(user_data_dir() / "vault")
    return "./data/vault"


def ensure_user_data() -> None:
    """Create the user dir tree and copy bundled skills on first run.

    Never overwrites: a skill folder that already exists in the user dir is
    left alone, so manual edits survive upgrades.
    """
    if not uses_user_dir():
        return
    root = user_data_dir()
    for sub in ("", "skills", "vault", "logs"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    bundled = bundled_skills_dir()
    target = skills_dir()
    if bundled is not None and bundled.resolve() != target.resolve():
        target.mkdir(parents=True, exist_ok=True)
        for item in bundled.iterdir():
            dest = target / item.name
            if item.is_dir() and not dest.exists():
                shutil.copytree(item, dest)

"""Общие помощники роутеров, вынесенные из stubs.py (локальное JSON-хранилище, путь к БД)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aria import paths


def data_dir() -> Path:
    return paths.data_dir()


def load_json(name: str, default: Any) -> Any:
    path = data_dir() / name
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return default


def save_json(name: str, data: Any) -> None:
    path = data_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    tmp.replace(path)


def action(name: str, ok: bool = True, message: str | None = None) -> dict[str, Any]:
    resp: dict[str, Any] = {"name": name, "ok": ok, "pid": None}
    if message:
        resp["message"] = message
    return resp


def db_path() -> Path:
    """Активный файл sqlite (settings.POSTGRES_DSN)."""
    try:
        from aria.config import get_settings

        dsn = get_settings().POSTGRES_DSN
        if dsn and dsn.startswith("sqlite:///"):
            raw = dsn.replace("sqlite:///", "", 1)
            candidate = Path(raw) if Path(raw).is_absolute() else Path.cwd() / raw
            if candidate.exists() or raw != "./data/local_agent.db":  # legacy relative default
                return candidate
    except Exception:
        pass
    return data_dir() / "local_agent.db"

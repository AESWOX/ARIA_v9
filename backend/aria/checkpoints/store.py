"""checkpoints/store.py — чекпоинты изменённых файлов (H4).

Перед записью файла тулом ``file_write`` loop/executor вызывает :func:`snapshot`: исходные байты файла
(или факт его отсутствия) сохраняются в ``data/checkpoints/<сессия>/<id>/``. :func:`restore` возвращает
файлы побайтно (созданные тулом файлы удаляются). :func:`prune` удаляет чекпоинты старше порога и/или
сверх лимита объёма (самые старые первыми).

Что НЕ покрыто: побочные эффекты ``shell_execute`` (какие файлы затронет команда, заранее неизвестно) —
для них чекпоинт не создаётся. Вместе с файлами сохраняется снимок состояния задачи (``task_state``: статус, роль, цель, число сообщений и вызовов тулов);
при восстановлении он возвращается в ответе, но БД не изменяет — статус задачи откатывать вслепую небезопасно.
Файлы крупнее ``MAX_FILE_BYTES`` не копируются; чекпоинт помечается
``complete=false`` и при восстановлении такие файлы пропускаются с явным предупреждением.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from aria import paths

logger = logging.getLogger("local_agent.checkpoints")

MAX_FILE_BYTES = 20 * 1024 * 1024
DEFAULT_PRUNE_DAYS = 14
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _base() -> Path:
    return paths.data_dir() / "checkpoints"


def _safe(name: str) -> str:
    return _SAFE.sub("_", str(name))[:80] or "none"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _within(root: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def snapshot(
    session_key: str,
    task_id: str | None,
    tool_name: str,
    sandbox_root: str,
    rel_paths: list[str],
    *,
    created_at: datetime | None = None,
    task_state: dict[str, Any] | None = None,
) -> str | None:
    """Снимок файлов ``rel_paths`` (относительно песочницы). Возвращает id чекпоинта или None."""
    root = Path(sandbox_root).resolve()
    entries: list[dict[str, Any]] = []
    cp_id = f"cp-{uuid.uuid4().hex[:12]}"
    cp_dir = _base() / _safe(session_key) / cp_id
    complete = True
    try:
        for rel in rel_paths:
            target = (root / rel).resolve()
            if not _within(root, target):
                continue
            entry: dict[str, Any] = {"path": target.relative_to(root).as_posix(), "existed": target.is_file(), "blob": None, "size": 0}
            if target.is_file():
                size = target.stat().st_size
                if size > MAX_FILE_BYTES:
                    entry["skipped"] = "too_large"
                    complete = False
                else:
                    blob = f"blob{len(entries)}"
                    cp_dir.mkdir(parents=True, exist_ok=True)
                    (cp_dir / blob).write_bytes(target.read_bytes())
                    entry["blob"], entry["size"] = blob, size
            entries.append(entry)
        if not entries:
            return None
        cp_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "id": cp_id,
            "session": str(session_key),
            "task_id": str(task_id) if task_id else None,
            "tool": tool_name,
            "root": str(root),
            "created_at": (created_at or _now()).isoformat(),
            "complete": complete,
            "task_state": task_state,
            "files": entries,
        }
        (cp_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return cp_id
    except Exception:  # noqa: BLE001
        logger.exception("checkpoint snapshot failed for %s", rel_paths)
        shutil.rmtree(cp_dir, ignore_errors=True)
        return None


def _manifests() -> list[tuple[Path, dict[str, Any]]]:
    out: list[tuple[Path, dict[str, Any]]] = []
    base = _base()
    if not base.is_dir():
        return out
    for mf in base.glob("*/*/manifest.json"):
        try:
            out.append((mf.parent, json.loads(mf.read_text(encoding="utf-8"))))
        except Exception:  # noqa: BLE001
            continue
    return out


def _dir_bytes(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def list_checkpoints(session: str | None = None, task_id: str | None = None) -> list[dict[str, Any]]:
    rows = []
    for cp_dir, mf in _manifests():
        if session is not None and mf.get("session") != str(session):
            continue
        if task_id is not None and mf.get("task_id") != str(task_id):
            continue
        rows.append({
            "id": mf["id"], "session": mf["session"], "task_id": mf.get("task_id"), "tool": mf.get("tool"),
            "created_at": mf["created_at"], "complete": mf.get("complete", True),
            "files": [f["path"] for f in mf["files"]], "bytes": _dir_bytes(cp_dir),
            "has_task_state": bool(mf.get("task_state")),
        })
    rows.sort(key=lambda r: r["created_at"])
    return rows


def summary() -> dict[str, Any]:
    """Форма ``CheckpointsResponse`` из api.ts + подробный список ``checkpoints``."""
    rows = list_checkpoints()
    per: dict[str, dict[str, Any]] = {}
    for r in rows:
        s = per.setdefault(r["session"], {"session": r["session"], "files": 0, "bytes": 0})
        s["files"] += len(r["files"])
        s["bytes"] += r["bytes"]
    return {"sessions": list(per.values()), "total_bytes": sum(r["bytes"] for r in rows), "checkpoints": rows}


def _find(cp_id: str) -> tuple[Path, dict[str, Any]] | None:
    for cp_dir, mf in _manifests():
        if mf.get("id") == cp_id:
            return cp_dir, mf
    return None


def restore(cp_id: str) -> dict[str, Any]:
    """Вернуть файлы чекпоинта побайтно. Файл, которого до записи не было, удаляется."""
    found = _find(cp_id)
    if found is None:
        raise KeyError(cp_id)
    cp_dir, mf = found
    root = Path(mf["root"])
    restored: list[str] = []
    removed: list[str] = []
    skipped: list[dict[str, str]] = []
    for f in mf["files"]:
        target = (root / f["path"]).resolve()
        if not _within(root, target):
            skipped.append({"path": f["path"], "reason": "escapes root"})
            continue
        if f.get("skipped"):
            skipped.append({"path": f["path"], "reason": f["skipped"]})
        elif f["existed"]:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((cp_dir / f["blob"]).read_bytes())
            restored.append(f["path"])
        elif target.exists():
            target.unlink()
            removed.append(f["path"])
    return {"id": cp_id, "restored": restored, "removed": removed, "skipped": skipped, "task_state": mf.get("task_state")}


def restore_task(task_id: str) -> list[dict[str, Any]]:
    """Откат всех чекпоинтов задачи в обратном порядке (новые первыми)."""
    rows = list_checkpoints(task_id=task_id)
    return [restore(r["id"]) for r in reversed(rows)]


def prune(older_than_days: float | None = None, max_bytes: int | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    """Удалить чекпоинты строго старше порога, затем — самые старые сверх ``max_bytes``."""
    if older_than_days is None and max_bytes is None:
        older_than_days = DEFAULT_PRUNE_DAYS
    now = now or _now()
    removed: list[str] = []
    freed = 0
    rows = sorted(_manifests(), key=lambda p: p[1]["created_at"])
    keep: list[tuple[Path, dict[str, Any]]] = []
    cutoff = now - timedelta(days=older_than_days) if older_than_days is not None else None
    for cp_dir, mf in rows:
        if cutoff is not None and datetime.fromisoformat(mf["created_at"]) < cutoff:
            freed += _dir_bytes(cp_dir)
            shutil.rmtree(cp_dir, ignore_errors=True)
            removed.append(mf["id"])
        else:
            keep.append((cp_dir, mf))
    if max_bytes is not None:
        total = sum(_dir_bytes(d) for d, _ in keep)
        for cp_dir, mf in keep:  # уже по возрастанию возраста
            if total <= max_bytes:
                break
            size = _dir_bytes(cp_dir)
            shutil.rmtree(cp_dir, ignore_errors=True)
            removed.append(mf["id"])
            freed += size
            total -= size
    base = _base()
    if base.is_dir():
        for d in base.iterdir():
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
    return {"removed": removed, "removed_count": len(removed), "freed_bytes": freed}


def selftest() -> dict[str, Any]:
    """Живая проверка для ``/system/self-test``: снимок → порча → откат → сравнение байтов → уборка."""
    import tempfile

    with tempfile.TemporaryDirectory(prefix="aria_cp_selftest_") as tmp:
        root = Path(tmp)
        original = b"selftest\x00\xff\r\n"
        (root / "probe.bin").write_bytes(original)
        cp_id = snapshot("_selftest", None, "selftest", str(root), ["probe.bin"])
        if cp_id is None:
            return {"ok": False, "error": "snapshot returned None"}
        try:
            (root / "probe.bin").write_bytes(b"changed")
            restore(cp_id)
            ok = (root / "probe.bin").read_bytes() == original
            return {"ok": ok, "error": None if ok else "restored bytes differ"}
        finally:
            found = _find(cp_id)
            if found:
                shutil.rmtree(found[0], ignore_errors=True)
            session_dir = _base() / "_selftest"
            if session_dir.is_dir() and not any(session_dir.iterdir()):
                session_dir.rmdir()

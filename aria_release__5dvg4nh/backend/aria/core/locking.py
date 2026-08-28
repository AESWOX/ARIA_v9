"""core/locking.py — §2.9 Optimistic locking + file-lock + TTL.

Обеспечивает:
1. Optimistic locking на task_plans (version + UPDATE WHERE version = ?)
2. File-lock для атомарных vault-записей (temp + rename)
3. Lock TTL = timeout для предотвращения вечных блокировок
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator, Self

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Значение TTL по умолчанию
DEFAULT_LOCK_TTL_SECONDS = 30
LOCK_DIR = ".locks"


class OptimisticLockError(Exception):
    """Конфликт версий при optimistic lock."""

    def __init__(self, task_id: str, expected_version: int, actual_version: int):
        self.task_id = task_id
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Optimistic lock conflict for task_plan {task_id[:8]}: "
            f"expected version {expected_version}, actual {actual_version}"
        )


async def update_task_plan_with_lock(
    session: AsyncSession,
    task_plan_id: str,
    expected_version: int,
    updates: dict[str, Any],
) -> int:
    """UPDATE task_plans с optimistic lock.

    Args:
        session: асинхронная сессия SQLAlchemy.
        task_plan_id: UUID записи task_plans.
        expected_version: ожидаемая версия (читается до изменения).
        updates: словарь полей для обновления.

    Returns:
        int: новая версия после инкремента.

    Raises:
        OptimisticLockError: если версия не совпала.
    """
    set_clause = ", ".join(f"{k} = :{k}" for k in updates)
    sql = text(f"""
        UPDATE task_plans
        SET {set_clause},
            version = version + 1,
            updated_at = datetime('now')
        WHERE id = :id AND version = :expected_version
        RETURNING version
    """)
    params = {"id": task_plan_id, "expected_version": expected_version, **updates}
    result = await session.execute(sql, params)
    row = result.fetchone()

    if row is None:
        # Проверяем текущую версию
        check = await session.execute(
            text("SELECT version FROM task_plans WHERE id = :id"),
            {"id": task_plan_id},
        )
        actual = check.scalar()
        raise OptimisticLockError(
            task_id=str(task_plan_id),
            expected_version=expected_version,
            actual_version=actual or 0,
        )

    return int(row[0])


# ═══════════════════════════════════════════════════════════════════
# File-lock (process-level) — для vault-записей
# ═══════════════════════════════════════════════════════════════════


class FileLockError(Exception):
    """Не удалось получить file-lock."""

    def __init__(self, lock_path: str, reason: str = ""):
        super().__init__(f"FileLock failed: {lock_path} — {reason}")
        self.lock_path = lock_path


def _lock_dir(vault_root: Path | None = None) -> Path:
    """Возвращает директорию для lock-файлов.

    Создаёт её при необходимости.
    """
    if vault_root is None:
        vault_root = Path("data/vault")
    lock_dir = vault_root / LOCK_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    return lock_dir


def _acquire_file_lock(lock_path: Path, ttl: int) -> bool:
    """Пытается захватить file-lock.

    Использует атомарное создание файла (O_EXCL).
    Если файл существует, проверяет TTL.

    Args:
        lock_path: путь к lock-файлу.
        ttl: TTL в секундах.

    Returns:
        True если lock захвачен.
    """
    try:
        # Атомарное создание
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "w") as f:
            f.write(f"{os.getpid()}\n{time.time()}\n")
        return True
    except FileExistsError:
        # Lock уже существует — проверяем TTL
        try:
            mtime = lock_path.stat().st_mtime
            if time.time() - mtime > ttl:
                # Lock просрочен — удаляем и пробуем снова
                lock_path.unlink(missing_ok=True)
                return _acquire_file_lock(lock_path, ttl)
        except OSError:
            pass
        return False


def _release_file_lock(lock_path: Path) -> None:
    """Освобождает file-lock."""
    try:
        lock_path.unlink(missing_ok=True)
    except OSError:
        pass


@contextmanager
def file_lock(
    vault_root: Path | str,
    task_id: str,
    ttl: int | None = None,
    retries: int = 10,
    retry_delay: float = 0.5,
) -> Generator[None, None, None]:
    """Контекстный менеджер для file-lock с TTL.

    Использование:
        with file_lock(vault_root, task_id):
            write_note(path, content)

    Args:
        vault_root: корень vault.
        task_id: UUID задачи.
        ttl: TTL в секундах (default: settings.lock_ttl_seconds или 30).
        retries: сколько раз повторять попытку.
        retry_delay: задержка между попытками.

    Yields:
        None — lock захвачен.

    Raises:
        FileLockError: если lock не получен после всех retry.
    """
    if ttl is None:
        ttl = DEFAULT_LOCK_TTL_SECONDS

    lock_dir = _lock_dir(vault_root)
    lock_path = lock_dir / f"{task_id}.lock"

    acquired = False
    for attempt in range(retries):
        if _acquire_file_lock(lock_path, ttl):
            acquired = True
            break
        if attempt < retries - 1:
            time.sleep(retry_delay)

    if not acquired:
        raise FileLockError(str(lock_path), f"not acquired after {retries} retries")

    try:
        yield
    finally:
        _release_file_lock(lock_path)

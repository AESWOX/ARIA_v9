"""Поиск исполняемых файлов для запуска MCP-серверов.

uv/uvx ставит установщик в <папка приложения>\\uv (см. desktop/src-tauri/install_uv.ps1).
Backend.exe в собранной сборке лежит в resources/ (Tauri externalBin), поэтому
рядом с ним каталога uv нет: проверяем и родительский каталог (resources/../uv).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

_BUNDLED = {"uv", "uvx"}


def bundled_uv_dirs() -> list[Path]:
    """Возможные каталоги встроенного uv (порядок поиска)."""
    dirs: list[Path] = []
    override = os.environ.get("ARIA_UV_DIR")
    if override:
        dirs.append(Path(override))
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        # Портативная сборка кладёт uv рядом с backend.exe; установленная —
        # на уровень выше, потому что backend.exe лежит в resources/.
        dirs.extend([exe_dir / "uv", exe_dir.parent / "uv"])
    return dirs


def bundled_uv_dir() -> Path | None:
    """Первый существующий каталог встроенного uv (для диагностики)."""
    for base in bundled_uv_dirs():
        if base.is_dir():
            return base
    return None


def resolve_executable(command: str) -> str:
    """Полный путь к команде: встроенный uv/uvx, затем PATH, иначе имя как есть."""
    stem = Path(command).stem.lower() if Path(command).name == command else ""
    if stem in _BUNDLED:
        for base in bundled_uv_dirs():
            for name in (f"{stem}.exe", stem):
                candidate = base / name
                if candidate.is_file():
                    return str(candidate)
    return shutil.which(command) or command

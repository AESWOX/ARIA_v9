"""Идемпотентный сидер навыков: синхронизирует skills_meta с диском.

Один навык = каталог в SKILLS_ROOT (backend/data/skills) с SKILL.md.
Сидер добавляет только отсутствующие записи и не трогает существующие,
поэтому безопасен для повторного запуска и не перетирает ручные правки
(статус active/archived из hub).

Вызывается:
- из startup приложения (aria/main.py),
- из MCP-сервера (aria/integrations/mcp_server/server.py),
- из conftest для чистого тестового прогона без dev-DB.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from sqlalchemy import select

from aria.db.base import session_scope
from aria.db.enums import SkillStatus
from aria.db.models import SkillMeta

if getattr(sys, "frozen", False):
    # PyInstaller: бандл лежит рядом с exe (onedir) или в _MEIPASS (onefile).
    # Path(__file__) внутри frozen указывает на _internal/aria/db/, а не на
    # дерево исходников — поэтому берём базовую директорию бандла явно.
    _BASE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    SKILLS_ROOT = Path(
        os.environ.get("ARIA_SKILLS_DIR")
        or (_BASE / "data" / "skills")
    )
else:
    SKILLS_ROOT = Path(
        os.environ.get("ARIA_SKILLS_DIR")
        or (Path(__file__).resolve().parent.parent.parent / "data" / "skills")
    )


def seed_skills() -> int:
    """Сканирует SKILLS_ROOT и добавляет отсутствующие навыки в skills_meta.

    Возвращает число добавленных записей.
    """
    if not SKILLS_ROOT.exists():
        return 0
    added = 0
    with session_scope() as db:
        existing = set(db.execute(select(SkillMeta.skill_name)).scalars())
        for path in sorted(SKILLS_ROOT.iterdir()):
            if not path.is_dir() or not (path / "SKILL.md").exists():
                continue
            if path.name in existing:
                continue
            db.add(SkillMeta(
                skill_name=path.name,
                category="general",
                status=SkillStatus.needs_adaptation,
                source_origin="migrated",
                needs_adaptation=True,
            ))
            added += 1
    return added


if __name__ == "__main__":
    from aria.db.base import init_db, run_migrations

    init_db()
    run_migrations()
    n = seed_skills()
    print(f"seeded {n} skills from {SKILLS_ROOT}")

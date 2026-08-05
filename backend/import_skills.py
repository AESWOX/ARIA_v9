"""Одноразовый импорт skills из легаси Hermes в skills_meta.
Запуск: python import_skills.py <путь_к_директории_с_skills>
"""
import sys
from pathlib import Path

from aria.db.base import session_scope, init_db
from aria.db.models import SkillMeta
from aria.db.enums import SkillStatus


def main(skills_dir: str) -> None:
    init_db(create_all=True)

    skills_root = Path(skills_dir)
    md_files = sorted(f for f in skills_root.rglob("SKILL.md") if f.is_file())
    if not md_files:
        raise SystemExit(f"skills не найдены в {skills_dir}")

    imported = 0
    with session_scope() as db:
        for md_path in md_files:
            # Имя навыка = относительный путь без .md, слеши заменяем на дефисы
            rel = md_path.relative_to(skills_root)
            # Категория = первая директория (если есть)
            parts = rel.parts
            if len(parts) > 1:
                name = "/".join(parts[:-1]) + "/" + parts[-1].replace(".md", "")
                category = parts[0]
            else:
                name = parts[-1].replace(".md", "")
                category = "general"

            existing = db.query(SkillMeta).filter_by(skill_name=name).first()
            if existing:
                continue

            db.add(SkillMeta(
                skill_name=name,
                category=category,
                status=SkillStatus.needs_adaptation,
                source_origin="migrated",
                needs_adaptation=True,
            ))
            imported += 1

    print(f"imported {imported} skills from {skills_dir}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python import_skills.py <path_to_skills_dir>")
        sys.exit(1)
    main(sys.argv[1])

"""Тесты интеграции Obsidian vault + skills_meta."""
import json
import os
import tempfile
import unittest

from aria.db.base import session_scope
from aria.db.models import SkillMeta


class VaultAndSkillsTests(unittest.TestCase):
    """Интеграционные тесты для Obsidian vault и импортированных навыков."""

    def test_skills_imported_count(self):
        """Skills должны быть в БД с корректными статусами (≥1, динамика от фактического импорта)."""
        with session_scope() as db:
            total = db.query(SkillMeta).count()
            migrated = db.query(SkillMeta).filter(
                SkillMeta.source_origin == 'migrated'
            ).count()
            needs_adapt = db.query(SkillMeta).filter(
                SkillMeta.status == 'needs_adaptation'
            ).count()

        self.assertGreater(total, 0, "В БД нет skills — забудь запустить import_skills.py")
        self.assertEqual(migrated, total, f"Все {total} skills должны быть source_origin='migrated', найдено {migrated}")
        self.assertEqual(needs_adapt, total, f"Все {total} skills должны быть status='needs_adaptation', найдено {needs_adapt}")

    def test_skills_have_valid_names(self):
        """Все импортированные навыки должны иметь непустые имена."""
        with session_scope() as db:
            names = [s.skill_name for s in db.query(SkillMeta).all()]

        self.assertGreater(len(names), 0)
        for name in names:
            self.assertTrue(len(name) > 0, f"Пустое имя навыка")

    def test_skills_unique_names(self):
        """Имена навыков должны быть уникальными."""
        with session_scope() as db:
            names = [s.skill_name for s in db.query(SkillMeta).all()]

        self.assertEqual(len(names), len(set(names)), "Обнаружены дубликаты имён навыков")

    def test_vault_notes_exist_on_disk(self):
        """Vault директория должна содержать .md файлы."""
        vault_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'vault')
        self.assertTrue(os.path.isdir(vault_path), f"Vault директория не найдена: {vault_path}")

        md_files = []
        for root, dirs, files in os.walk(vault_path):
            for f in files:
                if f.endswith('.md'):
                    md_files.append(os.path.join(root, f))

        self.assertGreater(len(md_files), 0, "В vault нет .md файлов")
        # Проверяем ключевые категории (соответствуют структуре second-brain из migration-package)
        categories = ['00-RAW', '03-PROJECTS', 'AGENTS', 'VAULT', 'DECISIONS']
        for cat in categories:
            cat_path = os.path.join(vault_path, cat)
            self.assertTrue(os.path.isdir(cat_path), f"Категория {cat} не найдена в vault")


if __name__ == '__main__':
    unittest.main()

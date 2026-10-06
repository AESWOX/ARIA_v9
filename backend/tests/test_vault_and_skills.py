"""Obsidian vault + skills_meta integration, against the hermetic fixtures.

These used to assert that the developer's own data (33 imported skills, a
populated vault under backend/data) was present - which made them fail on any
clean checkout and say nothing about the code. They now test the real code
paths (seeding, tree, search, read) on fixture data from conftest.py.
"""
import unittest
from pathlib import Path

from aria import paths
from aria.config import get_settings
from aria.db.base import session_scope
from aria.db.models import SkillMeta
from aria.db.skills_seed import seed_skills
from aria.storage import obsidian_vault as ov
from tests.conftest import FIXTURE_SKILLS, FIXTURE_VAULT


class VaultAndSkillsTests(unittest.TestCase):
    def test_suite_is_isolated_from_developer_data(self):
        root = Path(paths.user_data_dir()).resolve()
        self.assertTrue(str(root).startswith(str(Path(__import__("tempfile").gettempdir()).resolve())))
        self.assertIn(root.name, get_settings().POSTGRES_DSN)
        self.assertEqual(Path(get_settings().OBSIDIAN_VAULT_PATH).resolve(), root / "vault")
        self.assertEqual(paths.skills_dir().resolve(), root / "skills")

    def test_skills_seeded_from_disk(self):
        with session_scope() as db:
            rows = {s.skill_name: s for s in db.query(SkillMeta).all()}
        on_disk = {p.name for p in paths.skills_dir().iterdir() if (p / "SKILL.md").exists()}
        self.assertEqual(on_disk, set(FIXTURE_SKILLS))
        for name in on_disk:
            self.assertIn(name, rows, f"skill {name} on disk was not seeded")
            self.assertEqual(rows[name].source_origin, "migrated")
            self.assertEqual(rows[name].status, "needs_adaptation")

    def test_seed_is_idempotent_and_keeps_manual_changes(self):
        self.assertEqual(seed_skills(), 0)
        (paths.skills_dir() / "late-skill").mkdir(exist_ok=True)
        (paths.skills_dir() / "late-skill" / "SKILL.md").write_text("# late", encoding="utf-8")
        try:
            self.assertEqual(seed_skills(), 1)
            self.assertEqual(seed_skills(), 0)
        finally:
            with session_scope() as db:
                db.query(SkillMeta).filter(SkillMeta.skill_name == "late-skill").delete()
            (paths.skills_dir() / "late-skill" / "SKILL.md").unlink()
            (paths.skills_dir() / "late-skill").rmdir()

    def test_skill_names_unique_and_non_empty(self):
        with session_scope() as db:
            names = [s.skill_name for s in db.query(SkillMeta).all()]
        self.assertGreater(len(names), 0)
        self.assertTrue(all(n.strip() for n in names))
        self.assertEqual(len(names), len(set(names)), "duplicate skill names")

    def test_vault_tree_search_and_read_on_fixture(self):
        tree = ov.list_vault_tree()
        for cat in ("00-RAW", "03-PROJECTS", "AGENTS", "VAULT", "DECISIONS"):
            self.assertIn(cat, tree["dirs"])
        hits = ov.search_vault("fixture-token")
        self.assertEqual({h["file_path"] for h in hits["matches"]}, {"00-RAW/inbox.md", "03-PROJECTS/aria.md"})
        note = ov.read_note_by_path("03-PROJECTS/aria.md")
        self.assertTrue(note["found"])
        self.assertEqual(note["frontmatter"], {"status": "active"})
        self.assertEqual(note["wiki_links"], ["decision-1"])
        self.assertEqual(len(FIXTURE_VAULT), sum(1 for _ in ov.vault_root().rglob("*.md")) - self._extra_notes())

    @staticmethod
    def _extra_notes() -> int:
        # other tests may leave 00-TASKS notes behind; count only fixture notes
        return sum(1 for p in ov.vault_root().rglob("*.md") if p.relative_to(ov.vault_root()).as_posix() not in FIXTURE_VAULT)


if __name__ == "__main__":
    unittest.main()

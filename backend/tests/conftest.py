"""pytest configuration - hermetic test environment.

Every test run gets its OWN throw-away data directory (ARIA_DATA_DIR): a fresh
sqlite DB built from scratch via init_db + migrations, a fixture skills dir and
a fixture Obsidian vault. Nothing is copied from, or written to, the developer's
real backend/data, backend/.env or vault - so a clean `git clone` and a
developer machine give the same result, and a green run means the CODE works,
not that someone's local data happened to be present.

All env vars are set BEFORE any `aria` module is imported, because
config.get_settings is lru_cached.
"""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="aria_test_"))
atexit.register(shutil.rmtree, _TEST_ROOT, ignore_errors=True)

# Never inherit the developer's configuration or real provider keys.
for _name in (
    "ARIA_SKILLS_DIR", "OBSIDIAN_VAULT_PATH", "GEMINI_API_KEY", "GEMINI_API_KEYS", "GROQ_API_KEY",
    "GROQ_API_KEYS", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    "GEMINI_FLASH_MODEL", "GEMINI_PRO_MODEL", "VISION_GEMINI_API_KEYS", "COMPRESSION_GEMINI_API_KEYS",
):
    os.environ.pop(_name, None)

os.environ["ARIA_DATA_DIR"] = str(_TEST_ROOT)
os.environ["POSTGRES_DSN"] = "sqlite:///" + (_TEST_ROOT / "local_agent.db").as_posix()

FIXTURE_SKILLS = {
    "web-research": "# Web research\nSearch, read, summarise.",
    "code-review": "# Code review\nRead the diff, list risks.",
    "obsidian-notes": "# Obsidian notes\nRead and write notes in the vault.",
}
FIXTURE_VAULT = {
    "00-RAW/inbox.md": "# Inbox\nraw capture fixture-token",
    "03-PROJECTS/aria.md": "---\nstatus: active\n---\n# ARIA\nsee [[decision-1]] fixture-token",
    "AGENTS/oracle.md": "# Oracle\nplanner agent",
    "VAULT/index.md": "# Index\nentry point",
    "DECISIONS/decision-1.md": "# Decision 1\nuse sqlite",
}

for _name, _text in FIXTURE_SKILLS.items():
    _d = _TEST_ROOT / "skills" / _name
    _d.mkdir(parents=True, exist_ok=True)
    (_d / "SKILL.md").write_text(_text, encoding="utf-8")
for _rel, _text in FIXTURE_VAULT.items():
    _f = _TEST_ROOT / "vault" / _rel
    _f.parent.mkdir(parents=True, exist_ok=True)
    _f.write_text(_text, encoding="utf-8")

import pytest


def pytest_configure(config):
    """Build the schema from scratch and seed the fixture skills."""
    from aria.db.base import init_db, run_migrations
    from aria.db.skills_seed import seed_skills

    init_db()
    run_migrations()
    seed_skills()


def pytest_collection_modifyitems(items):
    """Auto-apply anyio marker to all async test functions."""
    for item in items:
        if item.get_closest_marker("anyio") is None:
            # Check if it's an async function
            if hasattr(item, "obj") and item.obj and hasattr(item.obj, "__code__"):
                try:
                    import inspect
                    if inspect.iscoroutinefunction(item.obj):
                        item.add_marker(pytest.mark.anyio)
                except Exception:
                    pass

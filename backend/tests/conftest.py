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
    "GEMINI_FLASH_MODEL", "GEMINI_PRO_MODEL", "GROQ_MODELS", "GROQ_MODEL", "VISION_GEMINI_API_KEYS", "COMPRESSION_GEMINI_API_KEYS",
):
    os.environ.pop(_name, None)

os.environ["ARIA_DATA_DIR"] = str(_TEST_ROOT)
os.environ["GROQ_MODELS_AUTO"] = "0"  # то же для Groq (aria/llm/groq_discovery.py)
os.environ["GEMINI_MODELS_AUTO"] = "0"  # тесты не ходят в сеть за списком моделей (0027); нужные тесты включают сами
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


_BASELINE: dict = {}


def pytest_configure(config):
    """Build the schema from scratch, seed the fixture skills and take a reference snapshot of the DB (B7)."""
    from aria.db import base as db_base
    from aria.db.base import init_db, run_migrations
    from aria.db.skills_seed import seed_skills

    init_db()
    run_migrations()
    seed_skills()
    _take_baseline(db_base)


def _sqlite_path() -> Path:
    return _TEST_ROOT / "local_agent.db"


def _take_baseline(db_base) -> None:
    """Эталонный снимок БД сразу после схемы+сида. Каждый тест стартует с него (см. _fresh_database)."""
    import sqlite3

    # Лениво создаваемые схемы (CREATE TABLE IF NOT EXISTS, кэш по id(engine)) должны попасть в снимок, иначе откат
    # удалит таблицы, а кэш будет считать их существующими.
    from aria.memory import store as memory_store

    memory_store._ensure()
    snap = _TEST_ROOT / "baseline.db"
    src = sqlite3.connect(_sqlite_path().as_posix())
    try:
        dst = sqlite3.connect(snap.as_posix())
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    _BASELINE["snapshot"] = snap
    _BASELINE["engine"] = db_base._engine
    _BASELINE["session_local"] = db_base._SessionLocal


def _restore_baseline_data() -> None:
    import sqlite3

    src = sqlite3.connect(_BASELINE["snapshot"].as_posix())
    try:
        dst = sqlite3.connect(_sqlite_path().as_posix(), timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


@pytest.fixture(scope="module", autouse=True)
def _restore_global_engine():
    """B7: тесты, подменяющие глобальный engine (init_db на свою БД), не должны влиять на соседние модули.

    После модуля возвращаем эталонный engine и закрываем те, что создал модуль.
    """
    from aria.db import base as db_base

    yield
    current = db_base._engine
    db_base._engine = _BASELINE["engine"]
    db_base._SessionLocal = _BASELINE["session_local"]
    if current is not None and current is not _BASELINE["engine"]:
        try:
            current.dispose()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture(autouse=True)
def _fresh_database():
    """B7: каждый тест начинает с одинакового состояния БД (схема + сид скиллов), в любом порядке запуска.

    Если модуль подменил engine на собственный файл БД, его содержимое не трогаем — он изолирован этим модулем.
    """
    from aria.db import base as db_base

    engine = db_base._engine
    db_file = getattr(getattr(engine, "url", None), "database", None)
    # Тот же файл БД (даже если тест пересоздал engine через init_db()) -> возвращаем эталонные данные.
    if db_file and Path(db_file).resolve() == _sqlite_path().resolve():
        _restore_baseline_data()
    yield


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

@pytest.fixture(autouse=True)
def _isolate_mcp_tool_registry():
    """Изоляция утечки состояния MCP между тестами (п.2 ТЗ).

    Тесты MCP-подсистемы (``tests/test_h7_mcp_oauth.py``) заводят серверы и регистрируют
    свои тулы в глобальном ``TOOL_REGISTRY``, но не всегда убирают их за собой. При
    случайном порядке запуска (``pytest-randomly``) такие тулы доживали до соседних
    тестов и роняли ``tests/test_h7_mcp_client.py`` на проверке
    «тулы MCP не должны оставаться в реестре» (в падении фигурировал чужой
    ``mcp__up__find_jobs``). Чистим реестр до и после каждого теста.
    """
    from aria.tools.registry import TOOL_REGISTRY

    def _drop_mcp_tools() -> None:
        for name in [n for n in list(TOOL_REGISTRY) if str(n).startswith("mcp__")]:
            TOOL_REGISTRY.pop(name, None)

    _drop_mcp_tools()
    yield
    _drop_mcp_tools()

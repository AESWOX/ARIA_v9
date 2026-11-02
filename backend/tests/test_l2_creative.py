"""L2 (TZ_PRODUCTION_LINE): creative role, skill template, path and call-count guards."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from aria.core import loop as loop_mod
from aria.core.roles import get_role
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus
from aria.llm.router import ProviderRouter
from aria.tools.handlers.files import PathEscapeError, file_write
from tests.test_compliance_v2 import FakeProvider

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "data" / "skills" / "creative-card"

EXPECTED_TOOLS = (
    "web_search",
    "web_fetch",
    "memory_search",
    "memory_save",
    "read_note",
    "write_note",
    "file_write",
)


def test_c1_role_whitelist_exact_and_no_shell():
    role = get_role("content_creative")
    assert role.tool_whitelist == EXPECTED_TOOLS
    assert "shell_execute" not in role.tool_whitelist


def test_c2_release_skill_gate_is_green():
    from scripts.check_release_skills import check, load_allowlist

    problems, _notes = check(ROOT / "data" / "skills", load_allowlist(), strict=True)
    assert problems == []


def test_c7_skill_stays_in_repo_but_is_not_bundled():
    """L4: creative-card — приватный навык владельца: остаётся в репозитории, но не в установщике."""
    from scripts.check_release_skills import load_allowlist

    allow = load_allowlist()
    assert "creative-card" not in allow, "creative-card must not be bundled into the public installer"
    assert SKILL.is_dir() and (SKILL / "SKILL.md").is_file(), "the skill must stay tracked in the repository"
    assert len(allow) == 3


def test_c3_template_renders_on_a_neutral_example(tmp_path):
    import sys

    sys.path.insert(0, str(SKILL))
    try:
        import render as render_mod
    finally:
        sys.path.pop(0)

    values = {
        "card_title": "Plant catalog entry",
        "role": "Greenhouse guide",
        "audience": "Home gardeners",
        "voice": "Calm, practical",
        "visual_anchors": "Terracotta pots, morning light",
        "boundaries": "Fictional; every character is an adult",
        "concept": "A short profile for one fictional plant keeper.",
        "beats": "1. Opens the greenhouse. 2. Checks seedlings. 3. Signs the log.",
        "notes": "Only species safe for indoor growing.",
    }
    output = render_mod.render((SKILL / "template.md").read_text(encoding="utf-8"), values)
    assert "{{" not in output
    for key in ("card_title", "concept", "beats", "notes"):
        assert values[key] in output
    target = tmp_path / "card.md"
    target.write_text(output, encoding="utf-8")
    assert target.is_file()


def test_c4_file_write_refuses_paths_outside_sandbox(tmp_path):
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    with pytest.raises(PathEscapeError):
        asyncio.run(file_write({"path": "../escape.md", "content": "x"}, str(sandbox)))


def test_c5_no_owner_card_samples_in_repo():
    markers = [m.strip() for m in os.environ.get("CREATIVE_CARD_SAMPLE_MARKERS", "").split(";") if m.strip()]
    if not markers:
        pytest.skip("CREATIVE_CARD_SAMPLE_MARKERS is not set by the owner")
    haystacks = list((ROOT / "data" / "skills").rglob("*")) + list((ROOT / "tests").rglob("*.py"))
    for path in haystacks:
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            for marker in markers:
                assert marker not in text, f"{marker} found in {path}"


def test_c6_creative_role_step_keeps_one_provider_call():
    provider = FakeProvider(text="card written")
    router = ProviderRouter()
    router.register(provider)

    async def _no_finalize(task_id, router_arg):
        return None

    original = loop_mod.finalize_task
    loop_mod.finalize_task = _no_finalize
    try:
        with session_scope() as db:
            session = repo.create_session(db, "l2-creative")
            task = repo.create_task(db, session, role="content_creative", objective="make one card")
            repo.set_task_status(db, task, TaskStatus.approved)
            task_id, session_id = task.id, session.id
        try:
            asyncio.run(loop_mod.execute_agent_loop(task_id, router, str(ROOT / "data")))
        finally:
            with session_scope() as db:
                db.query(m.Task).filter(m.Task.id == task_id).delete()
                db.query(m.Session).filter(m.Session.id == session_id).delete()
    finally:
        loop_mod.finalize_task = original
    assert provider.calls == 1

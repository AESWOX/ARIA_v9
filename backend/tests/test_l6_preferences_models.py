"""L6 (TZ_PRODUCTION_LINE): owner preferences in the stable prompt prefix, static role -> model map.

No live network, no timers: the prefix is compared byte for byte and role routing is
observed through the stub providers that actually receive the call.
"""
from __future__ import annotations

import asyncio
import json
import math
import uuid

import pytest
from sqlalchemy import select

from aria.config import get_settings
from aria.core import approvals, role_models
from aria.core.loop import execute_agent_loop, resume_after_approval
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import ApprovalStatus, TaskStatus
from aria.llm.providers.stub import StubProvider, final_answer, tool_call
from aria.llm.router import ProviderRouter
from aria.memory import preferences
from aria.routers import chat as chat_mod
from aria.tools.registry import TOOL_REGISTRY

FORMAT_MARKER = "карточки: сначала таблица полей"
LANGUAGE_MARKER = "отвечай по-русски"
ASK_MARKER = "не спрашивай про стек"


class RecordingStub(StubProvider):
    def __init__(self, provider_id: str, provider_class: str):
        super().__init__(provider_id=provider_id)
        self.provider_class = provider_class
        self.calls = 0
        self.seen: list[list] = []

    async def chat(self, messages, tools, timeout_sec):
        self.calls += 1
        self.seen.append(list(messages))
        return await super().chat(messages, tools, timeout_sec)


@pytest.fixture()
def pref_vault(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    settings = get_settings()
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", str(vault))
    preferences.reset_cache()
    yield vault
    preferences.reset_cache()


def _write_preferences(vault, fmt: str = FORMAT_MARKER, language: str = LANGUAGE_MARKER, ask: str = ASK_MARKER) -> None:
    (vault / "preferences.md").write_text(
        "# Предпочтения\n\n"
        f"## Формат\n- {fmt}\n\n"
        f"## Язык\n- {language}\n\n"
        f"## Не спрашивать\n- {ask}\n",
        encoding="utf-8",
    )


def _make_task(role: str = "general", objective: str = "do the task"):
    with session_scope() as db:
        session = repo.create_session(db, title=f"l6-{uuid.uuid4().hex[:6]}")
        task = repo.create_task(db, session, role=role, objective=objective)
        repo.set_task_status(db, task, TaskStatus.approved)
        return session.id, task.id


def _attach_providers(*classes: str):
    router = ProviderRouter()
    providers = {}
    for cls in classes:
        provider = RecordingStub(f"stub-{cls}", cls)
        for _ in range(6):
            provider.push(final_answer("OK"))
        router.register(provider)
        providers[cls] = provider
    return router, providers


# --- preferences.md: parsing, limit, stable prefix -------------------------


def test_preferences_block_has_the_three_owner_sections(pref_vault):
    _write_preferences(pref_vault)
    block = preferences.stable_block()
    assert "Формат" in block and FORMAT_MARKER in block
    assert "Язык" in block and LANGUAGE_MARKER in block
    assert "Не спрашивать" in block and ASK_MARKER in block


def test_preferences_block_is_byte_stable_between_calls(pref_vault):
    _write_preferences(pref_vault)
    assert preferences.stable_block() == preferences.stable_block()


def test_prefix_changes_only_after_the_file_changes(pref_vault):
    _write_preferences(pref_vault)
    first = preferences.stable_block()

    _write_preferences(pref_vault, fmt="карточки: списком")
    second = preferences.stable_block()
    assert second != first and "карточки: списком" in second

    again = preferences.stable_block()
    assert again == second


def test_preferences_block_is_capped_at_600_tokens_by_chars(pref_vault):
    (pref_vault / "preferences.md").write_text(
        "# Предпочтения\n\n## Формат\n" + "\n".join(f"- строка {i} " + "x" * 60 for i in range(200)) + "\n",
        encoding="utf-8",
    )
    block = preferences.stable_block()
    assert block
    assert math.ceil(len(block) / 4.2) <= 600, len(block)


def test_empty_file_keeps_the_previous_chat_prompt(pref_vault):
    assert preferences.stable_block() == ""


def test_chat_system_prompt_carries_preferences_as_stable_prefix(pref_vault):
    _write_preferences(pref_vault)
    prompt = chat_mod._system_prompt()
    assert FORMAT_MARKER in prompt and LANGUAGE_MARKER in prompt
    assert prompt == chat_mod._system_prompt()
    assert chat_mod._build_prompt([("user", "hi")])[0].content == prompt


def test_agent_role_prompt_carries_preferences(pref_vault):
    _write_preferences(pref_vault)
    router, providers = _attach_providers("standard_reasoning")
    _, task_id = _make_task()
    asyncio.run(execute_agent_loop(task_id, router, "."))
    system_prompt = providers["standard_reasoning"].seen[0][0].content
    assert FORMAT_MARKER in system_prompt and LANGUAGE_MARKER in system_prompt


# --- memory_save: preferences need an explicit owner Approve ---------------


def test_memory_save_preference_without_approval_changes_nothing(pref_vault):
    spec = TOOL_REGISTRY["memory_save"]
    out = asyncio.run(spec.handler(input_json={"layer": "preference", "section": "Язык", "content": "по-украински"}))
    assert out.get("status") == "approval_required"
    assert not (pref_vault / "preferences.md").exists()


def test_memory_save_preference_with_approval_writes_the_vault_file(pref_vault):
    spec = TOOL_REGISTRY["memory_save"]
    out = asyncio.run(
        spec.handler(input_json={"layer": "preference", "section": "Формат", "content": "таблица"}, approved=True)
    )
    assert out.get("saved") is True and out.get("path") == "preferences.md"
    text = (pref_vault / "preferences.md").read_text(encoding="utf-8")
    assert "## Формат" in text and "- таблица" in text


def test_memory_save_fact_keeps_working_without_approval(pref_vault):
    spec = TOOL_REGISTRY["memory_save"]
    out = asyncio.run(spec.handler(input_json={"layer": "fact", "content": f"факт {uuid.uuid4().hex}"}))
    assert out.get("saved") is True


def test_loop_pauses_on_preference_save_and_writes_after_approve(pref_vault):
    router, providers = _attach_providers("standard_reasoning")
    provider = providers["standard_reasoning"]
    provider._queue.clear()
    provider.push(tool_call("memory_save", {"layer": "preference", "section": "Язык", "content": LANGUAGE_MARKER}))
    provider.push(final_answer("saved"))
    _, task_id = _make_task(role="content_creative")

    asyncio.run(execute_agent_loop(task_id, router, "."))

    with session_scope() as db:
        task = repo.get_task(db, task_id)
        items = db.execute(select(m.AttentionItem).where(m.AttentionItem.task_id == task_id)).scalars().all()
        assert task.status == TaskStatus.awaiting_attention
        assert len(items) == 1 and items[0].status == ApprovalStatus.pending
        item_id = items[0].id
    assert not (pref_vault / "preferences.md").exists(), "до Approve файл не меняется"

    with session_scope() as db:
        approvals.resolve(db, repo.get_attention_item(db, item_id), approve=True)
        item = repo.get_attention_item(db, item_id)
        db.expunge(item)
    asyncio.run(resume_after_approval(task_id, router, ".", item))

    text = (pref_vault / "preferences.md").read_text(encoding="utf-8")
    assert "## Язык" in text and LANGUAGE_MARKER in text


def test_reject_never_writes_preferences(pref_vault):
    router, providers = _attach_providers("standard_reasoning")
    provider = providers["standard_reasoning"]
    provider._queue.clear()
    provider.push(tool_call("memory_save", {"layer": "preference", "section": "Формат", "content": FORMAT_MARKER}))
    _, task_id = _make_task(role="content_creative")
    asyncio.run(execute_agent_loop(task_id, router, "."))
    with session_scope() as db:
        items = db.execute(select(m.AttentionItem).where(m.AttentionItem.task_id == task_id)).scalars().all()
        approvals.resolve(db, repo.get_attention_item(db, items[0].id), approve=False)
    assert not (pref_vault / "preferences.md").exists()


def test_approve_endpoint_resumes_a_preference_item(pref_vault):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aria.api.auth import require_runtime_token
    from aria.core.taskrunner import TaskRunner
    from aria.routers import sessions as sessions_mod

    with session_scope() as db:
        session = repo.create_session(db, title=f"l6-api-{uuid.uuid4().hex[:6]}")
        task = repo.create_task(db, session, role="general", objective="save preference")
        repo.set_task_status(db, task, TaskStatus.approved)
        repo.set_task_status(db, task, TaskStatus.in_progress)
        item = approvals.request_preference_update_approval(
            db, session, task, {"layer": "preference", "section": "Язык", "content": LANGUAGE_MARKER}
        )
        item_id = item.id

    runner = TaskRunner(ProviderRouter(), ".", max_workers=1)
    app = FastAPI()
    app.include_router(sessions_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    app.state.task_runner = runner
    response = TestClient(app).post(f"/attention-items/{item_id}/approve")
    assert response.status_code == 200 and response.json() == {"ok": True, "resumed": True}, response.text
    job = runner._queue.get_nowait()
    assert job.approval_item_id == item_id and job.mode == "agent"


def test_general_role_stays_read_only_and_creative_can_save_preferences():
    from aria.core.roles import ROLE_REGISTRY

    assert "memory_save" not in ROLE_REGISTRY["general"].tool_whitelist
    assert "memory_save" in ROLE_REGISTRY["content_creative"].tool_whitelist


# --- static role -> provider class/model map ------------------------------


def test_role_models_map_overrides_the_role_default_class(monkeypatch):
    monkeypatch.setenv("ROLE_MODELS", json.dumps({"general": "premium_reasoning"}))
    get_settings.cache_clear()
    router, providers = _attach_providers("standard_reasoning", "premium_reasoning")
    _, task_id = _make_task(role="general")
    asyncio.run(execute_agent_loop(task_id, router, "."))
    assert providers["premium_reasoning"].calls == 1
    assert providers["standard_reasoning"].calls == 0


def test_role_models_accepts_a_concrete_provider_id(monkeypatch):
    monkeypatch.setenv("ROLE_MODELS", json.dumps({"general": "stub-premium_reasoning"}))
    get_settings.cache_clear()
    router, providers = _attach_providers("standard_reasoning", "premium_reasoning")
    _, task_id = _make_task(role="general")
    asyncio.run(execute_agent_loop(task_id, router, "."))
    assert providers["premium_reasoning"].calls == 1
    assert providers["standard_reasoning"].calls == 0


def test_role_models_bad_json_and_unknown_values_fall_back_silently(monkeypatch):
    monkeypatch.setenv("ROLE_MODELS", "{not json")
    get_settings.cache_clear()
    assert role_models.role_models_map() == {}
    assert role_models.resolve_role_model("general", "standard_reasoning") == ("standard_reasoning", None)

    monkeypatch.setenv("ROLE_MODELS", json.dumps({"general": "no_such_class"}))
    get_settings.cache_clear()
    assert role_models.resolve_role_model("general", "standard_reasoning") == ("standard_reasoning", None)


def test_role_models_do_not_add_llm_calls(monkeypatch):
    monkeypatch.setenv("ROLE_MODELS", json.dumps({"general": "premium_reasoning"}))
    get_settings.cache_clear()
    router, providers = _attach_providers("standard_reasoning", "premium_reasoning")
    _, task_id = _make_task(role="general")
    asyncio.run(execute_agent_loop(task_id, router, "."))
    total = sum(p.calls for p in providers.values())
    assert total == 1


def test_env_example_documents_the_role_models_map():
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / ".env.example"
    text = example.read_text(encoding="utf-8")
    assert "ROLE_MODELS=" in text

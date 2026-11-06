"""05.11 follow-up: concrete questions skip the clarifier, recall stays honest."""
from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.core import clarifier, recall_honesty
from aria.db import base as db_base
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter
from aria.routers.chat import router as chat_router


@pytest.fixture()
def env(tmp_path):
    saved = (db_base._engine, db_base._SessionLocal)
    db_base.init_db(f"sqlite:///{(tmp_path / 'recall.db').as_posix()}", create_all=True)
    yield
    db_base._engine, db_base._SessionLocal = saved


def _gate(content: str, context=None):
    with session_scope() as db:
        session = repo.create_session(db, "recall")
        task = repo.create_task(db, session, role="general", objective=content, draft_tz_md="# r\n")
        repo.set_task_status(db, task, TaskStatus.draft)
        task_id = task.id
    return asyncio.run(clarifier.run_gate(task_id, content, router=None, context=context))


def test_continuation_is_recognised():
    history = [("user", "Сделай лендинг на React"), ("assistant", "Готово")]
    assert clarifier.continues_dialog("а теперь добавь тёмную тему", history)
    assert clarifier.continues_dialog("и кнопку сохранения туда же", history)
    assert not clarifier.continues_dialog("посчитай налог за 2025 год", history)


def test_continuation_asks_no_questions(env):
    history = [("user", "Сделай лендинг на React"), ("assistant", "Готово")]
    out = _gate("а теперь добавь тёмную тему", context=history)
    assert out.questions == []
    assert out.status == "approved"


def test_a_concrete_stack_request_is_explicit():
    assert clarifier.explicit_goal_and_format("сверстай лендинг на React")
    assert clarifier.explicit_goal_and_format("напиши парсер на Python в файл parse.py")


def test_recall_question_is_detected_and_noted():
    assert recall_honesty.is_recall_question("мы такое делали?")
    assert recall_honesty.is_recall_question("а мы это генерили раньше?")
    assert recall_honesty.missing_note("мы такое делали?", ["", ""]).startswith("Проверка памяти")
    assert recall_honesty.missing_note("мы такое делали?", ["память: делали"]) == ""


class FakeLlm(LlmProvider):
    def __init__(self):
        self.provider_id = "fake-gemini"
        self.provider_class = "free_tier_reasoning"
        self.seen: list[list] = []

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        self.seen.append(list(messages))
        return LlmResponse(text="ответ")


def _client(provider):
    llm = ProviderRouter()
    llm.register(provider)
    app = FastAPI()
    app.include_router(chat_router)
    app.state.router = llm
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    return TestClient(app)


def _cleanup(sid):
    import uuid
    from aria.db import models as m
    from aria.db.models import Session as SessionRow

    with session_scope() as db:
        db.query(m.Message).filter(m.Message.session_id == uuid.UUID(sid)).delete()
        db.query(SessionRow).filter(SessionRow.id == uuid.UUID(sid)).delete()


def test_chat_prompt_carries_the_honest_recall_note(monkeypatch, env):
    from aria.memory import store as memory_store
    from aria.storage import obsidian_vault

    monkeypatch.setattr(memory_store, "recall_block", lambda *a, **k: "")
    monkeypatch.setattr(obsidian_vault, "relevant_block", lambda *a, **k: "")
    fake = FakeLlm()
    c = _client(fake)
    sid = c.post("/chat/sessions", json={}).json()["session_id"]
    try:
        assert c.post(f"/chat/sessions/{sid}/send", json={"content": "мы такое делали?"}).status_code == 200
        prompt = "\n".join(message.content for message in fake.seen[0])
        assert "Проверка памяти" in prompt
    finally:
        _cleanup(sid)


def test_chat_prompt_has_no_note_when_memory_matches(monkeypatch, env):
    from aria.memory import store as memory_store
    from aria.storage import obsidian_vault

    monkeypatch.setattr(memory_store, "recall_block", lambda *a, **k: "память: делали лендинг")
    monkeypatch.setattr(obsidian_vault, "relevant_block", lambda *a, **k: "")
    fake = FakeLlm()
    c = _client(fake)
    sid = c.post("/chat/sessions", json={}).json()["session_id"]
    try:
        assert c.post(f"/chat/sessions/{sid}/send", json={"content": "мы такое делали?"}).status_code == 200
        prompt = "\n".join(message.content for message in fake.seen[0])
        assert "Проверка памяти" not in prompt
        assert "память: делали лендинг" in prompt
    finally:
        _cleanup(sid)


@pytest.mark.parametrize("text", [
    "мы такое делали?",
    "а мы это генерили раньше?",
    "did we do this before?",
    "do you remember the flux config?",
])
def test_recall_questions_are_recognised(text):
    assert recall_honesty.is_recall_question(text) is True


@pytest.mark.parametrize("text", [
    "что такое SSRF",
    "сколько будет 2+2",
    "прочитай https://example.com",
])
def test_ordinary_questions_are_not_recall(text):
    assert recall_honesty.is_recall_question(text) is False


def test_recall_note_is_language_aware():
    russian = recall_honesty.missing_note("мы такое делали?", ["", "  "])
    english = recall_honesty.missing_note("did we do this before?", ["", ""])
    assert russian.startswith("Проверка памяти")
    assert english.startswith("Recall check")
    assert recall_honesty.missing_note("что такое SSRF", []) == ""


def test_rule_zero_needs_context_and_catches_pronouns():
    context = [("user", "Сделай лендинг на React"), ("assistant", "Готово")]
    assert clarifier.continues_dialog("а теперь добавь тёмную тему", None) is False
    assert clarifier.continues_dialog("а теперь добавь тёмную тему", context) is True
    assert clarifier.continues_dialog("сделай её красной", context) is True
    assert clarifier.continues_dialog("посчитай налог за 2025 год", context) is False


def test_language_instruction_matches_the_message():
    assert clarifier.language_name("write a poem") == "English"
    assert clarifier.language_name("Зроби звіт") == "Ukrainian"
    assert clarifier.language_name("сделай отчёт") == "Russian"
    assert "Answer only in Russian" in clarifier.language_instruction("сделай отчёт")

"""L3 Clarifier soft: explicit goal+format gives 0 cards, one round asks <=2, research is not blocked."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aria.core import clarifier
from aria.db import base as db_base
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus

# 4 фразы с явной целью и форматом
EXPLICIT = (
    "открой https://example.com и скажи заголовок",
    "собери отчёт по продажам и сохрани в файл generated/report.md; критерий: файл существует",
    "напиши функцию на python",
    "составь таблицу сравнения Postgres и SQLite",
)

# 3 исследовательские задачи
RESEARCH = (
    "найди ниши для кофейни в Берлине",
    "исследуй рынок электромобилей в Европе",
    "сравни Postgres и SQLite для локального приложения",
)

# 3 неоднозначные фразы
AMBIGUOUS = (
    "приведи дела в порядок",
    "сделай лучше",
    "помоги с проектом",
)


@pytest.fixture()
def env(tmp_path):
    saved = (db_base._engine, db_base._SessionLocal)
    db_base.init_db(f"sqlite:///{(tmp_path / 'l3.db').as_posix()}", create_all=True)
    yield
    db_base._engine, db_base._SessionLocal = saved


def _gate(content: str, router=None):
    with session_scope() as db:
        session = repo.create_session(db, "l3")
        task = repo.create_task(db, session, role="general", objective=content, draft_tz_md="# l3\n")
        repo.set_task_status(db, task, TaskStatus.draft)
        task_id = task.id
    return asyncio.run(clarifier.run_gate(task_id, content, router=router))


@pytest.mark.parametrize("content", EXPLICIT)
def test_explicit_goal_and_format_asks_zero_questions(env, content):
    out = _gate(content)
    assert out.questions == []
    assert out.status == "approved"
    assert any(a.startswith("принял:") for a in out.spec.assumptions)


@pytest.mark.parametrize("content", RESEARCH)
def test_research_tasks_are_not_blocked(env, content):
    out = _gate(content)
    assert len(out.questions) <= 1
    assert out.status == "approved"


@pytest.mark.parametrize("content", AMBIGUOUS)
def test_ambiguous_request_asks_at_most_two_questions(env, content):
    out = _gate(content)
    assert out.status == "awaiting_clarification"
    assert 1 <= len(out.questions) <= 2


def test_url_title_phrase_asks_zero():
    out = _gate("открой https://example.com и скажи заголовок")
    assert out.questions == [] and out.status == "approved"


def test_find_niches_phrase_asks_at_most_one():
    out = _gate("найди ниши X")
    assert len(out.questions) <= 1


def test_explicit_phrase_ignores_model_questions(env, monkeypatch):
    async def noisy(router, content, spec, **kwargs):
        return {
            "questions": [{"text": f"Вопрос {i}?"} for i in range(4)],
            "acceptance_criteria": [{"id": "ac-1", "statement": "exit_code=0"}],
            "assumptions": ["язык русский"],
        }

    monkeypatch.setattr(clarifier, "_ask_model", noisy)
    out = _gate("открой https://example.com и скажи заголовок", router=object())
    assert out.questions == [] and out.status == "approved"
    assert any("язык русский" in a for a in out.spec.assumptions)


def test_model_questions_are_capped_at_two_per_round(env, monkeypatch):
    async def many(router, content, spec, **kwargs):
        return {
            "questions": [{"text": f"Вопрос {i}?"} for i in range(5)],
            "acceptance_criteria": [{"id": "ac-1", "statement": "exit_code=0"}],
        }

    monkeypatch.setattr(clarifier, "_ask_model", many)
    out = _gate("сделай отчёт", router=object())
    assert out.status == "awaiting_clarification"
    assert len(out.questions) == 2


def test_prompt_states_the_soft_rules():
    prompt = (Path(clarifier.__file__).parent.parent / "prompts" / "clarifier.md").read_text(encoding="utf-8")
    assert "0 карточек" in prompt
    assert "не больше 2" in prompt
    assert "принял:" in prompt
    assert "5 вариантов" in prompt and "recommended" in prompt
    assert "ВСЕГДА" not in prompt

"""PR-3: TaskSpec, Clarifier-гейт, отказ автоперевода в approved."""
from __future__ import annotations

import pytest
from sqlalchemy import inspect

from aria.core import clarifier
from aria.core import taskspec as ts
from aria.core.taskspec import MAX_CLARIFY_ROUNDS, TaskSpec
from aria.db import repository as repo
from aria.db.base import get_engine, session_scope
from aria.db.enums import TaskStatus

GOOD = (
    "Собери отчёт по продажам и сохрани в файл generated/report.md; "
    "критерий: файл существует и содержит таблицу за квартал"
)


def _task(db, objective=GOOD):
    s = repo.create_session(db, "pr3")
    t = repo.create_task(db, s, role="general", objective=objective, draft_tz_md=f"# {objective[:20]}\n")
    repo.set_task_status(db, t, TaskStatus.draft)
    return s, t


# ─────────────────────────── TaskSpec ───────────────────────────

def test_verifiable_criterion_is_required():
    with pytest.raises(Exception):
        ts.AcceptanceCriterion(id="ac-1", statement="сделать хорошо")  # нет проверяемого условия
    assert ts.AcceptanceCriterion(id="ac-1", statement="файл generated/x.md существует").id == "ac-1"


def test_spec_validity_needs_goal_and_criteria():
    assert not TaskSpec(goal="сделай отчёт").is_valid
    spec = TaskSpec(goal="сделать отчёт", acceptance_criteria=[ts.default_criterion("отчёт")])
    assert spec.is_valid
    assert spec.missing_fields() == []


def test_open_questions_invalidate_the_spec():
    spec = TaskSpec(
        goal="сделать отчёт",
        acceptance_criteria=[ts.default_criterion("отчёт")],
        open_questions=["за какой квартал?"],
    )
    assert not spec.is_valid and spec.missing_fields() == ["open_questions"]


def test_derive_criteria_pulls_only_measurable_sentences():
    criteria = ts.derive_criteria("Сделай красиво. Файл report.md существует. Потом отдохни.")
    assert [c.id for c in criteria] == ["ac-1"]


def test_high_risk_detection():
    assert ts.is_high_risk("задеплой сервис в prod и ротацию ключей")
    assert not ts.is_high_risk("напиши черновик письма")


# ─────────────────────────── gate_required ───────────────────────────

def test_every_nonempty_request_goes_through_the_gate():
    """Решение владельца 19.10: структурированный запрос и вопросы — на ЛЮБОЙ непустой запрос."""
    assert clarifier.gate_required("привет", "agent") is True
    assert clarifier.gate_required("как дела?", "agent") is True
    assert clarifier.gate_required("   ", "agent") is False


def test_task_like_message_goes_through_the_gate():
    assert clarifier.gate_required("сделай отчёт по продажам", "agent") is True
    assert clarifier.gate_required("что угодно", "plan") is True


# ─────────────────────────── run_gate ───────────────────────────

@pytest.mark.anyio
async def test_gate_asks_a_deterministic_question_when_no_criteria(monkeypatch):
    with session_scope() as db:
        _s, t = _task(db, "приведи дела в порядок")
        task_id = t.id
    outcome = await clarifier.run_gate(task_id, "приведи дела в порядок", router=None)
    assert outcome.status == "awaiting_clarification"
    # модель недоступна (router=None) — всё равно спрашиваем: детерминированный набор с 5 вариантами
    assert len(outcome.questions) >= 3
    assert all(len(q["options"]) == 5 and q["default_value"] in q["options"] for q in outcome.questions)
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        row = repo.get_task_spec(db, task_id)
        assert task.status == TaskStatus.awaiting_clarification
        assert row is not None and row.valid is False and row.clarify_rounds == 1
        messages = repo.list_messages(db, task.session_id, limit=50)
    assert any(m.role == "assistant" and "нужны ответы" in m.content for m in messages)


@pytest.mark.anyio
async def test_gate_approves_when_model_returns_the_contract(monkeypatch):
    async def fake_ask(router, content, spec):
        return {
            "goal": "Собрать отчёт по продажам в файл",
            "acceptance_criteria": [
                {"id": "ac-1", "statement": "файл generated/report.md существует и содержит таблицу"}
            ],
            "questions": [],
            "assumptions": ["квартал — текущий по календарю"],
        }

    monkeypatch.setattr(clarifier, "_ask_model", fake_ask)
    with session_scope() as db:
        _s, t = _task(db)
        task_id = t.id
    first = await clarifier.run_gate(task_id, GOOD, router=object())
    # модель сочла запрос ясным, но вопросы задаются всегда (решение владельца)
    assert first.status == "awaiting_clarification" and len(first.questions) >= 3
    outcome = await clarifier.run_gate(task_id, GOOD, router=object())  # раунд 2: ответы получены
    assert outcome.status == "approved" and outcome.spec.is_valid
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        row = repo.get_task_spec(db, task_id)
        assert task.status == TaskStatus.approved
        assert row.valid is True
        spec = clarifier.load_spec(db, task_id)
        assert "квартал — текущий по календарю" in spec.assumptions
        assert clarifier.assert_runnable(db, task).is_valid


@pytest.mark.anyio
async def test_blocking_question_pauses_the_task(monkeypatch):
    async def fake_ask(router, content, spec):
        return {
            "questions": [{"text": "Какой квартал нужен?", "blocking": True, "field": "goal"}],
            "acceptance_criteria": [{"id": "ac-1", "statement": "файл отчёта существует"}],
            "assumptions": [],
        }

    monkeypatch.setattr(clarifier, "_ask_model", fake_ask)
    with session_scope() as db:
        _s, t = _task(db)
        task_id = t.id
    outcome = await clarifier.run_gate(task_id, GOOD, router=object())
    assert outcome.status == "awaiting_clarification"
    with session_scope() as db:
        assert repo.get_task(db, task_id).status == TaskStatus.awaiting_clarification
        assert repo.get_task_spec(db, task_id).clarify_rounds == 1


@pytest.mark.anyio
async def test_round_limit_turns_questions_into_assumptions(monkeypatch):
    async def fake_ask(router, content, spec):
        return {
            "questions": [{"text": "Какой квартал нужен?", "blocking": True, "field": "goal"}],
            "acceptance_criteria": [{"id": "ac-1", "statement": "файл отчёта существует"}],
            "assumptions": [],
        }

    monkeypatch.setattr(clarifier, "_ask_model", fake_ask)
    with session_scope() as db:
        _s, t = _task(db)
        repo.upsert_task_spec(
            db, t, TaskSpec(goal="Собрать отчёт по продажам", clarify_rounds=MAX_CLARIFY_ROUNDS).model_dump(mode="json"),
            valid=False, clarify_rounds=MAX_CLARIFY_ROUNDS,
        )
        task_id = t.id
    outcome = await clarifier.run_gate(task_id, GOOD, router=object())
    assert outcome.status == "approved"
    assert any("Лимит уточнений" in a for a in outcome.spec.assumptions)
    with session_scope() as db:
        assert repo.get_task(db, task_id).status == TaskStatus.approved


@pytest.mark.anyio
async def test_autonomous_mode_never_asks(monkeypatch):
    async def fake_ask(router, content, spec):  # pragma: no cover - не должен вызываться
        raise AssertionError("clarifier не вызывается в автономном режиме")

    monkeypatch.setattr(clarifier, "_ask_model", fake_ask)
    with session_scope() as db:
        _s, t = _task(db, "приведи дела в порядок")
        task_id = t.id
    outcome = await clarifier.run_gate(task_id, "приведи дела в порядок", autonomous=True)
    assert outcome.status == "approved"
    assert any("Автономный режим" in a for a in outcome.spec.assumptions)
    with session_scope() as db:
        assert repo.get_task(db, task_id).status == TaskStatus.approved


@pytest.mark.anyio
async def test_high_risk_task_goes_to_tz_approval(monkeypatch):
    async def fake_ask(router, content, spec):
        return {
            "goal": "Задеплоить сервис в prod",
            "acceptance_criteria": [{"id": "ac-1", "statement": "healthcheck возвращает 200 и exit_code=0"}],
            "questions": [],
            "assumptions": [],
        }

    monkeypatch.setattr(clarifier, "_ask_model", fake_ask)
    with session_scope() as db:
        _s, t = _task(db, "задеплой сервис в prod")
        task_id = t.id
    first = await clarifier.run_gate(task_id, "задеплой сервис в prod", router=object())
    assert first.status == "awaiting_clarification"
    outcome = await clarifier.run_gate(task_id, "задеплой сервис в prod", router=object())
    assert outcome.status == "awaiting_approval"
    with session_scope() as db:
        assert repo.get_task(db, task_id).status == TaskStatus.awaiting_approval
        items = repo.list_attention_items(db, only_pending=True)
    assert any(it.type.value == "task_tz_approval" for it in items)


def test_assert_runnable_refuses_a_task_without_a_contract():
    with session_scope() as db:
        _s, t = _task(db)
        with pytest.raises(clarifier.ClarifyRequired):
            clarifier.assert_runnable(db, t)


def test_task_specs_table_exists():
    assert "task_specs" in set(inspect(get_engine()).get_table_names())

"""Раунд 2, п.2.2 ТЗ: тесты эндпоинтов задач (/clarify, /audit-reports).

Стенд: FastAPI() + include_router(tasks.router) + dependency_overrides[require_runtime_token]
(паттерн test_a20_chat_failover.py). Слой БД подменяется детерминированно
(monkeypatch repo-функций и session_scope) — сценарии не зависят от состояния БД.

Обязательные сценарии ТЗ:
  * ответ на вопрос не меняет цель (регрессия п.1.1 — на старом коде падает);
  * два вопроса с одинаковым field получают разные id и не перетирают друг друга;
  * пустой запрос -> 400; accept_assumptions -> допущения записаны;
  * вызов на задаче не в awaiting_clarification -> 409;
  * инвариант: после любого ответа нет состояния «awaiting_clarification без вопросов»;
  * audit-reports для несуществующей задачи -> 404.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.core import clarifier, taskspec
from aria.core.taskspec import TaskSpec
from aria.db import repository as repo
from aria.db.enums import TaskStatus
from aria.routers import tasks as tasks_router

GOAL = "Собрать квартальный отчёт по продажам за Q3"


class FakeStore:
    """Детерминированная подмена слоя БД для эндпоинтов задач."""

    def __init__(self, *, task_status: TaskStatus, spec: TaskSpec | None, objective: str = GOAL):
        self.task = SimpleNamespace(
            id=uuid.uuid4(), session_id=uuid.uuid4(), status=task_status,
            objective=objective, draft_tz_md="", role="general",
        )
        self.spec = spec
        self.saved: dict = {}
        self.messages: list = []
        self.statuses: list = []

    # --- repo-функции, используемые эндпоинтами и clarifier.load_spec ---
    def get_task(self, db, task_id):
        return self.task if task_id == self.task.id else None

    def get_task_spec(self, db, task_id):
        if self.spec is None:
            return None
        return SimpleNamespace(spec_json=self.spec.model_dump(mode="json"))

    def upsert_task_spec(self, db, task, spec_json, valid=False, clarify_rounds=0):
        self.saved = {"spec_json": spec_json, "valid": valid, "clarify_rounds": clarify_rounds}
        return SimpleNamespace(spec_json=spec_json)

    def set_task_status(self, db, task, status):
        task.status = status
        self.statuses.append(status)

    def get_session(self, db, session_id):
        return None  # сессии нет -> ветка append_message пропускается

    def append_message(self, db, session, role="user", content=""):
        self.messages.append((role, content))

    def list_audit_reports(self, db, task_id):
        return []

    def list_child_tasks(self, db, task_id):
        return []


@pytest.fixture()
def client(monkeypatch):
    def _make(store: FakeStore) -> TestClient:
        app = FastAPI()
        app.include_router(tasks_router.router)
        app.dependency_overrides[require_runtime_token] = lambda: "test-token"

        @contextmanager
        def fake_scope():
            yield object()  # db — произвольный объект: repo-функции подменены

        monkeypatch.setattr(tasks_router, "session_scope", fake_scope)
        monkeypatch.setattr(repo, "get_task", store.get_task)
        monkeypatch.setattr(repo, "get_task_spec", store.get_task_spec)
        monkeypatch.setattr(repo, "upsert_task_spec", store.upsert_task_spec)
        monkeypatch.setattr(repo, "set_task_status", store.set_task_status)
        monkeypatch.setattr(repo, "get_session", store.get_session)
        monkeypatch.setattr(repo, "append_message", store.append_message)
        monkeypatch.setattr(repo, "list_audit_reports", store.list_audit_reports)
        monkeypatch.setattr(repo, "list_child_tasks", store.list_child_tasks)
        return TestClient(app, raise_server_exceptions=False)

    return _make


# ─────────────── регрессия п.1.1: ответ не затирает цель ───────────────

def test_answer_to_question_does_not_overwrite_goal(client):
    """Старый код: goal='PDF', questions=[], valid=False — тупик. Новый: цель цела,
    ответ — структурная запись, вопрос остаётся до полного контракта."""
    q1 = clarifier.question_id("В каком формате?")
    q2 = clarifier.question_id("Какой период?")
    spec = TaskSpec(
        goal=GOAL,
        clarify_questions=[
            {"id": q1, "text": "В каком формате?", "blocking": True, "field": "clarification"},
            {"id": q2, "text": "Какой период?", "blocking": True, "field": "clarification"},
        ],
    )
    spec.sync_questions()
    store = FakeStore(task_status=TaskStatus.awaiting_clarification, spec=spec)
    c = client(store)

    r = c.post(f"/tasks/{store.task.id}/clarify", json={
        "answers": [{"id": q1, "field": "clarification", "value": "PDF"}],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["valid"] is False  # второй вопрос ещё не отвечен
    assert body["status"] == "awaiting_clarification"
    # Цель НЕ затёрта
    saved = TaskSpec(**store.saved["spec_json"])
    assert saved.goal == GOAL
    # Ответ сохранён структурной записью, а не «q1: PDF» в constraints
    assert saved.clarifications == [{"question_id": q1, "question": "В каком формате?", "answer": "PDF"}]
    assert not saved.constraints
    # Неотвеченный вопрос остался, к нему добавлен недостающий слот
    # (acceptance_criteria из missing_fields) как новый вопрос — п.1.1.5.
    qids = [q["id"] for q in body["questions"]]
    assert q2 in qids
    assert any(q["field"] == "acceptance_criteria" for q in body["questions"])
    assert body["status"] == "awaiting_clarification"


def test_legacy_goal_answer_short_value_appends_instead_of_replace(client):
    """Старый формат данных (open_questions без id): ответ field=goal короче
    MIN_GOAL_CHARS дописывается к цели, а не заменяет её (п.1.1.3)."""
    spec = TaskSpec(goal=GOAL, open_questions=["В каком формате?"])
    store = FakeStore(task_status=TaskStatus.awaiting_clarification, spec=spec)
    c = client(store)
    r = c.post(f"/tasks/{store.task.id}/clarify", json={
        "answers": [{"id": "goal", "field": "goal", "value": "PDF"}],
    })
    assert r.status_code == 200
    saved = TaskSpec(**store.saved["spec_json"])
    assert saved.goal.startswith(GOAL) and "PDF" in saved.goal
    assert saved.goal != "PDF"


# ─────────────── п.1.1.1: стабильные и уникальные id вопросов ───────────────

def test_two_questions_with_same_field_get_distinct_ids():
    qs = clarifier._normalise_questions([
        {"text": "В каком формате?", "field": "goal"},
        {"text": "Какой период?", "field": "goal"},
    ])
    assert qs[0]["id"] and qs[1]["id"] and qs[0]["id"] != qs[1]["id"]
    # Поле по умолчанию — clarification, не goal (п.1.1.2)
    assert clarifier._normalise_questions(["Просто вопрос?"])[0]["field"] == "clarification"


def test_answers_to_same_field_questions_do_not_overwrite_each_other(client):
    q1 = clarifier.question_id("В каком формате?")
    q2 = clarifier.question_id("Какой период?")
    spec = TaskSpec(
        goal=GOAL,
        clarify_questions=[
            {"id": q1, "text": "В каком формате?", "blocking": True, "field": "clarification"},
            {"id": q2, "text": "Какой период?", "blocking": True, "field": "clarification"},
        ],
    )
    spec.sync_questions()
    store = FakeStore(task_status=TaskStatus.awaiting_clarification, spec=spec)
    c = client(store)
    r = c.post(f"/tasks/{store.task.id}/clarify", json={
        "answers": [
            {"id": q1, "field": "clarification", "value": "PDF"},
            {"id": q2, "field": "clarification", "value": "Q3 2026"},
        ],
    })
    assert r.status_code == 200
    saved = TaskSpec(**store.saved["spec_json"])
    answers = {c_["question_id"]: c_["answer"] for c_ in saved.clarifications}
    assert answers == {q1: "PDF", q2: "Q3 2026"}  # оба ответа на месте, не перетёрли друг друга


# ─────────────── п.1.2: пустой запрос и accept_assumptions ───────────────

def test_empty_answers_without_accept_assumptions_is_400(client):
    store = FakeStore(task_status=TaskStatus.awaiting_clarification, spec=TaskSpec(goal=GOAL, open_questions=["Формат?"]))
    c = client(store)
    r = c.post(f"/tasks/{store.task.id}/clarify", json={"answers": []})
    assert r.status_code == 400
    r = c.post(f"/tasks/{store.task.id}/clarify", json={})
    assert r.status_code == 400


def test_accept_assumptions_records_assumptions_and_unblocks(client):
    spec = TaskSpec(
        goal=GOAL,
        clarify_questions=[
            {"id": clarifier.question_id("Формат?"), "text": "Формат?", "blocking": True, "field": "clarification"},
            {"id": clarifier.question_id("Критерий?"), "text": "Критерий?", "blocking": True, "field": "acceptance_criteria"},
        ],
    )
    spec.sync_questions()
    store = FakeStore(task_status=TaskStatus.awaiting_clarification, spec=spec)
    c = client(store)
    r = c.post(f"/tasks/{store.task.id}/clarify", json={"answers": [], "accept_assumptions": True})
    assert r.status_code == 200, r.text
    saved = TaskSpec(**store.saved["spec_json"])
    assert any("Формат?" in a for a in saved.assumptions)  # неотвеченный вопрос -> допущение
    assert saved.acceptance_criteria  # слот критерия закрыт детерминированным критерием
    assert saved.clarify_questions == [] and saved.open_questions == []
    assert TaskStatus.approved in store.statuses  # допущения приняты -> идём дальше
    assert store.saved["valid"] is True


def test_accept_assumptions_keeps_goal_question_when_goal_is_empty(client):
    """Блокирующий слот, который нельзя допустить (пустая цель), остаётся вопросом (п.1.2.2)."""
    spec = TaskSpec(
        goal="",
        clarify_questions=[{"id": clarifier.question_id("Цель?"), "text": "Цель?", "blocking": True, "field": "goal"}],
    )
    spec.sync_questions()
    store = FakeStore(task_status=TaskStatus.awaiting_clarification, spec=spec, objective="кратко")
    c = client(store)
    r = c.post(f"/tasks/{store.task.id}/clarify", json={"answers": [], "accept_assumptions": True})
    assert r.status_code == 200
    saved = TaskSpec(**store.saved["spec_json"])
    assert len(saved.clarify_questions) == 1  # вопрос о цели НЕ закрыт допущением
    assert store.saved["valid"] is False


# ─────────────── п.1.2.3: счётчик раундов ───────────────

def test_clarify_rounds_increment_and_limit_accepts_assumptions(client):
    spec = TaskSpec(goal=GOAL, clarify_rounds=2, open_questions=["Формат?"])
    store = FakeStore(task_status=TaskStatus.awaiting_clarification, spec=spec)
    c = client(store)
    r = c.post(f"/tasks/{store.task.id}/clarify", json={"answers": [], "accept_assumptions": True})
    assert r.status_code == 200
    assert store.saved["clarify_rounds"] == 3  # MAX_CLARIFY_ROUNDS — поведение гейта
    saved = TaskSpec(**store.saved["spec_json"])
    assert any("Формат?" in a for a in saved.assumptions)


# ─────────────── п.1.3: статус задачи ───────────────

def test_clarify_requires_awaiting_clarification_status(client):
    store = FakeStore(task_status=TaskStatus.draft, spec=None)
    c = client(store)
    r = c.post(f"/tasks/{store.task.id}/clarify", json={"answers": [{"id": "goal", "value": "новая цель задачи"}]})
    assert r.status_code == 409
    assert "awaiting clarification" in r.json()["detail"]


# ─────────────── инвариант п.1.1.5 ───────────────

def test_invariant_no_awaiting_clarification_without_questions(client):
    """После любого ответа не бывает «awaiting_clarification без вопросов»:
    недостающие слоты возвращаются как новые вопросы."""
    q1 = clarifier.question_id("Формат?")
    spec = TaskSpec(
        goal=GOAL,
        clarify_questions=[{"id": q1, "text": "Формат?", "blocking": True, "field": "clarification"}],
    )
    spec.sync_questions()
    store = FakeStore(task_status=TaskStatus.awaiting_clarification, spec=spec)
    c = client(store)
    r = c.post(f"/tasks/{store.task.id}/clarify", json={
        "answers": [{"id": q1, "field": "clarification", "value": "PDF"}],
    })
    assert r.status_code == 200
    body = r.json()
    if body["status"] == "awaiting_clarification":
        assert body["questions"], "инвариант нарушен: awaiting_clarification без вопросов"
        fields = {q["field"] for q in body["questions"]}
        assert fields <= {"goal", "acceptance_criteria"}  # недостающие слоты стали вопросами
    else:
        assert body["valid"] is True and body["status"] == "approved"


# ─────────────── п.2.1: audit-reports ───────────────

def test_audit_reports_404_for_missing_task(client):
    c = client(FakeStore(task_status=TaskStatus.draft, spec=None))
    r = c.get(f"/tasks/{uuid.uuid4()}/audit-reports")
    assert r.status_code == 404


def test_audit_reports_route_declared_once():
    """П.2.1: маршрут GET /tasks/{id}/audit-reports объявлен ровно один раз."""
    routes = [
        r.path for r in tasks_router.router.routes
        if getattr(r, "path", "").endswith("/audit-reports") and "GET" in getattr(r, "methods", set())
    ]
    assert len(routes) == 1


# ─────────────── после approved задача обязана попасть в очередь (зависание «approved / none yet») ───────────────

class _FakeRunner:
    def __init__(self):
        self.calls = []

    def submit(self, task_id, mode="plan", source="ui", approval_item_id=None):
        self.calls.append({"task_id": task_id, "mode": mode, "source": source})
        return {"ok": True, "queued": True}


def _two_open_questions_spec():
    spec = TaskSpec(
        goal=GOAL,
        clarify_questions=[
            {"id": clarifier.question_id("Формат?"), "text": "Формат?", "blocking": True, "field": "clarification"},
            {"id": clarifier.question_id("Критерий?"), "text": "Критерий?", "blocking": True, "field": "acceptance_criteria"},
        ],
    )
    spec.sync_questions()
    return spec


def test_clarify_that_approves_the_task_submits_it_to_the_runner(client):
    store = FakeStore(task_status=TaskStatus.awaiting_clarification, spec=_two_open_questions_spec())
    c = client(store)
    runner = _FakeRunner()
    c.app.state.task_runner = runner
    r = c.post(f"/tasks/{store.task.id}/clarify", json={"answers": [], "accept_assumptions": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved" and body["queued"] is True and body["mode"] == "plan"
    assert len(runner.calls) == 1
    assert runner.calls[0]["source"] == "clarify" and runner.calls[0]["mode"] == "plan"


def test_clarify_passes_requested_mode_to_runner(client):
    store = FakeStore(task_status=TaskStatus.awaiting_clarification, spec=_two_open_questions_spec())
    c = client(store)
    runner = _FakeRunner()
    c.app.state.task_runner = runner
    r = c.post(f"/tasks/{store.task.id}/clarify", json={"answers": [], "accept_assumptions": True, "mode": "agent"})
    assert r.status_code == 200, r.text
    assert runner.calls[0]["mode"] == "agent"


def test_clarify_that_leaves_questions_does_not_submit(client):
    q1 = clarifier.question_id("В каком формате?")
    q2 = clarifier.question_id("Какой период?")
    spec = TaskSpec(
        goal=GOAL,
        clarify_questions=[
            {"id": q1, "text": "В каком формате?", "blocking": True, "field": "clarification"},
            {"id": q2, "text": "Какой период?", "blocking": True, "field": "clarification"},
        ],
    )
    spec.sync_questions()
    store = FakeStore(task_status=TaskStatus.awaiting_clarification, spec=spec)
    c = client(store)
    runner = _FakeRunner()
    c.app.state.task_runner = runner
    r = c.post(f"/tasks/{store.task.id}/clarify", json={"answers": [{"id": q1, "field": "clarification", "value": "PDF"}]})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "awaiting_clarification"
    assert runner.calls == []


def test_clarify_without_runner_does_not_crash_and_reports_not_queued(client):
    store = FakeStore(task_status=TaskStatus.awaiting_clarification, spec=_two_open_questions_spec())
    c = client(store)
    r = c.post(f"/tasks/{store.task.id}/clarify", json={"answers": [], "accept_assumptions": True})
    assert r.status_code == 200, r.text
    assert r.json()["queued"] is False


def test_clarify_rejects_unknown_mode(client):
    store = FakeStore(task_status=TaskStatus.awaiting_clarification, spec=_two_open_questions_spec())
    r = client(store).post(f"/tasks/{store.task.id}/clarify", json={"answers": [], "accept_assumptions": True, "mode": "bogus"})
    assert r.status_code == 400

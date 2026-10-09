"""Этап 2 («одна дверь»): режим agent/plan из UI, сообщение в чат-сессии начинает задачу,
лёгкое состояние запуска для опроса. Каждый тест падает на коде до этапа 2."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus
from aria.llm.router import ProviderRouter
from aria.routers import sessions as sessions_mod
from tests.test_wave1_runner_and_policy import _RecordingRunner


def _client() -> tuple[TestClient, _RecordingRunner]:
    app = FastAPI()
    app.include_router(sessions_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    rec = _RecordingRunner()
    app.state.task_runner = rec
    app.state.router = ProviderRouter()
    return TestClient(app), rec


def _chat_session(title: str = "chat-only") -> str:
    """Сессия, созданная чатом: задачи у неё нет."""
    with session_scope() as db:
        return str(repo.create_session(db, title, active_role="general").id)


def test_mode_plan_is_passed_to_runner():
    client, rec = _client()
    sid = _chat_session()
    r = client.post(f"/sessions/{sid}/messages", json={"content": "сделай", "mode": "plan"})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "plan"
    assert rec.submitted and rec.submitted[0][1] == "plan"


def test_mode_defaults_to_agent():
    client, rec = _client()
    sid = _chat_session()
    assert client.post(f"/sessions/{sid}/messages", json={"content": "x"}).status_code == 200
    assert rec.submitted[0][1] == "agent"


def test_unknown_mode_is_rejected_and_nothing_is_queued():
    client, rec = _client()
    sid = _chat_session()
    r = client.post(f"/sessions/{sid}/messages", json={"content": "x", "mode": "demo"})
    assert r.status_code == 400
    assert rec.submitted == []


def test_message_in_session_without_task_starts_a_task():
    client, rec = _client()
    sid = _chat_session()
    r = client.post(f"/sessions/{sid}/messages", json={"content": "сделай отчёт"})
    body = r.json()
    assert body["queued"] is True and body["task_id"]
    assert rec.submitted[0][0] == body["task_id"]


def test_message_after_finished_task_starts_a_new_task():
    client, rec = _client()
    with session_scope() as db:
        s = repo.create_session(db, "done-session", active_role="general")
        t = repo.create_task(db, s, role="general", objective="old")
        repo.set_task_status(db, t, TaskStatus.approved)
        repo.set_task_status(db, t, TaskStatus.in_progress)
        repo.set_task_status(db, t, TaskStatus.failed)
        sid, old_id = str(s.id), str(t.id)
    r = client.post(f"/sessions/{sid}/messages", json={"content": "ещё раз"})
    assert r.status_code == 200, r.text
    assert r.json()["task_id"] != old_id
    assert rec.submitted and rec.submitted[0][0] != old_id


def test_run_state_reports_status_and_terminal_flag():
    client, _ = _client()
    sid = _chat_session()
    r0 = client.get(f"/sessions/{sid}/run").json()
    assert r0["task_id"] is None and r0["status"] is None and r0["terminal"] is False
    client.post(f"/sessions/{sid}/messages", json={"content": "x"})
    r1 = client.get(f"/sessions/{sid}/run").json()
    assert r1["status"] == "approved" and r1["terminal"] is False and r1["attention"] == []


def test_run_state_unknown_session_is_404():
    client, _ = _client()
    r = client.get("/sessions/00000000-0000-0000-0000-000000000000/run")
    assert r.status_code == 404

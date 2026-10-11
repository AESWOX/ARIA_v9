"""Находки сквозного смоука (e2e_rehearsal): регрессии, пойманные на живом бэкенде."""
from __future__ import annotations

import sys

from aria.tools.handlers import shell


async def test_approved_shell_creates_missing_sandbox(tmp_path):
    sandbox = tmp_path / "fresh" / "sandbox"  # на свежем профиле каталога песочницы ещё нет
    assert not sandbox.exists()
    out = await shell.shell_execute({"command": f'"{sys.executable}" -c "print(41+1)"'}, timeout_sec=30, cwd=str(sandbox), approved=True)
    assert out["returncode"] == 0 and "42" in out["stdout"]
    assert sandbox.is_dir()


async def test_unapproved_shell_still_does_not_run_or_create_dirs(tmp_path):
    sandbox = tmp_path / "never"
    out = await shell.shell_execute({"command": 'python -c "print(1)"'}, timeout_sec=30, cwd=str(sandbox), approved=False)
    assert out["status"] == "approval_required"
    assert not sandbox.exists()  # без подтверждения — ничего не создаётся и не запускается


def test_fresh_seed_task_is_not_runnable():
    """Находка репетиции: демо-задача Bootstrap запускалась при каждом старте (approved -> needs_rework -> ...)."""
    from aria.core.taskrunner import RUNNABLE_STATUSES
    from aria.db import models as m
    from aria.db.base import session_scope
    from aria.db import repository as repo
    from aria.db.enums import TaskStatus
    from aria.http_utils import BOOTSTRAP_SESSION_TITLE, BOOTSTRAP_TASK_PREFIX

    with session_scope() as db:
        sess = repo.create_session(db, BOOTSTRAP_SESSION_TITLE, active_role="orchestrator")
        task = repo.create_task(db, sess, role="coder", objective=f"{BOOTSTRAP_TASK_PREFIX} test", draft_tz_md="#")
        status = task.status
        sid, tid = sess.id, task.id
    try:
        assert status not in RUNNABLE_STATUSES  # create_task оставляет draft, сид больше его не одобряет
    finally:
        with session_scope() as db:
            db.query(m.Task).filter(m.Task.id == tid).delete()
            db.query(m.Session).filter(m.Session.id == sid).delete()


async def test_resume_pending_parks_old_bootstrap_task_and_keeps_user_tasks():
    from aria.core.taskrunner import TaskRunner
    from aria.db import models as m
    from aria.db import repository as repo
    from aria.db.base import session_scope
    from aria.db.enums import TaskStatus
    from aria.http_utils import BOOTSTRAP_SESSION_TITLE, BOOTSTRAP_TASK_PREFIX

    with session_scope() as db:
        seed_s = repo.create_session(db, BOOTSTRAP_SESSION_TITLE, active_role="orchestrator")
        seed_t = repo.create_task(db, seed_s, role="coder", objective=f"{BOOTSTRAP_TASK_PREFIX} old profile", draft_tz_md="#")
        repo.set_task_status(db, seed_t, TaskStatus.approved)
        user_s = repo.create_session(db, "Моя задача", active_role="general")
        user_t = repo.create_task(db, user_s, role="general", objective="обычная задача пользователя", draft_tz_md="#")
        repo.set_task_status(db, user_t, TaskStatus.approved)
        ids = (seed_s.id, seed_t.id, user_s.id, user_t.id)
    submitted: list = []
    runner = TaskRunner.__new__(TaskRunner)  # без воркеров: нас интересует только решение «что ставить в очередь»
    runner.submit = lambda task_id, **kw: submitted.append(task_id) or {"queued": True}
    try:
        await TaskRunner.resume_pending(runner)
        assert ids[3] in submitted and ids[1] not in submitted
        with session_scope() as db:
            assert repo.get_task(db, ids[1]).status == TaskStatus.cancelled
            assert repo.get_task(db, ids[3]).status == TaskStatus.approved  # чужие задачи не трогаем
    finally:
        with session_scope() as db:
            db.query(m.Task).filter(m.Task.id.in_([ids[1], ids[3]])).delete(synchronize_session=False)
            db.query(m.Session).filter(m.Session.id.in_([ids[0], ids[2]])).delete(synchronize_session=False)


def test_run_state_exposes_failure_reason():
    """Живой смоук (11.10): задача падала за 1.7 с, а причины в API не было."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aria.api.auth import require_runtime_token
    from aria.db import models as m
    from aria.db import repository as repo
    from aria.db.base import session_scope
    from aria.db.enums import TaskStatus
    from aria.routers.sessions import router

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    with session_scope() as db:
        sess = repo.create_session(db, "run-state-test", active_role="general")
        task = repo.create_task(db, sess, role="general", objective="x", draft_tz_md="#")
        repo.set_task_status(db, task, TaskStatus.approved)
        repo.set_task_status(db, task, TaskStatus.in_progress)
        repo.set_task_status(db, task, TaskStatus.failed, error_code="provider_unavailable", error_message="All configured models are unavailable")
        sess.current_task_id = task.id
        sid, tid = sess.id, task.id
    try:
        r = TestClient(app).get(f"/sessions/{sid}/run")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "failed" and body["terminal"] is True
        assert body["error_code"] == "provider_unavailable" and "unavailable" in body["error_message"]
    finally:
        with session_scope() as db:
            db.query(m.Task).filter(m.Task.id == tid).delete()
            db.query(m.Session).filter(m.Session.id == sid).delete()


# ── агентский цикл: ошибки провайдера (живой смоук 11.10: агентские стадии падали за 1.7 с) ──

def _http_error(status: int, body: str):
    import httpx

    req = httpx.Request("POST", "http://provider.test/v1/chat/completions")
    return httpx.HTTPStatusError(f"{status}", request=req, response=httpx.Response(status, request=req, text=body))


def _failing_stub(status: int, body: str, pid="stub-fail", cls="subagent_execution"):
    from aria.llm.providers.stub import StubProvider

    class _Failing(StubProvider):
        calls = 0

        async def chat(self, messages, tools, timeout_sec):
            type(self).calls += 1
            raise _http_error(status, body)

    p = _Failing(provider_id=pid)
    p.provider_class = cls
    return p


def _run_loop_with(router, tmp_path):
    import asyncio

    from aria.core.loop import execute_agent_loop
    from aria.db import repository as repo
    from aria.db.base import session_scope
    from aria.db.enums import TaskStatus

    with session_scope() as db:
        session = repo.create_session(db, title="provider-errors")
        task = repo.create_task(db, session, role="coder", objective="say hi")
        repo.set_task_status(db, task, TaskStatus.approved)
        task_id, session_id = task.id, session.id
    asyncio.run(execute_agent_loop(task_id, router, str(tmp_path)))
    with session_scope() as db:
        t = repo.get_task(db, task_id)
        out = (t.status, t.error_code, t.error_message, [x.content for x in repo.list_messages(db, session_id, limit=50)])
    return out


def test_agent_loop_fails_over_to_fallback_class_on_429(tmp_path):
    from aria.db.enums import TaskStatus
    from aria.llm.providers.stub import StubProvider, final_answer
    from aria.llm.router import ProviderRouter

    bad = _failing_stub(429, "quota exceeded")
    good = StubProvider(provider_id="stub-good")
    good.provider_class = "standard_reasoning"
    good.push(final_answer("ANSWER-FROM-FALLBACK"))
    for _ in range(3):
        good.push(final_answer("OK"))
    router = ProviderRouter()
    router.register(bad)
    router.register(good)
    status, code, _msg, messages = _run_loop_with(router, tmp_path)
    assert status != TaskStatus.failed and code is None, (status, code)
    assert any("ANSWER-FROM-FALLBACK" in (c or "") for c in messages)
    assert type(bad).calls >= 1  # основная модель пробовалась, ответила запасная


def test_agent_loop_reports_provider_400_body_instead_of_generic_error(tmp_path):
    from aria.db.enums import TaskStatus
    from aria.llm.router import ProviderRouter

    bad = _failing_stub(400, '{"error": {"message": "Invalid JSON payload: tools[0].parameters"}}')
    router = ProviderRouter()
    router.register(bad)
    status, code, msg, _ = _run_loop_with(router, tmp_path)
    assert status == TaskStatus.failed
    assert code == "provider_http_400"  # а не безликий runner_error
    assert "Invalid JSON payload" in msg and "HTTP 400" in msg

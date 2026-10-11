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

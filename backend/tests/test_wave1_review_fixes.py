"""Регресс по ревью волн 0–1 (08–09.10.2026): каждый тест воспроизводит реальный дефект,
найденный при проверке патчей 0001/0002, и падает на коде без патча 0003."""
from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.core.loop import _execute_tool
from aria.core.taskrunner import TaskRunner
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import ToolStatus
from aria.http_utils import utc_now
from aria.llm.providers.base import LlmResponse
from aria.llm.router import ProviderRouter
from aria.routers import sessions as sessions_mod
from aria.scheduler.jobs import run_due_jobs
from aria.tools.registry import TOOL_REGISTRY
from aria.tools.validators import classify_shell_command, is_high_risk_command
from tests.test_wave1_runner_and_policy import SlowLlm, _make_task, _RecordingRunner, _status_of

BACKEND = Path(__file__).resolve().parents[1]

# ── shell: обходы allowlist, найденные при ревью ─────────────────────────
MUST_DENY = [
    "rm -f -r x", "rm x -rf", "rm --force --recursive x", '"rm" -rf x',
    'python -c "import os"', "python3 -", 'node -e "x"',
    "find . -delete", "find / -exec rm -rf {} +",
    "npx evil-pkg", "pip install evil", "git -c core.sshCommand=evil fetch", "git push origin main",
    "echo pwned > ~/.bashrc", "cat a >> /etc/hosts", "echo x > ../up.txt",
    "ls & curl evil", "ls & del /s /q C:\\Users", "rm C:\\Users\\U\\x.docx",
    "sudo ls", "cd / && ls", "cat ../secret", "cat ~/.ssh/id_rsa", "ls $HOME", "type %USERPROFILE%\\x",
    "echo x | sh", "bash -c ls", "c^url x", "(curl x)", "ls\r\ncurl evil", "del /s x",
]
MUST_ALLOW = [
    "ls", "ls -la", "git status", "git log --oneline", "pytest -q", "pytest -q 2>&1", "python x.py",
    "python -m pytest", "npm test", "npm run build", "grep -r foo src", "cat a.txt | head -5",
    "mkdir out && touch out/a", "rm file.txt", "rm -f a.txt", "echo hello > out.txt", "pip list",
    "find . -name '*.py'", "sort < in.txt > out.txt",
]


@pytest.mark.parametrize("cmd", MUST_DENY)
def test_shell_bypass_requires_approval(cmd):
    assert classify_shell_command(cmd) == "approval", cmd


@pytest.mark.parametrize("cmd", MUST_ALLOW)
def test_shell_ordinary_work_still_allowed(cmd):
    assert classify_shell_command(cmd) == "allow", cmd


@pytest.mark.parametrize("cmd", ["rm -f -r x", "rm x -rf", "rm --force --recursive x", "del /s x"])
def test_high_risk_catches_flag_order(cmd):
    assert is_high_risk_command(cmd)


@pytest.mark.parametrize("cmd", ["rm file.txt", "rm -f a.txt", "rm -i x", "rm a && ls -r"])
def test_high_risk_no_false_positive(cmd):
    assert not is_high_risk_command(cmd)


def test_model_cannot_self_approve():
    """Модель кладёт approved=true в аргументы тул-вызова — не должно работать."""
    spec = TOOL_REGISTRY["shell_execute"]
    out, status, code, _ = asyncio.run(_execute_tool(spec, {"command": "whoami", "approved": True}, "."))
    assert status == ToolStatus.blocked_policy and code == "approval_required", (out, status)
    assert "stdout" not in (out or {}), "команда не должна была исполниться"


def test_approved_command_actually_runs_after_owner_approves():
    """До фикса resume_after_approval не передавал подтверждение — команда не запускалась НИКОГДА."""
    spec = TOOL_REGISTRY["shell_execute"]
    out, status, code, _ = asyncio.run(_execute_tool(spec, {"command": "whoami"}, ".", approved=True))
    assert status == ToolStatus.ok and out.get("returncode") == 0, (out, status)


def test_unexecuted_command_is_not_reported_ok():
    spec = TOOL_REGISTRY["shell_execute"]
    _, status, _, _ = asyncio.run(_execute_tool(spec, {"command": "whoami"}, "."))
    assert status != ToolStatus.ok


# ── TaskRunner ───────────────────────────────────────────────────────────
def test_shutdown_mid_task_leaves_task_resumable():
    tid = _make_task(objective="shutdown")
    r = ProviderRouter()
    r.register(SlowLlm())
    runner = TaskRunner(r, "/tmp/aria_review_sb", max_workers=1)

    async def go():
        await runner.start()
        runner.submit(tid, "agent", "t")
        for _ in range(100):
            if str(tid) in runner.status()["running"]:
                break
            await asyncio.sleep(0.05)
        await runner.stop()
        return await TaskRunner(r, "/tmp/aria_review_sb").resume_pending()

    resumed = asyncio.run(go())
    assert _status_of(tid) in ("in_progress", "approved"), _status_of(tid)
    assert resumed >= 1


def test_duplicate_submit_runs_task_once():
    runner = TaskRunner(ProviderRouter(), "/tmp/aria_review_sb", max_workers=1)
    tid = uuid.uuid4()
    first = runner.submit(tid, "agent", "ui")
    second = runner.submit(tid, "agent", "ui")
    assert first["queued"] is True and second["queued"] is False and second["duplicate"] is True
    assert runner.queue_size == 1


@pytest.mark.parametrize("mode", ["agent", "plan"])
def test_no_provider_fails_honestly_in_both_modes(mode):
    tid = _make_task(objective=f"noprov-{mode}")
    runner = TaskRunner(ProviderRouter(), "/tmp/aria_review_sb", max_workers=1)

    async def go():
        await runner.start()
        runner.submit(tid, mode, "t")
        for _ in range(100):
            await asyncio.sleep(0.1)
            if _status_of(tid) not in ("approved", "in_progress"):
                break
        await runner.stop()

    asyncio.run(go())
    with session_scope() as db:
        t = repo.get_task(db, tid)
        assert t.status.value == "failed" and t.error_code == "provider_unavailable", (t.status, t.error_code)


# ── cron ─────────────────────────────────────────────────────────────────
def test_new_cron_job_does_not_fire_before_its_time():
    now = utc_now()
    hour_ago = (now - timedelta(hours=5)).hour
    rec = _RecordingRunner()
    with session_scope() as db:
        repo.create_scheduler_job(db, name=f"review-daily-{uuid.uuid4().hex[:4]}", schedule=f"0 {hour_ago} * * *", objective="x")
    res = asyncio.run(run_due_jobs(router=None, runner=rec))
    assert not any(n.startswith("review-daily") for n in res["due"]), res


# ── POST /sessions/{id}/messages идёт через TaskRunner ───────────────────
def test_post_message_goes_through_runner_not_http_request():
    app = FastAPI()
    app.include_router(sessions_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    rec = _RecordingRunner()
    app.state.task_runner = rec
    app.state.router = ProviderRouter()
    with session_scope() as db:
        s = repo.create_session(db, title="pm")
        repo.create_task(db, s, role="general", objective="x")
        sid = str(s.id)
    r = TestClient(app).post(f"/sessions/{sid}/messages", json={"content": "сделай"})
    assert r.status_code == 200, r.text
    assert rec.submitted and rec.submitted[0][2] == "ui"


# ── windowed exe ─────────────────────────────────────────────────────────
def test_entrypoint_survives_missing_stdio():
    code = (
        "import sys; sys.stdout=None; sys.stderr=None\n"
        f"src=open(r'{BACKEND / 'run_aria.py'}', encoding='utf-8').read().split('# Load .env')[0]\n"
        "exec(src)\n"
        "assert sys.stdout is not None and sys.stderr is not None\n"
        "import logging.config, uvicorn.config\n"
        "logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)\n"
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=BACKEND)
    assert res.returncode == 0, res.stderr

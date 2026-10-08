"""Регресс аудита 2026-10: SEC-1 (/sessions/prune), SEC-2 (rm-варианты), /env/reveal auth."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.routers import env as env_mod
from aria.routers import sessions as sessions_mod
from aria.tools.validators import is_high_risk_command

DANGEROUS = ["rm -rf /", "rm -fr /tmp/x", "rm -Rf x", "rm -rfv x", "rm --recursive x",
             "rm -r -f x", "sudo rm -rf /", "rm -r ~/docs"]
SAFE = ["rm file.txt", "rm -f a.txt", "ls -la", "echo rm", "git status", "rm -i x"]


@pytest.mark.parametrize("cmd", DANGEROUS)
def test_sec2_dangerous_rm_flagged(cmd):
    assert is_high_risk_command(cmd)


@pytest.mark.parametrize("cmd", SAFE)
def test_sec2_safe_not_flagged(cmd):
    assert not is_high_risk_command(cmd)


def test_sec1_prune_does_not_500():
    app = FastAPI()
    app.include_router(sessions_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    r = TestClient(app).post("/sessions/prune", json={"older_than_days": 36500})
    assert r.status_code == 200, r.text


def test_reveal_requires_token():
    app = FastAPI()
    app.include_router(env_mod.router)
    c = TestClient(app)
    assert c.post("/env/reveal", json={"key": "PATH"}).status_code in (401, 403)
    assert c.post("/env/reveal", json={"key": "PATH"},
                  headers={"Authorization": "Bearer wrong"}).status_code in (401, 403)


def test_curator_run_not_faked():
    import asyncio
    from aria.routers import stubs
    res = asyncio.run(stubs.curator_run(_="t"))
    assert res.get("ok") is False, res


def test_task_status_on_real_task():
    import asyncio
    from aria.db import repository as repo
    from aria.db.base import session_scope
    from aria.tools.registry import TOOL_REGISTRY
    with session_scope() as db:
        s = repo.create_session(db, title="ts")
        db.flush()
        t = repo.create_task(db, s, role="general", objective="x")
        db.flush()
        tid = str(t.id)
    spec = TOOL_REGISTRY["task_status"]
    out = asyncio.run(spec.handler(input_json={"task_id": tid}, timeout_sec=10, sandbox_root="."))
    assert out.get("status") and "not_found" not in out.get("status"), out

"""H4: хуки жизненного цикла и чекпоинты.

Критерии DoD: хук реально вызывается на событии и может заблокировать тул; откат к чекпоинту возвращает
файлы побайтно; ``prune`` удаляет ровно то, что старше порога."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria import paths
from aria.api.auth import require_runtime_token
from aria.checkpoints import store as cps
from aria.core.loop import execute_agent_loop
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus
from aria.hooks import EVENTS, engine, fire, save_hooks
from aria.llm.providers.stub import StubProvider, final_answer, tool_call
from aria.llm.router import ProviderRouter
from aria.routers import ops_hooks as ops_router_mod


@pytest.fixture(autouse=True)
def isolated_data(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")
    (tmp_path / "data").mkdir()
    yield tmp_path


def _script(tmp_path: Path, name: str, body: str) -> str:
    f = tmp_path / name
    f.write_text(body, encoding="utf-8")
    return f'"{sys.executable}" "{f}"'


def _hook(event, command, *, allowed=True, matcher=None, timeout=None):
    return {"event": event, "command": command, "allowed": allowed, "matcher": matcher, "timeout": timeout}


def _run(coro):
    return asyncio.run(coro)


# ── хуки: ядро ───────────────────────────────────────────────────────────
def test_hook_receives_payload_on_stdin_and_env(tmp_path):
    out = tmp_path / "seen.json"
    cmd = _script(tmp_path, "h.py", f"import sys,os,json\nd=json.load(sys.stdin)\nopen(r'{out}','w').write(json.dumps([d, os.environ['ARIA_HOOK_EVENT']]))\n")
    save_hooks([_hook("post_tool", cmd)])
    decision = _run(fire("post_tool", {"status": "ok"}, tool_name="file_write"))
    assert not decision.blocked and decision.runs[0].exit_code == 0
    payload, env_event = json.loads(out.read_text(encoding="utf-8"))
    assert payload["tool"] == "file_write" and payload["status"] == "ok" and env_event == "post_tool"


def test_unapproved_hook_is_not_executed(tmp_path):
    marker = tmp_path / "ran"
    cmd = _script(tmp_path, "h.py", f"open(r'{marker}','w').write('x')\n")
    save_hooks([_hook("pre_tool", cmd, allowed=False)])
    decision = _run(fire("pre_tool", {}, tool_name="file_write"))
    assert not marker.exists() and not decision.runs and decision.skipped[0]["reason"] == "not approved"


def test_pre_tool_exit_2_blocks_with_reason(tmp_path):
    cmd = _script(tmp_path, "h.py", "import sys\nsys.stderr.write('нельзя писать в prod')\nsys.exit(2)\n")
    save_hooks([_hook("pre_tool", cmd)])
    decision = _run(fire("pre_tool", {}, tool_name="file_write"))
    assert decision.blocked and "prod" in decision.reason


def test_pre_tool_json_block(tmp_path):
    cmd = _script(tmp_path, "h.py", "print('{\"block\": true, \"reason\": \"policy\"}')\n")
    save_hooks([_hook("pre_tool", cmd)])
    decision = _run(fire("pre_tool", {}, tool_name="shell_execute"))
    assert decision.blocked and decision.reason == "policy"


def test_failing_hook_does_not_block(tmp_path):
    cmd = _script(tmp_path, "h.py", "import sys\nsys.exit(1)\n")
    save_hooks([_hook("pre_tool", cmd)])
    decision = _run(fire("pre_tool", {}, tool_name="file_write"))
    assert not decision.blocked and decision.runs[0].exit_code == 1


def test_only_pre_tool_can_block(tmp_path):
    cmd = _script(tmp_path, "h.py", "import sys\nsys.exit(2)\n")
    save_hooks([_hook("post_tool", cmd), _hook("task_close", cmd)])
    assert not _run(fire("post_tool", {}, tool_name="x")).blocked
    assert not _run(fire("task_close", {})).blocked


def test_matcher_filters_by_tool_name(tmp_path):
    cmd = _script(tmp_path, "h.py", "import sys\nsys.exit(2)\n")
    save_hooks([_hook("pre_tool", cmd, matcher="^shell_")])
    assert _run(fire("pre_tool", {}, tool_name="shell_execute")).blocked
    assert not _run(fire("pre_tool", {}, tool_name="file_write")).blocked


def test_timeout_kills_hook_and_does_not_block(tmp_path):
    cmd = _script(tmp_path, "h.py", "import time\ntime.sleep(30)\n")
    save_hooks([_hook("pre_tool", cmd, timeout=1)])
    started = time.monotonic()
    decision = _run(fire("pre_tool", {}, tool_name="file_write"))
    assert time.monotonic() - started < 10
    assert decision.runs[0].timed_out and not decision.blocked


def test_secret_env_is_not_passed_to_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_SERVICE_API_KEY", "super-secret-value")
    out = tmp_path / "env.txt"
    cmd = _script(tmp_path, "h.py", f"import os\nopen(r'{out}','w').write(repr(dict(os.environ)))\n")
    save_hooks([_hook("task_start", cmd)])
    _run(fire("task_start", {}))
    assert "super-secret-value" not in out.read_text(encoding="utf-8")


def test_fire_never_raises_on_broken_hook_and_unknown_event(tmp_path):
    save_hooks([_hook("pre_tool", "definitely-not-a-command-xyz")])
    assert not _run(fire("pre_tool", {}, tool_name="a")).blocked
    assert _run(fire("no_such_event", {})).runs == []
    (tmp_path / "data" / "hooks.json").write_text("{broken", encoding="utf-8")
    assert _run(fire("pre_tool", {}, tool_name="a")).runs == []


def test_output_is_capped(tmp_path):
    cmd = _script(tmp_path, "h.py", f"import sys\nsys.stdout.write('a'*{engine.MAX_OUTPUT_BYTES * 3})\n")
    save_hooks([_hook("post_tool", cmd)])
    run = _run(fire("post_tool", {}, tool_name="x")).runs[0]
    assert len(run.stdout) <= engine.MAX_OUTPUT_BYTES


# ── чекпоинты: ядро ──────────────────────────────────────────────────────
def test_restore_returns_bytes_exactly(tmp_path):
    root = tmp_path / "sb"; root.mkdir()
    original = b"\x00\xff\r\n\x80binary\r\ntext \xd0\xb9\n"
    (root / "a.bin").write_bytes(original)
    cp = cps.snapshot("s1", "t1", "file_write", str(root), ["a.bin"])
    (root / "a.bin").write_bytes(b"overwritten")
    result = cps.restore(cp)
    assert (root / "a.bin").read_bytes() == original and result["restored"] == ["a.bin"]


def test_restore_removes_file_that_did_not_exist(tmp_path):
    root = tmp_path / "sb"; root.mkdir()
    cp = cps.snapshot("s1", "t1", "file_write", str(root), ["new/x.txt"])
    (root / "new").mkdir(); (root / "new" / "x.txt").write_text("created")
    result = cps.restore(cp)
    assert not (root / "new" / "x.txt").exists() and result["removed"] == ["new/x.txt"]


def test_snapshot_ignores_paths_outside_root(tmp_path):
    root = tmp_path / "sb"; root.mkdir()
    (tmp_path / "secret.txt").write_text("s")
    assert cps.snapshot("s1", "t1", "file_write", str(root), ["../secret.txt"]) is None


def test_restore_unknown_checkpoint_raises():
    with pytest.raises(KeyError):
        cps.restore("cp-nope")


def test_prune_removes_exactly_what_is_older_than_threshold(tmp_path):
    root = tmp_path / "sb"; root.mkdir()
    (root / "f.txt").write_text("v")
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    ids = {age: cps.snapshot("s1", "t1", "file_write", str(root), ["f.txt"], created_at=now - timedelta(days=age)) for age in (10, 8, 6, 1)}
    result = cps.prune(older_than_days=7, now=now)
    assert sorted(result["removed"]) == sorted([ids[10], ids[8]])
    left = {r["id"] for r in cps.list_checkpoints()}
    assert left == {ids[6], ids[1]}


def test_prune_by_size_drops_oldest_first(tmp_path):
    root = tmp_path / "sb"; root.mkdir()
    (root / "f.txt").write_bytes(b"x" * 1000)
    now = datetime(2026, 10, 9, tzinfo=timezone.utc)
    old = cps.snapshot("s1", "t1", "file_write", str(root), ["f.txt"], created_at=now - timedelta(days=3))
    mid = cps.snapshot("s1", "t1", "file_write", str(root), ["f.txt"], created_at=now - timedelta(days=2))
    new = cps.snapshot("s1", "t1", "file_write", str(root), ["f.txt"], created_at=now - timedelta(days=1))
    one = next(r["bytes"] for r in cps.list_checkpoints() if r["id"] == new)
    result = cps.prune(max_bytes=one + 10, now=now)
    assert result["removed"] == [old, mid] and {r["id"] for r in cps.list_checkpoints()} == {new}


def test_oversized_file_is_flagged_not_silently_restored(tmp_path, monkeypatch):
    monkeypatch.setattr(cps, "MAX_FILE_BYTES", 10)
    root = tmp_path / "sb"; root.mkdir()
    (root / "big.bin").write_bytes(b"y" * 100)
    cp = cps.snapshot("s1", "t1", "file_write", str(root), ["big.bin"])
    assert next(r for r in cps.list_checkpoints() if r["id"] == cp)["complete"] is False
    (root / "big.bin").write_bytes(b"changed")
    assert cps.restore(cp)["skipped"][0]["reason"] == "too_large"
    assert (root / "big.bin").read_bytes() == b"changed"


# ── интеграция с loop ────────────────────────────────────────────────────
def _loop_env(tmp_path, scripted):
    router = ProviderRouter()
    sub = StubProvider(provider_id="stub-sub"); sub.provider_class = "subagent_execution"
    std = StubProvider(provider_id="stub-std"); std.provider_class = "standard_reasoning"
    for r in scripted:
        sub.push(r)
    for _ in range(3):
        std.push(final_answer("OK"))
    router.register(sub); router.register(std)
    sandbox = tmp_path / "sandbox"; sandbox.mkdir()
    return router, str(sandbox)


def _make_task():
    with session_scope() as db:
        session = repo.create_session(db, title="h4")
        task = repo.create_task(db, session, role="coder", objective="write a file")
        repo.set_task_status(db, task, TaskStatus.approved)
        return task.id, str(session.id)


def _tool_calls(task_id):
    with session_scope() as db:
        return [(c.tool_name, c.status.value, c.error_code) for c in repo.list_tool_calls(db, task_id)]


def test_loop_pre_tool_hook_blocks_file_write(tmp_path):
    cmd = _script(tmp_path, "h.py", "import sys\nsys.stderr.write('no writes')\nsys.exit(2)\n")
    save_hooks([_hook("pre_tool", cmd, matcher="file_write")])
    router, sandbox = _loop_env(tmp_path, [
        tool_call("file_write", {"path": "out.txt", "content": "data"}), final_answer("done"),
    ])
    task_id, _sid = _make_task()
    _run(execute_agent_loop(task_id, router, sandbox))
    assert not (Path(sandbox) / "out.txt").exists()
    assert cps.list_checkpoints() == []
    assert ("file_write", "blocked_policy", "hook_blocked") in _tool_calls(task_id)


def test_loop_checkpoint_and_post_hook_and_task_events(tmp_path):
    log = tmp_path / "events.log"
    def logger_cmd(name):
        return _script(tmp_path, f"{name}.py", f"import sys,json\nd=json.load(sys.stdin)\nopen(r'{log}','a').write(d['event']+':'+str(d.get('tool'))+':'+str(d.get('status'))+'\\n')\n")
    save_hooks([_hook(e, logger_cmd(e)) for e in ("pre_tool", "post_tool", "task_start", "task_close")])
    router, sandbox = _loop_env(tmp_path, [
        tool_call("file_write", {"path": "note.txt", "content": "NEW"}), final_answer("done"),
    ])
    (Path(sandbox) / "note.txt").write_bytes(b"ORIGINAL\r\n\xff")
    task_id, _sid = _make_task()
    _run(execute_agent_loop(task_id, router, sandbox))

    assert (Path(sandbox) / "note.txt").read_text(encoding="utf-8") == "NEW"
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("task_start")
    assert "pre_tool:file_write:None" in lines and "post_tool:file_write:ok" in lines
    assert lines[-1].startswith("task_close")

    cps_rows = cps.list_checkpoints(task_id=str(task_id))
    assert len(cps_rows) == 1 and cps_rows[0]["files"] == ["note.txt"]
    cps.restore_task(str(task_id))
    assert (Path(sandbox) / "note.txt").read_bytes() == b"ORIGINAL\r\n\xff"


def test_loop_without_hooks_still_creates_checkpoint(tmp_path):
    router, sandbox = _loop_env(tmp_path, [tool_call("file_write", {"path": "n.txt", "content": "1"}), final_answer("ok")])
    task_id, _ = _make_task()
    _run(execute_agent_loop(task_id, router, sandbox))
    assert len(cps.list_checkpoints(task_id=str(task_id))) == 1


# ── API ──────────────────────────────────────────────────────────────────
def _client():
    app = FastAPI()
    app.include_router(ops_router_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    return TestClient(app)


def test_api_hooks_crud_and_validation():
    c = _client()
    assert c.get("/ops/hooks").json()["valid_events"] == list(EVENTS)
    assert c.post("/ops/hooks", json={"event": "bogus", "command": "echo"}).status_code == 400
    assert c.post("/ops/hooks", json={"event": "pre_tool", "command": ""}).status_code == 400
    assert c.post("/ops/hooks", json={"event": "pre_tool", "command": "echo", "matcher": "("}).status_code == 400
    assert c.post("/ops/hooks", json={"event": "pre_tool", "command": "echo", "timeout": 9999}).status_code == 400
    r = c.post("/ops/hooks", json={"event": "pre_tool", "command": "echo hi", "approve": True, "timeout": 5})
    assert r.status_code == 200 and r.json()["approved"] is True
    assert c.post("/ops/hooks", json={"event": "pre_tool", "command": "echo hi"}).status_code == 409
    hook = c.get("/ops/hooks").json()["hooks"][0]
    assert hook["allowed"] is True and hook["executable"] is True and hook["approved_at"]
    c.post("/ops/hooks", json={"event": "task_close", "command": "echo unapproved"})
    unapproved = next(h for h in c.get("/ops/hooks").json()["hooks"] if h["event"] == "task_close")
    assert unapproved["allowed"] is False and unapproved["executable"] is False
    assert c.request("DELETE", "/ops/hooks", json={"event": "pre_tool", "command": "echo hi"}).json()["removed"] == 1
    assert len(c.get("/ops/hooks").json()["hooks"]) == 1


def test_api_checkpoints_list_prune_restore(tmp_path):
    c = _client()
    assert c.get("/ops/checkpoints").json() == {"sessions": [], "total_bytes": 0, "checkpoints": []}
    root = tmp_path / "sb"; root.mkdir()
    (root / "f.txt").write_text("orig")
    now = datetime.now(timezone.utc)
    old = cps.snapshot("sess-A", "t1", "file_write", str(root), ["f.txt"], created_at=now - timedelta(days=30))
    fresh = cps.snapshot("sess-B", "t2", "file_write", str(root), ["f.txt"], created_at=now)
    body = c.get("/ops/checkpoints").json()
    assert {s["session"] for s in body["sessions"]} == {"sess-A", "sess-B"} and body["total_bytes"] > 0
    assert all(set(s) == {"session", "files", "bytes"} for s in body["sessions"])

    assert c.post("/ops/checkpoints/prune", json={"older_than_days": -1}).status_code == 400
    r = c.post("/ops/checkpoints/prune").json()  # без тела: порог по умолчанию 14 дней
    assert r["ok"] is True and r["removed"] == [old]
    assert {x["id"] for x in c.get("/ops/checkpoints").json()["checkpoints"]} == {fresh}

    (root / "f.txt").write_text("changed")
    assert c.post(f"/ops/checkpoints/{fresh}/restore").json()["restored"] == ["f.txt"]
    assert (root / "f.txt").read_text() == "orig"
    assert c.post("/ops/checkpoints/cp-missing/restore").status_code == 404
    assert c.post("/ops/checkpoints/restore-task/none").status_code == 404


def _event_logger(tmp_path, event):
    log = tmp_path / f"{event}.log"
    cmd = _script(tmp_path, f"log_{event}.py", f"import sys,json\nd=json.load(sys.stdin)\nopen(r'{log}','a').write(json.dumps(d)+'\\n')\n")
    return _hook(event, cmd), log


def test_on_error_fires_when_provider_is_unavailable(tmp_path):
    hook, log = _event_logger(tmp_path, "on_error")
    save_hooks([hook])
    task_id, _ = _make_task()
    _run(execute_agent_loop(task_id, ProviderRouter(), str(tmp_path)))  # ни одного провайдера
    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert entry["event"] == "on_error" and entry["code"] == "provider_unavailable"


def test_on_approval_fires_on_request_and_on_resolution(tmp_path):
    hook, log = _event_logger(tmp_path, "on_approval")
    save_hooks([hook])
    router, sandbox = _loop_env(tmp_path, [tool_call("shell_execute", {"command": "rm -rf somedir"}), final_answer("x")])
    task_id, _ = _make_task()
    _run(execute_agent_loop(task_id, router, sandbox))
    with session_scope() as db:
        assert repo.get_task(db, task_id).status == TaskStatus.awaiting_attention
        item_id = repo.list_attention_items(db)[-1].id
    states = [json.loads(line)["state"] for line in log.read_text(encoding="utf-8").splitlines()]
    assert states == ["requested"]

    from aria.routers import sessions as sessions_mod
    app = FastAPI(); app.include_router(sessions_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    assert TestClient(app).post(f"/attention-items/{item_id}/reject").status_code == 200
    states = [json.loads(line)["state"] for line in log.read_text(encoding="utf-8").splitlines()]
    assert states == ["requested", "rejected"]


# ── контракт с api.ts, self-test, снимок состояния задачи ────────────────
_API_TS = Path(__file__).resolve().parents[2] / "desktop" / "src" / "lib" / "api.ts"


def _ts_interface_fields(name: str) -> dict[str, bool]:
    """Поля интерфейса из api.ts → {имя: необязательное?}."""
    import re

    text = _API_TS.read_text(encoding="utf-8")
    m = re.search(r"export interface " + name + r"\s*\{(.*?)\n\}", text, re.S)
    assert m, f"{name} not found in api.ts"
    return {f.group(1): bool(f.group(2)) for f in re.finditer(r"^\s{2}(\w+)(\?)?:", m.group(1), re.M)}


def _assert_matches_contract(payload: dict, interface: str):
    fields = _ts_interface_fields(interface)
    required = {k for k, optional in fields.items() if not optional}
    assert required <= set(payload), f"{interface}: missing {required - set(payload)}"


def test_contract_hooks_and_checkpoints_match_api_ts(tmp_path):
    c = _client()
    c.post("/ops/hooks", json={"event": "pre_tool", "command": "echo x", "approve": True})
    body = c.get("/ops/hooks").json()
    _assert_matches_contract(body, "HooksResponse")
    for h in body["hooks"]:
        _assert_matches_contract(h, "HookEntry")
    root = tmp_path / "sb"; root.mkdir(); (root / "f").write_text("1")
    cps.snapshot("s", "t", "file_write", str(root), ["f"])
    cp_body = c.get("/ops/checkpoints").json()
    _assert_matches_contract(cp_body, "CheckpointsResponse")
    for s in cp_body["sessions"]:
        assert set(s) == set(_ts_interface_fields("CheckpointSession"))
    for entry in cp_body["checkpoints"]:
        _assert_matches_contract(entry, "CheckpointEntry")
    pr = c.post("/ops/checkpoints/prune").json()
    assert {"name", "ok"} <= set(pr)
    restore_res = c.post(f"/ops/checkpoints/{cp_body['checkpoints'][0]['id']}/restore").json()
    _assert_matches_contract(restore_res, "CheckpointRestoreResponse")


def test_checkpoint_carries_task_state_snapshot(tmp_path):
    router, sandbox = _loop_env(tmp_path, [tool_call("file_write", {"path": "s.txt", "content": "1"}), final_answer("ok")])
    task_id, _ = _make_task()
    _run(execute_agent_loop(task_id, router, sandbox))
    row = cps.list_checkpoints(task_id=str(task_id))[0]
    assert row["has_task_state"] is True
    state = cps.restore(row["id"])["task_state"]
    assert state["role"] == "coder" and state["status"] == "in_progress" and state["objective"] == "write a file"
    assert state["tool_call_count"] >= 1


def test_selftest_probe_is_clean_and_reports_in_system_self_test(tmp_path):
    assert cps.selftest() == {"ok": True, "error": None}
    assert cps.list_checkpoints() == [] and not (tmp_path / "data" / "checkpoints" / "_selftest").exists()

    from aria.routers import system as system_mod
    app = FastAPI(); app.include_router(system_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    body = TestClient(app).get("/system/self-test").json()
    assert body["checks"]["hooks"].startswith("ok") and body["checks"]["checkpoints"].startswith("ok")
    assert not [i for i in body["issues"] if "heckpoint" in i or "ooks" in i]


def test_self_test_flags_legacy_events_as_inert(tmp_path):
    save_hooks([_hook("session_created", "echo legacy")])
    from aria.routers import system as system_mod
    app = FastAPI(); app.include_router(system_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    assert "inert" in TestClient(app).get("/system/self-test").json()["checks"]["hooks"]


def test_dod_verify_has_h4_gate():
    text = (Path(__file__).resolve().parents[2] / "dod_verify.py").read_text(encoding="utf-8")
    assert 'for _name in ("hooks", "checkpoints")' in text and "h4_" in text


def test_executor_plan_path_fires_hooks_and_checkpoints(tmp_path):
    from aria.core.executor import _stage3_execute
    from aria.db.models import TaskPlan

    marker = tmp_path / "plan_hooks.log"
    cmd = _script(tmp_path, "p.py", f"import sys,json\nd=json.load(sys.stdin)\nopen(r'{marker}','a').write(d['event']+'\\n')\n")
    save_hooks([_hook("pre_tool", cmd), _hook("post_tool", cmd)])
    sandbox = tmp_path / "sandbox"; sandbox.mkdir()
    (sandbox / "p.txt").write_text("before")

    class _Router:
        pass

    import aria.config as cfg
    settings = cfg.get_settings()
    orig = settings.agent_sandbox_root
    task_id, _ = _make_task()
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        plan = TaskPlan(task_id=task.id, plan_json=[{"step_id": "s1", "objective": "o", "role": "coder", "tool_ref": "file_read", "path": "p.txt"}], status="approved")
        plan.plan_history = []
        db.add(plan); db.flush()
        try:
            settings.agent_sandbox_root = str(sandbox)
            calls = _run(_stage3_execute(db, task, plan, _Router()))
        finally:
            settings.agent_sandbox_root = orig
    assert calls[0]["status"] == "ok"
    assert marker.read_text().split() == ["pre_tool", "post_tool"]


# ── граничные ветки (покрытие нового кода ≥ 90 %) ────────────────────────
def test_invalid_timeout_and_bad_matcher_are_handled(tmp_path):
    assert engine._timeout_of({"timeout": "abc"}) == engine.DEFAULT_TIMEOUT_SEC
    assert engine._timeout_of({"timeout": 10_000}) == engine.MAX_TIMEOUT_SEC
    assert engine._timeout_of({"timeout": -5}) == 1
    cmd = _script(tmp_path, "h.py", "import sys\nsys.exit(2)\n")
    save_hooks([_hook("pre_tool", cmd, matcher="(")])  # битое регулярное выражение → хук не применяется
    assert not _run(fire("pre_tool", {}, tool_name="file_write")).blocked


def test_non_json_stdout_does_not_block(tmp_path):
    cmd = _script(tmp_path, "h.py", "print('{not json')\n")
    save_hooks([_hook("pre_tool", cmd)])
    assert not _run(fire("pre_tool", {}, tool_name="x")).blocked
    cmd2 = _script(tmp_path, "h2.py", "print('{\"block\": \"yes\"}')\n")  # только строгое true
    save_hooks([_hook("pre_tool", cmd2)])
    assert not _run(fire("pre_tool", {}, tool_name="x")).blocked


def test_spawn_failure_is_reported_not_raised(tmp_path, monkeypatch):
    async def boom(*a, **k):
        raise OSError("cannot spawn")

    monkeypatch.setattr(engine.asyncio, "create_subprocess_shell", boom)
    save_hooks([_hook("post_tool", "echo x")])
    run = _run(fire("post_tool", {}, tool_name="x")).runs[0]
    assert run.exit_code is None and "spawn failed" in run.error


def test_engine_crash_is_swallowed(monkeypatch):
    monkeypatch.setattr(engine, "load_hooks", lambda: (_ for _ in ()).throw(RuntimeError("disk gone")))
    assert not _run(fire("pre_tool", {}, tool_name="x")).blocked


def test_hook_stops_after_first_block(tmp_path):
    marker = tmp_path / "second"
    block = _script(tmp_path, "b.py", "import sys\nsys.exit(2)\n")
    second = _script(tmp_path, "s.py", f"open(r'{marker}','w').write('x')\n")
    save_hooks([_hook("pre_tool", block), _hook("pre_tool", second)])
    assert _run(fire("pre_tool", {}, tool_name="x")).blocked and not marker.exists()


def test_kill_tree_falls_back_to_proc_kill(monkeypatch):
    calls = []

    class P:
        pid = 999_999_999
        def kill(self): calls.append("kill")

    monkeypatch.setattr(engine.os, "killpg", lambda *a: (_ for _ in ()).throw(ProcessLookupError()), raising=False)
    engine._kill_tree(P())
    assert calls == ["kill"] or os.name == "nt"


def test_toolhooks_compact_and_task_state_edge_cases():
    from aria.core import toolhooks
    assert toolhooks._compact("a" * 5000).endswith("…") and toolhooks._compact("short") == "short"
    assert toolhooks._task_state("not-a-uuid") is None
    import uuid
    assert toolhooks._task_state(uuid.uuid4()) is None


def test_checkpoint_edge_cases(tmp_path, monkeypatch):
    # битый manifest пропускается, пустой список путей → None, ошибка записи → None и уборка
    root = tmp_path / "sb"; root.mkdir(); (root / "f").write_text("x")
    cp = cps.snapshot("s", "t", "file_write", str(root), ["f"])
    (cps._base() / "s" / cp / "manifest.json").write_text("{bad", encoding="utf-8")
    assert cps.list_checkpoints() == []
    assert cps.snapshot("s", "t", "file_write", str(root), []) is None
    monkeypatch.setattr(cps.Path, "write_bytes", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    assert cps.snapshot("s2", "t", "file_write", str(root), ["f"]) is None
    assert cps.list_checkpoints(session="s2") == []


def test_manifest_paths_are_posix_on_every_os(tmp_path):
    """На Windows relative_to() даёт обратные слэши — в манифесте и API путь всегда с «/»."""
    root = tmp_path / "sb"; root.mkdir()
    cp = cps.snapshot("s1", "t1", "file_write", str(root), ["a\\b.txt".replace("\\", os.sep), "x/y/z.txt"])
    files = next(r for r in cps.list_checkpoints() if r["id"] == cp)["files"]
    assert all("\\" not in f for f in files) and "x/y/z.txt" in files

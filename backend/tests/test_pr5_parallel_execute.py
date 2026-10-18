"""PR-5: EXECUTE волнами — параллелизм, write_scope, свой session_scope на узел."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from aria.core import executor
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import PlanStatus, TaskStatus, ToolStatus


def _spec(name, handler):
    return SimpleNamespace(
        tool_name=name,
        handler=handler,
        risk_level=SimpleNamespace(value="low"),
        timeout_sec=5,
        description="test tool",
        input_schema={},
    )


@pytest.fixture(autouse=True)
def _stub_llm(monkeypatch):
    """Шаги file_write/shell_execute берут контент у LLM — в тестах подменяем генерацию."""

    async def _content(router, session, role, objective):
        return "content"

    async def _command(router, session, role, objective):
        return "echo ok"

    monkeypatch.setattr(executor, "_llm_generate_content", _content)
    monkeypatch.setattr(executor, "_llm_generate_command", _command)

    from aria.core import toolhooks

    decision = SimpleNamespace(blocked=False, reason="")

    async def _pre_tool(*args, **kwargs):
        return decision, None

    async def _post_tool(*args, **kwargs):
        return decision

    monkeypatch.setattr(toolhooks, "pre_tool", _pre_tool)
    monkeypatch.setattr(toolhooks, "post_tool", _post_tool)


def _plan(steps):
    with session_scope() as db:
        s = repo.create_session(db, "pr5")
        t = repo.create_task(db, s, role="general", objective="параллельный EXECUTE", draft_tz_md="# tz\n")
        repo.set_task_status(db, t, TaskStatus.approved)
        repo.set_task_status(db, t, TaskStatus.in_progress)
        from aria.db.models import TaskPlan

        plan = TaskPlan(task_id=t.id, plan_json=steps, status=PlanStatus.in_progress.value)
        db.add(plan)
        db.flush()
        return t.id, t.session_id, plan.id


def _step(sid, *, deps=None, scope=None, tool="file_write", path=None):
    step = {
        "step_id": sid,
        "objective": f"шаг {sid}",
        "role": "coder",
        "tool_ref": tool,
        "depends_on": list(deps or []),
        "covers": ["ac-1"],
        "write_scope": list(scope or []),
        "status": "pending",
    }
    if path or scope:
        step["path"] = path or scope[0]
    return step


# ─────────────────────────── лимит и группы ───────────────────────────

def test_parallelism_limit_is_bounded_by_settings():
    from aria.config import get_settings

    st = get_settings()
    limit = executor._execute_parallelism()
    assert 1 <= limit <= max(1, min(st.runner_max_workers, st.delegate_max_parallel))


def test_parallelism_drops_to_one_under_memory_pressure(monkeypatch):
    import psutil

    class VM:
        available = int(0.5 * (1024 ** 3))

    monkeypatch.setattr(psutil, "virtual_memory", lambda: VM())
    assert executor._execute_parallelism() == 1


def test_overlapping_write_scope_is_serialised():
    steps = [_step("step-1", scope=["generated/a.txt"]), _step("step-2", scope=["generated/a.txt"]), _step("step-3", scope=["generated/b.txt"])]
    groups = executor._serialize_by_write_scope(steps, [0, 1, 2])
    assert groups == [[0, 2], [1]]


def test_disjoint_write_scope_runs_in_one_group():
    steps = [_step("step-1", scope=["generated/a.txt"]), _step("step-2", scope=["generated/b.txt"])]
    assert executor._serialize_by_write_scope(steps, [0, 1]) == [[0, 1]]


# ─────────────────────────── параллельное исполнение ───────────────────────────

@pytest.mark.anyio
async def test_independent_nodes_run_in_parallel(monkeypatch):
    calls: list[float] = []

    async def handler(*, input_json, timeout_sec, sandbox_root):
        calls.append(time.monotonic())
        import asyncio

        await asyncio.sleep(0.3)
        return {"path": input_json.get("path"), "hash_before": "a", "hash_after": "b"}

    monkeypatch.setattr("aria.tools.registry.get_tool", lambda name: _spec(name, handler))
    monkeypatch.setattr(executor, "_execute_parallelism", lambda: 4)

    steps = [_step("step-1", scope=["generated/a.txt"]), _step("step-2", scope=["generated/b.txt"])]
    task_id, _sid, plan_id = _plan(steps)

    started = time.monotonic()
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        plan = _load_plan(db, plan_id)
        raw = await executor._stage3_execute(db, task, plan, router=SimpleNamespace())
    elapsed = time.monotonic() - started

    assert len(raw) == 2 and all(tc["status"] == "ok" for tc in raw)
    assert elapsed < 0.55, f"узлы шли последовательно: {elapsed:.2f}s"
    assert [tc["input_json"]["path"] for tc in raw] == ["generated/a.txt", "generated/b.txt"]


@pytest.mark.anyio
async def test_overlapping_write_scope_is_sequential(monkeypatch):
    async def handler(*, input_json, timeout_sec, sandbox_root):
        import asyncio

        await asyncio.sleep(0.3)
        return {"path": input_json.get("path"), "hash_before": "a", "hash_after": "b"}

    monkeypatch.setattr("aria.tools.registry.get_tool", lambda name: _spec(name, handler))
    monkeypatch.setattr(executor, "_execute_parallelism", lambda: 4)

    steps = [_step("step-1", scope=["generated/same.txt"]), _step("step-2", scope=["generated/same.txt"])]
    task_id, _sid, plan_id = _plan(steps)

    started = time.monotonic()
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        plan = _load_plan(db, plan_id)
        raw = await executor._stage3_execute(db, task, plan, router=SimpleNamespace())
    elapsed = time.monotonic() - started

    assert len(raw) == 2
    assert elapsed >= 0.55, "узлы с общим write_scope обязаны идти последовательно"


@pytest.mark.anyio
async def test_dependencies_are_respected(monkeypatch):
    async def handler(*, input_json, timeout_sec, sandbox_root):
        return {"path": input_json.get("path"), "hash_before": "a", "hash_after": "b"}

    monkeypatch.setattr("aria.tools.registry.get_tool", lambda name: _spec(name, handler))

    steps = [
        _step("step-1", scope=["generated/a.txt"]),
        _step("step-2", deps=["step-1"], scope=["generated/b.txt"]),
        _step("step-3", deps=["step-1"], scope=["generated/c.txt"]),
    ]
    task_id, _sid, plan_id = _plan(steps)
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        plan = _load_plan(db, plan_id)
        raw = await executor._stage3_execute(db, task, plan, router=SimpleNamespace())

    assert len(raw) == 3
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        rows = repo.list_tool_calls(db, task.id)
    assert len(rows) == 3, "каждый узел пишет ToolCall из своего session_scope"
    assert all(r.status == ToolStatus.ok for r in rows)


@pytest.mark.anyio
async def test_node_failure_does_not_kill_the_wave(monkeypatch):
    async def handler(*, input_json, timeout_sec, sandbox_root):
        if input_json.get("path", "").endswith("bad.txt"):
            raise RuntimeError("boom")
        return {"path": input_json.get("path"), "hash_before": "a", "hash_after": "b"}

    monkeypatch.setattr("aria.tools.registry.get_tool", lambda name: _spec(name, handler))

    steps = [_step("step-1", scope=["generated/bad.txt"]), _step("step-2", scope=["generated/ok.txt"])]
    task_id, _sid, plan_id = _plan(steps)
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        plan = _load_plan(db, plan_id)
        raw = await executor._stage3_execute(db, task, plan, router=SimpleNamespace())
        plan_json = list(plan.plan_json)

    assert [tc["status"] for tc in raw] == ["error", "ok"]
    assert plan_json[0]["status"] == "failed" and "tool_call_ids" not in plan_json[0]
    assert plan_json[1]["status"] == "done" and plan_json[1]["tool_call_ids"]


def _load_plan(db, plan_id):
    from aria.db.models import TaskPlan

    return db.get(TaskPlan, plan_id)

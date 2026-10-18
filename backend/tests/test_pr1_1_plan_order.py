"""П.1 ТЗ: регрессия «план без depends_on исполняется параллельно».

Проверяет и форму волн у валидатора, и сквозной путь EXECUTE: старый план
(поля `depends_on` нет вовсе) обязан исполняться строго по порядку шагов,
а параллель включается только явным `depends_on: []`.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from aria.core import executor
from aria.core import plan_validator as pv
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import PlanStatus, TaskStatus


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
def _stub_side_effects(monkeypatch):
    async def _content(router, session, role, objective):
        return "content"

    async def _command(router, session, role, objective):
        return "echo ok"

    monkeypatch.setattr(executor, "_llm_generate_content", _content)
    monkeypatch.setattr(executor, "_llm_generate_command", _command)

    from aria.core import toolhooks

    decision = SimpleNamespace(blocked=False, reason="")

    async def _pre(*args, **kwargs):
        return decision, None

    async def _post(*args, **kwargs):
        return decision

    monkeypatch.setattr(toolhooks, "pre_tool", _pre)
    monkeypatch.setattr(toolhooks, "post_tool", _post)


def _old_step(i: int, tool: str = "file_read", **extra) -> dict:
    """Старый формат плана: поля `depends_on` нет вовсе."""
    step = {
        "step_id": f"old-{i}",
        "objective": f"шаг {i}",
        "role": "coder",
        "tool_ref": tool,
        "status": "pending",
    }
    step.update(extra)
    return step


# ─────────────────────────── валидатор: форма волн ───────────────────────────

def test_old_plan_without_depends_on_is_a_chain():
    """П.1.3: без поля depends_on шаги идут по одному, а не одной волной."""
    raw = [_old_step(0), _old_step(1), _old_step(2)]
    assert all("depends_on" not in s for s in raw)
    waves = pv.topological_waves(raw)
    assert [len(w) for w in waves] == [1, 1, 1], f"старый план ушёл в параллель: {waves}"


def test_explicit_empty_depends_on_keeps_parallelism():
    """П.1.3: явное `depends_on: []` по-прежнему разрешает параллель."""
    raw = [_old_step(i, "web_search", depends_on=[]) for i in range(3)]
    assert pv.topological_waves(raw) == [[0, 1, 2]]


def test_write_without_scope_never_shares_a_wave():
    """П.1.4: shell_execute/file_write без write_scope не делят волну."""
    raw = [
        _old_step(0, "shell_execute", depends_on=[]),
        _old_step(1, "file_write", depends_on=[]),
        _old_step(2, "web_search", depends_on=[]),
    ]
    waves = pv.topological_waves(raw)
    pos = {i: wi for wi, wave in enumerate(waves) for i in wave}
    assert pos[0] != pos[1], f"пишущие шаги в одной волне: {waves}"
    assert len(waves[pos[1]]) == 1, waves


def test_depends_on_is_not_defaulted_to_empty():
    """П.1.2: normalization/model_dump не подставляют [] вместо «не указано»."""
    import uuid as _uuid

    raw = _old_step(0)
    raw["step_id"] = str(_uuid.uuid4())
    step = pv.PlanStep(**raw)
    assert step.depends_on is None
    assert "depends_on" not in step.model_dump(exclude_none=True)
    normalized = pv._normalize_step_ids([_old_step(1)])[0]
    assert normalized.get("depends_on") is None


# ─────────────────────────── сквозной путь EXECUTE ───────────────────────────

def _plan(steps):
    with session_scope() as db:
        s = repo.create_session(db, "pr1-order")
        t = repo.create_task(db, s, role="general", objective="порядок шагов", draft_tz_md="# tz\n")
        repo.set_task_status(db, t, TaskStatus.approved)
        repo.set_task_status(db, t, TaskStatus.in_progress)
        from aria.db.models import TaskPlan

        plan = TaskPlan(task_id=t.id, plan_json=steps, status=PlanStatus.in_progress.value)
        db.add(plan)
        db.flush()
        return t.id, s.id, plan.id


def _load_plan(db, plan_id):
    from aria.db.models import TaskPlan

    return db.get(TaskPlan, plan_id)


@pytest.mark.anyio
async def test_old_plan_executes_strictly_in_order(monkeypatch):
    """П.1 приёмка: старый план без depends_on не перекрывает шаги во времени."""
    stamps: list[tuple[float, float]] = []

    async def handler(*, input_json, timeout_sec, sandbox_root):
        import asyncio

        start = time.monotonic()
        await asyncio.sleep(0.25)
        stamps.append((start, time.monotonic()))
        return {"path": input_json.get("path"), "hash_before": "a", "hash_after": "b"}

    monkeypatch.setattr("aria.tools.registry.get_tool", lambda name: _spec(name, handler))
    monkeypatch.setattr(executor, "_execute_parallelism", lambda: 4)

    steps = [_old_step(i, "file_read", path=f"generated/{i}.txt") for i in range(3)]
    task_id, _sid, plan_id = _plan(steps)

    started = time.monotonic()
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        plan = _load_plan(db, plan_id)
        raw = await executor._stage3_execute(db, task, plan, router=SimpleNamespace())
    elapsed = time.monotonic() - started

    assert len(raw) == 3 and all(tc["status"] == "ok" for tc in raw)
    overlaps = [
        (a, d) for i, (a, b) in enumerate(stamps) for j, (c, d) in enumerate(stamps)
        if i < j and a < d and c < b
    ]
    assert overlaps == [], f"шаги старого плана перекрылись во времени: {stamps}"
    assert elapsed >= 0.55, f"три шага по 0.25с прошли за {elapsed:.2f}с — порядок нарушен"


@pytest.mark.anyio
async def test_parallel_switch_off_runs_strictly_sequentially(monkeypatch):
    """П.1.6: plan_dag_parallel_enabled=False — строго последовательный EXECUTE."""
    from aria.config import get_settings

    monkeypatch.setattr(get_settings(), "plan_dag_parallel_enabled", False)

    async def handler(*, input_json, timeout_sec, sandbox_root):
        import asyncio

        await asyncio.sleep(0.25)
        return {"path": input_json.get("path"), "hash_before": "a", "hash_after": "b"}

    monkeypatch.setattr("aria.tools.registry.get_tool", lambda name: _spec(name, handler))
    monkeypatch.setattr(executor, "_execute_parallelism", lambda: 4)

    steps = [_old_step(i, "file_read", depends_on=[], path=f"generated/p{i}.txt") for i in range(2)]
    task_id, _sid, plan_id = _plan(steps)

    started = time.monotonic()
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        plan = _load_plan(db, plan_id)
        raw = await executor._stage3_execute(db, task, plan, router=SimpleNamespace())
    elapsed = time.monotonic() - started

    assert len(raw) == 2 and all(tc["status"] == "ok" for tc in raw)
    assert elapsed >= 0.5, f"выключатель не сработал: независимые шаги прошли за {elapsed:.2f}с"

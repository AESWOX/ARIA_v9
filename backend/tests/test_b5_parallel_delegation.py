"""B5-D1: несколько delegate_task из ОДНОГО ответа модели исполняются параллельно (боевой путь loop.py).

Проверяется: реальная одновременность, потолок delegate_max_parallel, изоляция сбоев, пауза родителя
до решения по ВСЕМ ждущим детям, отсутствие дублей результата.
"""
from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from sqlalchemy import select

from aria import paths
from aria.core import approvals
from aria.core import delegate as delegate_mod
from aria.core.loop import deliver_child_result, execute_agent_loop
from aria.core.taskrunner import Job, TaskRunner
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus
from aria.llm.providers.base import LlmResponse, ToolCallRequest
from aria.llm.providers.stub import StubProvider, final_answer
from aria.llm.router import ProviderRouter

CMD = "python -c print(1)"  # вне allowlist: нужен Approve


def _calls(*pairs) -> LlmResponse:
    """Один ответ модели с несколькими tool calls."""
    return LlmResponse(text=None, tool_calls=[ToolCallRequest(tool_name=n, arguments=a) for n, a in pairs])


def _deleg(i: int, role: str = "coder") -> tuple[str, dict]:
    return ("delegate_task", {"role": role, "objective": f"parallel job {i}"})


MARKER = "PAR-MARKER-READ"


class _Sub(StubProvider):
    """Саб-агент: каждый вызов модели занимает ``delay`` секунд; считает, сколько вызовов идут одновременно.

    Если очередь сценария пуста, ведёт себя как настоящий агент: сначала читает файл (tool call, иначе аудит
    считает ход «разговорным» и ставит needs_rework), увидев результат — отвечает итогом.
    """

    def __init__(self, *a, delay: float = 0.0, **k):
        super().__init__(*a, **k)
        self.delay = delay
        self.live = 0
        self.max_live = 0

    async def chat(self, messages, tools, timeout_sec):
        # Дети делят историю сессии родителя, поэтому решение принимается СРАЗУ при входе (до задержки):
        # последнее сообщение — результат чтения файла -> итог, иначе -> прочитать файл.
        saw_read = bool(messages) and MARKER in (getattr(messages[-1], "content", "") or "")
        self.live += 1
        self.max_live = max(self.max_live, self.live)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self._queue:
                return await super().chat(messages, tools, timeout_sec)
            if saw_read:
                return final_answer("sub-agent finished: file was read")
            return _calls(("file_read", {"path": "note.txt"}))
        finally:
            self.live -= 1


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    router = ProviderRouter()
    stubs: dict[str, StubProvider] = {}
    for cls, factory in (("premium_reasoning", StubProvider), ("standard_reasoning", StubProvider), ("subagent_execution", _Sub)):
        s = factory(provider_id=f"stub-{cls}")
        s.provider_class = cls
        router.register(s)
        stubs[cls] = s
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "note.txt").write_text(MARKER, encoding="utf-8")
    with session_scope() as db:
        session = repo.create_session(db, title=f"par-{uuid.uuid4().hex[:6]}")
        task = repo.create_task(db, session, role="orchestrator", objective="fan out")
        repo.set_task_status(db, task, TaskStatus.approved)
        task_id = task.id
    for _ in range(12):
        stubs["standard_reasoning"].push(final_answer("OK"))
    return router, stubs, str(sandbox), task_id


def _children(parent_id):
    with session_scope() as db:
        return [(k.id, k.status) for k in repo.list_child_tasks(db, parent_id)]


def _parent_status(parent_id):
    with session_scope() as db:
        return repo.get_task(db, parent_id).status


def _tool_msgs(parent_id):
    with session_scope() as db:
        parent = repo.get_task(db, parent_id)
        rows = db.execute(
            select(m.Message).where(m.Message.session_id == parent.session_id, m.Message.role == "tool").order_by(m.Message.seq_no)
        ).scalars().all()
        return [(r.content, r.content_json) for r in rows]


# --------------------------------------------------------------------------- wiring

def test_run_bounded_is_wired_into_production_loop():
    """Закрывает B5-D1: параллельный запуск подключён к боевому циклу (раньше run_parallel не вызывался нигде)."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "aria" / "core" / "loop.py").read_text(encoding="utf-8")
    assert "run_bounded" in src and "_run_delegate_batch" in src


# --------------------------------------------------------------------------- run_bounded (юнит)

def test_run_bounded_keeps_order_and_respects_limit():
    live = {"now": 0, "max": 0}

    def factory(i):
        async def go():
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
            await asyncio.sleep(0.05 * (5 - i))  # поздние заканчивают раньше
            live["now"] -= 1
            return i
        return go

    res = asyncio.run(delegate_mod.run_bounded([factory(i) for i in range(5)], limit=2))
    assert res == [0, 1, 2, 3, 4], "порядок результатов = порядку вызовов"
    assert live["max"] == 2


def test_run_bounded_isolates_exceptions():
    async def ok():
        return "ok"

    async def bad():
        raise ValueError("x")

    res = asyncio.run(delegate_mod.run_bounded([ok, bad, ok], limit=3))
    assert res[0] == "ok" and res[2] == "ok" and isinstance(res[1], ValueError)


def test_run_bounded_default_limit_comes_from_settings(monkeypatch):
    monkeypatch.setattr(delegate_mod, "max_parallel", lambda: 1)
    live = {"now": 0, "max": 0}

    async def go():
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.02)
        live["now"] -= 1

    asyncio.run(delegate_mod.run_bounded([go, go, go]))
    assert live["max"] == 1


# --------------------------------------------------------------------------- боевой путь

def test_three_delegations_from_one_response_run_concurrently(world):
    router, stubs, sandbox, parent_id = world
    prem, sub = stubs["premium_reasoning"], stubs["subagent_execution"]
    sub.delay = 0.6  # большой шаг: накладные расходы Windows (БД, event loop) не должны маскировать параллелизм
    prem.push(_calls(*[_deleg(i) for i in range(3)]))
    prem.push(final_answer("all three finished"))

    t0 = time.monotonic()
    asyncio.run(execute_agent_loop(parent_id, router, sandbox))
    elapsed = time.monotonic() - t0

    assert sub.max_live == 3, "три саб-агента должны работать одновременно"
    # Каждый ребёнок делает 2 вызова модели (прочитать файл -> итог) = 2 * 0.6 с.
    # Параллельно: ~1.2 с + накладные (на Windows до ~0.4 с). Последовательно: >= 3 * 1.2 = 3.6 с. Порог посередине.
    assert elapsed < 2.4, f"последовательно было бы >= 3.6 с, получилось {elapsed:.2f}"
    kids = _children(parent_id)
    assert len(kids) == 3 and all(st in (TaskStatus.done, TaskStatus.done_unaudited) for _i, st in kids)
    assert _parent_status(parent_id) in (TaskStatus.done, TaskStatus.done_unaudited)
    done_msgs = [c for c, _j in _tool_msgs(parent_id) if c.startswith("delegate_task -> done")]
    assert len(done_msgs) == 3, "родитель видит результат каждого делегирования"


def test_parallel_ceiling_comes_from_settings(world, monkeypatch):
    router, stubs, sandbox, parent_id = world
    prem, sub = stubs["premium_reasoning"], stubs["subagent_execution"]
    monkeypatch.setattr(delegate_mod, "max_parallel", lambda: 2)
    sub.delay = 0.08
    prem.push(_calls(*[_deleg(i) for i in range(5)]))
    prem.push(final_answer("done"))

    asyncio.run(execute_agent_loop(parent_id, router, sandbox))
    assert sub.max_live == 2, "потолок delegate_max_parallel=2: лишние ждут очереди, а не отбрасываются"
    assert len(_children(parent_id)) == 5, "ни одно делегирование не потеряно"


def test_one_failing_delegation_does_not_break_the_others(world):
    router, stubs, sandbox, parent_id = world
    prem, sub = stubs["premium_reasoning"], stubs["subagent_execution"]
    sub.delay = 0.05
    prem.push(_calls(_deleg(0), _deleg(1, role="no_such_role"), _deleg(2)))
    prem.push(final_answer("handled"))

    asyncio.run(execute_agent_loop(parent_id, router, sandbox))
    msgs = [c for c, _j in _tool_msgs(parent_id) if c.startswith("delegate_task ->")]
    assert msgs.count("delegate_task -> done") == 2
    assert msgs.count("delegate_task -> failed") == 1
    assert len(_children(parent_id)) == 2, "для несуществующей роли задача не создаётся"
    assert _parent_status(parent_id) in (TaskStatus.done, TaskStatus.done_unaudited), "родитель довёл задачу до конца"


def test_delegations_run_alongside_ordinary_tool_calls(world):
    router, stubs, sandbox, parent_id = world
    prem, sub = stubs["premium_reasoning"], stubs["subagent_execution"]
    prem.push(_calls(("file_read", {"path": "note.txt"}), _deleg(1), _deleg(2)))
    prem.push(final_answer("combined"))

    asyncio.run(execute_agent_loop(parent_id, router, sandbox))
    with session_scope() as db:
        rows = [(c.tool_name, c.status.value) for c in repo.list_tool_calls(db, parent_id)]
    assert sorted(rows) == [("delegate_task", "ok"), ("delegate_task", "ok"), ("file_read", "ok")], \
        "обычный тул и два делегата исполнены, ни одна запись tool_call не осталась «running»"


# --------------------------------------------------------------------------- паузa родителя

def _two_waiting_children(world):
    router, stubs, sandbox, parent_id = world
    prem, sub = stubs["premium_reasoning"], stubs["subagent_execution"]
    prem.push(_calls(_deleg(1), _deleg(2)))
    prem.push(final_answer("both approved, wrapping up"))
    sub.push(_calls(("shell_execute", {"command": CMD})))
    sub.push(_calls(("shell_execute", {"command": CMD})))
    sub.push(final_answer("child output A"))
    sub.push(final_answer("child output B"))
    asyncio.run(execute_agent_loop(parent_id, router, sandbox))
    with session_scope() as db:
        parent = repo.get_task(db, parent_id)
        items = db.execute(select(m.AttentionItem).where(m.AttentionItem.session_id == parent.session_id)).scalars().all()
        return [(i.id, i.task_id) for i in items]


def _approve(world, item_id, child_id):
    router, _stubs, sandbox, _pid = world
    with session_scope() as db:
        approvals.resolve(db, repo.get_attention_item(db, item_id), approve=True)
    runner = TaskRunner(router, sandbox, max_workers=1)
    asyncio.run(runner._run_approved(Job(task_id=child_id, mode="agent", source="approval", approval_item_id=item_id)))


def test_parent_waits_for_ALL_waiting_children_before_resuming(world):
    router, stubs, sandbox, parent_id = world
    prem = stubs["premium_reasoning"]
    items = _two_waiting_children(world)

    assert _parent_status(parent_id) == TaskStatus.awaiting_attention
    assert len(items) == 2 and {st for _i, st in _children(parent_id)} == {TaskStatus.awaiting_attention}
    assert len(prem.seen if hasattr(prem, "seen") else [1]) >= 1

    (item_a, child_a), (item_b, child_b) = items
    _approve(world, item_a, child_a)
    assert _parent_status(parent_id) == TaskStatus.awaiting_attention, "второй ребёнок ещё ждёт — родитель не просыпается"
    assert any(c.startswith("delegate_task (resumed)") for c, _j in _tool_msgs(parent_id)), "результат первого записан сразу"

    _approve(world, item_b, child_b)
    assert _parent_status(parent_id) in (TaskStatus.done, TaskStatus.done_unaudited), "после решения по обоим родитель продолжил и закончил"
    resumed = [c for c, _j in _tool_msgs(parent_id) if c.startswith("delegate_task (resumed)")]
    assert len(resumed) == 2


def test_reject_of_one_child_and_approve_of_other(world):
    router, stubs, sandbox, parent_id = world
    items = _two_waiting_children(world)
    (item_a, child_a), (item_b, child_b) = items

    with session_scope() as db:
        approvals.resolve(db, repo.get_attention_item(db, item_a), approve=False)
    assert deliver_child_result(child_a) is None, "второй ребёнок ещё ждёт"
    _approve(world, item_b, child_b)

    assert _parent_status(parent_id) in (TaskStatus.done, TaskStatus.done_unaudited)
    statuses = {c: st for c, st in _children(parent_id)}
    assert statuses[child_a] == TaskStatus.failed


def test_child_result_is_delivered_only_once(world):
    router, stubs, sandbox, parent_id = world
    items = _two_waiting_children(world)
    (item_a, child_a), (_item_b, _child_b) = items
    with session_scope() as db:
        approvals.resolve(db, repo.get_attention_item(db, item_a), approve=False)
    assert deliver_child_result(child_a) is None
    assert deliver_child_result(child_a) is None
    resumed = [j for c, j in _tool_msgs(parent_id) if c.startswith("delegate_task (resumed)")]
    assert len(resumed) == 1, "повторная доставка не плодит дубли в истории родителя"

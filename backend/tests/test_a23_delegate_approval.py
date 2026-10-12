"""A23: делегат ждёт Approve — родитель встаёт на паузу, не делегирует заново, а после решения получает результат."""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from aria import paths
from aria.core import approvals
from aria.core.loop import MAX_DELEGATE_RETRIES, deliver_child_result, execute_agent_loop
from aria.core.taskrunner import Job, TaskRunner
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus
from aria.llm.providers.stub import StubProvider, final_answer, tool_call
from aria.llm.router import ProviderRouter

CMD = "python -c print(1)"  # вне allowlist: нужен Approve


class _Rec(StubProvider):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.seen: list[list] = []

    async def chat(self, messages, tools, timeout_sec):
        self.seen.append(list(messages))
        return await super().chat(messages, tools, timeout_sec)


@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    router = ProviderRouter()
    stubs = {}
    for cls in ("premium_reasoning", "standard_reasoning", "subagent_execution"):
        s = _Rec(provider_id=f"stub-{cls}")
        s.provider_class = cls
        router.register(s)
        stubs[cls] = s
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    with session_scope() as db:
        session = repo.create_session(db, title=f"a23-{uuid.uuid4().hex[:6]}")
        task = repo.create_task(db, session, role="orchestrator", objective="delegate the shell work")
        repo.set_task_status(db, task, TaskStatus.approved)
        task_id = task.id
    return router, stubs, str(sandbox), task_id


def _state(parent_id):
    with session_scope() as db:
        parent = repo.get_task(db, parent_id)
        kids = repo.list_child_tasks(db, parent_id)
        items = db.execute(select(m.AttentionItem).where(m.AttentionItem.session_id == parent.session_id)).scalars().all()
        calls = [c for k in kids for c in repo.list_tool_calls(db, k.id)] + list(repo.list_tool_calls(db, parent_id))
        delegates = [c for c in repo.list_tool_calls(db, parent_id) if c.tool_name == "delegate_task"]
        return parent.status, [k.status for k in kids], [(i.id, i.task_id) for i in items], len(delegates)


def _runner(router, sandbox):
    return TaskRunner(router, sandbox, max_workers=1)


def test_parent_pauses_while_child_waits_and_gets_the_result_after_approve(world):
    router, stubs, sandbox, parent_id = world
    prem, sub = stubs["premium_reasoning"], stubs["subagent_execution"]
    prem.push(tool_call("delegate_task", {"role": "coder", "objective": "run python"}))
    prem.push(final_answer("wrapped up"))
    sub.push(tool_call("shell_execute", {"command": CMD}))
    sub.push(final_answer("child result: printed 1"))
    for _ in range(4):
        stubs["standard_reasoning"].push(final_answer("OK"))

    asyncio.run(execute_agent_loop(parent_id, router, sandbox))
    status, kids, items, delegates = _state(parent_id)
    assert status == TaskStatus.awaiting_attention, "корень не должен ни завершаться, ни додумывать, пока ребёнок ждёт Approve"
    assert kids == [TaskStatus.awaiting_attention] and len(items) == 1 and delegates == 1
    assert len(prem.seen) == 1, "оркестратор не вызывает модель повторно, пока ждёт"

    item_id, child_id = items[0]
    with session_scope() as db:
        approvals.resolve(db, repo.get_attention_item(db, item_id), approve=True)
        item = repo.get_attention_item(db, item_id)
        db.expunge(item)
    runner = _runner(router, sandbox)
    asyncio.run(runner._run_approved(Job(task_id=child_id, mode="agent", source="approval", approval_item_id=item_id)))

    status, kids, items, delegates = _state(parent_id)
    assert status in (TaskStatus.done, TaskStatus.done_unaudited)
    assert delegates == 1 and len(items) == 1, "повторных делегаций и лишних карточек нет"
    assert len(prem.seen) == 2
    assert "child result: printed 1" in "\n".join(msg.content for msg in prem.seen[1]), "родитель увидел итог ребёнка"


def test_reject_returns_failed_to_parent_instead_of_hanging(world):
    router, stubs, sandbox, parent_id = world
    prem, sub = stubs["premium_reasoning"], stubs["subagent_execution"]
    prem.push(tool_call("delegate_task", {"role": "coder", "objective": "run python"}))
    prem.push(final_answer("owner rejected, reporting back"))
    sub.push(tool_call("shell_execute", {"command": CMD}))
    for _ in range(3):
        stubs["standard_reasoning"].push(final_answer("OK"))

    asyncio.run(execute_agent_loop(parent_id, router, sandbox))
    _s, _k, items, _d = _state(parent_id)
    item_id, child_id = items[0]
    with session_scope() as db:
        approvals.resolve(db, repo.get_attention_item(db, item_id), approve=False)
    woken = deliver_child_result(child_id)
    assert woken == parent_id
    assert deliver_child_result(child_id) is None, "результат доставляется один раз"
    asyncio.run(execute_agent_loop(parent_id, router, sandbox))
    status, kids, _i, delegates = _state(parent_id)
    assert kids == [TaskStatus.failed] and delegates == 1
    assert status in (TaskStatus.done, TaskStatus.done_unaudited)
    assert "failed" in "\n".join(msg.content for msg in prem.seen[1])


def test_same_failing_delegation_is_blocked_after_max_retries(world):
    router, stubs, sandbox, parent_id = world
    prem = stubs["premium_reasoning"]
    bad = {"role": "no_such_role", "objective": "x"}
    for _ in range(MAX_DELEGATE_RETRIES + 1):
        prem.push(tool_call("delegate_task", bad))
    prem.push(final_answer("giving up"))
    for _ in range(3):
        stubs["standard_reasoning"].push(final_answer("OK"))
    asyncio.run(execute_agent_loop(parent_id, router, sandbox))
    with session_scope() as db:
        rows = [c for c in repo.list_tool_calls(db, parent_id) if c.tool_name == "delegate_task"]
        statuses = [c.status.value for c in rows]
    assert statuses.count("ok") == MAX_DELEGATE_RETRIES and statuses[-1] == "blocked_policy"
    assert any("do not retry" in msg.content for msg in prem.seen[-1])

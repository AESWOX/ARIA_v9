"""0008: Approve для пишущих тулов MCP, исполнение не более одного раза, возобновление после перезапуска."""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from aria import paths
from aria.core import approvals
from aria.core.loop import execute_agent_loop, resume_after_approval
from aria.core.taskrunner import TaskRunner, _unexecuted_approvals
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import ApprovalStatus, AttentionType, TaskStatus
from aria.llm.providers.stub import StubProvider, final_answer, tool_call
from aria.llm.router import ProviderRouter
from aria.mcp import manager as mcp_manager
from aria.mcp.manager import McpManager, validate_server

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


class _Rec(StubProvider):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.seen: list[list] = []

    async def chat(self, messages, tools, timeout_sec):
        self.seen.append(list(messages))
        return await super().chat(messages, tools, timeout_sec)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    return tmp_path


def _setup(tmp_path, monkeypatch, calls):
    mcp_manager.save_servers([validate_server({"name": "fake", "command": sys.executable, "args": [FAKE]})])
    mgr = McpManager()
    monkeypatch.setattr(mcp_manager, "_manager", mgr)
    std = _Rec(provider_id="stub-std")
    std.provider_class = "standard_reasoning"
    for c in calls:
        std.push(c)
    for _ in range(4):
        std.push(final_answer("done"))
    router = ProviderRouter()
    router.register(std)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    with session_scope() as db:
        session = repo.create_session(db, title=f"h7a-{uuid.uuid4().hex[:6]}")
        task = repo.create_task(db, session, role="general", objective="write via mcp")
        repo.set_task_status(db, task, TaskStatus.approved)
        task_id = task.id
    return mgr, std, router, str(sandbox), task_id


def _pending_item(task_id):
    with session_scope() as db:
        item = db.execute(select(m.AttentionItem).where(m.AttentionItem.task_id == task_id)).scalars().one()
        db.expunge(item)
        return item


def _approve(item_id):
    with session_scope() as db:
        approvals.resolve(db, repo.get_attention_item(db, item_id), approve=True)
    with session_scope() as db:
        item = repo.get_attention_item(db, item_id)
        db.expunge(item)
        return item


def _tool_rows(task_id):
    with session_scope() as db:
        return [(c.tool_name, c.status.value, c.approval_item_id) for c in repo.list_tool_calls(db, task_id)]


def test_approve_runs_the_write_tool_once_and_model_sees_the_result(data_dir, tmp_path, monkeypatch):
    mgr, std, router, sandbox, task_id = _setup(tmp_path, monkeypatch, [tool_call("mcp__fake__write_thing", {"v": "x"})])

    async def go():
        try:
            await execute_agent_loop(task_id, router, sandbox)
            assert _tool_rows(task_id) == []
            item = _approve(_pending_item(task_id).id)
            await resume_after_approval(task_id, router, sandbox, item)
            await resume_after_approval(task_id, router, sandbox, item)  # дубль не исполняет действие второй раз
        finally:
            await mgr.close_all()

    asyncio.run(go())
    item = _pending_item(task_id)
    assert _tool_rows(task_id) == [("mcp__fake__write_thing", "ok", item.id)]
    assert "wrote:x" in "\n".join(msg.content for msg in std.seen[1]), "модель увидела результат после Approve"


def test_rejected_write_tool_is_never_called(data_dir, tmp_path, monkeypatch):
    mgr, std, router, sandbox, task_id = _setup(tmp_path, monkeypatch, [tool_call("mcp__fake__write_thing", {"v": "x"})])

    async def go():
        try:
            await execute_agent_loop(task_id, router, sandbox)
        finally:
            await mgr.close_all()

    asyncio.run(go())
    with session_scope() as db:
        approvals.resolve(db, repo.get_attention_item(db, _pending_item(task_id).id), approve=False)
    assert _tool_rows(task_id) == []
    with session_scope() as db:
        assert repo.get_task(db, task_id).status == TaskStatus.failed


def test_unexecuted_approval_is_found_after_restart_and_executed_one_is_not(data_dir, tmp_path, monkeypatch):
    mgr, std, router, sandbox, task_id = _setup(tmp_path, monkeypatch, [tool_call("mcp__fake__write_thing", {"v": "r"})])

    async def first():
        try:
            await execute_agent_loop(task_id, router, sandbox)
        finally:
            await mgr.close_all()  # «перезапуск»: соединения и регистрация тулов потеряны

    asyncio.run(first())
    item = _approve(_pending_item(task_id).id)
    assert _unexecuted_approvals([task_id]) == {task_id: item.id}

    runner = TaskRunner(ProviderRouter(), sandbox, max_workers=1)
    asyncio.run(runner.resume_pending())
    jobs = []
    while not runner._queue.empty():
        jobs.append(runner._queue.get_nowait())
    mine = [j for j in jobs if j.task_id == task_id]  # в общей тестовой БД есть и чужие задачи
    assert len(mine) == 1 and mine[0].approval_item_id == item.id

    mgr2 = McpManager()
    monkeypatch.setattr(mcp_manager, "_manager", mgr2)

    async def second():
        try:
            await resume_after_approval(task_id, router, sandbox, item)
        finally:
            await mgr2.close_all()

    asyncio.run(second())
    assert [r[:2] for r in _tool_rows(task_id)] == [("mcp__fake__write_thing", "ok")]
    assert _unexecuted_approvals([task_id]) == {}


def test_old_approvals_without_link_are_not_replayed(data_dir, tmp_path, monkeypatch):
    """Подтверждения, исполненные до этой правки, не связаны с tool_call — но у задачи есть более поздний вызов."""
    _mgr, _std, _router, _sandbox, task_id = _setup(tmp_path, monkeypatch, [])
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        session = repo.get_session(db, task.session_id)
        item = repo.create_attention_item(db, AttentionType.high_risk_shell, "t", "b", session=session, task=task, payload_json={"command": "echo 1"})
        repo.resolve_attention_item(db, item, ApprovalStatus.approved)
        repo.start_tool_call(db, session, task, "shell_execute", "coder", "high", {"command": "echo 1"})
    assert _unexecuted_approvals([task_id]) == {}

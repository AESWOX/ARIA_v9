"""A26 (патч 0026): модель не должна бесконечно просить уже выполненный вызов после Approve.

Живой смоук 12.10: agent_shell — три раза подряд ``shell_execute (approved) -> ok``, agent_mcp — второй
запрос ``write_note`` и новая карточка Approve; задача зависала в awaiting_attention. В истории не было хода
ассистента с вызовом, и модель не знала, что вызов уже выполнен. Каждый тест падает на коде без патча.
"""
from __future__ import annotations

import asyncio

from aria.core import approvals
from aria.core.loop import _build_messages, _is_immediate_repeat, execute_agent_loop
from aria.core.taskrunner import TaskRunner
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import SourceTrust, TaskStatus
from aria.llm.providers.stub import StubProvider, final_answer, tool_call
from aria.llm.router import ProviderRouter

CMD = "echo A26MARK && echo x > ../a26_up.txt"


def _msg(role, content, content_json=None):
    return m.Message(role=role, content=content, content_json=content_json, source_trust=SourceTrust.trusted)


def test_rendered_result_says_the_call_is_already_done_and_shows_arguments():
    history = [
        _msg("user", "run"),
        _msg("tool", "shell_execute (approved) -> ok",
             {"tool_name": "shell_execute", "arguments": {"command": "git version"}, "output": {"stdout": "git version 2.1"}, "status": "ok"}),
    ]
    text = "\n".join(c.content for c in _build_messages("sys", history))
    assert "git version 2.1" in text
    assert "git version" in text and "arguments=" in text
    assert "уже выполнен" in text, "модель должна знать, что вызов уже выполнен"


def test_failed_result_has_no_do_not_repeat_note():
    history = [_msg("tool", "x -> error", {"tool_name": "x", "arguments": {}, "output": {"e": "boom"}, "status": "error"})]
    text = "\n".join(c.content for c in _build_messages("sys", history))
    assert "уже выполнен" not in text, "после ошибки повтор допустим"


def test_is_immediate_repeat_matches_only_same_ok_call_at_the_end():
    ok = _msg("tool", "t -> ok", {"tool_name": "t", "arguments": {"a": 1}, "output": {}, "status": "ok"})
    assert _is_immediate_repeat([ok], "t", {"a": 1})
    assert not _is_immediate_repeat([ok], "t", {"a": 2})
    assert not _is_immediate_repeat([ok], "other", {"a": 1})
    assert not _is_immediate_repeat([ok, _msg("assistant", "hi")], "t", {"a": 1})
    err = _msg("tool", "t -> error", {"tool_name": "t", "arguments": {"a": 1}, "output": {}, "status": "error"})
    assert not _is_immediate_repeat([err], "t", {"a": 1})
    assert not _is_immediate_repeat([], "t", {})


def test_model_that_repeats_an_approved_call_does_not_get_a_second_card(tmp_path):
    sub = StubProvider(provider_id="stub-sub-a26")
    sub.provider_class = "subagent_execution"
    for _ in range(8):  # модель упрямо просит одно и то же
        sub.push(tool_call("shell_execute", {"command": CMD}))
    std = StubProvider(provider_id="stub-std-a26")
    std.provider_class = "standard_reasoning"
    for _ in range(4):
        std.push(final_answer("OK"))
    router = ProviderRouter()
    router.register(sub)
    router.register(std)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()

    with session_scope() as db:
        session = repo.create_session(db, title="a26")
        task = repo.create_task(db, session, role="coder", objective="run command")
        repo.set_task_status(db, task, TaskStatus.approved)
        task_id = task.id

    asyncio.run(execute_agent_loop(task_id, router, str(sandbox)))
    with session_scope() as db:
        assert repo.get_task(db, task_id).status == TaskStatus.awaiting_attention
        item_id = next(i.id for i in repo.list_attention_items(db) if i.task_id == task_id)
    with session_scope() as db:
        approvals.resolve(db, repo.get_attention_item(db, item_id), approve=True)

    async def drive():
        runner = TaskRunner(router, str(sandbox), max_workers=1)
        await runner.start()
        try:
            runner.submit(task_id, "agent", "approval", approval_item_id=item_id)
            for _ in range(400):
                with session_scope() as db:
                    status = repo.get_task(db, task_id).status
                if status not in (TaskStatus.in_progress, TaskStatus.approved):
                    break
                await asyncio.sleep(0.05)
        finally:
            await runner.stop()

    asyncio.run(drive())

    with session_scope() as db:
        task = repo.get_task(db, task_id)
        assert task.status != TaskStatus.awaiting_attention, "задача не должна зависать на повторной карточке"
        items = [i for i in repo.list_attention_items(db) if i.task_id == task_id]
        assert len(items) == 1, f"второй запрос Approve на тот же вызов не нужен: {len(items)}"
        runs = db.query(m.ToolCall).filter(m.ToolCall.task_id == task_id, m.ToolCall.tool_name == "shell_execute").count()
        assert runs == 1, f"команда выполнилась {runs} раз(а), ожидался один"

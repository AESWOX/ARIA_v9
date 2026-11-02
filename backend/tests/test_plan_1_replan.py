"""R5/R6/R7 replan-семантика Plan-режима.

- R5: невалидный план → один replan с причиной в feedback; второй невалидный = НАЕБАЛ.
- R6: детерминированная ошибка шага (invalid_arguments и др.) → падение сразу, без ЗАБЫЛ-replan.
- R7: сообщение об отказе переживает ``database is locked``.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from aria.config import get_settings
from aria.core import executor as ex
from aria.db import base as db_base
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import PlanStatus, TaskStatus


@pytest.fixture()
def env(tmp_path, monkeypatch):
    saved = (db_base._engine, db_base._SessionLocal)
    db_base.init_db(f"sqlite:///{(tmp_path / 'replan.db').as_posix()}", create_all=True)
    sandbox = tmp_path / "sandbox"
    monkeypatch.setattr(get_settings(), "agent_sandbox_root", str(sandbox))
    yield sandbox
    db_base._engine, db_base._SessionLocal = saved


class PlanRouter:
    """Скриптовый Oracle: возвращает один и тот же план и пишет все user-сообщения."""

    def __init__(self, plan_steps):
        self.plan_steps = plan_steps
        self.messages: list[list] = []

    async def route_chat(self, cls, messages, **kw):
        self.messages.append(messages)
        return SimpleNamespace(response=SimpleNamespace(text=json.dumps(self.plan_steps), tool_calls=[]))

    @property
    def user_contents(self) -> list[str]:
        return [m[1].content for m in self.messages]


def test_invalid_plan_reason_in_feedback_and_fail_second(env):
    invalid = [{"step_id": "s1", "objective": "шаг без инструмента", "role": "coder"}]
    router = PlanRouter(invalid)
    with session_scope() as db:
        s = repo.create_session(db, "r5")
        t = repo.create_task(db, s, role="general", objective="x")
        plan, flags = asyncio.run(ex._stage2_plan(db, t, {}, router))

    assert [f.kind for f in flags] == ["naebal"], "второй невалидный план = НАЕБАЛ"
    assert plan.status == PlanStatus.escalated.value
    assert len(router.messages) == 2, "первая генерация + один replan"
    assert "freeform step forbidden" in router.user_contents[1], "причина в feedback для replan"


@pytest.mark.parametrize(
    "call,code",
    [
        ({"status": "error", "output_json": {"error": "invalid_arguments (web_fetch): url обязателен"}}, "invalid_arguments"),
        ({"status": "error", "output_json": {"error": "unix_only_command: команда невыполнима"}}, "unix_only_command"),
        ({"status": "error", "output_json": {"error": "command_not_found_in_path: foo"}}, "command_not_found_in_path"),
        ({"status": "error", "output_json": {"error": "hook_blocked: deny"}}, "hook_blocked"),
        ({"status": "error", "output_json": {"error": "no LLM provider configured — step s1"}}, "provider_unavailable"),
        ({"status": "approval_required", "output_json": {"status": "approval_required", "command": "rm -rf /"}}, "approval_required"),
    ],
)
def test_deterministic_codes_table(call, code):
    got = ex._deterministic_step_error([call])
    assert got is not None and got[0] == code


def test_unknown_tool_ref_is_not_deterministic():
    assert ex._deterministic_step_error(
        [{"status": "error", "output_json": {"error": "unknown tool_ref: foo (нет в registry)"}}]
    ) is None


def _step(sid, tool, objective, **extra):
    return {"step_id": sid, "objective": objective, "role": "coder", "tool_ref": tool,
            "depends_on": [], "covers": [], "write_scope": [], **extra}


class Script:
    def __init__(self, plans, command="echo ok"):
        self.plans, self.command = list(plans), command
        self.oracle_calls = 0

    async def route_chat(self, cls, messages, **kw):
        sysmsg = messages[0].content
        if "PlanStep" in sysmsg:
            plan = self.plans[min(self.oracle_calls, len(self.plans) - 1)]
            self.oracle_calls += 1
            text = json.dumps(plan)
        elif "ОДНУ shell-команду" in sysmsg:
            text = self.command
        else:
            text = '{"verdict": "pass", "issues": [], "criteria": []}'
        return SimpleNamespace(response=SimpleNamespace(text=text, tool_calls=[]), provider_id="script",
                               degraded_to_free=False, fallback=False)


def _run(router):
    with session_scope() as db:
        s = repo.create_session(db, "r6")
        t = repo.create_task(db, s, role="general", objective="модель человека в Blender")
        repo.set_task_status(db, t, TaskStatus.approved)
        tid = t.id
        result = asyncio.run(ex.run_task(db, t, router=router))
    with session_scope() as db:
        task = repo.get_task(db, tid)
        return result, task.status.value, task.error_code, task.error_message or ""


def test_invalid_arguments_does_not_replan(env):
    # Триггер (file_read + path="") держится на том, что validate_plan пропускает пустой path; если валидацию ужесточат — сменить триггер.
    plan = [_step("s1", "file_read", "прочитать файл", path="")]
    router = Script([plan])
    _, status, code, msg = _run(router)
    assert (status, code) == ("failed", "invalid_arguments"), (code, msg)
    assert router.oracle_calls == 1, "детерминированная ошибка не должна запускать replan"


def test_hook_blocked_does_not_replan(env, monkeypatch):
    from aria.core import toolhooks

    decision = SimpleNamespace(blocked=True, reason="deny by test")

    async def _pre_tool(*args, **kwargs):
        return decision, None

    monkeypatch.setattr(toolhooks, "pre_tool", _pre_tool)
    plan = [_step("s1", "shell_execute", "выполнить echo ok")]
    router = Script([plan])
    _, status, code, msg = _run(router)
    assert (status, code) == ("failed", "hook_blocked"), (code, msg)
    assert router.oracle_calls == 1, "детерминированная ошибка не должна запускать replan"


def test_post_failure_message_retries_on_database_locked(env, monkeypatch):
    calls = []
    real_append = repo.append_message

    def fake_append(db, session, role, content, **kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("database is locked (sqlite3.OperationalError)")
        return real_append(db, session, role, content, **kwargs)

    monkeypatch.setattr(repo, "append_message", fake_append)
    monkeypatch.setattr(ex.time, "sleep", lambda s: None)
    with session_scope() as db:
        s = repo.create_session(db, "r7")
        t = repo.create_task(db, s, role="general", objective="x")
        ex._post_failure_message(db, t)
    assert len(calls) == 3, "transient-ошибка ретраится до успеха"


def test_post_failure_message_stops_on_non_transient(env, monkeypatch):
    calls = []

    def fake_append(db, session, role, content, **kwargs):
        calls.append(1)
        raise ValueError("boom")

    monkeypatch.setattr(repo, "append_message", fake_append)
    with session_scope() as db:
        s = repo.create_session(db, "r7")
        t = repo.create_task(db, s, role="general", objective="x")
        ex._post_failure_message(db, t)
    assert len(calls) == 1, "не-transient ошибка не ретраится"

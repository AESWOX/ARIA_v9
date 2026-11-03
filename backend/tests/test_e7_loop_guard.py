"""E7: страж цикла — лимит шагов, детектор повторов, жёсткий бюджет, предупреждение 80%."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from aria import paths
from aria.config import get_settings
from aria.core import loop
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus
from aria.llm.providers.stub import StubProvider, final_answer, tool_call
from aria.llm.router import ProviderRouter


def _world(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    router = ProviderRouter()
    prem = StubProvider(provider_id="stub-premium")
    prem.provider_class = "premium_reasoning"
    std = StubProvider(provider_id="stub-standard")
    std.provider_class = "standard_reasoning"
    free = StubProvider(provider_id="stub-free")
    free.provider_class = "free_tier_reasoning"
    for provider in (prem, std, free):
        router.register(provider)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "note.txt").write_text("x", encoding="utf-8")
    with session_scope() as db:
        session = repo.create_session(db, "e7", active_role="general")
        task = repo.create_task(db, session, role="general", objective="e7 guard")
        repo.set_task_status(db, task, TaskStatus.approved)
        task_id = task.id
    return router, (prem, std, free), sandbox, task_id


def _read_call():
    return tool_call("file_read", {"path": "note.txt"})


def _state(task_id):
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        messages = [msg.content for msg in repo.list_messages(db, task.session_id, limit=200)]
        return task.status, task.error_code, messages


def _spend(task_id, amount: float) -> None:
    with session_scope() as db:
        db.add(
            m.LlmUsage(
                task_id=task_id,
                provider_id="deepseek-chat",
                model="deepseek-flash",
                purpose="agent",
                prompt_total=1000,
                completion=100,
                cost_usd=amount,
                tier="off",
                created_at=datetime.now(timezone.utc),
            )
        )


def test_step_limit_stops_with_reason(tmp_path, monkeypatch):
    router, providers, sandbox, task_id = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(get_settings(), "agent_max_llm_calls", 3)
    monkeypatch.setattr(get_settings(), "loop_max_iterations", 10)
    for i in range(10):
        for provider in providers:
            provider.push(tool_call("file_read", {"path": f"note-{i}.txt"}))

    asyncio.run(loop.execute_agent_loop(task_id, router, str(sandbox)))

    status, code, messages = _state(task_id)
    assert status == TaskStatus.failed and code == "step_limit"
    assert any("лимит 3 вызовов" in m for m in messages)


def test_loop_detected_on_repeated_call(tmp_path, monkeypatch):
    router, providers, sandbox, task_id = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(get_settings(), "agent_max_llm_calls", 10)
    monkeypatch.setattr(get_settings(), "loop_max_iterations", 10)
    for _ in range(5):
        for provider in providers:
            provider.push(_read_call())

    asyncio.run(loop.execute_agent_loop(task_id, router, str(sandbox)))

    status, code, messages = _state(task_id)
    assert status == TaskStatus.failed and code == "loop_detected"
    assert any("повторяет" in m for m in messages)


def test_repeat_signature_normalizes_abs_and_basename():
    assert loop._repeat_signature("file_read", {"path": "note.txt"}) == loop._repeat_signature(
        "file_read", {"path": r"C:\x\note.txt"}
    )


def test_session_repeat_counter_catches_separate_messages():
    """A4: 3 одинаковых file_read в РАЗНЫХ сообщениях одной сессии тоже стопорятся."""
    loop._SESSION_TOOL_REPEATS.clear()
    sid = uuid.uuid4()
    assert loop._note_session_repeat(sid, "file_read", {"path": "note.txt"})[0] is False
    assert loop._note_session_repeat(sid, "file_read", {"path": r"C:\x\note.txt"})[0] is False
    repeated, count = loop._note_session_repeat(sid, "file_read", {"path": "note.txt"})
    assert repeated is True and count == 3


def test_hard_budget_stops_before_call(tmp_path, monkeypatch):
    router, providers, sandbox, task_id = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(get_settings(), "budget_task_hard_usd", 0.01)
    _spend(task_id, 0.02)
    for provider in providers:
        provider.push(final_answer("should not be called"))

    asyncio.run(loop.execute_agent_loop(task_id, router, str(sandbox)))

    status, code, messages = _state(task_id)
    assert status == TaskStatus.failed and code == "budget_exceeded"
    assert any("жёстком лимите" in m for m in messages)


def test_budget_warning_once_at_80_percent(tmp_path, monkeypatch):
    router, providers, sandbox, task_id = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(get_settings(), "budget_task_hard_usd", 0.01)
    monkeypatch.setattr(get_settings(), "agent_max_llm_calls", 20)
    monkeypatch.setattr(get_settings(), "loop_max_iterations", 10)
    _spend(task_id, 0.0085)
    for i in range(3):
        for provider in providers:
            provider.push(tool_call("file_read", {"path": f"warn-{i}.txt"}))

    asyncio.run(loop.execute_agent_loop(task_id, router, str(sandbox)))

    _status, _code, messages = _state(task_id)
    warnings = [m for m in messages if "потратила" in m and "80%" in m]
    assert len(warnings) == 1


def test_step_warning_once_at_80_percent(tmp_path, monkeypatch):
    router, providers, sandbox, task_id = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(get_settings(), "agent_max_llm_calls", 10)
    monkeypatch.setattr(get_settings(), "loop_max_iterations", 10)
    for i in range(8):
        for provider in providers:
            provider.push(tool_call("file_read", {"path": f"step-{i}.txt"}))

    asyncio.run(loop.execute_agent_loop(task_id, router, str(sandbox)))

    _status, _code, messages = _state(task_id)
    warnings = [m for m in messages if "80% лимита шагов" in m]
    assert len(warnings) == 1


def test_default_loop_cap_does_not_hide_step_limit(tmp_path, monkeypatch):
    router, providers, sandbox, task_id = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(get_settings(), "agent_max_llm_calls", 2)  # loop_max остаётся 15
    for i in range(3):
        for provider in providers:
            provider.push(tool_call("file_read", {"path": f"cap-{i}.txt"}))

    asyncio.run(loop.execute_agent_loop(task_id, router, str(sandbox)))

    status, code, _messages = _state(task_id)
    assert status == TaskStatus.failed and code == "step_limit"


def test_warning_dedupe_helper_appends_once(tmp_path, monkeypatch):
    _router, _providers, _sandbox, task_id = _world(tmp_path, monkeypatch)
    asyncio.run(loop._append_once(task_id, "Внимание: использовано", "Внимание: использовано 1 из 2"))
    asyncio.run(loop._append_once(task_id, "Внимание: использовано", "Внимание: использовано 1 из 2"))

    _status, _code, messages = _state(task_id)
    assert len([m for m in messages if m.startswith("Внимание: использовано")]) == 1

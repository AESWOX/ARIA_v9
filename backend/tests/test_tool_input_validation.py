"""Центральная валидация аргументов тулов по input_schema (core.loop._execute_tool).

Раньше схема была подсказкой для модели: handler получал что угодно, падал с «KeyError: 'path'»
или молча «успешно» отрабатывал пустой/опечатанный вызов (например file_search с `pattern`
вместо `glob` отдавал ВСЕ файлы).
"""
from __future__ import annotations

import asyncio

import pytest

from aria.core.loop import _execute_tool
from aria.db.enums import ToolStatus
from aria.tools.registry import TOOL_REGISTRY
from aria.tools.validators import validate_tool_input

HANDLER_TOOLS = sorted(n for n, s in TOOL_REGISTRY.items() if s.handler is not None)


def _run(tool, args, root):
    return asyncio.run(_execute_tool(TOOL_REGISTRY[tool], args, str(root)))


REQUIRED_TOOLS = sorted(n for n in HANDLER_TOOLS if TOOL_REGISTRY[n].input_schema.get("required"))


@pytest.mark.parametrize("tool", REQUIRED_TOOLS)
def test_every_schema_required_arg_is_enforced(tool, tmp_path):
    required = TOOL_REGISTRY[tool].input_schema["required"]
    out, st, code, msg = _run(tool, {}, tmp_path)
    assert st == ToolStatus.error and code == "invalid_arguments", (st, code, msg)
    assert all(name in msg for name in required), msg


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_required_string_is_rejected(blank):
    assert validate_tool_input({"required": ["path"], "properties": {"path": {"type": "string"}}}, {"path": blank})


def test_wrong_type_is_rejected_and_bool_is_not_an_int():
    schema = {"properties": {"limit": {"type": "integer"}}}
    assert validate_tool_input(schema, {"limit": "5"})
    assert validate_tool_input(schema, {"limit": True})
    assert validate_tool_input(schema, {"limit": 5}) == []


def test_enum_and_array_items():
    schema = {"properties": {"mode": {"type": "string", "enum": ["a", "b"]}, "tags": {"type": "array", "items": {"type": "string"}}}}
    assert validate_tool_input(schema, {"mode": "c"})
    assert validate_tool_input(schema, {"tags": ["x", 1]})
    assert validate_tool_input(schema, {"mode": "a", "tags": ["x"]}) == []


def test_non_object_arguments_rejected():
    assert validate_tool_input({"properties": {}}, "строка")
    assert validate_tool_input({"properties": {}}, [1, 2])


def test_one_of_requirement_vision():
    schema = TOOL_REGISTRY["vision_analyze"].input_schema
    assert validate_tool_input(schema, {"prompt": "x"})
    assert validate_tool_input(schema, {"file_path": "a.png"}) == []


def test_typo_in_argument_name_is_not_silently_ignored(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    out, st, code, msg = _run("file_search", {"pattern": "*.txt"}, tmp_path)
    assert st == ToolStatus.error and code == "invalid_arguments"
    assert "glob" in msg  # модель видит, как правильно


def test_optional_only_tool_still_works_with_empty_args(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    out, st, *_ = _run("file_search", {}, tmp_path)
    assert st == ToolStatus.ok and "a.txt" in out["matches"]


def test_model_supplied_approved_flag_is_tolerated_but_still_ignored(tmp_path):
    out, st, code, msg = _run("shell_execute", {"command": "rm -rf x", "approved": True}, tmp_path)
    assert code != "invalid_arguments"  # не ошибка схемы
    assert st != ToolStatus.ok          # и подтверждением не считается (high-risk → approval)


def test_error_message_is_human_not_keyerror(tmp_path):
    out, st, code, msg = _run("file_read", {}, tmp_path)
    assert "KeyError" not in msg and "path" in msg


# ── Plan-режим (executor Stage 3) ходит в handler напрямую — те же гарантии ──────────────────────────

def test_strip_code_fence():
    from aria.core.executor import _strip_code_fence as f

    assert f("```python\nprint(1)\n```") == "print(1)\n"
    assert f("```\nx\n```") == "x\n"
    assert f("plain text") == "plain text"  # без обёртки не трогаем
    assert f("") == "" and f("```") == ""


def _stage3_env(plan_json, tmp_path, monkeypatch):
    from aria.config import get_settings
    from aria.db import models as m
    from aria.db.base import init_db, session_scope

    init_db(f"sqlite:///{(tmp_path / 's3.db').as_posix()}", create_all=True)
    monkeypatch.setattr(get_settings(), "agent_sandbox_root", str(tmp_path / "sb"))
    cm = session_scope()
    db = cm.__enter__()
    sess = m.Session(title="s3", current_task_id=None)
    db.add(sess); db.flush()
    task = m.Task(session_id=sess.id, objective="t")
    db.add(task); db.flush()
    plan = m.TaskPlan(task_id=task.id, plan_json=plan_json)
    db.add(plan); db.flush()
    return db, task, plan


def test_plan_mode_empty_llm_file_content_is_a_failed_step_not_an_empty_file(tmp_path, monkeypatch):
    from aria.core import executor as ex

    async def _empty(*a, **k):
        return "   "

    monkeypatch.setattr(ex, "_llm_generate_content", _empty)
    plan = [{"step_id": "11111111-1111-1111-1111-111111111111", "objective": "write", "role": "coder",
             "tool_ref": "file_write", "path": "character_base.py"}]
    db, task, p = _stage3_env(plan, tmp_path, monkeypatch)
    calls = asyncio.run(ex._stage3_execute(db, task, p, router=object()))
    assert calls[0]["status"] == "error" and "пустое" in calls[0]["output_json"]["error"]
    assert not (tmp_path / "sb" / "character_base.py").exists()


def test_plan_mode_web_fetch_without_url_is_rejected_by_schema(tmp_path, monkeypatch):
    from aria.core import executor as ex

    plan = [{"step_id": "22222222-2222-2222-2222-222222222222", "objective": "read page", "role": "research",
             "tool_ref": "web_fetch"}]
    db, task, p = _stage3_env(plan, tmp_path, monkeypatch)
    calls = asyncio.run(ex._stage3_execute(db, task, p, router=object()))
    assert calls[0]["status"] == "error" and "invalid_arguments" in calls[0]["output_json"]["error"]


def test_skill_read_roundtrip_and_traversal(tmp_path, monkeypatch):
    from aria import paths

    (tmp_path / "web-research").mkdir()
    (tmp_path / "web-research" / "SKILL.md").write_text("# как искать", encoding="utf-8")
    monkeypatch.setattr(paths, "skills_dir", lambda: tmp_path)
    out, st, *_ = _run("skill_read", {"name": "web-research"}, tmp_path)
    assert st == ToolStatus.ok and out["found"] and "как искать" in out["content"]
    for evil in ("../x", "a/b", "..", "/etc/passwd"):
        out, st, *_ = _run("skill_read", {"name": evil}, tmp_path)
        assert out["found"] is False
    _, st, code, _ = _run("skill_read", {}, tmp_path)
    assert code == "invalid_arguments"

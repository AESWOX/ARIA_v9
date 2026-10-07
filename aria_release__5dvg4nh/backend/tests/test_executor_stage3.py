"""test_executor_stage3.py — Stage 3 e2e через реальные handler'ы registry.

Регрессия Task G: hash_before/hash_after из file_write обязаны доходить до
integrity-аудита, чтобы НАЕБАЛ-детектор не давал ложных срабатываний.

Закрывает дыру существующих тестов: TestStage3Execute использует router=None
(мок-исполнение) и не проверяет цепочку executor → registry handler → audit.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest

import aria.core.executor as exec_mod
from aria.core.executor import _stage3_execute, _stage4_audit, _stage5_hooks
from aria.core.integrity import assert_file_changed
from aria.core.plan_validator import validate_plan


def _make_env(plan_json, title="E2E Executor Stage3", objective="execute plan"):
    from aria.db.base import init_db, session_scope, get_engine

    get_engine().dispose()
    db_path = os.path.join(os.path.dirname(__file__), "__pycache__", f"e2e_executor_stage3_{uuid.uuid4().hex[:8]}.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    try:
        os.remove(db_path)
    except OSError:
        pass
    init_db(f"sqlite:///{db_path}", create_all=True)

    from aria.db import models as m
    with session_scope() as db:
        sess = m.Session(title=title, current_task_id=None)
        db.add(sess)
        db.flush()
        task = m.Task(session_id=sess.id, objective=objective)
        db.add(task)
        db.flush()
        plan = m.TaskPlan(task_id=task.id, plan_json=plan_json)
        db.add(plan)
        db.flush()
        return db, task, plan


def _sandbox() -> Path:
    return Path(tempfile.mkdtemp(prefix="aria_sandbox_")).resolve()


def _patch_llm(monkeypatch):
    async def _fake_command(router, session, role, objective):
        return "echo hello > out.txt"

    async def _fake_content(router, session, role, objective):
        return "hello report"

    monkeypatch.setattr(exec_mod, "_llm_generate_command", _fake_command)
    monkeypatch.setattr(exec_mod, "_llm_generate_content", _fake_content)


class TestStage3RealHandlers:
    def test_shell_read_write_chain(self, monkeypatch):
        """Реальные handler'ы: shell создаёт out.txt, file_read его видит,
        file_write перезаписывает с корректной hash-цепочкой."""
        plan_json = [
            {"step_id": "11111111-1111-1111-1111-111111111111", "objective": "echo hello", "role": "coder", "tool_ref": "shell_execute"},
            {"step_id": "22222222-2222-2222-2222-222222222222", "objective": "read out.txt", "role": "coder", "tool_ref": "file_read", "path": "out.txt"},
            {"step_id": "33333333-3333-3333-3333-333333333333", "objective": "overwrite out.txt", "role": "coder", "tool_ref": "file_write", "path": "out.txt"},
            {"step_id": "44444444-4444-4444-4444-444444444444", "objective": "find txt files", "role": "coder", "tool_ref": "file_search", "glob": "*.txt"},
        ]
        validate_plan(plan_json)
        db, task, plan = _make_env(plan_json)
        sandbox = _sandbox()

        from aria.config import get_settings
        monkeypatch.setattr(get_settings(), "agent_sandbox_root", str(sandbox))
        _patch_llm(monkeypatch)

        calls = asyncio.run(_stage3_execute(db, task, plan, router=object()))

        assert len(calls) == 4

        shell_tc = calls[0]
        assert shell_tc["tool_name"] == "shell_execute"
        assert shell_tc["status"] == "ok"
        assert shell_tc["output_json"]["returncode"] == 0
        assert (sandbox / "out.txt").exists()

        read_tc = calls[1]
        assert read_tc["tool_name"] == "file_read"
        assert read_tc["status"] == "ok"
        assert read_tc["output_json"]["exists"] is True
        assert read_tc["output_json"]["is_dir"] is False

        write_tc = calls[2]
        assert write_tc["tool_name"] == "file_write"
        assert write_tc["status"] == "ok"
        out = write_tc["output_json"]
        assert out["hash_before"] is not None, "hash_before должен прийти из реального ФС (sandbox дошёл до handler)"
        assert out["hash_after"] is not None
        assert len(out["hash_before"]) == 64
        assert len(out["hash_after"]) == 64
        assert out["hash_before"] != out["hash_after"]

        search_tc = calls[3]
        assert search_tc["tool_name"] == "file_search"
        assert search_tc["status"] == "ok"
        assert search_tc["output_json"]["matches"] == ["out.txt"]
        assert search_tc["output_json"]["truncated"] is False

        # Шаги плана отмечены done + tool_call_ids (для ЗАБЫЛ-детектора)
        done = [s for s in plan.plan_json if s.get("status") == "done"]
        assert len(done) == 4
        for s in done:
            assert s["tool_call_ids"]

        # Stage 4: integrity-аудит на реальных вызовах чистый — ни НАЕБАЛ, ни ЗАБЫЛ
        flags = asyncio.run(_stage4_audit(db, task, plan, calls, router=None))
        assert flags == [], f"неожиданные integrity-флаги: {[(f.kind, f.reason) for f in flags]}"

        # Stage 5: secret-scan не блокирует
        hooks = _stage5_hooks(calls)
        assert not hooks.get("blocked", False)

        shutil.rmtree(sandbox)

    def test_unknown_tool_ref_explicit_fail(self, monkeypatch):
        """Неизвестный tool_ref → status error (НЕ деградация в текст)."""
        plan_json = [
            {"step_id": "s9", "objective": "bogus", "role": "coder", "tool_ref": "definitely_not_a_tool"},
        ]
        db, task, plan = _make_env(plan_json, objective="unknown tool")
        sandbox = _sandbox()

        from aria.config import get_settings
        monkeypatch.setattr(get_settings(), "agent_sandbox_root", str(sandbox))

        calls = asyncio.run(_stage3_execute(db, task, plan, router=object()))

        assert len(calls) == 1
        assert calls[0]["status"] == "error"
        assert "unknown tool_ref" in calls[0]["output_json"]["error"]
        assert plan.plan_json[0]["status"] == "failed"
        # tool_call_ids не проставляется при failure → ЗАБЫЛ-детектор сработает в Stage 6
        assert not plan.plan_json[0].get("tool_call_ids")

        # Stage 4: незакрытый шаг обязан дать ZABYL (иначе Stage 6 не запустит retry)
        flags = asyncio.run(_stage4_audit(db, task, plan, calls, router=None))
        zabyl = [f for f in flags if f.kind == "zabyl"]
        assert zabyl, f"failure-шаг не дал ZABYL: {[f.kind for f in flags]}"

        shutil.rmtree(sandbox)


class TestAssertFileChangedFallback:
    def test_fallback_output_hash_before(self):
        """hash_before только в output_json (реальный executor) — тоже срабатывает."""
        assert assert_file_changed({"path": "out.txt"}, {"hash_before": "a", "hash_after": "b"}) is True

    def test_fallback_equal_hashes_false(self):
        assert assert_file_changed({}, {"hash_before": "a", "hash_after": "a"}) is False

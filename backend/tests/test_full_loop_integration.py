"""test_full_loop_integration.py — Task G: полный loop-engineering pipeline (Stage 1-7)."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from aria.core.executor import (
    _stage2_plan,
    _stage3_execute,
    _stage4_audit,
    _stage5_hooks,
)
from aria.core.integrity import assert_file_changed
from aria.llm.router import ProviderUnavailable
from aria.core.plan_validator import validate_plan
from aria.tools.handlers.files import file_write


# ═══════════════════════════════════════════════════════════════════
# Stage 2 — Oracle Plan
# ═══════════════════════════════════════════════════════════════════

class TestStage2Plan:
    def test_no_router_is_an_honest_failure(self):
        """Волна 1 (A9): без LLM Oracle-плана нет — тихий мок-план удалён.

        Формулировка «плана нет, но шаги фиктивно выполнены» больше не проходит:
        отсутствие провайдера обязано быть ошибкой, а не успехом.
        """
        import os
        from aria.db.base import init_db, session_scope, get_engine

        get_engine().dispose()
        db_path = os.path.join(os.path.dirname(__file__), "__pycache__", "e2e_stage2_raises.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        try:
            os.remove(db_path)
        except OSError:
            pass
        init_db(f"sqlite:///{db_path}", create_all=True)

        from aria.db import models as m
        with session_scope() as db:
            sess = m.Session(title="E2E Stage2 no router", current_task_id=None)
            db.add(sess)
            db.flush()
            task = m.Task(session_id=sess.id, objective="explain what e2e testing is")
            db.add(task)
            db.flush()
            with pytest.raises(ProviderUnavailable):
                asyncio.run(_stage2_plan(db, task, {}, router=None))

    def test_e2e_without_router(self, monkeypatch):
        """_stage2_plan с router=None больше не подменяет план моком."""
        import os
        from aria.db.base import init_db, session_scope, get_engine

        # Dispose old engine first to avoid file lock
        get_engine().dispose()
        db_path = os.path.join(os.path.dirname(__file__), "__pycache__", "e2e_stage2.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        try:
            os.remove(db_path)
        except OSError:
            pass
        dsn = f"sqlite:///{db_path}"
        init_db(dsn, create_all=True)

        from aria.db import models as m
        with session_scope() as db:
            sess = m.Session(title="E2E Stage2", current_task_id=None)
            db.add(sess)
            db.flush()
            task = m.Task(session_id=sess.id, objective="explain what e2e testing is")
            db.add(task)
            db.flush()

            vault_ctx = {}
            with pytest.raises(ProviderUnavailable):
                asyncio.run(_stage2_plan(db, task, vault_ctx, router=None))


# ═══════════════════════════════════════════════════════════════════
# Stage 3 — Execute
# ═══════════════════════════════════════════════════════════════════

class TestStage3Execute:
    def test_no_router_does_not_fake_success(self):
        """Волна 1 (A9): без LLM шаги не «выполняются» фиктивно.

        Раньше router=None давал 2 tool-call'а со status=ok и stdout
        «done: ...» — то есть отчёт об успехе без единого реального действия.
        Теперь это ProviderUnavailable.
        """
        import os
        from aria.db.base import init_db, session_scope, get_engine

        get_engine().dispose()
        db_path = os.path.join(os.path.dirname(__file__), "__pycache__", "e2e_stage3_raises.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        try:
            os.remove(db_path)
        except OSError:
            pass
        init_db(f"sqlite:///{db_path}", create_all=True)

        from aria.db import models as m
        with session_scope() as db:
            sess = m.Session(title="E2E Stage3 no router", current_task_id=None)
            db.add(sess)
            db.flush()
            task = m.Task(session_id=sess.id, objective="run a terminal command")
            db.add(task)
            db.flush()
            plan = m.TaskPlan(
                task_id=task.id,
                plan_json=[
                    {"step_id": "s1", "objective": "run echo test", "role": "coder", "tool_ref": "terminal"},
                ],
            )
            db.add(plan)
            db.flush()
            with pytest.raises(ProviderUnavailable):
                asyncio.run(_stage3_execute(db, task, plan, router=None))

    def test_e2e_without_router(self, monkeypatch):
        """_stage3_execute с router=None больше не даёт фиктивный ok."""
        import os
        from aria.db.base import init_db, session_scope, get_engine

        get_engine().dispose()
        db_path = os.path.join(os.path.dirname(__file__), "__pycache__", "e2e_stage3.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        try:
            os.remove(db_path)
        except OSError:
            pass
        dsn = f"sqlite:///{db_path}"
        init_db(dsn, create_all=True)

        from aria.db import models as m
        with session_scope() as db:
            sess = m.Session(title="E2E Stage3", current_task_id=None)
            db.add(sess)
            db.flush()
            task = m.Task(session_id=sess.id, objective="run a terminal command")
            db.add(task)
            db.flush()

            plan = m.TaskPlan(
                task_id=task.id,
                plan_json=[
                    {"step_id": "s1", "objective": "run echo test", "role": "coder", "tool_ref": "terminal"},
                    {"step_id": "s2", "objective": "write hello.txt", "role": "coder", "tool_ref": "write_file"},
                ],
            )
            db.add(plan)
            db.flush()

            with pytest.raises(ProviderUnavailable):
                asyncio.run(_stage3_execute(db, task, plan, router=None))


# ═══════════════════════════════════════════════════════════════════
# Stage 4 — Integrity Audit
# ═══════════════════════════════════════════════════════════════════

class TestStage4Audit:
    def _db_fixture(self, plan_json):
        import os
        from aria.db.base import init_db, session_scope, get_engine

        get_engine().dispose()
        db_path = os.path.join(os.path.dirname(__file__), "__pycache__", "e2e_stage4.db")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        try:
            os.remove(db_path)
        except OSError:
            pass
        init_db(f"sqlite:///{db_path}", create_all=True)

        from aria.db import models as m
        with session_scope() as db:
            sess = m.Session(title="E2E Stage4", current_task_id=None)
            db.add(sess)
            db.flush()
            task = m.Task(session_id=sess.id, objective="test")
            db.add(task)
            db.flush()
            plan = m.TaskPlan(task_id=task.id, plan_json=plan_json)
            db.add(plan)
            db.flush()
            return db, task, plan

    def test_empty_plan_no_flags(self):
        db, task, plan = self._db_fixture([])
        flags = asyncio.run(_stage4_audit(db, task, plan, [], router=None))
        assert len(flags) == 0

    def test_mock_calls_no_naebal(self):
        db, task, plan = self._db_fixture(
            [{"step_id": "s1", "objective": "test", "role": "coder", "tool_ref": "terminal"}]
        )
        calls = [{"tool_name": "terminal", "output_json": {"stdout": "ok"}, "status": "ok"}]
        flags = asyncio.run(_stage4_audit(db, task, plan, calls, router=None))
        naebal = [f for f in flags if f.kind == "naebal"]
        assert len(naebal) == 0


# ═══════════════════════════════════════════════════════════════════
# Stage 5 — Hooks (secret scan)
# ═══════════════════════════════════════════════════════════════════

class TestStage5Hooks:
    def test_empty_changed_files_ok(self):
        calls = [{"tool_name": "terminal", "output_json": {"stdout": "hello"}}]
        result = _stage5_hooks(calls)
        assert not result.get("blocked", False)


# ═══════════════════════════════════════════════════════════════════
# Stage 7 — hash_before/hash_after в file_write
# ═══════════════════════════════════════════════════════════════════

class TestHashBeforeAfter:
    async def _write(self, data: dict, sandbox: str) -> dict:
        return await file_write(data, sandbox)

    def test_new_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = str(Path(tmp).resolve())
            out = asyncio.run(file_write({"path": "new.txt", "content": "hello"}, sandbox))
            assert out["hash_before"] is None
            assert out["hash_after"] is not None
            assert len(out["hash_after"]) == 64

    def test_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            sandbox = str(Path(tmp).resolve())
            asyncio.run(file_write({"path": "data.txt", "content": "original"}, sandbox))
            out = asyncio.run(file_write({"path": "data.txt", "content": "modified"}, sandbox))
            assert out["hash_before"] is not None
            assert out["hash_after"] is not None
            assert out["hash_before"] != out["hash_after"]

    def test_assert_file_changed_true(self):
        assert assert_file_changed({"hash_before": "a"}, {"hash_after": "b"}) is True

    def test_assert_file_changed_false(self):
        assert assert_file_changed({"hash_before": "abc"}, {"hash_after": "abc"}) is False

    def test_assert_file_changed_missing_hash(self):
        assert assert_file_changed({}, {"hash_after": "abc"}) is False

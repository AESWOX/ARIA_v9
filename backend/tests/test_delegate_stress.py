"""Stress test: run delegate_task 10x to prove SQLite lock is fixed.

Regression for: SQLite "database is locked" when run_audit → route_chat
→ _pick_available → _record_provider_status opens a nested session_scope()
inside finalize_task's outer transaction.

The fix pass db through the chain so _record_provider_status reuses the
outer session instead of creating a nested one.
"""
from __future__ import annotations

import asyncio
import os
import unittest
import uuid

from aria.core.loop import execute_agent_loop as run_task
from aria.db import repository as repo
from aria.db.base import init_db, session_scope
from aria.db.enums import TaskStatus
from aria.llm.providers.stub import StubProvider, final_answer, tool_call
from aria.llm.router import ProviderRouter


class DelegateTaskStressTests(unittest.TestCase):
    """Run the delegate flow 10 times sequentially — zero DB lock errors."""

    def setUp(self):
        self.tmp_dir = f"/tmp/local_agent_stress_{uuid.uuid4().hex[:8]}"
        os.makedirs(self.tmp_dir, exist_ok=True)
        dsn = f"sqlite:///{self.tmp_dir}/stress.db"
        init_db(dsn, create_all=True)

        self.router = ProviderRouter()

        p = StubProvider("stub-premium"); p.provider_class = "premium_reasoning"; self.router.register(p)
        s = StubProvider("stub-standard"); s.provider_class = "standard_reasoning"; self.router.register(s)
        f = StubProvider("stub-free"); f.provider_class = "free_tier_reasoning"; self.router.register(f)
        sa = StubProvider("stub-subagent"); sa.provider_class = "subagent_execution"; self.router.register(sa)

        self.sandbox_root = os.path.join(self.tmp_dir, "sandbox")
        os.makedirs(self.sandbox_root, exist_ok=True)

    def _push_stubs(self, iteration: int):
        """Push stub responses for one delegate cycle."""
        # orchestrator → delegate_task
        stub_premium = self.router.providers_by_class["premium_reasoning"][0]
        stub_premium.push(
            tool_call("delegate_task", {"role": "coder", "objective": f"write iter-{iteration}.txt"})
        )
        stub_premium.push(final_answer("Delegation done."))

        # coder (subagent_execution) → file_write + finish
        stub_sub = self.router.providers_by_class["subagent_execution"][0]
        stub_sub.push(
            tool_call("file_write", {"path": f"iter-{iteration}.txt", "content": f"Hello #{iteration}", "mode": "overwrite"})
        )
        stub_sub.push(final_answer("Written."))

        # audit (standard_reasoning)
        stub_std = self.router.providers_by_class["standard_reasoning"][0]
        stub_std.push(final_answer("OK"))

    def _create_and_run(self, iteration: int) -> uuid.UUID:
        """Create a task, run it, and return the task_id."""
        with session_scope() as db:
            session = repo.create_session(db, title=f"stress-iter-{iteration}")
            task = repo.create_task(db, session, role="orchestrator", objective=f"Write iter-{iteration}.txt")
            repo.set_task_status(db, task, TaskStatus.approved)
            tid = task.id

        asyncio.run(run_task(tid, self.router, self.sandbox_root))

        with session_scope() as db:
            parent = repo.get_task(db, tid)
            assert parent.status.value in ("done", "done_unaudited"), (
                f"iter {iteration}: parent status={parent.status.value}"
            )
            children = repo.list_child_tasks(db, tid)
            assert len(children) == 1, f"iter {iteration}: expected 1 child, got {len(children)}"
            assert children[0].status.value in ("done", "done_unaudited"), (
                f"iter {iteration}: child status={children[0].status.value}"
            )
            # Verify file artifact
            path = os.path.join(self.sandbox_root, f"iter-{iteration}.txt")
            assert os.path.exists(path), f"iter {iteration}: artifact missing"
            with open(path) as f:
                assert f.read() == f"Hello #{iteration}", f"iter {iteration}: content mismatch"

        return tid

    def test_10_delegates_in_sequence(self):
        """10 sequential delegate rounds — stress test for nested-session SQLite lock."""
        for i in range(1, 11):
            self._push_stubs(i)
            self._create_and_run(i)
        # If we reach here without OperationalError, the fix works.

    def test_10_delegates_single_stub_reuse(self):
        """Verify stubs aren't accidentally depleted across iterations."""
        for i in range(1, 11):
            self._push_stubs(i)
            self._create_and_run(i)
        # All 10 should have clean stubs — no stale responses.
        for stub_list in self.router.providers_by_class.values():
            for stub in stub_list:
                self.assertEqual(len(stub._queue), 0, f"{stub.provider_id} still has {len(stub._queue)} stubs")

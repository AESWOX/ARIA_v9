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


class DelegateTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = f"/tmp/local_agent_test_{uuid.uuid4().hex[:8]}"
        os.makedirs(self.tmp_dir, exist_ok=True)
        dsn = f"sqlite:///{self.tmp_dir}/test.db"
        init_db(dsn, create_all=True)

        self.router = ProviderRouter()

        self.stub_premium = StubProvider(provider_id="stub-premium")
        self.stub_premium.provider_class = "premium_reasoning"
        self.router.register(self.stub_premium)

        self.stub_standard = StubProvider(provider_id="stub-standard")
        self.stub_standard.provider_class = "standard_reasoning"
        self.router.register(self.stub_standard)

        self.stub_free = StubProvider(provider_id="stub-free")
        self.stub_free.provider_class = "free_tier_reasoning"
        self.router.register(self.stub_free)

        self.stub_subagent = StubProvider(provider_id="stub-subagent")
        self.stub_subagent.provider_class = "subagent_execution"
        self.router.register(self.stub_subagent)

        self.sandbox_root = os.path.join(self.tmp_dir, "sandbox")
        os.makedirs(self.sandbox_root, exist_ok=True)

    def test_orchestrator_delegates_to_coder_and_completes(self):
        # Orchestrator (premium_reasoning): iter 1 delegates, iter 2 wraps up.
        self.stub_premium.push(
            tool_call("delegate_task", {"role": "coder", "objective": "create hello.txt with content Hello"})
        )
        self.stub_premium.push(final_answer("Delegation complete, coder wrote the file."))

        # Coder sub-agent (subagent_execution): iter 1 writes file, iter 2 finishes.
        self.stub_subagent.push(
            tool_call("file_write", {"path": "hello.txt", "content": "Hello", "mode": "overwrite"})
        )
        self.stub_subagent.push(final_answer("Done: wrote hello.txt"))
        # qa_auditor's own chat call inside run_audit (also standard_reasoning).
        self.stub_standard.push(final_answer("OK"))

        with session_scope() as db:
            session = repo.create_session(db, title="delegate-test-session")
            task = repo.create_task(db, session, role="orchestrator", objective="Delegate hello.txt creation to coder")
            repo.set_task_status(db, task, TaskStatus.approved)
            task_id = task.id

        asyncio.run(run_task(task_id, self.router, self.sandbox_root))

        with session_scope() as db:
            parent = repo.get_task(db, task_id)
            self.assertIn(parent.status.value, ("done", "done_unaudited"))

            children = repo.list_child_tasks(db, task_id)
            self.assertEqual(len(children), 1)
            child = children[0]
            self.assertEqual(child.role, "coder")
            self.assertEqual(child.delegation_depth, 1)
            self.assertIn(child.status.value, ("done", "done_unaudited"))

            # delegated sub-task must NOT hijack the session's foreground task.
            session_row = repo.get_session(db, parent.session_id)
            self.assertEqual(session_row.current_task_id, task_id)

        written_file = os.path.join(self.sandbox_root, "hello.txt")
        self.assertTrue(os.path.exists(written_file))
        with open(written_file) as f:
            self.assertEqual(f.read(), "Hello")

    def test_delegate_rejects_self_delegation_and_unknown_role(self):
        from aria.tools.registry import _delegate_task

        with session_scope() as db:
            session = repo.create_session(db, title="reject-test-session")
            task = repo.create_task(db, session, role="orchestrator", objective="noop")
            task_id = task.id
            session_id = session.id

        result = asyncio.run(
            _delegate_task(
                {"role": "orchestrator", "objective": "try to self-delegate"},
                timeout_sec=30,
                sandbox_root=self.sandbox_root,
                parent_task_id=task_id,
                session_id=session_id,
                router=self.router,
                delegation_depth=0,
            )
        )
        self.assertEqual(result["status"], "failed")

        result2 = asyncio.run(
            _delegate_task(
                {"role": "not_a_real_role", "objective": "x"},
                timeout_sec=30,
                sandbox_root=self.sandbox_root,
                parent_task_id=task_id,
                session_id=session_id,
                router=self.router,
                delegation_depth=0,
            )
        )
        self.assertEqual(result2["status"], "failed")

    def test_delegate_enforces_max_depth(self):
        from aria.tools.registry import _delegate_task

        with session_scope() as db:
            session = repo.create_session(db, title="depth-test-session")
            task = repo.create_task(db, session, role="orchestrator", objective="noop")
            task_id = task.id
            session_id = session.id

        result = asyncio.run(
            _delegate_task(
                {"role": "coder", "objective": "should be blocked, already at max depth"},
                timeout_sec=30,
                sandbox_root=self.sandbox_root,
                parent_task_id=task_id,
                session_id=session_id,
                router=self.router,
                delegation_depth=1,  # already == MAX_DEPTH
            )
        )
        self.assertEqual(result["status"], "failed")
        self.assertIn("depth", result["error"])


if __name__ == "__main__":
    unittest.main()

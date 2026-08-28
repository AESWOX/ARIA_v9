import asyncio
import unittest
from datetime import timedelta

from sqlalchemy import select

from aria.db.base import init_db, session_scope
from aria.db import repository as repo
from aria.db.models import AttentionItem
from aria.db.enums import AttentionType, ApprovalStatus
from aria.scheduler.jobs import (
    DEFAULT_SCHEDULER_JOBS,
    expire_stale_attention_items_job,
    run_scheduler_job_by_name,
    seed_default_scheduler_jobs,
)


def _make_stale_item(title: str = "stale", objective: str = "L2 test"):
    """Создать session+task+attention item с истёкшим TTL."""
    with session_scope() as db:
        sess = repo.create_session(db, f"sess-{title}")
        task = repo.create_task(db, sess, role="coder", objective=objective)
        item = repo.create_attention_item(
            db,
            AttentionType.task_tz_approval,
            title,
            "expired by watchdog",
            session=sess,
            task=task,
        )
        item.expires_at = item.expires_at - timedelta(hours=25)
        db.flush()
        return item.id


class SchedulerJobTests(unittest.TestCase):
    def test_expire_job_returns_int(self):
        init_db(create_all=True)
        result = expire_stale_attention_items_job()
        self.assertIsInstance(result, int)

    def test_seed_creates_default_jobs(self):
        init_db(create_all=True)
        created = seed_default_scheduler_jobs()
        self.assertEqual(created, len(DEFAULT_SCHEDULER_JOBS))
        with session_scope() as db:
            names = [j.name for j in repo.list_scheduler_jobs(db)]
        self.assertIn("expire_stale_attention_items", names)
        self.assertIn("refresh_provider_models", names)

    def test_seed_is_idempotent(self):
        init_db(create_all=True)
        seed_default_scheduler_jobs()
        again = seed_default_scheduler_jobs()
        self.assertEqual(again, 0)
        with session_scope() as db:
            names = [j.name for j in repo.list_scheduler_jobs(db)]
        self.assertEqual(names.count("expire_stale_attention_items"), 1)

    def test_expire_job_writes_event_when_expired(self):
        init_db(create_all=True)
        item_id = _make_stale_item()
        expired = expire_stale_attention_items_job()
        self.assertEqual(expired, 1)
        with session_scope() as db:
            row = db.get(AttentionItem, item_id)
            self.assertEqual(row.status, ApprovalStatus.expired)
            events = repo.list_events_after(db, None)
            types = [e.event_type for e in events]
        self.assertIn("scheduler.job_run", types)

    def test_trigger_dispatches_expire_job(self):
        init_db(create_all=True)
        _make_stale_item(title="stale2")
        result = asyncio.run(run_scheduler_job_by_name("expire_stale_attention_items"))
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["expired"], 1)

    def test_trigger_unknown_name_best_effort(self):
        init_db(create_all=True)
        result = asyncio.run(run_scheduler_job_by_name("nonexistent-job"))
        self.assertTrue(result["ok"])
        self.assertEqual(result.get("note"), "no registered runner (best-effort)")


if __name__ == '__main__':
    unittest.main()

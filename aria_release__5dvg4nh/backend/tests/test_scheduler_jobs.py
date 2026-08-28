import unittest

from aria.db.base import init_db
from aria.scheduler.jobs import expire_stale_attention_items_job


class SchedulerJobTests(unittest.TestCase):
    def test_expire_job_returns_int(self):
        init_db(create_all=True)
        result = expire_stale_attention_items_job()
        self.assertIsInstance(result, int)


if __name__ == '__main__':
    unittest.main()

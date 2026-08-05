import unittest

from aria.config import get_settings


class ConfigContractTests(unittest.TestCase):
    def test_canonical_defaults_present(self):
        s = get_settings()
        self.assertEqual(s.loop_max_iterations, 15)
        self.assertEqual(s.audit_max_attempts, 3)
        self.assertEqual(s.delegate_max_parallel, 5)
        self.assertEqual(s.security_auto_lock_minutes, 15)
        self.assertEqual(s.budget_warn_threshold_pct, 80)
        self.assertEqual(s.budget_block_threshold_pct, 100)


if __name__ == '__main__':
    unittest.main()

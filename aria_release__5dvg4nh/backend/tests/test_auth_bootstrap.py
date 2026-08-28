import json
import os
import tempfile
import unittest
from functools import lru_cache

from aria import config as config_module
from aria.api.auth import RuntimeTokenStore


class AuthBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ['RUNTIME_TOKEN_PATH'] = os.path.join(self.tmpdir.name, 'bootstrap.json')
        os.environ['LOCAL_AGENT_UI_PIN'] = '654321'
        config_module.get_settings.cache_clear()

    def tearDown(self):
        config_module.get_settings.cache_clear()
        os.environ.pop('RUNTIME_TOKEN_PATH', None)
        os.environ.pop('LOCAL_AGENT_UI_PIN', None)
        self.tmpdir.cleanup()

    def test_issue_writes_bootstrap_and_verifies_credentials(self):
        store = RuntimeTokenStore()
        token, pin = store.issue()
        self.assertEqual(pin, '654321')
        self.assertTrue(store.verify_token(token))
        self.assertTrue(store.verify_pin(pin))
        with open(os.environ['RUNTIME_TOKEN_PATH'], 'r', encoding='utf-8') as fh:
            payload = json.load(fh)
        self.assertEqual(payload['runtimeToken'], token)
        self.assertEqual(payload['pinRequired'], True)
        self.assertNotIn('pin', payload)


if __name__ == '__main__':
    unittest.main()

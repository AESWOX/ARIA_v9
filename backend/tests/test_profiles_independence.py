"""L4: независимость профилей — онбординг-маркер и настройки.

Маркер first-run (onboarded_marker_path) глобален на пользователя ОС
(~/.local-agent-ui/.onboarded), а не на профиль. Это намеренно: онбординг
срабатывает один раз на пользователя, поэтому профиль B, созданный ПОСЛЕ
онбординга профиля A, не показывает «снова первый запуск» и не сбрасывает
настройки A (каждый профиль живёт в своём data/profiles/<name>/profile.yaml).
"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aria import config as config_module
from aria.api.auth import RuntimeTokenStore
from aria.routers import profiles as profiles_router


class ProfileIndependenceTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        os.environ["ONBOARDED_MARKER_PATH"] = os.path.join(self.tmpdir.name, ".onboarded")
        os.environ["LOCAL_AGENT_DISABLE_BOOTSTRAP_WRITE"] = "1"
        config_module.get_settings.cache_clear()
        self._patch = patch.multiple(
            profiles_router,
            _PROFILES_ROOT=Path(self.tmpdir.name) / "profiles",
            _STATE_FILE=Path(self.tmpdir.name) / "profile_state.json",
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def tearDown(self):
        config_module.get_settings.cache_clear()
        os.environ.pop("ONBOARDED_MARKER_PATH", None)
        os.environ.pop("LOCAL_AGENT_DISABLE_BOOTSTRAP_WRITE", None)
        self.tmpdir.cleanup()

    def test_second_profile_does_not_retrigger_onboarding(self):
        # Профиль A проходит онбординг первым → first_run=True, маркер создан.
        first = RuntimeTokenStore()
        first.issue()
        self.assertTrue(first.first_run())
        marker = Path(os.environ["ONBOARDED_MARKER_PATH"])
        self.assertTrue(marker.exists())

        # Запуск профиля B после онбординга A: маркер глобален → first_run=False.
        second = RuntimeTokenStore()
        second.issue()
        self.assertFalse(second.first_run())

        # И повторный запуск самого A тоже не ретриггерит онбординг.
        again = RuntimeTokenStore()
        again.issue()
        self.assertFalse(again.first_run())

    def test_second_profile_does_not_reset_first_profile_settings(self):
        async def run():
            r_a = await profiles_router.profiles_create(
                {
                    "name": "profileA",
                    "description": "Alice",
                    "provider": "gemini",
                    "model": "gemini-2.5-flash",
                },
                _="",
            )
            self.assertTrue(r_a["ok"])
            await profiles_router.profiles_create(
                {
                    "name": "profileB",
                    "description": "Bob",
                    "provider": "deepseek",
                    "model": "deepseek-v3",
                },
                _="",
            )
            # Активация B не должна затронуть настройки A.
            await profiles_router.profiles_set_active({"name": "profileB"}, _="")

        asyncio.run(run())

        data_a = profiles_router._load_profile("profileA")
        self.assertEqual(data_a.get("description"), "Alice")
        self.assertEqual(data_a.get("provider"), "gemini")
        self.assertEqual(data_a.get("model"), "gemini-2.5-flash")

        data_b = profiles_router._load_profile("profileB")
        self.assertEqual(data_b.get("description"), "Bob")
        self.assertEqual(data_b.get("model"), "deepseek-v3")

        state = profiles_router._load_state()
        self.assertEqual(state.get("active"), "profileB")

    def test_profiles_are_isolated_files(self):
        async def run():
            await profiles_router.profiles_create({"name": "isoA", "description": "D1", "model": "m1"}, _="")
            await profiles_router.profiles_create({"name": "isoB", "description": "D2", "model": "m2"}, _="")

        asyncio.run(run())

        root = Path(self.tmpdir.name) / "profiles"
        self.assertTrue((root / "isoA" / "profile.yaml").exists())
        self.assertTrue((root / "isoB" / "profile.yaml").exists())
        # Настройки A и B независимы — содержимое файлов различается.
        raw_a = (root / "isoA" / "profile.yaml").read_text(encoding="utf-8")
        raw_b = (root / "isoB" / "profile.yaml").read_text(encoding="utf-8")
        self.assertNotEqual(raw_a, raw_b)


if __name__ == "__main__":
    unittest.main()

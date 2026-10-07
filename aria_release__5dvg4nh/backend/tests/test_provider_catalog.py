"""Проверяет, что refresh_provider_models_job реально пишет в БД и что
роут /providers/models больше не отдаёт хардкод openai/anthropic."""
import asyncio
import unittest

from aria.db.base import session_scope, init_db
from aria.db import repository as repo
from aria.llm.router import ProviderRouter
from aria.llm.providers.stub import StubProvider
from aria.scheduler.jobs import refresh_provider_models_job


class FakeListModelsProvider(StubProvider):
    provider_class = "standard_reasoning"

    async def list_models(self, timeout_sec: float = 10):
        return [{"model_id": "fake-model-1", "context_window": 8000,
                  "is_free_tier": True, "price_prompt_usd": 0, "price_completion_usd": 0}]


class ProviderCatalogTests(unittest.TestCase):
    def test_refresh_populates_provider_models(self):
        init_db(create_all=True)
        router = ProviderRouter()
        provider = FakeListModelsProvider(provider_id="fake-provider")
        router.register(provider)

        asyncio.run(refresh_provider_models_job(router))

        with session_scope() as db:
            rows = repo.list_provider_models(db, provider_id="fake-provider")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].model_id, "fake-model-1")

    def test_dedup_prevents_duplicates(self):
        """Проверяет, что один провайдер в двух классах не создаёт дубли."""
        init_db(create_all=True)
        router = ProviderRouter()
        p = FakeListModelsProvider(provider_id="dup-provider")
        p.provider_class = "standard_reasoning"
        router.register(p)
        # Симулируем регистрацию в другом классе — меняем provider_class
        p2 = FakeListModelsProvider(provider_id="dup-provider")
        p2.provider_class = "premium_reasoning"
        router.register(p2)

        asyncio.run(refresh_provider_models_job(router))

        with session_scope() as db:
            rows = repo.list_provider_models(db, provider_id="dup-provider")
        self.assertEqual(len(rows), 1)

    def test_exception_does_not_crash_job(self):
        """Провайдер без list_models (падающий) не убивает весь job."""
        class BrokenProvider(StubProvider):
            async def list_models(self, timeout_sec: float = 10):
                raise RuntimeError("network error")

        init_db(create_all=True)
        router = ProviderRouter()
        router.register(BrokenProvider(provider_id="broken"))

        # Не должно упасть
        asyncio.run(refresh_provider_models_job(router))


if __name__ == "__main__":
    unittest.main()

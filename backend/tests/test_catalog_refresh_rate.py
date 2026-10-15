"""Прод 12–14.10: 32 639 GET /models за три дня — цикл планировщика обновлял каталог моделей на каждом тике (30 с)."""
from __future__ import annotations

import asyncio

from aria.scheduler.jobs import RefreshGate, refresh_provider_models_job


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_gate_starts_closed_and_opens_once_per_interval():
    clock = Clock()
    gate = RefreshGate(1800, clock=clock)
    assert gate.due() is False, "первый запуск делает старт приложения"
    clock.now += 1799
    assert gate.due() is False
    clock.now += 2
    assert gate.due() is True and gate.due() is False
    clock.now += 1800
    assert gate.due() is True


def test_gate_with_start_due_opens_immediately():
    assert RefreshGate(60, clock=Clock(), start_due=True).due() is True


def test_every_30s_tick_does_not_become_a_refresh_for_an_hour():
    clock = Clock()
    gate, fired = RefreshGate(1800, clock=clock), 0
    for _ in range(120):  # час тиков по 30 секунд
        clock.now += 30
        fired += gate.due()
    assert fired == 2  # раньше было 120


class FakeProvider:
    def __init__(self, pid, base_url, fail=False):
        self.provider_id, self.base_url, self.fail, self.calls = pid, base_url, fail, 0

    async def list_models(self, timeout_sec=10):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return [{"model_id": "m1", "context_window": 1}]


class FakeRouter:
    def __init__(self, providers):
        self.providers_by_class = {"c": providers}


def test_one_request_per_host_not_per_chain_link():
    links = [FakeProvider(f"gemini-{i}", "https://g.example/v1") for i in range(30)]
    other = FakeProvider("deepseek", "https://d.example/v1")
    asyncio.run(refresh_provider_models_job(FakeRouter([*links, other])))
    assert sum(p.calls for p in links) == 1 and other.calls == 1


def test_failed_host_is_not_cached_so_the_next_provider_may_succeed():
    bad, good = FakeProvider("a", "https://g.example/v1", fail=True), FakeProvider("b", "https://g.example/v1")
    asyncio.run(refresh_provider_models_job(FakeRouter([bad, good])))
    assert bad.calls == 1 and good.calls == 1

"""Beta criterion «Stop/Cancel срабатывает ≤ 1 с»: отмена идущей и стоящей в очереди задачи."""
from __future__ import annotations

import asyncio
import time

from aria.core.taskrunner import TaskRunner
from aria.llm.providers.base import ChatMessage, LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter

from tests.test_wave1_runner_and_policy import _make_task, _status_of

STOP_BUDGET_SEC = 1.0


class HangingLlm(LlmProvider):
    provider_id = "hang-stub"
    provider_class = "standard_reasoning"

    async def check_connectivity(self, timeout_sec) -> bool:  # pragma: no cover
        return True

    async def chat(self, messages: list[ChatMessage], tools, timeout_sec) -> LlmResponse:
        await asyncio.sleep(60)
        return LlmResponse(text="never")


def _runner(workers: int) -> TaskRunner:
    router = ProviderRouter()
    router.register(HangingLlm())
    return TaskRunner(router, "/tmp/aria_stop_sandbox", max_workers=workers)


async def _wait_running(runner: TaskRunner, key: str, sec: float = 10.0) -> bool:
    end = time.monotonic() + sec
    while time.monotonic() < end:
        if key in runner.status()["running"]:
            return True
        await asyncio.sleep(0.02)
    return False


def test_cancel_of_running_task_finishes_within_budget(monkeypatch):
    monkeypatch.setattr("aria.storage.obsidian_vault.relevant_block", lambda *a, **k: "")
    task_id = _make_task(objective="stop latency running")
    runner = _runner(1)
    took: list[float] = []

    async def drive():
        await runner.start()
        try:
            runner.submit(task_id, mode="agent", source="test")
            assert await _wait_running(runner, str(task_id)), "task never started"
            t0 = time.monotonic()
            runner.cancel(task_id)
            while str(task_id) in runner.status()["running"]:
                await asyncio.sleep(0.01)
                assert time.monotonic() - t0 < 10
            took.append(time.monotonic() - t0)
        finally:
            await runner.stop()

    asyncio.run(drive())
    assert took[0] <= STOP_BUDGET_SEC, f"Stop took {took[0]:.2f}s, budget {STOP_BUDGET_SEC}s"
    assert _status_of(task_id) == "cancelled"


def test_cancel_stops_all_parallel_tasks_within_budget(monkeypatch):
    monkeypatch.setattr("aria.storage.obsidian_vault.relevant_block", lambda *a, **k: "")
    # TaskRunner drops to 1 worker when little RAM is free; this test needs all 3 running in parallel.
    monkeypatch.setattr("aria.core.taskrunner.available_ram_mb", lambda: None)
    ids = [_make_task(objective=f"stop latency parallel {i}") for i in range(3)]
    runner = _runner(3)
    took: list[float] = []

    async def drive():
        await runner.start()
        try:
            for i in ids:
                runner.submit(i, mode="agent", source="test")
            for i in ids:
                assert await _wait_running(runner, str(i)), "task never started"
            t0 = time.monotonic()
            for i in ids:
                runner.cancel(i)
            while runner.status()["running"]:
                await asyncio.sleep(0.01)
                assert time.monotonic() - t0 < 10
            took.append(time.monotonic() - t0)
        finally:
            await runner.stop()

    asyncio.run(drive())
    assert took[0] <= STOP_BUDGET_SEC, f"Stop of 3 tasks took {took[0]:.2f}s"
    assert {_status_of(i) for i in ids} == {"cancelled"}


def test_cancel_of_queued_task_never_runs_it():
    first = _make_task(objective="stop queued blocker")
    queued = _make_task(objective="stop queued victim")
    runner = _runner(1)

    async def drive():
        await runner.start()
        try:
            runner.submit(first, mode="agent", source="test")
            assert await _wait_running(runner, str(first))
            runner.submit(queued, mode="agent", source="test")
            runner.cancel(queued)
            runner.cancel(first)
            await asyncio.sleep(0.6)
            assert str(queued) not in runner.status()["running"]
        finally:
            await runner.stop()

    asyncio.run(drive())
    assert _status_of(queued) != "in_progress"

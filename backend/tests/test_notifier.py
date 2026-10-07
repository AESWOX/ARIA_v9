"""test_notifier.py — L5: TelegramNotifier доставка, retry, idempotency; executor escalation."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from aria.core.executor import MAX_ZABYL_RETRIES, run_task
from aria.core.integrity import IntegrityFlag
from aria.core.notifiers import telegram as telegram_mod
from aria.core.notifiers.telegram import TelegramNotifier


# ═══════════════════════════════════════════════════════════════════
# Fake HTTP transport (httpx.AsyncClient подменяется)
# ═══════════════════════════════════════════════════════════════════

class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = f"HTTP {status_code}"

    def json(self):
        return self._body


class FakeClient:
    """Мини-клиент с запланированной очередью ответов."""

    responses: list[FakeResponse] = []
    all_posts: list[tuple[str, dict]] = []

    def __init__(self, *args, **kwargs):
        FakeClient.instance = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        self.posts = [(url, json)]
        FakeClient.all_posts.append((url, json))
        if FakeClient.responses:
            resp = FakeClient.responses.pop(0)
        else:
            resp = FakeResponse(200)
        if resp.status_code == 429:
            resp._body = {"parameters": {"retry_after": 0}}
        return resp


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    telegram_mod._SENT_ESCALATIONS.clear()
    FakeClient.responses = []
    FakeClient.all_posts = []
    FakeClient.instance = None
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    yield
    telegram_mod._SENT_ESCALATIONS.clear()
    FakeClient.responses = []
    FakeClient.all_posts = []
    FakeClient.instance = None


def _notifier(**kw) -> TelegramNotifier:
    defaults = dict(bot_token="123:TOKEN", chat_id="777", timeout=1, retry=2)
    defaults.update(kw)
    return TelegramNotifier(**defaults)


class RecordingNotifier:
    """Записывает эскалации; используется для executor-интеграции."""

    def __init__(self):
        self.calls: list[dict] = []

    async def send_escalation(self, **kwargs) -> None:
        self.calls.append(kwargs)


# ═══════════════════════════════════════════════════════════════════
# TelegramNotifier — доставка
# ═══════════════════════════════════════════════════════════════════

class TestTelegramDelivery:
    def test_success_sends_post(self):
        asyncio.run(_notifier().send_escalation(
            task_id="aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000",
            objective="do the thing",
            claimed_result="done",
            audit_findings="all good",
            iteration=2,
        ))
        client = FakeClient.instance
        assert client is not None
        assert len(client.posts) == 1
        url, payload = client.posts[0]
        assert url.startswith("https://api.telegram.org/bot123:TOKEN/sendMessage")
        assert payload["chat_id"] == "777"
        assert "aaaaaaaa" in payload["text"]
        assert "do the thing" in payload["text"]
        assert "**Итерация:** 2/3" in payload["text"]

    def test_non_200_records_error_but_returns(self):
        FakeClient.responses = [FakeResponse(500, {"description": "boom"})]
        asyncio.run(_notifier().send_escalation(
            task_id="aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000",
            objective="x", claimed_result="y", audit_findings="z", iteration=1,
        ))
        assert FakeClient.all_posts


# ═══════════════════════════════════════════════════════════════════
# TelegramNotifier — retry на 429
# ═══════════════════════════════════════════════════════════════════

class TestTelegramRateLimit:
    def test_429_then_200_succeeds(self):
        FakeClient.responses = [FakeResponse(429), FakeResponse(200)]
        asyncio.run(_notifier(retry=2).send_escalation(
            task_id="aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000",
            objective="x", claimed_result="y", audit_findings="z", iteration=1,
        ))
        assert len(FakeClient.all_posts) == 2

    def test_all_retries_exhausted_does_not_raise(self):
        FakeClient.responses = [FakeResponse(500)] * 5
        asyncio.run(_notifier(retry=2).send_escalation(
            task_id="aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000",
            objective="x", claimed_result="y", audit_findings="z", iteration=1,
        ))
        assert len(FakeClient.all_posts) <= 2


# ═══════════════════════════════════════════════════════════════════
# TelegramNotifier — idempotency (одна эскалация на task_id+iteration)
# ═══════════════════════════════════════════════════════════════════

class TestTelegramIdempotency:
    def test_same_key_skipped(self):
        n = _notifier()
        kw = dict(
            task_id="aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000",
            objective="x", claimed_result="y", audit_findings="z", iteration=3,
        )
        asyncio.run(n.send_escalation(**kw))
        asyncio.run(n.send_escalation(**kw))
        assert len(FakeClient.all_posts) == 1

    def test_different_iteration_is_new_key(self):
        n = _notifier()
        base = dict(
            task_id="aaaaaaaa-bbbb-cccc-dddd-eeeeffff0000",
            objective="x", claimed_result="y", audit_findings="z",
        )
        asyncio.run(n.send_escalation(**{**base, "iteration": 2}))
        asyncio.run(n.send_escalation(**{**base, "iteration": 3}))
        assert len(FakeClient.all_posts) == 2


# ═══════════════════════════════════════════════════════════════════
# Executor integration — исчерпание bounded-retry вызывает notifier
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture()
def executor_db(tmp_path):
    from aria.db.base import get_engine, init_db, session_scope
    from aria.db import models as m

    get_engine().dispose()
    db_path = tmp_path / "notifier_e2e.db"
    init_db(f"sqlite:///{db_path}", create_all=True)

    with session_scope() as db:
        sess = m.Session(title="Notifier E2E", current_task_id=None)
        db.add(sess)
        db.flush()
        task = m.Task(session_id=sess.id, objective="write report.txt with summary")
        db.add(task)
        db.flush()
        yield db, task
    get_engine().dispose()


class TestExecutorEscalationNotifier:
    def test_retry_exhaustion_notifies_once(self, executor_db, monkeypatch):
        db, task = executor_db
        notifier = RecordingNotifier()

        async def fake_stage2(session, task, vault_ctx, router, force_replan=False, feedback=None):
            from aria.db import models as m
            plan = session.get(m.TaskPlan, task.id) or m.TaskPlan(
                task_id=task.id,
                plan_json=[{"step_id": "s1", "objective": "write report.txt", "role": "coder", "tool_ref": "write_file"}],
            )
            plan.iteration_count = MAX_ZABYL_RETRIES  # сразу исчерпаны
            session.add(plan)
            session.flush()
            return plan, []

        async def fake_stage3(session, task, plan, router):
            return [{"tool_name": "write_file", "status": "ok", "output_json": {}}]

        async def fake_stage4(session, task, plan, calls, router):
            return [IntegrityFlag.ZABYL("report.txt not written", missing_steps=["write report.txt"])]

        monkeypatch.setattr("aria.core.executor._stage2_plan", fake_stage2)
        monkeypatch.setattr("aria.core.executor._stage3_execute", fake_stage3)
        monkeypatch.setattr("aria.core.executor._stage4_audit", fake_stage4)
        monkeypatch.setattr("aria.core.executor.search_vault", lambda objective: {"matches": []})

        result = asyncio.run(run_task(db, task, router=None, notifier=notifier))

        assert result["status"] == "escalated"
        assert len(notifier.calls) == 1
        assert notifier.calls[0]["task_id"] == str(task.id)
        assert notifier.calls[0]["iteration"] == MAX_ZABYL_RETRIES
        assert "report.txt" in notifier.calls[0]["audit_findings"]

    def test_repeat_run_does_not_duplicate(self, executor_db, monkeypatch):
        """Повторный прогон той же задачи — TelegramNotifier не шлёт дубль."""
        db, task = executor_db
        notifier = _notifier()

        async def fake_stage2(session, task, vault_ctx, router, force_replan=False, feedback=None):
            from aria.db import models as m
            plan = session.get(m.TaskPlan, task.id) or m.TaskPlan(
                task_id=task.id,
                plan_json=[{"step_id": "s1", "objective": "write report.txt", "role": "coder", "tool_ref": "write_file"}],
            )
            plan.iteration_count = MAX_ZABYL_RETRIES
            session.add(plan)
            session.flush()
            return plan, []

        async def fake_stage3(session, task, plan, router):
            return [{"tool_name": "write_file", "status": "ok", "output_json": {}}]

        async def fake_stage4(session, task, plan, calls, router):
            return [IntegrityFlag.ZABYL("report.txt not written", missing_steps=["write report.txt"])]

        monkeypatch.setattr("aria.core.executor._stage2_plan", fake_stage2)
        monkeypatch.setattr("aria.core.executor._stage3_execute", fake_stage3)
        monkeypatch.setattr("aria.core.executor._stage4_audit", fake_stage4)
        monkeypatch.setattr("aria.core.executor.search_vault", lambda objective: {"matches": []})

        asyncio.run(run_task(db, task, router=None, notifier=notifier))
        first = len(FakeClient.all_posts)
        asyncio.run(run_task(db, task, router=None, notifier=notifier))

        assert first == 1
        assert len(FakeClient.all_posts) == 1  # дубль отсечён

    def test_naebal_notifies(self, executor_db, monkeypatch):
        """Oracle НАЕБАЛ — тоже эскалация через notifier."""
        db, task = executor_db
        notifier = RecordingNotifier()

        async def fake_stage2(session, task, vault_ctx, router, force_replan=False, feedback=None):
            from aria.db import models as m
            plan = m.TaskPlan(task_id=task.id, plan_json=[])
            session.add(plan)
            session.flush()
            return plan, [IntegrityFlag.NAEBAL("plan contradicts vault")]

        monkeypatch.setattr("aria.core.executor._stage2_plan", fake_stage2)
        monkeypatch.setattr("aria.core.executor.search_vault", lambda objective: {"matches": []})

        result = asyncio.run(run_task(db, task, router=None, notifier=notifier))

        assert result["status"] == "escalated"
        assert len(notifier.calls) == 1
        assert "plan contradicts vault" in notifier.calls[0]["audit_findings"]

"""19.4a (ARG-1): `web_fetch` не теряет url.

- A1: `PlanStep.url` сохраняется.
- A2: `resolve_web_fetch_url(step)` — общая функция для validate_plan и executor.
  Порядок: `step.url` -> `step.input["url"]` -> первый `https?://…` из objective.
  Только схемы http/https; несколько разных URL в objective = неоднозначно.
- A3: executor `web_fetch` берёт url через A2; нет нигде -> invalid_arguments сразу (R6).
- A4: validate_plan отклоняет `web_fetch` без url, причина уходит в feedback replan.
- A6: интеграционный тест R4 через `_stage2_plan` (file_read несуществующего пути).

Сеть фейковая, настоящих LLM нет, каждый тест быстрее 2 с.
"""
from __future__ import annotations

import asyncio
import email.message
import io
import json
import socket
from types import SimpleNamespace

import pytest

from aria.config import get_settings
from aria.core import executor as ex
from aria.core import plan_validator as pv
from aria.db import base as db_base
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import PlanStatus
from aria.tools.handlers import web as webmod

# ── A1 / A2: PlanStep.url и resolve_web_fetch_url ───────────────────────────


def test_planstep_keeps_url_field():
    step = pv.PlanStep(objective="x", tool_ref="web_fetch", url="https://example.com")
    assert step.url == "https://example.com"
    assert step.model_dump(mode="json")["url"] == "https://example.com"


def test_url_from_step_url():
    assert pv.resolve_web_fetch_url({"url": "https://example.com", "objective": "x"}) == "https://example.com"


def test_url_from_step_input():
    assert pv.resolve_web_fetch_url({"input": {"url": "https://example.com"}, "objective": "x"}) == "https://example.com"


def test_url_from_objective_strips_trailing_punctuation():
    assert pv.resolve_web_fetch_url({"objective": "открой https://example.com."}) == "https://example.com"
    assert pv.resolve_web_fetch_url({"objective": "см. https://example.com)»"}) == "https://example.com"


def test_non_http_scheme_rejected():
    assert pv.resolve_web_fetch_url({"url": "ftp://example.com/file"}) is None
    assert pv.resolve_web_fetch_url({"url": "file:///etc/passwd"}) is None
    assert pv.resolve_web_fetch_url({"input": {"url": "ftp://example.com"}}) is None


def test_objective_with_multiple_distinct_urls_is_ambiguous():
    with pytest.raises(pv.WebFetchUrlAmbiguous):
        pv.resolve_web_fetch_url({"objective": "см. https://a.com и https://b.com"})


# ── A4: validate_plan ───────────────────────────────────────────────────────


def _wf_step(objective="прочитать страницу", **extra):
    step = {"step_id": "s1", "objective": objective, "role": "research", "tool_ref": "web_fetch"}
    step.update(extra)
    return step


def test_validate_plan_rejects_web_fetch_without_url_with_reason():
    with pytest.raises(ValueError) as ei:
        pv.validate_plan([_wf_step()])
    msg = str(ei.value)
    assert "web_fetch" in msg and "url" in msg


def test_validate_plan_web_fetch_hint_is_actionable():
    """L4: причина отказа должна не только констатировать пропуск, но и говорить Оракулу, что делать."""
    with pytest.raises(ValueError) as ei:
        pv.validate_plan([_wf_step()])
    msg = str(ei.value)
    assert "Укажи" in msg and "http" in msg, msg


def test_validate_plan_rejects_ambiguous_multiple_urls():
    with pytest.raises(ValueError) as ei:
        pv.validate_plan([_wf_step(objective="см. https://a.com и https://b.com")])
    assert "неоднознач" in str(ei.value) or "несколько" in str(ei.value)


def test_validate_plan_accepts_web_fetch_with_url_in_objective():
    steps = pv.validate_plan([_wf_step(objective="открой https://example.com")])
    assert steps[0].tool_ref == "web_fetch"


# ── A4/A6: replan через _stage2_plan ────────────────────────────────────────


@pytest.fixture()
def plan_env(tmp_path, monkeypatch):
    saved = (db_base._engine, db_base._SessionLocal)
    db_base.init_db(f"sqlite:///{(tmp_path / 'plan.db').as_posix()}", create_all=True)
    sandbox = tmp_path / "sandbox"
    monkeypatch.setattr(get_settings(), "agent_sandbox_root", str(sandbox))
    yield sandbox
    db_base._engine, db_base._SessionLocal = saved


class PlanRouter:
    """Скриптовый Oracle: возвращает один и тот же план и пишет все user-сообщения."""

    def __init__(self, plan_steps):
        self.plan_steps = plan_steps
        self.messages: list[list] = []

    async def route_chat(self, cls, messages, **kw):
        self.messages.append(messages)
        return SimpleNamespace(response=SimpleNamespace(text=json.dumps(self.plan_steps), tool_calls=[]))

    @property
    def user_contents(self) -> list[str]:
        return [m[1].content for m in self.messages]


def test_replan_feedback_contains_missing_url_reason(plan_env):
    router = PlanRouter([_wf_step()])
    with session_scope() as db:
        s = repo.create_session(db, "19_4a_r")
        t = repo.create_task(db, s, role="general", objective="x")
        plan, flags = asyncio.run(ex._stage2_plan(db, t, {}, router))
    assert [f.kind for f in flags] == ["naebal"]
    assert plan.status == PlanStatus.escalated.value
    assert len(router.messages) == 2, "первая генерация + один replan"
    assert "web_fetch" in router.user_contents[1] and "url" in router.user_contents[1]


def test_stage2_plan_rejects_nonexistent_file_read(plan_env):
    plan_steps = [{"step_id": "s1", "objective": "прочитать", "role": "coder",
                   "tool_ref": "file_read", "path": "missing.txt", "depends_on": []}]
    router = PlanRouter(plan_steps)
    with session_scope() as db:
        s = repo.create_session(db, "19_4a_a6")
        t = repo.create_task(db, s, role="general", objective="x")
        plan, flags = asyncio.run(ex._stage2_plan(db, t, {}, router))
    assert plan.status == PlanStatus.escalated.value
    assert [f.kind for f in flags] == ["naebal"]
    assert len(router.messages) == 2, "невалидный путь -> один replan"
    assert "non-existent" in router.user_contents[1]


# ── A3: executor web_fetch ──────────────────────────────────────────────────


def _stage3_env(plan_json, tmp_path, monkeypatch):
    from aria.db import models as m
    from aria.db.base import init_db

    init_db(f"sqlite:///{(tmp_path / 's3.db').as_posix()}", create_all=True)
    monkeypatch.setattr(get_settings(), "agent_sandbox_root", str(tmp_path / "sb"))
    cm = session_scope()
    db = cm.__enter__()
    sess = m.Session(title="s3", current_task_id=None)
    db.add(sess); db.flush()
    task = m.Task(session_id=sess.id, objective="t")
    db.add(task); db.flush()
    plan = m.TaskPlan(task_id=task.id, plan_json=plan_json)
    db.add(plan); db.flush()
    return db, task, plan


class FakeResp:
    def __init__(self, body: bytes):
        self._buf = io.BytesIO(body)
        self.status = 200
        self._url = "https://example.com/"
        self.headers = email.message.Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_REAL_GETADDRINFO = socket.getaddrinfo


def _public_dns(host, port, *a, **k):
    try:
        return _REAL_GETADDRINFO(host, port, type=socket.SOCK_STREAM, flags=socket.AI_NUMERICHOST)
    except (socket.gaierror, UnicodeError):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


def _install_fake_net(monkeypatch, captured):
    monkeypatch.setattr(socket, "getaddrinfo", _public_dns)

    def fake_open(req, timeout):
        captured["url"] = req.full_url
        return FakeResp(b"<!doctype html><title>Example Domain</title><p>hello</p>")

    monkeypatch.setattr(webmod, "_open_url", fake_open)


def test_executor_web_fetch_uses_resolved_url(tmp_path, monkeypatch):
    captured: dict = {}
    _install_fake_net(monkeypatch, captured)
    plan = [{"step_id": "wf1", "objective": "Загрузить страницу https://example.com/ и прочитать заголовок",
             "role": "research", "tool_ref": "web_fetch", "depends_on": [], "covers": [], "write_scope": []}]
    db, task, p = _stage3_env(plan, tmp_path, monkeypatch)
    calls = asyncio.run(ex._stage3_execute(db, task, p, router=object()))
    assert calls[0]["status"] == "ok", calls[0]
    assert captured.get("url") == "https://example.com/"


def test_executor_web_fetch_without_url_fails_fast_without_replan(tmp_path, monkeypatch):
    plan = [{"step_id": "wf1", "objective": "прочитать страницу", "role": "research",
             "tool_ref": "web_fetch", "depends_on": [], "covers": [], "write_scope": []}]
    db, task, p = _stage3_env(plan, tmp_path, monkeypatch)
    calls = asyncio.run(ex._stage3_execute(db, task, p, router=object()))
    assert calls[0]["status"] == "error", calls[0]
    err = calls[0]["output_json"]["error"]
    assert err.startswith("invalid_arguments (web_fetch)"), err
    assert "поле url" in err and "objective" in err, err
    assert ex._deterministic_step_error([calls[0]]) == ("invalid_arguments", err)

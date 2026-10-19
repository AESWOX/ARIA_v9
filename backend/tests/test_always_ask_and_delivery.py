"""Решения владельца 19.10: вопросы всегда (до 7, по 5 вариантов, с рекомендацией), результат задачи
приходит в чат, кнопка подтверждения плана не роняет сервер 500 на завершённой задаче."""
from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.core import clarifier, executor
from aria.db import base as db_base
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus
from aria.routers import tasks as tasks_router


@pytest.fixture()
def env(tmp_path, monkeypatch):
    saved = (db_base._engine, db_base._SessionLocal)
    db_base.init_db(f"sqlite:///{(tmp_path / 'ask.db').as_posix()}", create_all=True)
    sandbox = tmp_path / "sandbox"
    monkeypatch.setattr(get_settings(), "agent_sandbox_root", str(sandbox))
    yield sandbox
    db_base._engine, db_base._SessionLocal = saved


# ── вопросы ──────────────────────────────────────────────────────────────

def test_normalise_keeps_five_options_default_and_caps_at_seven():
    raw = [{"text": f"Вопрос {i}", "options": [f"o{j}" for j in range(9)], "recommended": "o3"} for i in range(10)]
    out = clarifier._normalise_questions(raw)
    assert len(out) == clarifier.MAX_QUESTIONS == 7
    assert all(len(q["options"]) == 5 for q in out)
    assert out[0]["default_value"] == "o3" and out[0]["blocking"] is True


def test_recommended_outside_options_falls_back_to_first_option():
    out = clarifier._normalise_questions([{"text": "Формат?", "options": ["a", "b"], "recommended": "zzz"}])
    assert out[0]["default_value"] == "a"


def test_fallback_questions_are_three_with_five_options_each():
    qs = clarifier._fallback_questions()
    assert len(qs) == 3
    assert all(len(q["options"]) == 5 and q["default_value"] in q["options"] and q["blocking"] for q in qs)


def test_question_message_lists_lettered_options_and_marks_recommended():
    msg = clarifier._question_message(clarifier._fallback_questions())
    assert "   A) " in msg and "   E) " in msg and "(рекомендую)" in msg


def test_gate_asks_even_when_model_says_request_is_clear(env, monkeypatch):
    async def clear(router, content, spec):
        return {"goal": "Составить список навыков Blender", "questions": [],
                "acceptance_criteria": [{"id": "ac-1", "statement": "ответ содержит перечень из 15 пунктов"}]}

    monkeypatch.setattr(clarifier, "_ask_model", clear)
    with session_scope() as db:
        s = repo.create_session(db, "q")
        t = repo.create_task(db, s, role="general", objective="навыки blender", draft_tz_md="# x\n")
        repo.set_task_status(db, t, TaskStatus.draft)
        tid = t.id
    out = asyncio.run(clarifier.run_gate(tid, "навыки blender", router=object()))
    assert out.status == "awaiting_clarification" and len(out.questions) >= 3


# ── 500 -> 409 ───────────────────────────────────────────────────────────

def test_request_approval_on_finished_task_is_409_not_500(env):
    app = FastAPI()
    app.include_router(tasks_router.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    with session_scope() as db:
        s = repo.create_session(db, "done")
        t = repo.create_task(db, s, role="general", objective="x", draft_tz_md="# x\n")
        for st in (TaskStatus.approved, TaskStatus.in_progress, TaskStatus.under_audit, TaskStatus.done):
            repo.set_task_status(db, t, st)
        tid = str(t.id)
    r = TestClient(app, raise_server_exceptions=False).post(f"/tasks/{tid}/plan/request-approval")
    assert r.status_code == 409, r.text


# ── результат в чат ──────────────────────────────────────────────────────

def test_build_result_message_has_text_criteria_assumptions_and_paths():
    spec = SimpleNamespace(assumptions=["формат — Markdown"])
    msg = executor.build_result_message(
        SimpleNamespace(), [("generated/skills.md", "1. Скульптинг\n2. Ретопология")], spec,
        {"ac-1": "pass"}, "00-TASKS/task-1.md",
    )
    assert "1. Скульптинг" in msg and "ac-1: pass" in msg and "формат — Markdown" in msg
    assert "generated/skills.md" in msg and "00-TASKS/task-1.md" in msg


def test_build_result_message_truncates_huge_files():
    msg = executor.build_result_message(SimpleNamespace(), [("a.md", "x" * 20000)], None, None, None)
    assert len(msg) < executor.MAX_RESULT_CHARS + 600 and "обрезано" in msg


def test_result_files_ignores_paths_outside_sandbox(env):
    env.mkdir(parents=True)
    (env / "ok.md").write_text("текст", encoding="utf-8")
    outside = env.parent / "secret.txt"
    outside.write_text("секрет", encoding="utf-8")
    calls = [
        {"tool_name": "file_write", "status": "ok", "output_json": {"path": "ok.md"}},
        {"tool_name": "file_write", "status": "ok", "output_json": {"path": "../secret.txt"}},
    ]
    got = executor._result_files(calls, str(env))
    assert got == [("ok.md", "текст")]


class _Script:
    async def route_chat(self, cls, messages, **kw):
        sysmsg = messages[0].content
        if "PlanStep" in sysmsg:
            text = json.dumps([{"step_id": "s1", "objective": "записать отчёт", "role": "coder", "tool_ref": "file_write",
                                "path": "report.md", "depends_on": [], "covers": [], "write_scope": ["report.md"]}])
        elif "Сгенерируй содержимое файла" in sysmsg:
            text = "# Навыки\n\n1. Скульптинг — лепка формы"
        else:
            text = '{"verdict": "pass", "issues": [], "criteria": []}'
        return SimpleNamespace(response=SimpleNamespace(text=text, tool_calls=[]), provider_id="s",
                               degraded_to_free=False, fallback=False)


def test_finished_plan_task_posts_the_result_into_the_chat(env):
    with session_scope() as db:
        s = repo.create_session(db, "deliver")
        t = repo.create_task(db, s, role="general", objective="навыки blender", draft_tz_md="# tz\n")
        repo.set_task_status(db, t, TaskStatus.approved)
        sid, tid = s.id, t.id
        res = asyncio.run(executor.run_task(db, t, router=_Script()))
    assert res["status"] == "done", res
    with session_scope() as db:
        msgs = [m for m in repo.list_messages(db, sid, limit=50) if m.role == "assistant"]
    assert msgs, "в чате нет ответа ассистента"
    assert "1. Скульптинг — лепка формы" in msgs[-1].content and "report.md" in msgs[-1].content

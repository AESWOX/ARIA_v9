"""Сквозной прогон Plan-режима: реальный executor Stage 1-7, реальные БД и инструменты, LLM — скрипт.

Закрывает дыры, которых не видели юнит-тесты стадий по отдельности:
  * Stage 7 падал NameError (`repo` не импортирован) на ЛЮБОЙ задаче, дошедшей до доставки;
  * шаг shell с командой вне allowlist (approval_required) запускал ЗАБЫЛ-retry всего плана, повторная
    идентичная запись файла давала hash_before == hash_after и задача падала ложным integrity_naebal;
  * ЗАБЫЛ-retry заново выполнял уже успешные шаги.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from aria.config import get_settings
from aria.core import executor
from aria.db import base as db_base
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus


@pytest.fixture()
def env(tmp_path, monkeypatch):
    saved = (db_base._engine, db_base._SessionLocal)
    db_base.init_db(f"sqlite:///{(tmp_path / 'e2e.db').as_posix()}", create_all=True)
    sandbox = tmp_path / "sandbox"
    monkeypatch.setattr(get_settings(), "agent_sandbox_root", str(sandbox))
    yield sandbox
    db_base._engine, db_base._SessionLocal = saved


def _step(sid, tool, objective, deps=(), **extra):
    return {"step_id": sid, "objective": objective, "role": "coder", "tool_ref": tool,
            "depends_on": list(deps), "covers": [], "write_scope": [extra["path"]] if "path" in extra else [], **extra}


class Script:
    """Скриптовый LLM: плани Oracle по очереди, контент файла и команда фиксированы."""

    def __init__(self, plans, content="```python\nimport bpy\n```", command="echo ok"):
        self.plans, self.content, self.command = list(plans), content, command
        self.oracle_calls = 0

    async def route_chat(self, cls, messages, **kw):
        sysmsg = messages[0].content
        if "PlanStep" in sysmsg:
            plan = self.plans[min(self.oracle_calls, len(self.plans) - 1)]
            self.oracle_calls += 1
            text = json.dumps(plan)
        elif "Сгенерируй содержимое файла" in sysmsg:
            text = self.content
        elif "ОДНУ shell-команду" in sysmsg:
            text = self.command
        else:
            text = '{"verdict": "pass", "issues": [], "criteria": []}'
        return SimpleNamespace(response=SimpleNamespace(text=text, tool_calls=[]), provider_id="script",
                               degraded_to_free=False, fallback=False)


def _run(router):
    with session_scope() as db:
        s = repo.create_session(db, "e2e")
        t = repo.create_task(db, s, role="general", objective="модель человека в Blender", draft_tz_md="# tz\n")
        repo.set_task_status(db, t, TaskStatus.approved)
        tid = t.id
        result = asyncio.run(executor.run_task(db, t, router=router))
    with session_scope() as db:
        task = repo.get_task(db, tid)
        calls = [(c.tool_name, c.status.value) for c in repo.list_tool_calls(db, tid)]
        return result, task.status.value, task.error_code, task.error_message or "", calls


def test_happy_path_reaches_delivery_and_done(env):
    plan = [_step("s1", "file_write", "создать character_base.py", path="character_base.py"),
            _step("s2", "shell_execute", "проверить синтаксис", deps=["s1"])]
    result, status, code, msg, calls = _run(Script([plan], command="python -m py_compile character_base.py"))
    assert (status, code) == ("done", None), (result, msg)
    assert result["note_path"], "Stage 7 обязан записать заметку доставки"
    # markdown-обёртка модели не попала в файл
    assert (env / "character_base.py").read_text() == "import bpy\n"


def test_shell_step_needing_approval_fails_honestly_not_as_naebal(env):
    plan = [_step("s1", "file_write", "создать character_base.py", path="character_base.py"),
            _step("s2", "shell_execute", "запустить blender", deps=["s1"])]
    result, status, code, msg, calls = _run(Script([plan], command="blender --background --python character_base.py"))
    assert status == "failed" and code == "approval_required", (code, msg)
    assert "blender --background" in msg and "naebal" not in msg.lower()
    assert [c for c in calls if c[0] == "file_write"] == [("file_write", "ok")], "никаких повторных записей"


def test_zabyl_retry_does_not_rewrite_completed_steps(env):
    broken = [_step("s1", "file_write", "создать character_base.py", path="character_base.py"),
              _step("s2", "web_fetch", "прочитать справку", deps=["s1"])]          # нет url -> шаг падает
    fixed = [_step("s1", "file_write", "создать character_base.py", path="character_base.py"),
             _step("s2", "shell_execute", "проверить синтаксис", deps=["s1"])]
    router = Script([broken, fixed], command="python -m py_compile character_base.py")
    result, status, code, msg, calls = _run(router)
    assert router.oracle_calls == 2, "второй прогон — после replan"
    assert (status, code) == ("done", None), (result, code, msg)
    assert [c for c in calls if c[0] == "file_write"] == [("file_write", "ok")], "файл записан ровно один раз"


def test_skill_ref_step_runs_with_skill_body_in_prompt(env, tmp_path, monkeypatch):
    from aria import paths

    skills = tmp_path / "skills"
    (skills / "blender-human").mkdir(parents=True)
    (skills / "blender-human" / "SKILL.md").write_text("ПРАВИЛО-БЛЕНДЕР: начинай с базовой сетки", encoding="utf-8")
    monkeypatch.setattr(paths, "skills_dir", lambda: skills)
    seen: list[str] = []

    class Spy(Script):
        async def route_chat(self, cls, messages, **kw):
            seen.append(messages[0].content)
            return await super().route_chat(cls, messages, **kw)

    plan = [{"step_id": "s1", "objective": "описать этапы моделирования", "role": "coder", "skill_ref": "blender-human",
             "depends_on": [], "covers": [], "write_scope": []}]
    result, status, code, msg, calls = _run(Spy([plan]))
    assert (status, code) == ("done", None), (code, msg)
    assert any("ПРАВИЛО-БЛЕНДЕР" in m for m in seen), "тело навыка должно попасть в промпт шага"


def test_skill_ref_missing_skill_is_an_explicit_failure(env, tmp_path, monkeypatch):
    from aria import paths

    monkeypatch.setattr(paths, "skills_dir", lambda: tmp_path / "none")
    plan = [{"step_id": "s1", "objective": "x", "role": "coder", "skill_ref": "no-such-skill",
             "depends_on": [], "covers": [], "write_scope": []}]
    result, status, code, msg, calls = _run(Script([plan, plan, plan, plan]))
    assert status == "failed" and code != "done"

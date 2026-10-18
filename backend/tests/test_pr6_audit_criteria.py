"""PR-6: аудит по критериям приёмки + отчёт из TaskSpec."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aria.core import criteria_audit as ca
from aria.core.audit import _parse_criteria_statuses, run_audit
from aria.core.taskspec import TaskSpec, default_criterion
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import AuditVerdict, TaskStatus, ToolStatus


def _tc(tool, output, status="ok", cid="1"):
    return {"id": cid, "tool_name": tool, "status": status, "input_json": {}, "output_json": output}


# ─────────────────────────── детерминированные проверки ───────────────────────────

def test_evidence_match_passes():
    criteria = [{"id": "ac-1", "statement": "файл отчёта создан", "evidence": {"tool": "file_write", "field": "path", "equals": "generated/report.md"}}]
    statuses = ca.check_criteria(criteria, [_tc("file_write", {"path": "generated/report.md"})])
    assert statuses["ac-1"]["status"] == ca.STATUS_PASS


def test_evidence_mismatch_fails():
    criteria = [{"id": "ac-1", "statement": "файл отчёта создан", "evidence": {"tool": "file_write", "field": "path", "equals": "generated/report.md"}}]
    statuses = ca.check_criteria(criteria, [_tc("file_write", {"path": "generated/other.md"})])
    assert statuses["ac-1"]["status"] == ca.STATUS_FAIL


def test_path_in_statement_is_checked_against_tool_output():
    criteria = [{"id": "ac-2", "statement": "generated/report.md содержит таблицу"}]
    assert ca.check_criteria(criteria, [_tc("file_write", {"path": "generated/report.md", "bytes": 120})])["ac-2"]["status"] == ca.STATUS_PASS
    assert ca.check_criteria(criteria, [_tc("file_write", {"path": "generated/nope.txt"})])["ac-2"]["status"] == ca.STATUS_FAIL


def test_failed_tool_call_fails_the_criterion():
    criteria = [{"id": "ac-3", "statement": "тесты проходят и exit_code равен 0"}]
    statuses = ca.check_criteria(criteria, [_tc("shell_execute", {"exit_code": 1}, status="error")])
    assert statuses["ac-3"]["status"] == ca.STATUS_FAIL


def test_criterion_without_evidence_stays_unknown():
    criteria = [{"id": "ac-4", "statement": "текст отчёта соответствует требованиям"}]
    statuses = ca.check_criteria(criteria, [_tc("llm_task", {"content": "готово"})])
    assert statuses["ac-4"]["status"] == ca.STATUS_UNKNOWN


def test_structural_fail_is_not_overridden_by_the_model():
    det = {"ac-1": {"status": ca.STATUS_FAIL, "note": "нет файла"}}
    merged = ca.merge_statuses(det, {"ac-1": {"status": ca.STATUS_PASS, "note": "модель довольна"}})
    assert merged["ac-1"]["status"] == ca.STATUS_FAIL


# ─────────────────────────── парсер ответа аудитора ───────────────────────────

def test_parse_criteria_statuses_from_json():
    text = '{"verdict": "pass", "issues": [], "criteria": [{"id": "ac-1", "status": "pass", "note": "ок"}, {"id": "ac-2", "status": "fail"}]}'
    parsed = _parse_criteria_statuses(text)
    assert parsed["ac-1"]["status"] == "pass" and parsed["ac-2"]["status"] == "fail"


def test_parse_criteria_statuses_ignores_garbage():
    assert _parse_criteria_statuses("никакого json тут нет") == {}
    assert _parse_criteria_statuses('{"criteria": [{"id": "ac-1", "status": "banana"}]}') == {}


# ─────────────────────────── run_audit ───────────────────────────

class _FakeRouter:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    async def route_chat(self, *args, **kwargs):
        self.calls += 1
        return SimpleNamespace(
            response=SimpleNamespace(text=self.text, tool_calls=[]),
            provider_id="fake-qa",
            degraded_to_free=False,
            fallback=False,
        )


@pytest.mark.anyio
async def test_deterministic_criteria_failure_skips_the_model():
    router = _FakeRouter('{"verdict": "pass", "issues": []}')
    with session_scope() as db:
        s = repo.create_session(db, "pr6-det")
        t = repo.create_task(db, s, role="general", objective="собрать отчёт", draft_tz_md="# tz\n")
        repo.set_task_status(db, t, TaskStatus.approved)
        repo.set_task_status(db, t, TaskStatus.in_progress)
        repo.start_tool_call(db, s, t, "file_write", "coder", "low", {"path": "generated/other.md"})
        call = repo.list_tool_calls(db, t.id)[0]
        repo.finish_tool_call(db, call, ToolStatus.ok, {"path": "generated/other.md"})

        report = await run_audit(
            db, s, t, router,
            criteria=[{"id": "ac-1", "statement": "файл generated/report.md создан",
                       "evidence": {"tool": "file_write", "field": "path", "equals": "generated/report.md"}}],
            assumptions=["квартал — текущий"],
        )
        assert report.verdict == AuditVerdict.needs_rework
        assert report.auditor_model == "criteria-deterministic"
        assert any("ac-1" in p for p in report.patch_suggestions)
        assert report.plan_vs_fact["assumptions"] == ["квартал — текущий"]
    assert router.calls == 0, "при структурном провале критерия модель не опрашивается"


@pytest.mark.anyio
async def test_model_criteria_status_reaches_the_report():
    router = _FakeRouter('{"verdict": "pass", "issues": [], "criteria": [{"id": "ac-1", "status": "pass", "note": "совпало"}]}')
    with session_scope() as db:
        s = repo.create_session(db, "pr6-llm")
        t = repo.create_task(db, s, role="general", objective="собрать отчёт", draft_tz_md="# tz\n")
        repo.set_task_status(db, t, TaskStatus.approved)
        repo.set_task_status(db, t, TaskStatus.in_progress)
        repo.start_tool_call(db, s, t, "llm_task", "coder", "low", {"objective": "отчёт"})
        call = repo.list_tool_calls(db, t.id)[0]
        repo.finish_tool_call(db, call, ToolStatus.ok, {"content": "готово"})

        report = await run_audit(
            db, s, t, router,
            criteria=[{"id": "ac-1", "statement": "текст отчёта соответствует требованиям владельца"}],
        )
        assert report.verdict == AuditVerdict.pass_
        assert report.plan_vs_fact["criteria_status"]["ac-1"]["status"] == "pass"
        assert report.metrics_compared["criteria"]["ac-1"]["note"] == "совпало"


@pytest.mark.anyio
async def test_model_criterion_failure_forces_rework():
    router = _FakeRouter('{"verdict": "pass", "issues": [], "criteria": [{"id": "ac-1", "status": "fail", "note": "таблицы нет"}]}')
    with session_scope() as db:
        s = repo.create_session(db, "pr6-fail")
        t = repo.create_task(db, s, role="general", objective="собрать отчёт", draft_tz_md="# tz\n")
        repo.set_task_status(db, t, TaskStatus.approved)
        repo.set_task_status(db, t, TaskStatus.in_progress)
        repo.start_tool_call(db, s, t, "llm_task", "coder", "low", {"objective": "отчёт"})
        call = repo.list_tool_calls(db, t.id)[0]
        repo.finish_tool_call(db, call, ToolStatus.ok, {"content": "готово"})

        report = await run_audit(db, s, t, router, criteria=[{"id": "ac-1", "statement": "в отчёте есть таблица за квартал"}])
        assert report.verdict == AuditVerdict.needs_rework
        assert any("ac-1" in p for p in report.patch_suggestions)


# ─────────────────────────── отчёт (delivery note) ───────────────────────────

def test_delivery_note_contains_criteria_and_assumptions():
    from aria.core.executor import _generate_delivery_note
    from aria.db.models import TaskPlan

    spec = TaskSpec(
        goal="Собрать отчёт по продажам",
        acceptance_criteria=[default_criterion("отчёт")],
        assumptions=["квартал — текущий"],
    )
    with session_scope() as db:
        s = repo.create_session(db, "pr6-note")
        t = repo.create_task(db, s, role="general", objective="Собрать отчёт по продажам", draft_tz_md="# tz\n")
        plan = TaskPlan(task_id=t.id, plan_json=[], status="done", integrity_verdict="pass")
        db.add(plan)
        db.flush()
        note = _generate_delivery_note(t, plan, [], spec, {"ac-default": {"status": "pass", "note": "файл найден"}})

    assert "## Acceptance criteria" in note
    assert "ac-default" in note and "файл найден" in note
    assert "## Assumptions" in note and "квартал — текущий" in note

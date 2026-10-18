"""PR-2: executor ходит через FSM, аудит не путает "ok" с "token"."""
from __future__ import annotations

import pytest

from aria.core.audit import _parse_audit_verdict
from aria.core.state_machine import assert_transition_allowed
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import AuditVerdict, TaskStatus


def test_allowed_transitions_on_the_audit_path():
    assert_transition_allowed(TaskStatus.approved, TaskStatus.in_progress)
    assert_transition_allowed(TaskStatus.in_progress, TaskStatus.under_audit)
    assert_transition_allowed(TaskStatus.under_audit, TaskStatus.done)
    assert_transition_allowed(TaskStatus.awaiting_clarification, TaskStatus.approved)


def test_approved_to_done_is_still_forbidden():
    with pytest.raises(Exception):
        assert_transition_allowed(TaskStatus.approved, TaskStatus.done)


def test_transition_helper_refuses_to_jump_the_fsm():
    from aria.core.executor import _transition

    with session_scope() as db:
        s = repo.create_session(db, "pr2-fsm")
        t = repo.create_task(db, s, role="general", objective="проверка FSM", draft_tz_md="# tz\n")
        repo.set_task_status(db, t, TaskStatus.approved)

        assert _transition(db, t, TaskStatus.done) is False
        assert t.status == TaskStatus.approved

        assert _transition(db, t, TaskStatus.in_progress) is True
        assert _transition(db, t, TaskStatus.under_audit) is True
        assert _transition(db, t, TaskStatus.done) is True
        assert t.status == TaskStatus.done
        assert _transition(db, t, TaskStatus.in_progress) is False


def test_substring_ok_is_not_a_pass():
    assert _parse_audit_verdict("Модель упомянула token budget и look результата")[0] == AuditVerdict.needs_rework
    assert _parse_audit_verdict("см. book of changes")[0] == AuditVerdict.needs_rework


def test_word_ok_is_a_pass():
    assert _parse_audit_verdict("Вердикт: OK. Замечаний нет.")[0] == AuditVerdict.pass_
    assert _parse_audit_verdict("pass")[0] == AuditVerdict.pass_
    assert _parse_audit_verdict("результат соответствует objective")[0] == AuditVerdict.pass_


def test_json_verdict_still_works():
    assert _parse_audit_verdict('{"verdict": "pass", "issues": []}')[0] == AuditVerdict.pass_
    assert _parse_audit_verdict('{"verdict": "pass", "issues": ["нет теста"]}')[0] == AuditVerdict.needs_rework


def test_fail_markers_still_win():
    assert _parse_audit_verdict("есть проблема с артефактом")[0] == AuditVerdict.needs_rework

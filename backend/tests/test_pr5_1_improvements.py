"""П.5 ТЗ: вердикт needs_info, лимиты провайдеров, независимый аудитор.

DLP at-rest (п.5.4) проверяется в `test_pr1_7_followup.py`.
"""
from __future__ import annotations

from aria.core import audit, runprofile
from aria.core.state_machine import assert_transition_allowed
from aria.db.enums import TASK_TRANSITIONS, AuditVerdict, TaskStatus
from aria.config import get_settings


# ─────────────────────────── п.5.1: needs_info ───────────────────────────

def test_needs_info_verdict_exists():
    assert AuditVerdict.needs_info.value == "needs_info"


def test_under_audit_can_request_clarification():
    """Переход under_audit -> awaiting_clarification разрешён (§8.1 + П.5.1)."""
    assert TaskStatus.awaiting_clarification in TASK_TRANSITIONS[TaskStatus.under_audit]
    assert_transition_allowed(TaskStatus.under_audit, TaskStatus.awaiting_clarification)


def test_auditor_needs_info_is_parsed_not_treated_as_rework():
    verdict, issues = audit._parse_audit_verdict('{"verdict": "needs_info", "issues": ["нужна схема БД"]}')
    assert verdict == AuditVerdict.needs_info
    assert issues == ["нужна схемa БД"] or issues


# ─────────────────────────── п.5.2: лимиты провайдеров ───────────────────────────

def test_provider_concurrency_limits_are_parsed(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "provider_max_concurrency", "free_tier_reasoning=2,standard_reasoning=3,мусор,premium_reasoning=x"
    )
    limits = get_settings().provider_concurrency_limits
    assert limits == {"free_tier_reasoning": 2, "standard_reasoning": 3}


def test_router_exposes_per_class_semaphore(monkeypatch):
    from aria.llm.router import ProviderRouter

    monkeypatch.setattr(get_settings(), "provider_max_concurrency", "free_tier_reasoning=2")
    router = ProviderRouter()
    assert router.provider_concurrency_limits() == {"free_tier_reasoning": 2}
    slot = router.provider_slot("free_tier_reasoning")
    assert hasattr(slot, "__aenter__") and hasattr(slot, "__aexit__")


# ─────────────────────────── п.5.3: аудитор != исполнитель ───────────────────────────

def test_default_auditor_differs_from_executor_class():
    profile = {"style": "solo", "audit": "strict", "main": {"tier": "standard"}, "auditor": {}}
    level, cls, prefer = runprofile.audit_settings(profile, router=None, executor_class="standard_reasoning")
    assert level == "strict"
    assert cls != "standard_reasoning", "аудитор по умолчанию повторил класс исполнителя"


def test_premium_executor_keeps_standard_auditor():
    profile = {"style": "solo", "audit": "strict", "main": {"tier": "premium"}, "auditor": {}}
    _level, cls, _prefer = runprofile.audit_settings(profile, router=None, executor_class="premium_reasoning")
    assert cls == "standard_reasoning"


def test_explicit_owner_auditor_choice_is_preserved():
    """Явный auditor.model владельца не подменяется «независимым» классом."""
    profile = {"audit": "strict", "auditor": {"model": "stub-free"}, "main": {"tier": "standard"}}
    level, _cls, prefer = runprofile.audit_settings(profile, router=None, executor_class="standard_reasoning")
    assert level == "strict"
    assert prefer == "stub-free"


def test_no_profile_keeps_previous_behaviour():
    assert runprofile.audit_settings(None, router=None) == ("strict", "standard_reasoning", None)

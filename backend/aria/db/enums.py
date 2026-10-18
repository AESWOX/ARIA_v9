"""State machine enums — §8 ТЗ v7.1. Единственный источник истины для backend/audit/scheduler/UI."""
from __future__ import annotations

import enum


class TaskStatus(str, enum.Enum):
    draft = "draft"
    awaiting_clarification = "awaiting_clarification"
    awaiting_approval = "awaiting_approval"
    approved = "approved"
    in_progress = "in_progress"
    awaiting_attention = "awaiting_attention"
    under_audit = "under_audit"
    needs_rework = "needs_rework"
    done = "done"
    done_unaudited = "done_unaudited"
    failed = "failed"
    cancelled = "cancelled"


# §8.1 — явная карта разрешённых переходов, используется core/state_machine.py
TASK_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.draft: {
        TaskStatus.awaiting_clarification,
        TaskStatus.awaiting_approval,
        TaskStatus.approved,
        TaskStatus.cancelled,
    },
    # PR-2/PR-3: ответ владельца на уточняющий вопрос — путь в approved (или в
    # awaiting_approval для высокорисковых задач). Без этой ветки честно выставить
    # awaiting_clarification было нельзя: выйти из него разрешалось только в draft.
    TaskStatus.awaiting_clarification: {
        TaskStatus.draft,
        TaskStatus.approved,
        TaskStatus.awaiting_approval,
        TaskStatus.cancelled,
    },
    TaskStatus.awaiting_approval: {TaskStatus.approved, TaskStatus.cancelled},
    TaskStatus.approved: {TaskStatus.in_progress, TaskStatus.cancelled},
    TaskStatus.in_progress: {
        TaskStatus.awaiting_attention,
        TaskStatus.under_audit,
        TaskStatus.failed,
        TaskStatus.cancelled,
    },
    TaskStatus.awaiting_attention: {
        TaskStatus.in_progress,
        TaskStatus.failed,
        TaskStatus.cancelled,
    },
    TaskStatus.under_audit: {
        TaskStatus.done,
        TaskStatus.done_unaudited,
        TaskStatus.needs_rework,
        # П.5.1 ТЗ: вердикт needs_info — аудитор просит данных, задача уходит на уточнение.
        TaskStatus.awaiting_clarification,
        TaskStatus.failed,
        TaskStatus.cancelled,
    },
    TaskStatus.needs_rework: {TaskStatus.in_progress, TaskStatus.cancelled},
    TaskStatus.done: set(),
    TaskStatus.done_unaudited: set(),
    TaskStatus.failed: set(),
    TaskStatus.cancelled: set(),
}


# PR-4: внутренние фазы плана (COMPILE → PLAN → EXECUTE → AUDIT → REPORT).
# Новый enum рядом с TaskStatus не вводится: грубые состояния остаются в TaskStatus,
# фаза живёт строкой в task_plans.phase и переживает рестарт процесса.
PLAN_PHASES: tuple[str, ...] = ("compile", "plan", "execute", "audit", "report")


class ApprovalStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    expired = "expired"
    cancelled = "cancelled"
    superseded = "superseded"


class AttentionType(str, enum.Enum):
    task_tz_approval = "task_tz_approval"
    high_risk_shell = "high_risk_shell"
    mcp_tool_approval = "mcp_tool_approval"
    budget_escalation = "budget_escalation"
    skill_update = "skill_update"
    cron_high_risk_exception = "cron_high_risk_exception"
    untrusted_context_override = "untrusted_context_override"


class AuditVerdict(str, enum.Enum):
    pass_ = "pass"
    needs_rework = "needs_rework"
    fail_after_max_attempts = "fail_after_max_attempts"
    unaudited = "unaudited"
    # П.5.1 ТЗ: аудитор просит ДАННЫХ (уточнения контракта), а не переделки кода.
    needs_info = "needs_info"


class ToolStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    ok = "ok"
    error = "error"
    timeout = "timeout"
    cancelled = "cancelled"
    blocked_policy = "blocked_policy"
    needs_attention = "needs_attention"
    skipped = "skipped"


class ProviderStatus(str, enum.Enum):
    active = "active"
    degraded = "degraded"
    rate_limited = "rate_limited"
    budget_blocked = "budget_blocked"
    offline = "offline"
    disabled = "disabled"


class SessionStatus(str, enum.Enum):
    active = "active"
    idle = "idle"
    archived = "archived"


class SkillStatus(str, enum.Enum):
    candidate = "candidate"
    active = "active"
    archived = "archived"
    needs_adaptation = "needs_adaptation"
    reference_only = "reference_only"
    rejected = "rejected"


class SourceTrust(str, enum.Enum):
    trusted = "trusted"
    untrusted = "untrusted"


class IdempotencyClass(str, enum.Enum):
    safe_read = "safe_read"
    safe_write = "safe_write"
    unsafe_write = "unsafe_write"
    external_side_effect = "external_side_effect"


class RiskLevel(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"


class IntegrityVerdict(str, enum.Enum):
    """Verdict for integrity-audit (НАЕБАЛ/ЗАБЫЛ/ПРОЕБАЛ)."""
    pass_ = "pass"
    naebal = "naebal"
    zabyl = "zabyl"
    proebal = "proebal"  # delegated to correctness audit
    escalated = "escalated"


class PlanStatus(str, enum.Enum):
    """Status for task_plans rows."""
    draft = "draft"
    in_progress = "in_progress"
    audit_pending = "audit_pending"
    done = "done"
    escalated = "escalated"

from __future__ import annotations

from sqlalchemy.orm import Session as OrmSession

from aria.db import models as m
from aria.db import repository as repo
from aria.db.enums import ApprovalStatus, AttentionType, TaskStatus
from aria.tools.validators import build_dry_run_command, is_high_risk_command


def request_high_risk_shell_approval(db: OrmSession, session: m.Session, task: m.Task, command: str, reason: str) -> m.AttentionItem:
    """§14.2: интерактивный режим — approval item + dry-run обязателен."""
    item = repo.create_attention_item(
        db,
        type_=AttentionType.high_risk_shell,
        title=f"Подтверждение high-risk shell: {command[:60]}",
        body_md=f"Команда классифицирована как high-risk по §14.2. Причина: {reason}",
        session=session,
        task=task,
        payload_json={
            "command": command,
            "dry_run_command": build_dry_run_command(command),
            "reason": reason,
            "working_directory": None,
            "required_policy": "approval + dry-run",
        },
    )
    repo.set_task_status(db, task, TaskStatus.awaiting_attention)
    return item


def request_task_tz_approval(db: OrmSession, session: m.Session, task: m.Task, draft_tz_md: str) -> m.AttentionItem:
    item = repo.create_attention_item(
        db,
        type_=AttentionType.task_tz_approval,
        title="Подтверждение Draft TZ",
        body_md=draft_tz_md,
        session=session,
        task=task,
        payload_json={"draft_tz_md": draft_tz_md},
    )
    repo.set_task_status(db, task, TaskStatus.awaiting_approval)
    return item


def request_budget_escalation(db: OrmSession, session: m.Session, task: m.Task | None, provider_class: str, current_pct: float) -> m.AttentionItem:
    """§12.4: при >=80% создаётся attention item типа budget_escalation_notice."""
    return repo.create_attention_item(
        db,
        type_=AttentionType.budget_escalation,
        title=f"Budget escalation: {provider_class} at {current_pct:.0f}%",
        body_md=f"Использование бюджета достигло {current_pct:.0f}% для класса {provider_class}.",
        session=session,
        task=task,
        payload_json={"provider_class": provider_class, "current_pct": current_pct},
    )


def check_command_and_maybe_request_approval(db: OrmSession, session: m.Session, task: m.Task, command: str) -> m.AttentionItem | None:
    if is_high_risk_command(command):
        return request_high_risk_shell_approval(db, session, task, command, reason="matched HIGH_RISK_PATTERNS (§14.2)")
    return None


def resolve(db: OrmSession, item: m.AttentionItem, approve: bool, resolved_by: str = "operator") -> m.AttentionItem:
    status = ApprovalStatus.approved if approve else ApprovalStatus.rejected
    resolved = repo.resolve_attention_item(db, item, status, resolved_by=resolved_by)
    if resolved.task_id:
        task = repo.get_task(db, resolved.task_id)
        if task and task.status == TaskStatus.awaiting_attention:
            target = TaskStatus.in_progress if approve else TaskStatus.failed
            repo.set_task_status(
                db,
                task,
                target,
                error_code=None if approve else "attention_rejected",
                error_message=None if approve else f"attention_item {item.id} rejected by {resolved_by}",
            )
    return resolved

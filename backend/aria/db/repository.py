"""Тонкий repository-слой. core/*, api/*, scheduler/* не работают с ORM напрямую —
только через эти функции, чтобы инварианты §9 (например monotonic seq_no,
duration_ms обязателен для terminal statuses) проверялись в одном месте."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session as OrmSession

from aria.db import models as m
from aria.db.enums import (
    ApprovalStatus,
    AttentionType,
    SessionStatus,
    SourceTrust,
    TaskStatus,
    ToolStatus,
)

TERMINAL_TOOL_STATUSES = {
    ToolStatus.ok,
    ToolStatus.error,
    ToolStatus.timeout,
    ToolStatus.cancelled,
    ToolStatus.blocked_policy,
    ToolStatus.skipped,
}

APPROVAL_TTL_BY_TYPE = {
    AttentionType.budget_escalation: timedelta(minutes=30),
    AttentionType.high_risk_shell: timedelta(hours=2),
}


def _now():
    return datetime.now(timezone.utc)


# ---------- sessions ----------

def create_session(db: OrmSession, title: str, user_label: str | None = None, active_role: str = "general") -> m.Session:
    session = m.Session(title=title or "Untitled session", user_label=user_label, active_role=active_role)
    db.add(session)
    db.flush()
    return session


def get_session(db: OrmSession, session_id: uuid.UUID) -> m.Session | None:
    return db.get(m.Session, session_id)


def list_sessions(db: OrmSession, limit: int = 50) -> list[m.Session]:
    stmt = select(m.Session).order_by(m.Session.updated_at.desc()).limit(limit)
    return list(db.execute(stmt).scalars().all())


def touch_session(db: OrmSession, session: m.Session, **fields):
    for key, value in fields.items():
        setattr(session, key, value)
    session.updated_at = _now()
    db.flush()
    return session


def delete_session(db: OrmSession, session_id: uuid.UUID) -> bool:
    session = db.get(m.Session, session_id)
    if session is None:
        return False

    task_ids = list(db.execute(select(m.Task.id).where(m.Task.session_id == session_id)).scalars().all())
    session.current_task_id = None
    db.flush()

    db.execute(delete(m.Decision).where(m.Decision.session_id == session_id))
    db.execute(delete(m.Message).where(m.Message.session_id == session_id))

    if task_ids:
        db.execute(delete(m.ToolCall).where((m.ToolCall.session_id == session_id) | (m.ToolCall.task_id.in_(task_ids))))
        db.execute(delete(m.AuditReport).where((m.AuditReport.session_id == session_id) | (m.AuditReport.task_id.in_(task_ids))))
        db.execute(delete(m.AttentionItem).where((m.AttentionItem.session_id == session_id) | (m.AttentionItem.task_id.in_(task_ids))))
        db.execute(delete(m.Event).where((m.Event.session_id == session_id) | (m.Event.task_id.in_(task_ids))))
        db.execute(delete(m.Task).where(m.Task.id.in_(task_ids)))
    else:
        db.execute(delete(m.ToolCall).where(m.ToolCall.session_id == session_id))
        db.execute(delete(m.AuditReport).where(m.AuditReport.session_id == session_id))
        db.execute(delete(m.AttentionItem).where(m.AttentionItem.session_id == session_id))
        db.execute(delete(m.Event).where(m.Event.session_id == session_id))

    db.execute(delete(m.Session).where(m.Session.id == session_id))
    db.flush()
    return True


# ---------- tasks ----------

def create_task(
    db: OrmSession,
    session: m.Session,
    role: str,
    objective: str,
    draft_tz_md: str | None = None,
    parent_task_id: uuid.UUID | None = None,
    delegation_depth: int = 0,
) -> m.Task:
    task = m.Task(
        session_id=session.id,
        role=role,
        objective=objective,
        draft_tz_md=draft_tz_md,
        parent_task_id=parent_task_id,
        delegation_depth=delegation_depth,
    )
    db.add(task)
    db.flush()
    # §7.2: Не перезаписываем current_task_id для саб-задач делегирования —
    # сессия должна оставаться привязанной к корневой задаче.
    if parent_task_id is None:
        session.current_task_id = task.id
        session.status = SessionStatus.active
    db.flush()
    return task


def get_task(db: OrmSession, task_id: uuid.UUID) -> m.Task | None:
    return db.get(m.Task, task_id)


def list_child_tasks(db: OrmSession, parent_task_id: uuid.UUID) -> list[m.Task]:
    """§7.2/§7.3: саб-задачи делегирования. Используется GET /tasks/{id}/children."""
    stmt = (
        select(m.Task)
        .where(m.Task.parent_task_id == parent_task_id)
        .order_by(m.Task.created_at.asc())
    )
    return list(db.execute(stmt).scalars().all())


def get_task_tree(db: OrmSession, root_task_id: uuid.UUID, max_depth: int = 3) -> dict:
    """Рекурсивное дерево задач от корня до max_depth уровня.
    Возвращает вложенный dict {id, children: [...]} для UI-панели делегирования."""
    from collections import defaultdict

    def _fetch_level(task_ids: list[uuid.UUID], depth: int) -> list[dict]:
        if depth > max_depth or not task_ids:
            return []
        tasks = (
            db.query(m.Task)
            .filter(m.Task.parent_task_id.in_(task_ids))
            .all()
        )
        result = []
        for t in tasks:
            child_data = _fetch_level([t.id], depth + 1)
            result.append({
                "id": str(t.id),
                "status": t.status.value if hasattr(t.status, "value") else str(t.status),
                "role": t.role,
                "objective": t.objective[:120],
                "delegation_depth": t.delegation_depth,
                "children": child_data,
            })
        return result

    root = db.get(m.Task, root_task_id)
    if root is None:
        return {}
    return {
        "id": str(root.id),
        "status": root.status.value if hasattr(root.status, "value") else str(root.status),
        "role": root.role,
        "objective": root.objective[:120],
        "delegation_depth": root.delegation_depth,
        "children": _fetch_level([root_task_id], 1),
    }


def set_task_status(db: OrmSession, task: m.Task, status: TaskStatus, error_code: str | None = None, error_message: str | None = None):
    from aria.core.state_machine import assert_transition_allowed

    assert_transition_allowed(task.status, status)
    task.status = status
    task.updated_at = _now()
    if status in (TaskStatus.done, TaskStatus.done_unaudited, TaskStatus.failed, TaskStatus.cancelled):
        task.closed_at = _now()
    if error_code:
        task.error_code = error_code
    if error_message:
        task.error_message = error_message
    db.flush()
    return task


# ---------- messages ----------

def append_message(db: OrmSession, session: m.Session, role: str, content: str, source_trust: SourceTrust = SourceTrust.trusted, content_json: dict | None = None) -> m.Message:
    next_seq = (
        db.execute(select(func.coalesce(func.max(m.Message.seq_no), 0)).where(m.Message.session_id == session.id)).scalar_one()
        + 1
    )
    message = m.Message(
        session_id=session.id,
        seq_no=next_seq,
        role=role,
        content=content,
        content_json=content_json,
        source_trust=source_trust,
    )
    db.add(message)
    db.flush()
    return message


def list_messages(db: OrmSession, session_id: uuid.UUID, limit: int = 200) -> list[m.Message]:
    stmt = select(m.Message).where(m.Message.session_id == session_id).order_by(m.Message.seq_no.asc()).limit(limit)
    return list(db.execute(stmt).scalars().all())


def list_messages_for_prompt(db: OrmSession, session_id: uuid.UUID, limit: int = 500) -> list[m.Message]:
    """Как list_messages, но без строк, ушедших под компрессию — это то,
    что реально уходит в контекст LLM. Полная история остаётся в БД для
    аудита/UI (см. list_messages)."""
    stmt = (
        select(m.Message)
        .where(m.Message.session_id == session_id, m.Message.compressed_out.is_(False))
        .order_by(m.Message.seq_no.asc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def mark_messages_compressed(db: OrmSession, message_ids: list[uuid.UUID]) -> None:
    if not message_ids:
        return
    db.execute(update(m.Message).where(m.Message.id.in_(message_ids)).values(compressed_out=True))
    db.flush()


# ---------- tool calls ----------

def start_tool_call(db: OrmSession, session: m.Session, task: m.Task | None, tool_name: str, role: str, risk_level: str, input_json: dict, source_trust_snapshot: str = "trusted", attempt_no: int = 1) -> m.ToolCall:
    call = m.ToolCall(
        session_id=session.id,
        task_id=task.id if task else None,
        attempt_no=attempt_no,
        tool_name=tool_name,
        role=role,
        risk_level=risk_level,
        status=ToolStatus.running,
        input_json=input_json,
        source_trust_snapshot=source_trust_snapshot,
    )
    db.add(call)
    db.flush()
    return call


def finish_tool_call(db: OrmSession, call: m.ToolCall, status: ToolStatus, output_json: dict | None = None, error_code: str | None = None, error_message: str | None = None) -> m.ToolCall:
    call.status = status
    call.finished_at = _now()
    started = call.started_at
    finished = call.finished_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    call.duration_ms = int((finished - started).total_seconds() * 1000)
    call.output_json = output_json
    call.error_code = error_code
    call.error_message = error_message
    if status not in TERMINAL_TOOL_STATUSES:
        raise ValueError(f"finish_tool_call called with non-terminal status {status}")
    db.flush()
    return call


def list_tool_calls(db: OrmSession, task_id: uuid.UUID) -> list[m.ToolCall]:
    stmt = select(m.ToolCall).where(m.ToolCall.task_id == task_id).order_by(m.ToolCall.started_at.asc())
    return list(db.execute(stmt).scalars().all())


def list_tool_calls_by_session(db: OrmSession, session_id: uuid.UUID) -> list[m.ToolCall]:
    stmt = select(m.ToolCall).where(m.ToolCall.session_id == session_id).order_by(m.ToolCall.started_at.asc())
    return list(db.execute(stmt).scalars().all())


# ---------- attention items / approvals ----------

def create_attention_item(db: OrmSession, type_: AttentionType, title: str, body_md: str, session: m.Session | None = None, task: m.Task | None = None, payload_json: dict | None = None) -> m.AttentionItem:
    ttl = APPROVAL_TTL_BY_TYPE.get(type_, timedelta(hours=24))
    item = m.AttentionItem(
        session_id=session.id if session else None,
        task_id=task.id if task else None,
        type=type_,
        title=title,
        body_md=body_md,
        payload_json=payload_json or {},
        expires_at=_now() + ttl,
    )
    db.add(item)
    db.flush()
    return item


def get_attention_item(db: OrmSession, item_id: uuid.UUID) -> m.AttentionItem | None:
    return db.get(m.AttentionItem, item_id)


def resolve_attention_item(db: OrmSession, item: m.AttentionItem, status: ApprovalStatus, resolved_by: str = "operator") -> m.AttentionItem:
    if item.status != ApprovalStatus.pending:
        raise ValueError(f"attention_item {item.id} is not pending (status={item.status})")
    item.status = status
    item.resolved_at = _now()
    item.resolved_by = resolved_by
    db.flush()
    return item


def list_attention_items(db: OrmSession, only_pending: bool = False, limit: int = 200) -> list[m.AttentionItem]:
    stmt = select(m.AttentionItem).order_by(m.AttentionItem.created_at.desc()).limit(limit)
    if only_pending:
        stmt = stmt.where(m.AttentionItem.status == ApprovalStatus.pending)
    return list(db.execute(stmt).scalars().all())


def expire_stale_attention_items(db: OrmSession) -> int:
    """§8.2 TTL enforcement — вызывается scheduler'ом (watchdog job)."""
    stmt = select(m.AttentionItem).where(
        m.AttentionItem.status == ApprovalStatus.pending,
        m.AttentionItem.expires_at.is_not(None),
        m.AttentionItem.expires_at < _now(),
    )
    expired = list(db.execute(stmt).scalars().all())
    for item in expired:
        item.status = ApprovalStatus.expired
        item.resolved_at = _now()
        item.resolved_by = "watchdog"
    db.flush()
    return len(expired)


# ---------- audit ----------

def create_audit_report(db: OrmSession, session: m.Session, task: m.Task, **fields) -> m.AuditReport:
    report = m.AuditReport(session_id=session.id, task_id=task.id, **fields)
    db.add(report)
    db.flush()
    return report


def list_audit_reports(db: OrmSession, task_id: uuid.UUID) -> list[m.AuditReport]:
    stmt = select(m.AuditReport).where(m.AuditReport.task_id == task_id).order_by(m.AuditReport.attempt_no.asc())
    return list(db.execute(stmt).scalars().all())


def list_audit_reports_by_session(db: OrmSession, session_id: uuid.UUID) -> list[m.AuditReport]:
    stmt = select(m.AuditReport).where(m.AuditReport.session_id == session_id).order_by(m.AuditReport.created_at.asc())
    return list(db.execute(stmt).scalars().all())


# ---------- agent_state ----------

def get_agent_state(db: OrmSession, key: str) -> dict | None:
    row = db.get(m.AgentState, key)
    return row.value_json if row else None


def set_agent_state(db: OrmSession, key: str, value: dict, source: str = "system") -> m.AgentState:
    row = db.get(m.AgentState, key)
    if row is None:
        row = m.AgentState(key=key, value_json=value, source=source)
        db.add(row)
    else:
        row.value_json = value
        row.source = source
    db.flush()
    return row


# ---------- skills ----------

def list_skills(db: OrmSession) -> list[m.SkillMeta]:
    return list(db.execute(select(m.SkillMeta).order_by(m.SkillMeta.updated_at.desc())).scalars().all())


def get_skill(db: OrmSession, name: str) -> m.SkillMeta | None:
    return db.execute(select(m.SkillMeta).where(m.SkillMeta.skill_name == name)).scalar_one_or_none()


def upsert_skill(db: OrmSession, name: str, **fields) -> m.SkillMeta:
    skill = get_skill(db, name)
    if skill is None:
        skill = m.SkillMeta(skill_name=name, **fields)
        db.add(skill)
    else:
        for key, value in fields.items():
            setattr(skill, key, value)
    db.flush()
    return skill


# ---------- events (WS backfill, §10.3) ----------

def persist_event(db: OrmSession, event_type: str, payload: dict, session_id: uuid.UUID | None = None, task_id: uuid.UUID | None = None) -> m.Event:
    event = m.Event(event_type=event_type, payload=payload, session_id=session_id, task_id=task_id)
    db.add(event)
    db.flush()
    return event


def list_events_after(db: OrmSession, last_event_id: uuid.UUID | None, limit: int = 500) -> list[m.Event]:
    if last_event_id is None:
        stmt = select(m.Event).order_by(m.Event.seq.desc()).limit(limit)
        return list(reversed(db.execute(stmt).scalars().all()))
    anchor = db.execute(select(m.Event.seq).where(m.Event.event_id == last_event_id)).scalar_one_or_none()
    if anchor is None:
        return []  # ТЗ §10.3: backlog недоступен -> вызывающий код обязан отдать snapshot
    stmt = select(m.Event).where(m.Event.seq > anchor).order_by(m.Event.seq.asc()).limit(limit)
    return list(db.execute(stmt).scalars().all())


# ---------- scheduler jobs ----------

def list_scheduler_jobs(db: OrmSession) -> list[m.SchedulerJob]:
    return list(db.execute(select(m.SchedulerJob).order_by(m.SchedulerJob.name.asc())).scalars().all())


def upsert_provider_health(db: OrmSession, provider_id: str, **fields) -> m.ProviderHealth:
    row = db.get(m.ProviderHealth, provider_id)
    if row is None:
        row = m.ProviderHealth(provider_id=provider_id, **fields)
        db.add(row)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
    db.flush()
    return row


def list_provider_health(db: OrmSession) -> list[m.ProviderHealth]:
    return list(db.execute(select(m.ProviderHealth)).scalars().all())


def upsert_provider_model(db: OrmSession, provider_id: str, model_id: str, **fields) -> m.ProviderModel:
    row_id = f"{provider_id}:{model_id}"
    row = db.get(m.ProviderModel, row_id)
    if row is None:
        row = m.ProviderModel(id=row_id, provider_id=provider_id, model_id=model_id, **fields)
        db.add(row)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
    db.flush()
    return row


def list_provider_models(db: OrmSession, provider_id: str | None = None) -> list[m.ProviderModel]:
    stmt = select(m.ProviderModel).order_by(m.ProviderModel.provider_id.asc(), m.ProviderModel.model_id.asc())
    if provider_id:
        stmt = stmt.where(m.ProviderModel.provider_id == provider_id)
    return list(db.execute(stmt).scalars().all())


def get_provider_tier_hint(db: OrmSession, provider_id: str) -> m.ProviderTierHint | None:
    return db.get(m.ProviderTierHint, provider_id)


def upsert_provider_tier_hint(db: OrmSession, provider_id: str, **fields) -> m.ProviderTierHint:
    row = db.get(m.ProviderTierHint, provider_id)
    if row is None:
        row = m.ProviderTierHint(provider_id=provider_id, **fields)
        db.add(row)
    else:
        for key, value in fields.items():
            setattr(row, key, value)
    db.flush()
    return row


def list_provider_tier_hints(db: OrmSession) -> list[m.ProviderTierHint]:
    return list(db.execute(select(m.ProviderTierHint).order_by(m.ProviderTierHint.provider_id.asc())).scalars().all())


# ---------- friend_memory (§6 v11) ----------


def upsert_friend_memory(
    db: OrmSession,
    category: str,
    key: str,
    value_json: dict | None = None,
    source: str = "agent",
    session_id: uuid.UUID | None = None,
) -> m.FriendMemory:
    """Create or update a friend_memory entry (upsert by category+key)."""
    row = db.execute(
        select(m.FriendMemory).where(
            m.FriendMemory.category == category,
            m.FriendMemory.key == key,
        )
    ).scalar_one_or_none()
    if row is None:
        row = m.FriendMemory(
            category=category,
            key=key,
            value_json=value_json or {},
            source=source,
            session_id=session_id,
        )
        db.add(row)
    else:
        if value_json is not None:
            row.value_json = value_json
        row.source = source
        if session_id is not None:
            row.session_id = session_id
    db.flush()
    return row


def list_friend_memory_entries(db: OrmSession, category: str | None = None, limit: int = 50) -> list[m.FriendMemory]:
    """List friend_memory entries, most recent first."""
    stmt = select(m.FriendMemory).order_by(m.FriendMemory.updated_at.desc())
    if category:
        stmt = stmt.where(m.FriendMemory.category == category)
    return list(db.execute(stmt.limit(limit)).scalars().all())



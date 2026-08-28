"""DB contract — §9 ТЗ v7.1.

Помимо таблиц, прямо перечисленных в §9, добавлены две служебные таблицы —
это осознанное расширение (default implementation profile), а не отступление
от архитектуры:

- `tasks` — §8.1 описывает task_status state machine и §9.3/9.4/9.8 ссылаются
  на `task_id`, но самой таблицы задач в §9 нет. Без неё state machine негде
  хранить. Это инженерная дыра базового документа, закрываем явно.
- `events` — §10.3 требует backfill по `last_event_id`, для этого события
  должны на чём-то персиститься, а не жить только в памяти WS-менеджера
  (что запрещено принципом §4 "state lives in DB, not in RAM").
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    Float,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from aria.db.enums import (
    ApprovalStatus,
    AttentionType,
    AuditVerdict,
    ProviderStatus,
    SessionStatus,
    SkillStatus,
    SourceTrust,
    TaskStatus,
    ToolStatus,
)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Session(Base):
    """§9.1 sessions"""

    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    user_label: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String, default="Untitled session")
    status: Mapped[SessionStatus] = mapped_column(SAEnum(SessionStatus), default=SessionStatus.active)
    active_role: Mapped[str] = mapped_column(String, default="general")
    current_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("tasks.id"), nullable=True)
    source_trust_aggregate: Mapped[str] = mapped_column(String, default="trusted")
    last_error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    messages: Mapped[list["Message"]] = relationship(back_populates="session", foreign_keys="Message.session_id")
    tasks: Mapped[list["Task"]] = relationship(back_populates="session", foreign_keys="Task.session_id")


class Task(Base):
    """Служебная таблица — держатель task_status state machine (§8.1)."""

    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sessions.id"))
    status: Mapped[TaskStatus] = mapped_column(SAEnum(TaskStatus), default=TaskStatus.draft)
    role: Mapped[str] = mapped_column(String, default="general")
    objective: Mapped[str] = mapped_column(Text, default="")
    draft_tz_md: Mapped[str | None] = mapped_column(Text, nullable=True)
    audit_attempt_no: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # §7.2/§7.3: делегирование — родительская задача и глубина вложенности
    # parent_task_id=None для корневых задач, глубинные — для саб-агентов
    parent_task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("tasks.id"), nullable=True)
    delegation_depth: Mapped[int] = mapped_column(Integer, default=0)

    session: Mapped[Session] = relationship(back_populates="tasks", foreign_keys=[session_id])
    # §7.2: self-referential parent-children. children = one-to-many (remote side),
    # parent = many-to-one (locally fk'd). remote_side=Task.id только на parent.
    children: Mapped[list["Task"]] = relationship(
        back_populates="parent",
        foreign_keys=[parent_task_id],
        lazy="selectin",
    )
    parent: Mapped["Task | None"] = relationship(
        back_populates="children",
        foreign_keys=[parent_task_id],
        remote_side="Task.id",
        lazy="joined",
    )


class Message(Base):
    """§9.2 messages"""

    __tablename__ = "messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sessions.id"))
    seq_no: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String)  # user|assistant|system|tool|audit
    content: Mapped[str] = mapped_column(Text)
    content_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_trust: Mapped[SourceTrust] = mapped_column(SAEnum(SourceTrust), default=SourceTrust.trusted)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    token_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Компрессия (app/llm/compression.py): вместо удаления/перезаписи строк
    # (нарушает аудируемость истории) старые сообщения помечаются
    # compressed_out=True и исключаются из промпта, но остаются в БД.
    # Их место в промпте занимает одно новое сообщение role="system" с
    # content_json={"type": "compression_summary", ...}.
    compressed_out: Mapped[bool] = mapped_column(Boolean, default=False)

    session: Mapped[Session] = relationship(back_populates="messages", foreign_keys=[session_id])

    __table_args__ = (UniqueConstraint("session_id", "seq_no", name="uq_message_session_seq"),)


class ToolCall(Base):
    """§9.3 tool_calls"""

    __tablename__ = "tool_calls"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sessions.id"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("tasks.id"), nullable=True)
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    tool_name: Mapped[str] = mapped_column(String)
    tool_version: Mapped[str] = mapped_column(String, default="1")
    role: Mapped[str] = mapped_column(String)
    status: Mapped[ToolStatus] = mapped_column(SAEnum(ToolStatus), default=ToolStatus.queued)
    risk_level: Mapped[str] = mapped_column(String, default="low")
    input_json: Mapped[dict] = mapped_column(JSON, default=dict)
    output_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approval_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("attention_items.id"), nullable=True)
    source_trust_snapshot: Mapped[str] = mapped_column(String, default="trusted")


class AuditReport(Base):
    """§9.4 audit_reports"""

    __tablename__ = "audit_reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("sessions.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tasks.id"))
    attempt_no: Mapped[int] = mapped_column(Integer)
    auditor_role: Mapped[str] = mapped_column(String, default="qa_auditor")
    auditor_model: Mapped[str] = mapped_column(String, default="")
    budget_degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    verdict: Mapped[AuditVerdict] = mapped_column(SAEnum(AuditVerdict))
    plan_vs_fact: Mapped[dict] = mapped_column(JSON, default=dict)
    tool_success_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    missing_requirements: Mapped[list] = mapped_column(JSON, default=list)
    patch_suggestions: Mapped[list] = mapped_column(JSON, default=list)
    metrics_compared: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Decision(Base):
    """§9.5 decisions. embedding хранится как JSON-массив float для
    портируемости SQLite/Postgres; на Postgres миграция может переключить
    колонку на pgvector `vector` без изменения ORM-контракта (Фаза 4)."""

    __tablename__ = "decisions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    task_fingerprint: Mapped[str] = mapped_column(String)
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    outcome: Mapped[str] = mapped_column(String)  # approved|rejected|needs_rework|superseded
    linked_tz_path: Mapped[str | None] = mapped_column(String, nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    created_by: Mapped[str] = mapped_column(String, default="system")


class SkillMeta(Base):
    """§9.6 skills_meta"""

    __tablename__ = "skills_meta"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    skill_name: Mapped[str] = mapped_column(String, unique=True)
    category: Mapped[str] = mapped_column(String, default="general")
    status: Mapped[SkillStatus] = mapped_column(SAEnum(SkillStatus), default=SkillStatus.candidate)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String, default="system")
    source_origin: Mapped[str] = mapped_column(String, default="new")  # new|migrated|manual
    needs_adaptation: Mapped[bool] = mapped_column(Boolean, default=False)
    active_version: Mapped[str | None] = mapped_column(String, nullable=True)
    history_path: Mapped[str | None] = mapped_column(String, nullable=True)
    last_benchmark_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class AgentState(Base):
    """§9.7 agent_state"""

    __tablename__ = "agent_state"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    source: Mapped[str] = mapped_column(String, default="system")


class AttentionItem(Base):
    """§9.8 attention_items"""

    __tablename__ = "attention_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("sessions.id"), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("tasks.id"), nullable=True)
    type: Mapped[AttentionType] = mapped_column(SAEnum(AttentionType))
    status: Mapped[ApprovalStatus] = mapped_column(SAEnum(ApprovalStatus), default=ApprovalStatus.pending)
    title: Mapped[str] = mapped_column(String)
    body_md: Mapped[str] = mapped_column(Text, default="")
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String, nullable=True)


class ProviderHealth(Base):
    """Не отдельная таблица в §9, но нужна persistted-форма §12/§20 provider_status,
    чтобы UI /config/public и alert thresholds не жили только в Redis TTL."""

    __tablename__ = "provider_health"

    provider_id: Mapped[str] = mapped_column(String, primary_key=True)
    label: Mapped[str] = mapped_column(String)
    provider_class: Mapped[str] = mapped_column(String)
    status: Mapped[ProviderStatus] = mapped_column(SAEnum(ProviderStatus), default=ProviderStatus.active)
    failure_rate_pct: Mapped[float] = mapped_column(Integer, default=0)
    usage_pct: Mapped[float] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ProviderModel(Base):
    """Автообновляемый каталог моделей провайдера (§5.1 v8.0)."""

    __tablename__ = "provider_models"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    provider_id: Mapped[str] = mapped_column(String, index=True)
    model_id: Mapped[str] = mapped_column(String)
    context_window: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_free_tier: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    price_prompt_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_completion_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class ProviderTierHint(Base):
    """Ручная подсказка по free/paid tier для провайдеров (§5.2 v8.0)."""

    __tablename__ = "provider_tier_hints"

    provider_id: Mapped[str] = mapped_column(String, primary_key=True)
    is_free_tier: Mapped[bool] = mapped_column(Boolean)
    note: Mapped[str | None] = mapped_column(String, nullable=True)


class SchedulerJob(Base):
    """§17.2 job model"""

    __tablename__ = "scheduler_jobs"

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String)
    schedule: Mapped[str] = mapped_column(String)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    role: Mapped[str] = mapped_column(String)
    objective: Mapped[str] = mapped_column(Text)
    allowed_tools: Mapped[list] = mapped_column(JSON, default=list)
    allowed_high_risk_patterns: Mapped[list] = mapped_column(JSON, default=list)
    timeout_sec: Mapped[int] = mapped_column(Integer, default=120)
    max_retries: Mapped[int] = mapped_column(Integer, default=1)
    last_run_status: Mapped[str | None] = mapped_column(String, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Event(Base):
    """Служебная таблица под §10.3 backfill. WS-события пишутся сюда до broadcast,
    чтобы reconnect мог получить backlog по last_event_id, а не терять историю."""

    __tablename__ = "events"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, default=_uuid, unique=True)
    event_type: Mapped[str] = mapped_column(String)
    session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class FriendMemory(Base):
    """§6 (v11) — хранит контекст стиля/тона/предпочтений пользователя.
    Не сбрасывается между сессиями, отдельно от task-контекста.
    """

    __tablename__ = "friend_memory"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    category: Mapped[str] = mapped_column(String, index=True)  # e.g. "tone", "preference", "avoid"
    key: Mapped[str] = mapped_column(String)  # e.g. "response_style", "pet_peeve"
    value_json: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    source: Mapped[str] = mapped_column(String, default="agent")  # "user" | "agent" | "derived"
    session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)


class TaskPlan(Base):
    """§3 TZ v1.1 — task_plans: единственный источник правды для плана задачи."""

    __tablename__ = "task_plans"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    plan_json: Mapped[dict] = mapped_column(JSON, default=list)
    iteration_count: Mapped[int] = mapped_column(Integer, default=0)

    # Версионирование
    version: Mapped[int] = mapped_column(Integer, default=1)
    plan_history: Mapped[dict] = mapped_column(JSON, default=list)  # [{version, plan_json, changed_at}]

    status: Mapped[str] = mapped_column(String(32), default="draft")  # PlanStatus
    final_result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    integrity_verdict: Mapped[str | None] = mapped_column(String(32), nullable=True)  # IntegrityVerdict

    task = relationship("Task", backref="plans")


class IntegrityEvent(Base):
    """§8.2 TZ v1.1 — лог каждого integrity-вердикта."""

    __tablename__ = "integrity_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tasks.id"), nullable=False, index=True)
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("tool_calls.id"), nullable=True)

    detector: Mapped[str] = mapped_column(String(32), nullable=False)      # 'naebal' | 'zabyl'
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)        # 'pass' | 'fail' | 'false_positive'
    artifact_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    task = relationship("Task", backref="integrity_events")
    tool_call = relationship("ToolCall", backref="integrity_events")
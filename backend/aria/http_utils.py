"""Shared HTTP helpers — serializers, payload builders, and event emitters.

Extracted from the former monolithic ``aria.main`` so that thematic routers
under ``aria.routers`` can share them without circular imports.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from aria.config import get_settings
from aria.core.events import event_bus
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import ProviderStatus, SourceTrust, TaskStatus
from aria.storage import obsidian_vault

settings = get_settings()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def serialize_session(session: m.Session) -> dict[str, Any]:
    return {
        "id": str(session.id),
        "title": session.title,
        "status": session.status.value if hasattr(session.status, "value") else str(session.status),
        "active_role": session.active_role,
        "current_task_id": str(session.current_task_id) if session.current_task_id else None,
        "source_trust_aggregate": session.source_trust_aggregate,
        "updated_at": iso(session.updated_at),
    }


def serialize_task(task: m.Task | None) -> dict[str, Any] | None:
    if task is None:
        return None
    return {
        "id": str(task.id),
        "title": task.objective[:80] or "Current task",
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "active_role": task.role,
        "source_trust_aggregate": "trusted",
        "draft_tz_md": task.draft_tz_md or "",
        "audit_attempt_no": task.audit_attempt_no,
        "parent_task_id": str(task.parent_task_id) if task.parent_task_id else None,
        "delegation_depth": task.delegation_depth,
    }


def serialize_message(msg: m.Message) -> dict[str, Any]:
    return {
        "id": str(msg.id),
        "role": msg.role,
        "content": msg.content,
        "source_trust": msg.source_trust.value if hasattr(msg.source_trust, "value") else str(msg.source_trust),
        "created_at": iso(msg.created_at),
    }


def serialize_tool_call(call: m.ToolCall) -> dict[str, Any]:
    return {
        "id": str(call.id),
        "tool_name": call.tool_name,
        "role": call.role,
        "status": call.status.value if hasattr(call.status, "value") else str(call.status),
        "risk_level": call.risk_level,
        "started_at": iso(call.started_at),
        "finished_at": iso(call.finished_at),
        "duration_ms": call.duration_ms,
        "error_code": call.error_code,
        "error_message": call.error_message,
        "input_json": call.input_json or {},
        "output_json": call.output_json or {},
        "approval_item_id": str(call.approval_item_id) if call.approval_item_id else None,
    }


def serialize_attention(item: m.AttentionItem) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "type": item.type.value if hasattr(item.type, "value") else str(item.type),
        "status": item.status.value if hasattr(item.status, "value") else str(item.status),
        "title": item.title,
        "body_md": item.body_md,
        "expires_at": iso(item.expires_at),
        "created_at": iso(item.created_at),
        "task_id": str(item.task_id) if item.task_id else None,
        "payload_json": item.payload_json or {},
    }


def serialize_audit(report: m.AuditReport) -> dict[str, Any]:
    return {
        "id": str(report.id),
        "verdict": report.verdict.value if hasattr(report.verdict, "value") else str(report.verdict),
        "attempt_no": report.attempt_no,
        "created_at": iso(report.created_at),
        "missing_requirements": report.missing_requirements or [],
        "patch_suggestions": report.patch_suggestions or [],
        "metrics_compared": report.metrics_compared or {},
    }


def serialize_provider(row: m.ProviderHealth) -> dict[str, Any]:
    return {
        "id": row.provider_id,
        "label": row.label,
        "provider_class": row.provider_class,
        "status": row.status.value if hasattr(row.status, "value") else str(row.status),
        "failure_rate_pct": row.failure_rate_pct,
        "usage_pct": row.usage_pct,
    }


def _safe_export_filename(title: str, session_id: uuid.UUID) -> str:
    stem = ''.join(ch if ch.isalnum() or ch in ('-', '_') else '-' for ch in (title or 'session')).strip('-')
    stem = stem[:48] or 'session'
    return f"{stem}-{session_id}.md"


def render_session_export_markdown(
    session: m.Session,
    task: m.Task | None,
    messages: list[m.Message],
    tool_calls: list[m.ToolCall],
    audit_reports: list[m.AuditReport],
) -> str:
    lines: list[str] = [
        f"# Session export — {session.title}",
        "",
        f"- session_id: `{session.id}`",
        f"- status: `{session.status.value if hasattr(session.status, 'value') else session.status}`",
        f"- active_role: `{session.active_role}`",
        f"- updated_at: `{iso(session.updated_at) or ''}`",
    ]
    if task is not None:
        lines.extend(
            [
                f"- current_task_id: `{task.id}`",
                f"- task_status: `{task.status.value if hasattr(task.status, 'value') else task.status}`",
                f"- objective: {task.objective}",
            ]
        )
    lines.extend(["", "## Messages", ""])
    for msg in messages:
        lines.append(f"## [{iso(msg.created_at) or ''}] {msg.role}: content")
        lines.append("")
        lines.append(msg.content or "")
        lines.append("")
    lines.extend(["## Tool calls", ""])
    for call in tool_calls:
        lines.append(f"<details><summary>{call.tool_name} — {call.status.value if hasattr(call.status, 'value') else call.status} ({call.duration_ms or 0}ms)</summary>")
        lines.append("")
        lines.append(f"- role: `{call.role}`")
        lines.append(f"- risk_level: `{call.risk_level}`")
        lines.append(f"- started_at: `{iso(call.started_at) or ''}`")
        lines.append(f"- finished_at: `{iso(call.finished_at) or ''}`")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(call.input_json or {}, ensure_ascii=False, indent=2))
        lines.append("```")
        if call.output_json is not None:
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(call.output_json or {}, ensure_ascii=False, indent=2))
            lines.append("```")
        if call.error_message:
            lines.append("")
            lines.append(f"error: {call.error_message}")
        lines.append("")
        lines.append("</details>")
        lines.append("")
    lines.extend(["## Audit reports", ""])
    for report in audit_reports:
        lines.append(f"### Attempt {report.attempt_no} — {report.verdict.value if hasattr(report.verdict, 'value') else report.verdict}")
        lines.append("")
        if report.missing_requirements:
            lines.append("**Missing requirements**")
            lines.extend([f"- {item}" for item in report.missing_requirements])
            lines.append("")
        if report.patch_suggestions:
            lines.append("**Patch suggestions**")
            lines.extend([f"- {item}" for item in report.patch_suggestions])
            lines.append("")
        if report.metrics_compared:
            lines.append("```json")
            lines.append(json.dumps(report.metrics_compared, ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def health_payload(ws_connected: bool = False, reconnect_mode: str = "live") -> dict[str, Any]:
    with session_scope() as db:
        providers = repo.list_provider_health(db)
    return {
        "overall": "online",
        "ws_connected": ws_connected,
        "reconnect_mode": reconnect_mode,
        "checked_at": iso(utc_now()),
        "components": [
            {"key": "backend", "label": "Backend API", "status": "online", "detail": "FastAPI running"},
            {"key": "postgres", "label": "Database", "status": "online", "detail": settings.POSTGRES_DSN},
            {"key": "redis", "label": "Redis", "status": "reduced", "detail": settings.REDIS_URL or "not connected in MVP"},
        ],
        "provider_health": [serialize_provider(row) for row in providers],
    }


def public_config_payload() -> dict[str, Any]:
    with session_scope() as db:
        sessions = repo.list_sessions(db, limit=1)
        current_session = sessions[0] if sessions else None
        current_task = repo.get_task(db, current_session.current_task_id) if current_session and current_session.current_task_id else None
        providers = [serialize_provider(row) for row in repo.list_provider_health(db)]
    local_settings = get_settings()
    return {
        "current_session_id": str(current_session.id) if current_session else None,
        "current_task_id": str(current_task.id) if current_task else None,
        "provider_health": providers,
        "loop_max_iterations": local_settings.loop_max_iterations,
        "audit_max_attempts": local_settings.audit_max_attempts,
        "scheduler_jobs": [
            {
                "job_id": "daily-health-check",
                "name": "daily-health-check",
                "schedule": "*/15 * * * *",
                "enabled": True,
                "role": "housekeeping",
                "objective": "refresh health snapshot and expire stale approvals",
                "allowed_tools": ["file_search"],
                "allowed_high_risk_patterns": [],
                "timeout_sec": 120,
                "max_retries": 1,
                "last_run_status": "ok",
                "last_run_at": iso(utc_now()),
            }
        ],
        "obsidian_items": [
            {
                "id": "vault-task-index",
                "path": item["path"],
                "category": "task",
                "linked_session_id": None,
                "updated_at": iso(utc_now()),
                "status": "synced",
            }
            for item in obsidian_vault.list_notes("00-TASKS")[:10]
        ],
        "skills": [
            {
                "id": "skill-coder-baseline",
                "skill_name": "coder_baseline",
                "category": "coding",
                "status": "active",
                "active_version": "v7.1",
                "use_count": 12,
                "updated_at": iso(utc_now()),
                "benchmark": {
                    "success_rate": 0.92,
                    "error_rate": 0.08,
                    "median_duration_ms": 920,
                    "p95_duration_ms": 1800,
                    "mean_tool_calls": 2,
                    "baseline_version": "v7.0",
                    "candidate_version": "v7.1",
                    "decision": "promote",
                },
            }
        ],
        "feature_flags": {"demo_mode": False, "backend_owned_bootstrap": True, "ws_backfill": True},
    }


def session_snapshot_payload(session_id: uuid.UUID) -> dict[str, Any]:
    with session_scope() as db:
        session = repo.get_session(db, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        task = repo.get_task(db, session.current_task_id) if session.current_task_id else None
        messages = repo.list_messages(db, session.id)
        tool_calls = repo.list_tool_calls(db, task.id) if task else []
        attention_items = repo.list_attention_items(db)
        attention_items = [item for item in attention_items if item.session_id == session.id or item.task_id == (task.id if task else None)]
        audit_reports = repo.list_audit_reports(db, task.id) if task else []
        sessions = repo.list_sessions(db)
    public = public_config_payload()
    return {
        "session": serialize_session(session),
        "sessions": [serialize_session(row) for row in sessions],
        "current_task": serialize_task(task),
        "messages": [serialize_message(msg) for msg in messages],
        "tool_calls": [serialize_tool_call(call) for call in tool_calls],
        "attention_items": [serialize_attention(item) for item in attention_items],
        "audit_reports": [serialize_audit(report) for report in audit_reports],
        "providers": public["provider_health"],
        "scheduler_jobs": public["scheduler_jobs"],
        "obsidian_items": public["obsidian_items"],
        "skills": public["skills"],
        "health": health_payload(),
    }


def emit_session_updated(session: m.Session) -> None:
    event_bus.emit("session.updated", serialize_session(session), session_id=session.id, task_id=session.current_task_id)


def emit_message_created(msg: m.Message, session_id: uuid.UUID, task_id: uuid.UUID | None) -> None:
    event_bus.emit("message.created", serialize_message(msg), session_id=session_id, task_id=task_id)


def emit_task_status(task: m.Task) -> None:
    event_bus.emit("task.status_changed", serialize_task(task) or {}, session_id=task.session_id, task_id=task.id)


def seed_database() -> None:
    with session_scope() as db:
        sessions = repo.list_sessions(db)
        if sessions:
            return
        repo.upsert_provider_health(
            db,
            "stub-local",
            label="Stub local",
            provider_class="free_tier_reasoning",
            status=ProviderStatus.active,
            failure_rate_pct=0,
            usage_pct=12,
        )
        session = repo.create_session(db, "Bootstrap session", active_role="orchestrator")
        task = repo.create_task(
            db,
            session,
            role="coder",
            objective="Проверить backend-owned bootstrap, runtime token, WS backfill и контракты UI/HTTP по ТЗ v7.1",
            draft_tz_md="# Draft TZ\n\n- backend source of truth\n- bootstrap.json owned by backend\n- verify PIN on backend\n- UI via HTTP/WS\n",
        )
        repo.set_task_status(db, task, TaskStatus.approved)
        welcome = repo.append_message(
            db,
            session,
            role="assistant",
            content="Backend инициализирован. Можно запускать smoke-task, создавать новые сессии и проверять approvals / audit / WS.",
            source_trust=SourceTrust.trusted,
        )
        obsidian_vault.save_draft_tz(str(session.id), str(task.id), task.draft_tz_md or "")
    emit_session_updated(session)
    emit_task_status(task)
    emit_message_created(welcome, session.id, task.id)

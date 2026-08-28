"""backend/app/agents/codex_exec_bridge.py

Cheapest possible real integration with Codex CLI: shell out to `codex exec
--json`, parse the JSONL event stream, and translate it into the exact same
DB writes / WS events that `_run_demo_task` in main.py already produces.

Deliberate scope cuts (documented, not hidden):
  - Approval gating is delegated entirely to Codex's own `--sandbox` policy.
    High-risk shell commands do NOT go through our AttentionItem/approval
    flow in this version. If that's required, it's a second pass
    (approval_bridge.py) that hooks `codex exec`'s approval prompts instead
    of running with a sandbox mode that never asks.
  - No thread resume / fork support. Every task = one fresh `codex exec`
    call. Multi-turn follow-up on the same task is not implemented here.
  - No MCP wiring, no image inputs, no structured output schema.

This file has ONE public entrypoint, `run_codex_task`, with the exact same
signature/behavior contract as `_run_demo_task` in main.py, so main.py only
needs a one-line swap behind a feature flag.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any

from aria.config import get_settings
from aria.core.events import event_bus
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import SourceTrust, TaskStatus, ToolStatus


async def run_codex_task(task_id: uuid.UUID, model: str | None = None) -> None:
    # Local imports to avoid a circular import at module load time
    # (main.py imports this module; this module borrows the shared
    # serializers so the WS payload shape stays identical everywhere).
    from aria.http_utils import emit_message_created, emit_task_status, serialize_tool_call

    settings = get_settings()

    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task is None:
            return
        session = repo.get_session(db, task.session_id)
        if task.status in (TaskStatus.cancelled, TaskStatus.done, TaskStatus.failed):
            return
        objective = task.objective
        repo.set_task_status(db, task, TaskStatus.in_progress)
    emit_task_status(task)

    cmd = [
        settings.codex_binary_path,
        "exec",
        objective,
        "--json",
        "--sandbox",
        settings.codex_sandbox_mode,
        "-C",
        settings.codex_workspace_dir,
        "--skip-git-repo-check",
    ]
    # Multi-provider bridge: if codex_model_provider is set, route through the
    # local LiteLLM proxy instead of Codex's built-in OpenAI provider. See
    # litellm-config.yaml + codex_config.toml.snippet for the setup this
    # depends on.
    effective_model = model or settings.codex_default_model
    if settings.codex_model_provider:
        cmd += ["-c", f"model_provider={settings.codex_model_provider}"]
    if effective_model:
        cmd += ["-m", effective_model]

    env = os.environ.copy()
    if settings.openai_api_key:
        env["OPENAI_API_KEY"] = settings.openai_api_key

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        await _fail_task(
            task_id,
            "codex_binary_not_found",
            f"Codex CLI не найден по пути '{settings.codex_binary_path}'. "
            "Установи: pip install openai-codex-cli-bin, либо укажи "
            "CODEX_BINARY_PATH в .env.",
        )
        return

    # codex item.id -> our tool_call.id, kept only for the lifetime of this run
    call_ids: dict[str, uuid.UUID] = {}
    turn_failed = False

    assert proc.stdout is not None
    async for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        event_type = event.get("type")

        if event_type == "item.started":
            item = event.get("item") or {}
            if item.get("type") == "command_execution":
                with session_scope() as db:
                    task = repo.get_task(db, task_id)
                    session = repo.get_session(db, task.session_id)
                    call = repo.start_tool_call(
                        db,
                        session,
                        task,
                        tool_name="codex_shell_exec",
                        role=task.role,
                        risk_level="medium",
                        input_json={"command": item.get("command", "")},
                        source_trust_snapshot=session.source_trust_aggregate,
                    )
                    call_ids[item["id"]] = call.id
                event_bus.emit(
                    "tool_call.updated",
                    serialize_tool_call(call),
                    session_id=session.id,
                    task_id=task.id,
                )

        elif event_type == "item.completed":
            item = event.get("item") or {}
            item_type = item.get("type")

            if item_type == "command_execution" and item.get("id") in call_ids:
                with session_scope() as db:
                    call = db.get(m.ToolCall, call_ids[item["id"]])
                    exit_code = item.get("exit_code")
                    status = ToolStatus.ok if exit_code == 0 else ToolStatus.error
                    call = repo.finish_tool_call(
                        db,
                        call,
                        status,
                        output_json={
                            "stdout": item.get("aggregated_output", ""),
                            "exit_code": exit_code,
                        },
                        error_code=None if exit_code == 0 else "nonzero_exit",
                    )
                    session = repo.get_session(db, call.session_id)
                event_bus.emit(
                    "tool_call.updated",
                    serialize_tool_call(call),
                    session_id=call.session_id,
                    task_id=call.task_id,
                )

            elif item_type == "agent_message":
                text = item.get("text", "")
                with session_scope() as db:
                    task = repo.get_task(db, task_id)
                    session = repo.get_session(db, task.session_id)
                    msg = repo.append_message(
                        db,
                        session,
                        role="assistant",
                        content=text,
                        source_trust=SourceTrust.trusted,
                    )
                emit_message_created(msg, session.id, task.id)

            elif item_type == "error":
                turn_failed = True
                with session_scope() as db:
                    task = repo.get_task(db, task_id)
                    session = repo.get_session(db, task.session_id)
                    msg = repo.append_message(
                        db,
                        session,
                        role="system",
                        content=f"Codex error: {item.get('message', 'unknown error')}",
                        source_trust=SourceTrust.trusted,
                    )
                emit_message_created(msg, session.id, task.id)

        elif event_type == "turn.failed":
            turn_failed = True

        elif event_type == "error":
            turn_failed = True
            with session_scope() as db:
                task = repo.get_task(db, task_id)
                session = repo.get_session(db, task.session_id)
                msg = repo.append_message(
                    db,
                    session,
                    role="system",
                    content=f"Codex fatal error: {event.get('message', 'unknown error')}",
                    source_trust=SourceTrust.trusted,
                )
            emit_message_created(msg, session.id, task.id)

    stderr_tail = b""
    if proc.stderr is not None:
        stderr_tail = await proc.stderr.read()
    returncode = await proc.wait()

    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task.status in (TaskStatus.cancelled, TaskStatus.done, TaskStatus.failed):
            return
        if turn_failed or returncode != 0:
            repo.set_task_status(
                db,
                task,
                TaskStatus.failed,
                error_code="codex_exec_failed",
                error_message=stderr_tail.decode("utf-8", errors="replace")[-2000:] or None,
            )
        else:
            # Deliberately routed to under_audit, not done: this bridge does not
            # skip your existing audit stage, it only replaces the demo executor.
            repo.set_task_status(db, task, TaskStatus.under_audit)
    emit_task_status(task)


async def _fail_task(task_id: uuid.UUID, error_code: str, error_message: str) -> None:
    from aria.http_utils import emit_task_status

    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task is None:
            return
        repo.set_task_status(db, task, TaskStatus.failed, error_code=error_code, error_message=error_message)
    emit_task_status(task)

"""core/toolhooks.py — склейка хуков и чекпоинтов с исполнением тулов (H4).

Порядок вокруг тула: ``pre_tool`` (может заблокировать) → чекпоинт (для ``file_write``) → тул → ``post_tool``.
Все функции не бросают исключений: сбой хука или чекпоинта не должен ронять задачу.
"""
from __future__ import annotations

import logging
from typing import Any

from aria.checkpoints import store as checkpoints
from aria.hooks import HookDecision, fire

logger = logging.getLogger("local_agent.toolhooks")

CHECKPOINT_TOOLS = ("file_write",)


def _compact(value: Any, limit: int = 4000) -> Any:
    text = str(value)
    return value if len(text) <= limit else text[:limit] + "…"


def _task_state(task_id: Any) -> dict[str, Any] | None:
    """Снимок состояния задачи для чекпоинта. Сбой чтения не мешает исполнению."""
    try:
        import uuid as _uuid

        from aria.db import repository as repo
        from aria.db.base import session_scope

        tid = task_id if isinstance(task_id, _uuid.UUID) else _uuid.UUID(str(task_id))
        with session_scope() as db:
            task = repo.get_task(db, tid)
            if task is None:
                return None
            return {
                "status": task.status.value, "role": task.role, "objective": str(task.objective)[:500],
                "message_count": len(repo.list_messages_for_prompt(db, task.session_id)),
                "tool_call_count": len(repo.list_tool_calls(db, tid)),
            }
    except Exception:  # noqa: BLE001
        logger.warning("task state snapshot failed", exc_info=True)
        return None


async def pre_tool(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    session_id: Any,
    task_id: Any,
    sandbox_root: str,
) -> tuple[HookDecision, str | None]:
    """Вернуть решение хуков и id созданного чекпоинта (None, если не создавался или тул заблокирован)."""
    decision = await fire(
        "pre_tool",
        {"session_id": str(session_id), "task_id": str(task_id), "input": _compact(arguments)},
        tool_name=tool_name,
        cwd=sandbox_root,
    )
    if decision.blocked:
        return decision, None
    cp_id = None
    if tool_name in CHECKPOINT_TOOLS and isinstance(arguments, dict) and arguments.get("path"):
        cp_id = checkpoints.snapshot(
            str(session_id), str(task_id), tool_name, sandbox_root, [str(arguments["path"])],
            task_state=_task_state(task_id),
        )
    return decision, cp_id


async def post_tool(
    tool_name: str,
    arguments: dict[str, Any],
    output: Any,
    status: str,
    *,
    session_id: Any,
    task_id: Any,
    sandbox_root: str,
    checkpoint_id: str | None = None,
) -> HookDecision:
    return await fire(
        "post_tool",
        {
            "session_id": str(session_id), "task_id": str(task_id), "status": status,
            "input": _compact(arguments), "output": _compact(output), "checkpoint_id": checkpoint_id,
        },
        tool_name=tool_name,
        cwd=sandbox_root,
    )


async def lifecycle(event: str, *, session_id: Any = None, task_id: Any = None, **extra: Any) -> HookDecision:
    return await fire(event, {"session_id": str(session_id), "task_id": str(task_id), **extra})

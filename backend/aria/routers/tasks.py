"""Task pipeline routes (moved from aria.main).

Волна 1 (A9/A10/A11):

* исполнение больше не идёт внутри HTTP-запроса — задача кладётся в
  :class:`aria.core.taskrunner.TaskRunner` (единая дверь), который держит
  очередь и пул воркеров;
* тихий демо-мок (``_run_demo_task``) удалён — режима ``demo`` не существует;
* режимы: ``plan`` — Stage 1–7 (:mod:`aria.core.executor`), ``agent`` —
  ReAct-цикл (:func:`aria.core.loop.execute_agent_loop`).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from aria.api.auth import require_runtime_token
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus
from aria.http_utils import emit_task_status, serialize_audit, serialize_task

logger = logging.getLogger("local_agent.main")

router = APIRouter(tags=["tasks"])

RUN_MODES = ("agent", "plan")


def _get_runner(request: Request):
    runner = getattr(request.app.state, "task_runner", None)
    if runner is None:
        raise HTTPException(status_code=503, detail="task runner is not running")
    return runner


def _normalize_mode(payload: dict[str, Any] | None) -> str:
    mode = str((payload or {}).get("mode") or "plan").strip().lower()
    if mode not in RUN_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {list(RUN_MODES)}")
    return mode


@router.get("/tasks/runner/status")
async def runner_status(request: Request, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    """Состояние единой очереди: воркеры, глубина очереди, идущие задачи, RAM."""
    return _get_runner(request).status()


@router.post("/tasks/{task_id}/start")
async def start_task(
    task_id: uuid.UUID,
    request: Request,
    payload: dict[str, Any] | None = None,
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    """Поставить задачу в очередь TaskRunner и сразу вернуть управление."""
    mode = _normalize_mode(payload)
    runner = _get_runner(request)
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if task.status in (TaskStatus.draft, TaskStatus.awaiting_clarification):
            repo.set_task_status(db, task, TaskStatus.approved)
        elif task.status == TaskStatus.needs_rework:
            # §8.1: needs_rework допускает только in_progress/cancelled, approved запрещён
            repo.set_task_status(db, task, TaskStatus.in_progress)
    return runner.submit(task_id, mode=mode, source="ui")


@router.post("/tasks/{task_id}/run-executor")
async def run_executor_pipeline(
    task_id: uuid.UUID,
    request: Request,
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    """Stage 1–7 pipeline (vault-check → plan → handlers → audit → hooks → delivery).

    Волна 1: роут больше не блокирует запрос до конца пайплайна — он ставит
    задачу в очередь в режиме ``plan``. Владелец порядка Stage 1–7 —
    ``aria.core.executor.run_task``.
    """
    runner = _get_runner(request)
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if task.status == TaskStatus.draft:
            repo.set_task_status(db, task, TaskStatus.approved)
    return runner.submit(task_id, mode="plan", source="executor")


@router.get("/tasks/{task_id}/children")
async def get_task_children(task_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> list[dict[str, Any]]:
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        children = repo.list_child_tasks(db, task_id)
        return [serialize_task(child) for child in children]


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(task_id: uuid.UUID, request: Request, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    """Отменить задачу: снять с очереди или прервать исполнение (§6, волна 1 A10)."""
    runner = getattr(request.app.state, "task_runner", None)
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        if runner is not None:
            runner.cancel(task_id)
        if task.status not in (TaskStatus.done, TaskStatus.done_unaudited, TaskStatus.failed, TaskStatus.cancelled):
            repo.set_task_status(db, task, TaskStatus.cancelled)
    emit_task_status(task)
    return {"ok": True, "cancelled": True, "task_id": str(task_id)}


@router.get("/tasks/{task_id}/audit-reports")
async def get_audit_reports(task_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> list[dict[str, Any]]:
    with session_scope() as db:
        return [serialize_audit(row) for row in repo.list_audit_reports(db, task_id)]

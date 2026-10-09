"""core/taskrunner.py — единая дверь для задач (волна 1, пункт A10).

Проблема, которую это закрывает: до волны 1 ``POST /sessions/{id}/messages``
и ``POST /tasks/{id}/start`` исполняли агента **внутри HTTP-запроса**. Из-за
этого были невозможны отмена, параллелизм, расписания, Telegram-шлюз и рой:
любой такой источник блокировал бы обработчик до конца задачи.

Теперь есть одна очередь и пул воркеров. Все источники (UI, cron, Telegram,
рой) вызывают :meth:`TaskRunner.submit`; задача получает задачу, а не поток
исполнения. Есть отмена и возобновление после рестарта процесса.

Ограничение по железу: пул по умолчанию 3 воркера (``runner_max_workers``),
и при свободной памяти меньше ~1.5 ГБ он опускается до одного — целевая
машина владельца имеет 4 ГБ RAM.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select

from aria.config import get_settings
from aria.core.events import event_bus
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus

logger = logging.getLogger("local_agent.taskrunner")

Mode = Literal["agent", "plan"]

# Статусы, из которых задачу осмысленно (пере)запускать.
RUNNABLE_STATUSES = (TaskStatus.approved, TaskStatus.in_progress, TaskStatus.needs_rework)

TERMINAL_STATUSES = (TaskStatus.done, TaskStatus.done_unaudited, TaskStatus.failed, TaskStatus.cancelled)

_LOW_RAM_MB = 1500


def _build_notifier():
    """Telegram-эскалации (ТЗ): раньше run-executor строил нотификатор сам, и при
    переезде в очередь он был потерян (notifier=None) — вернули."""
    try:
        settings = get_settings()
        if settings.telegram_bot_token and settings.telegram_chat_id:
            from aria.core.notifiers.telegram import TelegramNotifier

            return TelegramNotifier(bot_token=settings.telegram_bot_token, chat_id=settings.telegram_chat_id)
    except Exception:
        logger.warning("TelegramNotifier init failed", exc_info=True)
    return None


def _mark_failed(db, task, code: str, message: str) -> None:
    """Перевести в failed по карте переходов §8.1. approved/needs_rework → failed
    напрямую нельзя (карта запрещает), поэтому сначала in_progress. Без этого
    задача, упавшая до старта (нет провайдера), навсегда оставалась ``approved``
    и заново запускалась при каждом рестарте."""
    if task.status in TERMINAL_STATUSES:
        return
    if task.status in (TaskStatus.approved, TaskStatus.needs_rework):
        repo.set_task_status(db, task, TaskStatus.in_progress)
    repo.set_task_status(db, task, TaskStatus.failed, error_code=code, error_message=message[:500])


def available_ram_mb() -> int | None:
    """Свободная память в МБ, если psutil доступен (иначе None)."""
    try:
        import psutil
    except Exception:
        return None
    try:
        return int(psutil.virtual_memory().available / (1024 * 1024))
    except Exception:
        return None


@dataclass(frozen=True)
class Job:
    task_id: uuid.UUID
    mode: Mode
    source: str
    # A22: если задан — это возобновление после Approve: исполнить подтверждённую
    # команду из attention item и продолжить цикл, а не начинать задачу заново.
    approval_item_id: uuid.UUID | None = None

    @property
    def key(self) -> str:
        """Ключ дедупликации. У возобновления свой ключ: оно не должно теряться, если
        исходное задание этой же задачи ещё дозавершается в воркере."""
        if self.approval_item_id is not None:
            return f"{self.task_id}:approval:{self.approval_item_id}"
        return str(self.task_id)



def _record_episode(task_id: uuid.UUID, mode: str) -> None:
    """H3: итог завершённой задачи → слой ``episode`` долговременной памяти (недоверенный источник)."""
    try:
        from aria.memory import store as memory_store

        if not memory_store.enabled():
            return
        with session_scope() as db:
            task = repo.get_task(db, task_id)
            if task is None or task.status not in TERMINAL_STATUSES or task.status == TaskStatus.cancelled:
                return
            status = task.status.value if hasattr(task.status, "value") else str(task.status)
            objective = " ".join((task.objective or "").split())[:400]
            session_id = str(task.session_id)
        memory_store.add(
            "episode",
            f"Task ({mode}) finished as {status}: {objective}",
            source="task",
            session_id=session_id,
            task_id=str(task_id),
        )
    except Exception:  # noqa: BLE001 — память не должна ломать завершение задачи
        logger.exception("could not record task episode")

def _unexecuted_approvals(task_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    """task_id -> id подтверждённого attention item, действие по которому ещё не исполнялось.

    «Исполнялось» = к item привязан tool_call (``approval_item_id``) ИЛИ у задачи есть tool_call,
    начатый после подтверждения (так выглядят подтверждения, исполненные до этой правки: связи у них нет).
    """
    if not task_ids:
        return {}
    from aria.db.enums import ApprovalStatus, AttentionType

    found: dict[uuid.UUID, uuid.UUID] = {}
    with session_scope() as db:
        items = (
            db.execute(
                select(m.AttentionItem)
                .where(
                    m.AttentionItem.task_id.in_(task_ids),
                    m.AttentionItem.status == ApprovalStatus.approved,
                    m.AttentionItem.type.in_((AttentionType.high_risk_shell, AttentionType.mcp_tool_approval)),
                    m.AttentionItem.resolved_at.is_not(None),
                )
                .order_by(m.AttentionItem.resolved_at.desc())
            )
            .scalars()
            .all()
        )
        for item in items:
            if item.task_id in found:
                continue  # только самое свежее подтверждение задачи
            linked = db.execute(select(m.ToolCall.id).where(m.ToolCall.approval_item_id == item.id).limit(1)).first()
            later = db.execute(
                select(m.ToolCall.id).where(m.ToolCall.task_id == item.task_id, m.ToolCall.started_at >= item.resolved_at).limit(1)
            ).first()
            if linked is None and later is None:
                found[item.task_id] = item.id
    return found


class TaskRunner:
    """Очередь задач + пул воркеров + отмена + возобновление после рестарта."""

    def __init__(self, router, sandbox_root: str, max_workers: int | None = None) -> None:
        self._router = router
        self._sandbox_root = sandbox_root
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._running: dict[str, asyncio.Task] = {}
        self._cancelled: set[str] = set()
        self._pending: set[str] = set()  # в очереди ИЛИ исполняется — защита от дублей
        self._stopping = False
        self._started = False
        configured = getattr(get_settings(), "runner_max_workers", 3)
        self._max_workers = int(max_workers or configured or 3)

    # ── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        workers = self._max_workers
        ram = available_ram_mb()
        if ram is not None and ram < _LOW_RAM_MB:
            workers = 1
            logger.warning("only %s MB RAM free — TaskRunner runs 1 worker", ram)
        for i in range(workers):
            self._workers.append(asyncio.create_task(self._worker_loop(i), name=f"taskrunner-{i}"))
        logger.info("TaskRunner started: %d worker(s), max_workers=%d", workers, self._max_workers)

    async def stop(self) -> None:
        self._stopping = True
        for job_task in list(self._running.values()):
            job_task.cancel()
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            try:
                await worker
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("taskrunner worker failed during stop")
        self._workers.clear()
        self._running.clear()
        self._started = False
        logger.info("TaskRunner stopped")

    # ── public API ───────────────────────────────────────────────────

    def submit(
        self,
        task_id: uuid.UUID,
        mode: Mode = "plan",
        source: str = "ui",
        approval_item_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Поставить задачу в очередь. Возврат — сразу, без ожидания исполнения.

        ``approval_item_id`` (A22): возобновить задачу после Approve; исполняется в
        режиме ``agent`` (цикл с тулами) независимо от ``mode``.
        """
        if mode not in ("agent", "plan"):
            raise ValueError(f"unknown mode={mode!r} (expected 'agent' or 'plan')")
        job = Job(task_id=task_id, mode=mode, source=source, approval_item_id=approval_item_id)
        key = job.key
        if key in self._pending:
            logger.info("task %s already queued/running — duplicate submit ignored", key[:8])
            return {"ok": True, "queued": False, "duplicate": True, "task_id": str(task_id), "mode": mode, "source": source}
        self._cancelled.discard(str(task_id))
        self._pending.add(key)
        self._queue.put_nowait(job)
        event_bus.emit("task.queued", {"task_id": str(task_id), "mode": mode, "source": source}, session_id=None, task_id=task_id)
        logger.info("queued task %s (mode=%s, source=%s, depth=%d)", key[:8], mode, source, self._queue.qsize())
        return {"ok": True, "queued": True, "task_id": str(task_id), "mode": mode, "source": source, "queue": self._queue.qsize()}

    def cancel(self, task_id: uuid.UUID) -> bool:
        """Отменить задачу: снять с очереди (если ждёт) и прервать (если идёт)."""
        key = str(task_id)
        self._cancelled.add(key)
        # Под одной задачей могут идти исходное задание и возобновления (ключи с префиксом).
        running = [t for k, t in self._running.items() if k == key or k.startswith(f"{key}:")]
        for job_task in running:
            job_task.cancel()
        logger.info("cancel requested for task %s (%s)", key[:8], "running" if running else "queued")
        return True

    async def resume_pending(self) -> int:
        """После рестарта снова поставить в очередь задачи, оставшиеся незакрытыми."""
        with session_scope() as db:
            rows = (
                db.execute(
                    select(m.Task)
                    .where(m.Task.status.in_(RUNNABLE_STATUSES))
                    .order_by(m.Task.created_at.asc())
                    .limit(20)
                )
                .scalars()
                .all()
            )
            # Аудит, прерванный рестартом, не имеет исполнителя — отправляем на доработку.
            for stale in db.execute(select(m.Task).where(m.Task.status == TaskStatus.under_audit)).scalars().all():
                repo.set_task_status(db, stale, TaskStatus.needs_rework)
            rows = (
                db.execute(
                    select(m.Task)
                    .where(m.Task.status.in_(RUNNABLE_STATUSES))
                    .order_by(m.Task.created_at.asc())
                    .limit(20)
                )
                .scalars()
                .all()
            )
            pending = [(t.id, str(t.status.value if hasattr(t.status, "value") else t.status)) for t in rows]
        approvals_by_task = _unexecuted_approvals([tid for tid, _ in pending])
        for task_id, _status in pending:
            # Approve получен, а действие не успело исполниться (перезапуск между ними): исполнить его, а не начинать заново.
            self.submit(task_id, mode="agent", source="resume", approval_item_id=approvals_by_task.get(task_id))
        if pending:
            logger.info("resumed %d unfinished task(s) after restart", len(pending))
        return len(pending)

    def status(self) -> dict[str, Any]:
        return {
            "started": self._started,
            "max_workers": self._max_workers,
            "workers": len(self._workers),
            "queue": self._queue.qsize(),
            "running": sorted(self._running.keys()),
            "available_ram_mb": available_ram_mb(),
        }

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    # ── execution ────────────────────────────────────────────────────

    async def _worker_loop(self, index: int) -> None:
        logger.debug("taskrunner worker %d ready", index)
        while not self._stopping:
            job = await self._queue.get()
            key = job.key
            task_key = str(job.task_id)
            if task_key in self._cancelled:
                self._cancelled.discard(task_key)
                self._pending.discard(key)
                self._queue.task_done()
                logger.info("skipped cancelled task %s", task_key[:8])
                continue
            inner = asyncio.create_task(self._run(job), name=f"task-{key[:8]}")
            self._running[key] = inner
            try:
                await inner
            except asyncio.CancelledError:
                if self._stopping:
                    raise
                logger.info("task %s cancelled", task_key[:8])
            except Exception:
                logger.exception("task %s crashed in runner", task_key[:8])
            finally:
                self._running.pop(key, None)
                self._cancelled.discard(task_key)
                self._pending.discard(key)
                self._queue.task_done()

    async def _run_fresh(self, job: Job) -> dict[str, Any]:
        """Обычный запуск задачи: ``plan`` — через executor, ``agent`` — цикл с тулами."""
        key = str(job.task_id)
        with session_scope() as db:
            task = repo.get_task(db, job.task_id)
            if task is None:
                logger.warning("task %s vanished before execution", key[:8])
                return {"status": "not_found"}
            if job.mode == "plan":
                from aria.core.executor import run_task as executor_run_task

                return await executor_run_task(session=db, task=task, router=self._router, notifier=_build_notifier())
        from aria.core.loop import execute_agent_loop

        await execute_agent_loop(job.task_id, self._router, self._sandbox_root)
        return {"status": "ok"}

    async def _run_approved(self, job: Job) -> dict[str, Any]:
        """A22: после Approve исполнить подтверждённую команду и продолжить цикл.

        До A22 ``resume_after_approval`` не вызывался нигде в боевом коде: Approve
        переводил задачу в ``in_progress``, но команда не запускалась.
        """
        key = str(job.task_id)
        with session_scope() as db:
            task = repo.get_task(db, job.task_id)
            item = repo.get_attention_item(db, job.approval_item_id)
            if task is None or item is None:
                logger.warning("task %s or approval item vanished before resume", key[:8])
                return {"status": "not_found"}
        from aria.core.loop import resume_after_approval

        await resume_after_approval(job.task_id, self._router, self._sandbox_root, item)
        return {"status": "ok", "resumed": True}

    async def _run(self, job: Job) -> dict[str, Any]:
        key = str(job.task_id)
        event_bus.emit("task.started", {"task_id": key, "mode": job.mode, "source": job.source}, session_id=None, task_id=job.task_id)
        try:
            if job.approval_item_id is not None:
                result = await self._run_approved(job)
            else:
                result = await self._run_fresh(job)
            if result.get("status") == "not_found":
                return result
        except asyncio.CancelledError:
            if self._stopping:
                # Остановка приложения — НЕ отмена пользователем: статус не трогаем,
                # задача остаётся in_progress/approved и подхватится resume_pending().
                logger.info("task %s interrupted by shutdown — left resumable", key[:8])
                raise
            # Отмена пользователем: состояние фиксируем явно.
            with session_scope() as db:
                task = repo.get_task(db, job.task_id)
                if task is not None and task.status not in TERMINAL_STATUSES:
                    repo.set_task_status(db, task, TaskStatus.cancelled)
            event_bus.emit("task.cancelled", {"task_id": key}, session_id=None, task_id=job.task_id)
            raise
        except Exception as exc:  # noqa: BLE001 — граница задачи: падение одной не рушит воркер
            logger.exception("task %s failed in runner", key[:8])
            code = "provider_unavailable" if type(exc).__name__ == "ProviderUnavailable" else "runner_error"
            with session_scope() as db:
                task = repo.get_task(db, job.task_id)
                if task is not None and task.status != TaskStatus.cancelled:
                    try:
                        _mark_failed(db, task, code, str(exc))
                    except Exception:
                        logger.exception("could not mark task %s failed", key[:8])
            return {"status": "failed", "error": str(exc)[:200]}
        _record_episode(job.task_id, job.mode)
        event_bus.emit("task.finished", {"task_id": key, "result": result}, session_id=None, task_id=job.task_id)
        return result

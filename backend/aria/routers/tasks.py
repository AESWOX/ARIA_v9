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
from aria.core import clarifier
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
            # PR-3: draft/awaiting_clarification -> approved только с валидным TaskSpec.
            try:
                clarifier.assert_runnable(db, task)
            except clarifier.ClarifyRequired as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
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
            # PR-3: тот же гейт, что и в /start — контракт обязателен до Stage 1–7.
            try:
                clarifier.assert_runnable(db, task)
            except clarifier.ClarifyRequired as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
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
        if repo.get_task(db, task_id) is None:
            raise HTTPException(status_code=404, detail="task not found")
        return [serialize_audit(row) for row in repo.list_audit_reports(db, task_id)]


@router.post("/tasks/{task_id}/clarify")
async def clarify_task(
    task_id: uuid.UUID,
    request: Request,
    payload: dict[str, Any] | None = None,
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    """Раунд 2 (п.1.1–1.3): ответы владельца на карточку уточнений.

    * ответ на вопрос с полем по умолчанию (``clarification``) сохраняется структурной
      записью в TaskSpec.clarifications — цель больше не затирается;
    * цель заменяется ТОЛЬКО явным ответом на вопрос с field="goal", прошедшим
      MIN_GOAL_CHARS; короткий ответ дописывается к цели, а не заменяет её;
    * пустой answers без accept_assumptions=true -> 400; accept_assumptions=true ->
      неотвеченные вопросы фиксируются как допущения (слот критерия приёмки —
      детерминированным критерием по умолчанию, как в гейте); пустую цель принять
      как допущение нельзя — такой вопрос остаётся;
    * задача принимается только в awaiting_clarification, иначе 409;
    * после применения ответов валидность пересчитывается: если спецификация всё ещё
      невалидна, open_questions НЕ очищаются — остаются неотвеченные вопросы и
      добавляются недостающие слоты из missing_fields(). Инвариант: состояния
      «awaiting_clarification без вопросов» быть не может.
    """
    from aria.core import taskspec
    from aria.core.taskspec import MAX_CLARIFY_ROUNDS, MIN_GOAL_CHARS

    body = payload or {}
    mode = _normalize_mode(body)
    answers = body.get("answers") or []
    accept_assumptions = bool(body.get("accept_assumptions"))
    if not isinstance(answers, list):
        raise HTTPException(status_code=400, detail="answers must be a list of {id|field, value}")

    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        # п.1.3: явная проверка состояния — не полагаемся на assert_transition_allowed.
        if task.status != TaskStatus.awaiting_clarification:
            raise HTTPException(
                status_code=409,
                detail=f"task is not awaiting clarification (current status: {task.status.value})",
            )
        spec = clarifier.load_spec(db, task_id) or taskspec.derive_spec(task.objective or "")
        # Старые данные (до раунда 2): вопросы без id — восстанавливаем id из текстов.
        if not spec.clarify_questions and spec.open_questions:
            spec.clarify_questions = [
                {"id": clarifier.question_id(str(q)), "text": str(q), "blocking": True, "field": "clarification"}
                for q in spec.open_questions
                if str(q).strip()
            ]

        # п.1.2.1: пустой запрос без accept_assumptions — 400.
        if not answers and not accept_assumptions:
            raise HTTPException(
                status_code=400,
                detail="empty answers require accept_assumptions=true (кнопка «Принять допущения»)",
            )

        # п.1.2.3: счётчик растёт на каждый вызов; лимит -> поведение гейта (допущения).
        spec.clarify_rounds = int(spec.clarify_rounds or 0) + 1
        limit_reached = spec.clarify_rounds >= MAX_CLARIFY_ROUNDS

        answered_ids = {
            str((a or {}).get("id") or "").strip()
            for a in answers
            if isinstance(a, dict) and str(a.get("value") or "").strip()
        }
        unanswered = [q for q in spec.clarify_questions if str(q.get("id") or "") not in answered_ids]

        if accept_assumptions or limit_reached:
            # п.1.2.2: неотвеченные вопросы -> допущения. Слот критерия приёмки закрывается
            # детерминированным критерием по умолчанию (правила гейта/автономного режима).
            # Пустую цель нельзя «принять как допущение» — такой вопрос остаётся (blocking).
            kept_questions: list[dict] = []
            for q in unanswered:
                field = str(q.get("field") or "clarification")
                if field in ("acceptance_criteria", "criterion"):
                    if not spec.acceptance_criteria:
                        spec.acceptance_criteria.append(taskspec.default_criterion(spec.goal or task.objective or ""))
                        spec.assumptions.append(
                            "Критерий приёмки не задан: принят детерминированный критерий по умолчанию."
                        )
                    continue
                if field in ("goal", "objective"):
                    if len((spec.goal or "").strip()) >= MIN_GOAL_CHARS:
                        # Цель уже валидна: неотвеченное уточнение цели — как допущение.
                        text = str(q.get("text") or "").strip()
                        if text:
                            spec.assumptions.append(f"Допущение (ответ не получен): {text}")
                        continue
                    # Цель короче MIN_GOAL_CHARS: fallback на objective задачи (как в гейте);
                    # если и он не помогает — вопрос ОСТАЁТСЯ: пустую цель не допускаем.
                    spec.goal = (task.objective or "").strip()[:400]
                    if len((spec.goal or "").strip()) >= MIN_GOAL_CHARS:
                        spec.assumptions.append("Цель взята из формулировки задачи (ответ владельца не получен).")
                    else:
                        kept_questions.append(q)
                    continue
                text = str(q.get("text") or "").strip()
                if text:
                    spec.assumptions.append(f"Допущение (ответ не получен): {text}")
            spec.assumptions = clarifier._dedupe(list(spec.assumptions))
            if limit_reached:
                # Поведение гейта при исчерпанном лимите: остаток неопределённости —
                # допущения, недостающий критерий — детерминированный по умолчанию.
                if not spec.acceptance_criteria:
                    spec.acceptance_criteria.append(taskspec.default_criterion(spec.goal or task.objective or ""))
            # Остаются только вопросы, которые нельзя закрыть допущением (блокирующий пустой goal);
            # инвариант п.1.1.5 добирает недостающие слоты из missing_fields().
            spec.clarify_questions = kept_questions
            spec.sync_questions()
            if not spec.is_valid and not spec.clarify_questions:
                existing_texts = {str(q.get("text") or "") for q in spec.clarify_questions}
                for slot in spec.missing_fields():
                    qtext = taskspec.slot_question(slot)
                    if qtext and qtext not in existing_texts:
                        spec.clarify_questions.append(
                            {"id": clarifier.question_id(qtext), "text": qtext, "blocking": True, "field": slot}
                        )
                spec.sync_questions()
        else:
            # п.1.1.2–3: применение ответов.
            for item in answers:
                if not isinstance(item, dict):
                    continue
                value = str(item.get("value") or "").strip()
                if not value:
                    continue
                qid = str(item.get("id") or item.get("field") or "").strip()
                q = next((x for x in spec.clarify_questions if str(x.get("id") or "") == qid), None)
                field = str(item.get("field") or (q or {}).get("field") or "clarification").strip()
                if field in ("goal", "objective"):
                    if len(value) >= MIN_GOAL_CHARS:
                        spec.goal = value[:400]
                    else:
                        # Короткий ответ не заменяет цель — дописывается к ней.
                        spec.goal = ((spec.goal or "") + " " + value).strip()[:400]
                elif field in ("acceptance_criteria", "criterion"):
                    try:
                        spec.acceptance_criteria.append(
                            taskspec.AcceptanceCriterion(
                                id=f"ac-{len(spec.acceptance_criteria) + 1}", statement=value[:300]
                            )
                        )
                    except Exception:  # noqa: BLE001 — непроверяемая формулировка
                        spec.constraints.append(f"Критерий (не проверяем): {value}")
                else:
                    # п.1.1.2: структурная запись вместо «q1: да» в constraints.
                    spec.clarifications.append(
                        {"question_id": qid or field, "question": str((q or {}).get("text") or field), "answer": value}
                    )

            # п.1.1.5: пересчёт валидности. Невалидно -> вопросы НЕ очищаются.
            spec.clarify_questions = [q for q in spec.clarify_questions if str(q.get("id") or "") not in answered_ids]
            if not spec.is_valid:
                existing_texts = {str(q.get("text") or "") for q in spec.clarify_questions}
                for slot in spec.missing_fields():
                    qtext = taskspec.slot_question(slot)
                    if qtext and qtext not in existing_texts:
                        spec.clarify_questions.append(
                            {"id": clarifier.question_id(qtext), "text": qtext, "blocking": True, "field": slot}
                        )
            spec.sync_questions()

        # п.4.4 ТЗ (раунд 1): исходная цель задачи остаётся; ответы живут в TaskSpec.
        session = repo.get_session(db, task.session_id)
        repo.upsert_task_spec(
            db, task, spec.model_dump(mode="json"), valid=spec.is_valid, clarify_rounds=spec.clarify_rounds
        )
        if spec.is_valid:
            repo.set_task_status(db, task, TaskStatus.approved)
        # Инвариант п.1.1.5: awaiting_clarification без вопросов невозможен.
        if not spec.is_valid and not spec.clarify_questions:
            raise HTTPException(status_code=500, detail="invariant violated: awaiting_clarification without questions")
        if session is not None:
            # Запасной путь П.3.2: ответы остаются текстом в чате.
            repo.append_message(
                db, session, role="user",
                content="Уточнения: " + "; ".join(
                    f"{a.get('field') or a.get('id')}: {a.get('value')}" for a in answers if isinstance(a, dict)
                ),
            )
        result = {
            "ok": True,
            "task_id": str(task_id),
            "status": task.status.value,
            "valid": bool(spec.is_valid),
            "clarify_rounds": spec.clarify_rounds,
            "questions": [
                {k: v for k, v in {
                    "id": q.get("id"), "text": q.get("text"), "field": q.get("field"),
                    "options": q.get("options"), "default_value": q.get("default_value"),
                }.items() if v is not None}
                for q in spec.clarify_questions
            ],
        }

    # Контракт стал валидным и задача approved: её надо ПОСТАВИТЬ В ОЧЕРЕДЬ. Раньше этого не
    # происходило: статус менялся, а исполнитель о задаче не знал, и она висела в «approved /
    # working / Subtasks: none yet» бесконечно. Постановка вне DB-scope, как в sessions.py.
    if result["status"] == TaskStatus.approved.value:
        runner = getattr(request.app.state, "task_runner", None)
        if runner is not None:
            result["queued"] = bool(runner.submit(task_id, mode=mode, source="clarify").get("queued"))
            result["mode"] = mode
        else:
            logger.warning("clarify: task %s approved but task runner is not running", str(task_id)[:8])
            result["queued"] = False
    return result


@router.get("/tasks/{task_id}/plan")
async def get_task_plan(task_id: uuid.UUID, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    """П.3.3 ТЗ: предпросмотр плана — шаги, зависимости, роли, критерии, волны."""
    from aria.core.plan_validator import topological_waves

    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        plan = repo.get_latest_task_plan(db, task_id) if hasattr(repo, "get_latest_task_plan") else None
        if plan is None:
            from sqlalchemy import select as _select

            from aria.db.models import TaskPlan as _TaskPlan

            plan = db.execute(
                _select(_TaskPlan).where(_TaskPlan.task_id == task_id).order_by(_TaskPlan.version.desc())
            ).scalars().first()
        steps = list((plan.plan_json if plan is not None else []) or [])
        spec = clarifier.load_spec(db, task_id)
        return {
            "ok": True,
            "task_id": str(task_id),
            "phase": getattr(plan, "phase", None),
            "status": getattr(plan, "status", None),
            "waves": topological_waves(steps) if steps else [],
            "criteria": [c.model_dump(mode="json") for c in spec.acceptance_criteria] if spec else [],
            "steps": [
                {
                    "step_id": str(s.get("step_id")),
                    "objective": s.get("objective"),
                    "role": s.get("role"),
                    "tool_ref": s.get("tool_ref") or s.get("skill_ref"),
                    "depends_on": s.get("depends_on"),
                    "covers": s.get("covers") or [],
                    "write_scope": s.get("write_scope") or [],
                    "status": s.get("status"),
                }
                for s in steps
            ],
        }


@router.post("/tasks/{task_id}/plan/request-approval")
async def request_plan_approval(
    task_id: uuid.UUID,
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    """П.3.3 ТЗ: подтверждение плана через существующий AttentionType.task_tz_approval."""
    from aria.core import approvals
    from aria.core import clarifier as _clarifier
    from aria.http_utils import serialize_attention

    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        session = repo.get_session(db, task.session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        # Раньше из done/failed это падало InvalidTransition -> 500: кнопка была видна всегда.
        from aria.core.state_machine import InvalidTransition, assert_transition_allowed

        try:
            assert_transition_allowed(task.status, TaskStatus.awaiting_approval)
        except InvalidTransition:
            raise HTTPException(
                status_code=409,
                detail=f"plan approval is not available in task status {task.status.value}",
            )
        spec = _clarifier.load_spec(db, task_id)
        body = spec.to_markdown() if spec is not None else (task.draft_tz_md or task.objective or "")
        item = approvals.request_task_tz_approval(db, session, task, body)
        return {"ok": True, "attention_item": serialize_attention(item)}

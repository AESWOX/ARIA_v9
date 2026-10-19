"""core/clarifier.py — PR-3: Clarifier-роль и гейт уточнения.

Гейт стоит ровно там, где задача реально запускается: сообщение в сессии и
start_task. Быстрый путь — короткая реплика простого чата и agent-запрос без
маркера задачи — гейт не проходит: уточнять там нечего.

Зачем это в коде, а не в промпте: автоперевод draft -> approved по факту прихода
сообщения означал, что задача уходила в исполнение с пустым контрактом, и
аудиту (PR-6) было не с чем сверять результат.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from aria.core import approvals, taskspec
from aria.core.taskspec import MAX_CLARIFY_ROUNDS, TaskSpec, default_criterion, derive_spec
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus
from aria.llm.providers.base import ChatMessage
from aria.llm.router import ProviderRouter

logger = logging.getLogger(__name__)

# П.4.2 ТЗ: маркеры задачи для agent-режима. Латиница сверяется ЦЕЛЫМ словом
# (иначе "fix" срабатывал на "prefix", а "build" — на "building"), кириллица —
# по основе слова (основа + любые окончания: "сдела" -> "сделай", "почин" -> "почини").
# Список переопределяется настройкой clarifier_task_verbs (через запятую).
_DEFAULT_TASK_VERBS: tuple[str, ...] = (
    "сдела", "созда", "напиш", "провер", "почин", "исправ", "найд", "собер",
    "разверн", "задепло", "обнов", "перенес", "проанализиру", "посчита",
    "сгенерир", "настро", "установ", "запуст", "перепиш", "отрефактор",
    "build", "create", "implement", "fix", "refactor", "research", "write",
    "deploy", "generate", "analyze", "analyse", "migrate", "install", "review",
)
_TASK_VERBS = _DEFAULT_TASK_VERBS  # обратная совместимость импортов


def configured_task_verbs() -> list[str]:
    """П.4.2 ТЗ: список глаголов-маркеров из настроек, иначе встроенный."""
    from aria.config import get_settings

    configured = list(getattr(get_settings(), "clarifier_verbs_list", []) or [])
    return configured or list(_DEFAULT_TASK_VERBS)


def _verb_pattern(verb: str) -> re.Pattern:
    v = (verb or "").strip()
    if not v:
        return re.compile(r"(?!x)x")  # заведомо не срабатывающий шаблон
    if re.search(r"[A-Za-z]", v) and not re.search(r"[А-Яа-яЁё]", v):
        return re.compile(rf"(?<!\w){re.escape(v)}(?!\w)", re.IGNORECASE)
    return re.compile(rf"(?<![А-Яа-яЁё]){re.escape(v)}[А-Яа-яЁё]*", re.IGNORECASE)

AUTONOMOUS_NOTE = (
    "Автономный режим (cron/Telegram): уточняющие вопросы не задавались, "
    "необязательные неизвестные приняты как допущения."
)

_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "clarifier.md"
_FALLBACK_PROMPT = (
    "Ты clarifier. Собери TaskSpec (goal, constraints, acceptance_criteria, questions, assumptions) "
    "и верни СТРОГО один JSON-объект без markdown-обёртки."
)


class ClarifyRequired(RuntimeError):
    """TaskSpec невалиден — задачу нельзя запускать (PR-3)."""


@dataclass
class ClarifyOutcome:
    status: str  # approved | awaiting_clarification | awaiting_approval
    spec: TaskSpec
    questions: list[dict] = field(default_factory=list)


# ═══════════════════════════════════════════════════════════════════
# Гейт: нужен ли он вообще
# ═══════════════════════════════════════════════════════════════════

def gate_required(content: str, mode: str = "plan") -> bool:
    """Нужен ли гейт уточнения для этого сообщения.

    plan-режим — это Stage 1–7 с tool calls, там контракт обязателен. В agent-режиме
    гейт включается только при явном маркере задачи, иначе обычная переписка
    («привет», «как дела») получала бы вопросы вместо ответа.

    П.4.1 ТЗ: при ``clarifier_gate_enabled=False`` гейт выключен полностью — задача
    идёт быстрым путём ``draft -> approved``, как до PR-3.
    П.4.2 ТЗ: маркеры сверяются по границам слов/основ, а не подстрокой.
    """
    from aria.config import get_settings

    if not bool(getattr(get_settings(), "clarifier_gate_enabled", True)):
        return False
    # Решение владельца (19.10): структурированный запрос и вопросы формируются на ЛЮБОЙ непустой
    # запрос в plan и agent. Режим больше не влияет; список глаголов оставлен только для совместимости.
    return bool((content or "").strip())


# ═══════════════════════════════════════════════════════════════════
# LLM-проход
# ═══════════════════════════════════════════════════════════════════

def _prompt_text() -> str:
    try:
        return _PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return _FALLBACK_PROMPT


def _strip_fence(raw: str) -> str:
    text = (raw or "").strip()
    if text.startswith("```json"):
        text = text.split("```json", 1)[1].split("```", 1)[0]
    elif text.startswith("```"):
        text = text.split("```", 1)[1].split("```", 1)[0]
    return text.strip()


async def _ask_model(router: ProviderRouter, content: str, spec: TaskSpec) -> dict:
    """Один запрос к clarifier-роли. Любой сбой = пустой payload, а не падение задачи."""
    messages = [
        ChatMessage(role="system", content=_prompt_text()),
        ChatMessage(
            role="user",
            content=(
                f"Запрос владельца: {content}\n\n"
                f"Уже известный контракт (может быть пустым): {spec.model_dump_json()}\n"
                f"Раунд уточнения: {spec.clarify_rounds}/{MAX_CLARIFY_ROUNDS}"
            ),
        ),
    ]
    result = await router.route_chat(
        "standard_reasoning", messages, tools=[], allow_degrade=True,
        resilient=True, fallback_classes=("free_tier_reasoning", "premium_reasoning"),
    )
    import json

    data = json.loads(_strip_fence(result.response.text or ""))
    if not isinstance(data, dict):
        raise ValueError("clarifier response is not a JSON object")
    return data


def question_id(text: str) -> str:
    """Раунд 2 (п.1.1.1): стабильный id вопроса — короткий хеш текста.

    Один и тот же вопрос в разных раундах получает один и тот же id, поэтому
    ответы в UI и в POST /clarify больше не перетирают друг друга.
    """
    import hashlib

    return "q-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]


MAX_QUESTIONS = 7
MAX_OPTIONS = 5


def _clean_options(raw) -> list[str]:
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for o in raw:
        text = str(o).strip()
        if text and text not in out:
            out.append(text[:160])
    return out[:MAX_OPTIONS]


def _normalise_questions(raw) -> list[dict]:
    """Каждый вопрос: стабильный id, до 5 вариантов ответа, рекомендованный вариант (default_value).

    Решение владельца: вопросы блокирующие по умолчанию — задача не стартует без ответа или
    явного «Принять допущения». Ответ можно дать и текстом (поле свободного ввода в UI)."""
    out: list[dict] = []
    seen_ids: set[str] = set()
    if not isinstance(raw, list):
        return out
    for item in raw:
        if len(out) >= MAX_QUESTIONS:
            break
        if isinstance(item, str) and item.strip():
            text, extra = item.strip(), {}
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("question") or "").strip()
            extra = item
        else:
            continue
        if not text:
            continue
        qid = str(extra.get("id") or "").strip() or question_id(text)
        if qid in seen_ids:
            continue
        seen_ids.add(qid)
        options = _clean_options(extra.get("options"))
        rec = str(extra.get("recommended") or extra.get("default_value") or "").strip()
        entry = {
            "id": qid,
            "text": text,
            "blocking": bool(extra.get("blocking", True)),
            "field": str(extra.get("field") or "clarification"),
        }
        if options:
            entry["options"] = options
            entry["default_value"] = rec if rec in options else options[0]
        out.append(entry)
    return out


# Набор на случай, когда модель вопросов не вернула или недоступна: спрашиваем всё равно.
_FALLBACK_QUESTIONS: tuple[dict, ...] = (
    {
        "text": "В каком виде нужен результат?",
        "field": "clarification",
        "options": ["Текст прямо в чате", "Файл Markdown (.md)", "Таблица", "Краткое резюме и файл с деталями", "Свой вариант"],
        "recommended": "Текст прямо в чате",
    },
    {
        "text": "Насколько подробно?",
        "field": "clarification",
        "options": ["Коротко (5–10 строк)", "Средне (1–2 страницы)", "Подробно, с обоснованиями", "Максимально полно, по разделам", "Свой вариант"],
        "recommended": "Подробно, с обоснованиями",
    },
    {
        "text": "Есть ограничения или то, чего делать нельзя?",
        "field": "constraints",
        "options": ["Нет ограничений", "Не запускать внешние программы", "Только бесплатные инструменты", "Только офлайн, без интернета", "Свой вариант"],
        "recommended": "Нет ограничений",
    },
)


def _fallback_questions() -> list[dict]:
    return _normalise_questions([dict(q) for q in _FALLBACK_QUESTIONS])


def _merge_payload(spec: TaskSpec, payload: dict) -> list[str]:
    """Переносит ответ модели в TaskSpec. Возвращает новые допущения."""
    assumptions: list[str] = []
    goal = str(payload.get("goal") or "").strip()
    if goal and len(goal) >= len((spec.goal or "").strip()):
        spec.goal = goal[:400]
    constraints = payload.get("constraints")
    if isinstance(constraints, list):
        spec.constraints = _dedupe(spec.constraints + [str(c).strip() for c in constraints if str(c).strip()])
    for raw in payload.get("acceptance_criteria") or []:
        if not isinstance(raw, dict):
            continue
        try:
            spec.acceptance_criteria.append(taskspec.AcceptanceCriterion(**raw))
        except Exception:  # noqa: BLE001 — непроверяемый критерий от модели просто не берём
            logger.info("clarifier: критерий отклонён как непроверяемый: %s", raw)
    for raw in payload.get("assumptions") or []:
        text = str(raw).strip()
        if text:
            assumptions.append(text)
    return assumptions


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ═══════════════════════════════════════════════════════════════════
# Основной вход
# ═══════════════════════════════════════════════════════════════════

def load_spec(db, task_id: uuid.UUID) -> TaskSpec | None:
    """TaskSpec задачи из БД (None — контракта нет или он не читается)."""
    row = repo.get_task_spec(db, task_id)
    if row is None:
        return None
    try:
        return TaskSpec(**dict(row.spec_json or {}))
    except Exception:  # noqa: BLE001 — битая версия не должна ронять весь путь
        logger.warning("clarifier: TaskSpec задачи %s не читается, считаем отсутствующим", str(task_id)[:8])
        return None


def assert_runnable(db, task) -> TaskSpec:
    """Пускать задачу в исполнение можно только с валидным TaskSpec (PR-3)."""
    spec = load_spec(db, task.id)
    if spec is None or not spec.is_valid:
        missing = ", ".join(spec.missing_fields()) if spec is not None else "task_spec"
        raise ClarifyRequired(
            f"task {str(task.id)[:8]} has no valid TaskSpec (missing: {missing}); "
            "clarify the task first — PR-3 gate"
        )
    return spec


def _load_or_derive(task_id: uuid.UUID, content: str) -> TaskSpec:
    with session_scope() as db:
        spec = load_spec(db, task_id)
    if spec is not None:
        return spec
    return derive_spec(content)


def _question_message(questions: list[dict]) -> str:
    lines = ["Чтобы выполнить и проверить задачу, нужны ответы:"]
    for i, q in enumerate(questions, start=1):
        lines.append(f"{i}. {q['text']}")
        for letter, opt in zip("ABCDE", q.get("options") or []):
            mark = " (рекомендую)" if opt == q.get("default_value") else ""
            lines.append(f"   {letter}) {opt}{mark}")
    lines.append("")
    lines.append("Выберите варианты в карточке или ответьте текстом в этом же чате; можно нажать «Принять допущения».")
    return "\n".join(lines)


async def run_gate(
    task_id: uuid.UUID,
    content: str,
    *,
    router: ProviderRouter | None = None,
    autonomous: bool = False,
) -> ClarifyOutcome:
    """Прогоняет гейт уточнения и приводит задачу к одному из трёх состояний.

    * блокирующие вопросы и есть запас раундов -> awaiting_clarification + вопрос владельцу;
    * контракт полный -> approved (или awaiting_approval, если задача высокого риска);
    * автономный режим или лимит раундов исчерпан -> допущения записываются в TaskSpec
      и попадают в отчёт (PR-6), вопросов владельцу не задаём.
    """
    spec = _load_or_derive(task_id, content)
    # PR-3: риск считаем и по запросу владельца, и по уточнённой clarifier цели:
    # модель могла переписать формулировку, и маркер риска остался бы только в исходном тексте.
    spec.high_risk = (
        bool(spec.high_risk)
        or taskspec.is_high_risk(content)
        or taskspec.is_high_risk(spec.goal or "")
    )
    rounds = int(spec.clarify_rounds or 0)

    questions: list[dict] = []
    new_assumptions: list[str] = []

    if autonomous:
        new_assumptions.append(AUTONOMOUS_NOTE)
    elif router is not None and rounds < MAX_CLARIFY_ROUNDS:
        try:
            payload = await _ask_model(router, content, spec)
        except Exception as exc:  # noqa: BLE001 — clarifier недоступен, работаем детерминированно
            logger.warning("clarifier недоступен (%s), гейт работает детерминированно", type(exc).__name__)
            payload = {}
        questions = _normalise_questions(payload.get("questions"))
        new_assumptions += _merge_payload(spec, payload)

    if not autonomous and rounds >= MAX_CLARIFY_ROUNDS:
        # Лимит исчерпан: вопросов владельцу больше не задаём (PR-3). Остаток
        # неопределённости — и незакрытые вопросы прошлых раундов, если они есть, —
        # фиксируется как допущения и попадает в отчёт (PR-6).
        leftover = [q.get("text") for q in questions if isinstance(q, dict) and q.get("text")]
        leftover += [str(q) for q in (spec.open_questions or [])]
        note = (
            f"Лимит уточнений ({MAX_CLARIFY_ROUNDS} раунда) исчерпан: остаток неопределённости "
            "принят как допущения."
        )
        if leftover:
            note += " Незакрытые вопросы: " + "; ".join(dict.fromkeys(leftover)) + "."
        new_assumptions.append(note)
        questions = []

    blocking = [q for q in questions if q.get("blocking")]

    if not autonomous and not blocking and rounds == 0:
        # Решение владельца: вопросы задаются ВСЕГДА, даже если модель решила, что запрос ясен
        # (или clarifier недоступен). Дальше раунды идут только по неснятым пробелам контракта.
        blocking = _fallback_questions()

    if not autonomous and not blocking and not spec.acceptance_criteria and rounds < MAX_CLARIFY_ROUNDS:
        # Контракт неполный, а модель вопросов не задала: спрашиваем детерминированно,
        # иначе задача ушла бы в исполнение без критериев приёмки (PR-6 проверять нечего).
        # После исчерпания лимита раундов вопросов владельцу больше не задаём: остаток
        # превращается в допущения и попадает в отчёт (PR-6).
        blocking = [{
            "id": question_id(taskspec.SLOT_CRITERION_QUESTION),
            "text": taskspec.SLOT_CRITERION_QUESTION,
            "blocking": True,
            "field": "acceptance_criteria",
        }]

    if blocking:
        spec.clarify_rounds = rounds + 1
        # Раунд 2 (п.1.1.1): вопросы хранятся со стабильными id, open_questions —
        # зеркало текстов для валидации missing_fields() и отчёта.
        spec.clarify_questions = blocking
        spec.sync_questions()
        with session_scope() as db:
            task = repo.get_task(db, task_id)
            if task is None:
                raise ValueError(f"unknown task_id={task_id}")
            agent_session = repo.get_session(db, task.session_id)
            repo.upsert_task_spec(
                db, task, spec.model_dump(mode="json"), valid=False, clarify_rounds=spec.clarify_rounds
            )
            repo.set_task_status(db, task, TaskStatus.awaiting_clarification)
            if agent_session is not None:
                repo.append_message(db, agent_session, role="assistant", content=_question_message(blocking))
        logger.info("clarifier: задача %s ждёт ответа (раунд %s)", str(task_id)[:8], spec.clarify_rounds)
        return ClarifyOutcome("awaiting_clarification", spec, blocking)

    if not spec.acceptance_criteria:
        spec.acceptance_criteria = [default_criterion(spec.goal or content)]
        new_assumptions.append("Критерий приёмки не задан: принят детерминированный критерий по умолчанию.")
    if len((spec.goal or "").strip()) < taskspec.MIN_GOAL_CHARS:
        spec.goal = (content or "").strip()[:400]
    spec.assumptions = _dedupe(list(spec.assumptions) + new_assumptions)
    spec.open_questions = []

    status = "approved"
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        if task is None:
            raise ValueError(f"unknown task_id={task_id}")
        agent_session = repo.get_session(db, task.session_id)
        repo.upsert_task_spec(
            db, task, spec.model_dump(mode="json"), valid=spec.is_valid, clarify_rounds=spec.clarify_rounds
        )
        if spec.high_risk and agent_session is not None:
            # Высокорисковые задачи идут через TZ-подтверждение владельца, а не сразу в approved.
            approvals.request_task_tz_approval(db, agent_session, task, spec.to_markdown())
            status = "awaiting_approval"
        elif spec.is_valid:
            repo.set_task_status(db, task, TaskStatus.approved)
        else:
            repo.set_task_status(db, task, TaskStatus.awaiting_clarification)
            status = "awaiting_clarification"
    logger.info("clarifier: задача %s -> %s (критериев: %d)", str(task_id)[:8], status, len(spec.acceptance_criteria))
    return ClarifyOutcome(status, spec, [])


def spec_markdown_block(spec: TaskSpec | None) -> str:
    """Блок TaskSpec для отчёта (delivery note)."""
    if spec is None:
        return ""
    return spec.to_markdown()

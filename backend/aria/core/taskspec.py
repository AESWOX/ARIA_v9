"""core/taskspec.py — PR-3: контракт задачи (TaskSpec) и его валидация.

Критерий «100% уточняет» в коде означает ровно одно: переход задачи в approved
разрешён только если TaskSpec прошёл валидацию обязательных полей —
непустая цель, хотя бы один ПРОВЕРЯЕМЫЙ критерий приёмки и отсутствие открытых
блокирующих вопросов. Это проверка полноты контракта, а не гарантия, что модель
поняла задачу; поэтому критерии приёмки дальше используются аудитом (PR-6) и
сборкой отчёта, а не остаются украшением.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# Сколько раундов уточнения допускаем до перехода на допущения (PR-3).
MAX_CLARIFY_ROUNDS = 3

MIN_GOAL_CHARS = 8
MIN_CRITERION_CHARS = 10

# Раунд 2 (п.1.1.5): детерминированные тексты вопросов для недостающих слотов.
SLOT_GOAL_QUESTION = "Сформулируйте цель задачи: что именно нужно сделать и зачем (минимум 8 символов)?"
SLOT_CRITERION_QUESTION = (
    "Как проверить, что задача выполнена? Опишите критерий приёмки: что и где должно получиться."
)

# Слоты, для которых вопрос детерминированно восстанавливается из недостающего поля.
SLOT_QUESTIONS: dict[str, str] = {
    "goal": SLOT_GOAL_QUESTION,
    "acceptance_criteria": SLOT_CRITERION_QUESTION,
}

# «Проверяемость» формулировки критерия: без измеримого условия это пожелание,
# а не критерий приёмки — такой TaskSpec гейт не проходит.
_VERIFIABLE_RE = re.compile(
    r"(exit\s*0|exit_code|код возврата|0 ошиб|passed|pytest|тест|провер|совпад|содерж"
    r"|включ|равен|равно|не более|не менее|>=|<=|==|файл|путь|возвраща|присутств"
    r"|отсутств|длина|колич|кол-во|секунд|мс\b|все\b|кажд|ровно|соответств"
    r"|\bfile\b|\bpath\b|\bexists\b|\bcontains?\b|\bequals?\b|\bequal to\b"
    r"|\bpass(es|ed)?\b|\btests?\b|\blength\b|\blines?\b|\bnot less than\b"
    r"|\bnot more than\b|\bexactly\b|\bcheck\b|\bverify\b|\bensure\b|\bmake sure\b)",
    re.IGNORECASE,
)

# Маркеры задач высокого риска (§ PR-3: для них включается TZ-approval).
_HIGH_RISK_RE = re.compile(
    r"(deploy|задеплой|production|prod\b|прод\b|rm\s+-rf|drop\s+table|миграци|kubectl|terraform"
    r"|firewall|перезапиши|удали|снеси|ротаци|секрет|ssh\b|docker)",
    re.IGNORECASE,
)


class AcceptanceCriterion(BaseModel):
    """Один критерий приёмки: id + проверяемая формулировка (+ машиночитаемое доказательство).

    ``evidence`` использует PR-6 для детерминированной проверки ДО обращения к модели:
    ``{"tool": "file_write", "field": "path", "equals": "generated/x.txt"}``.
    """

    id: str
    statement: str
    evidence: dict | None = None

    @field_validator("id")
    @classmethod
    def _id_not_empty(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("acceptance criterion id must not be empty")
        return v

    @field_validator("statement")
    @classmethod
    def _statement_verifiable(cls, v: str) -> str:
        v = (v or "").strip()
        if len(v) < MIN_CRITERION_CHARS:
            raise ValueError(f"acceptance criterion too short (min {MIN_CRITERION_CHARS} chars)")
        if not _VERIFIABLE_RE.search(v):
            raise ValueError("acceptance criterion is not verifiable: no measurable condition")
        return v


class TaskSpec(BaseModel):
    """Контракт задачи. Версионируется в task_specs, переживает рестарт процесса."""

    goal: str = ""
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[AcceptanceCriterion] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    # Раунд 2 (п.1.1.1): вопросы со стабильными id ({id, text, field, blocking}) —
    # open_questions остаётся зеркалом текстов для валидации и отчёта.
    clarify_questions: list[dict] = Field(default_factory=list)
    # Раунд 2 (п.1.1.2): структурные записи ответов на уточнения
    # ({question_id, question, answer}) вместо «q1: да» в constraints.
    clarifications: list[dict] = Field(default_factory=list)
    clarify_rounds: int = 0
    high_risk: bool = False

    def sync_questions(self) -> None:
        """Синхронизирует зеркало open_questions с уточнённым списком вопросов."""
        self.open_questions = [str(q.get("text") or "") for q in (self.clarify_questions or []) if q.get("text")]

    @property
    def is_valid(self) -> bool:
        return not self.missing_fields()

    def missing_fields(self) -> list[str]:
        problems: list[str] = []
        if len((self.goal or "").strip()) < MIN_GOAL_CHARS:
            problems.append("goal")
        if not self.acceptance_criteria:
            problems.append("acceptance_criteria")
        if self.open_questions:
            problems.append("open_questions")
        return problems

    def to_markdown(self) -> str:
        lines = [f"## Goal\n\n{self.goal.strip()}", ""]
        if self.constraints:
            lines += ["## Constraints", ""] + [f"- {c}" for c in self.constraints] + [""]
        if self.acceptance_criteria:
            lines += ["## Acceptance criteria", ""] + [
                f"- `{c.id}` {c.statement}" for c in self.acceptance_criteria
            ] + [""]
        if self.assumptions:
            lines += ["## Assumptions", ""] + [f"- {a}" for a in self.assumptions] + [""]
        if self.open_questions:
            lines += ["## Open questions", ""] + [f"- {q}" for q in self.open_questions] + [""]
        return "\n".join(lines).strip() + "\n"


def is_high_risk(text: str) -> bool:
    return bool(_HIGH_RISK_RE.search(text or ""))


def derive_criteria(content: str, limit: int = 5) -> list[AcceptanceCriterion]:
    """Вытаскивает проверяемые критерии прямо из текста запроса (без LLM).

    Используется как детерминированная основа контракта: то, что владелец уже
    сформулировал измеримо, не нужно переспрашивать.
    """
    criteria: list[AcceptanceCriterion] = []
    for chunk in re.split(r"[\n.;!?]+", content or ""):
        sentence = chunk.strip()
        if len(sentence) < MIN_CRITERION_CHARS or not _VERIFIABLE_RE.search(sentence):
            continue
        criteria.append(
            AcceptanceCriterion(id=f"ac-{len(criteria) + 1}", statement=sentence[:300])
        )
        if len(criteria) >= limit:
            break
    return criteria


def slot_question(slot: str) -> str | None:
    """Раунд 2 (п.1.1.5): детерминированный текст вопроса для недостающего слота."""
    return SLOT_QUESTIONS.get(slot)


def default_criterion(goal: str) -> AcceptanceCriterion:
    """Критерий по умолчанию — только для автономного режима и исчерпанного лимита уточнений.

    Он честно описывает то, что можно проверить структурно: выполнение дошло до
    конца без ошибок инструментов (audit получит exit_code/handler error из tool_calls).
    """
    short = (goal or "задача").strip()[:80].replace("\n", " ")
    return AcceptanceCriterion(
        id="ac-default",
        statement=f"по цели «{short}» выполнение завершено: все шаги плана закрыты tool calls с exit_code=0 и без handler error",
    )


def derive_spec(content: str, *, assumptions: list[str] | None = None) -> TaskSpec:
    """Детерминированный TaskSpec из текста запроса (fallback, когда LLM недоступна)."""
    text = (content or "").strip()
    return TaskSpec(
        goal=text[:400],
        acceptance_criteria=derive_criteria(text),
        assumptions=list(assumptions or []),
        high_risk=is_high_risk(text),
    )

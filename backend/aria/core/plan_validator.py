"""core/plan_validator.py — §2.3 PlanStep валидатор + replan policy.

Строгая схема шага плана. Freeform-шаг (без tool_ref и skill_ref) запрещён.
Oracle 3× invalid plan → НАЕБАЛ, escalation.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class PlanStep(BaseModel):
    """Один шаг плана задачи. Обязателен tool_ref или skill_ref."""

    step_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    objective: str
    role: str = "coder"
    tool_ref: str | None = None
    skill_ref: str | None = None
    status: Literal["pending", "in_progress", "done", "failed"] = "pending"
    tool_call_ids: list[uuid.UUID] = []

    @model_validator(mode="after")
    def must_have_tool_or_skill(self) -> "PlanStep":
        if not self.tool_ref and not self.skill_ref:
            raise ValueError(
                f"freeform step forbidden: step '{self.objective[:50]}...' "
                "has neither tool_ref nor skill_ref"
            )
        return self


# Максимум replan-попыток для Oracle до НАЕБАЛ
MAX_REPLAN_ATTEMPTS = 2


def validate_plan(steps: list[dict]) -> list[PlanStep]:
    """Валидирует список словарей как PlanStep[].

    Args:
        steps: сырой список словарей от Oracle.

    Returns:
        list[PlanStep]: валидированные шаги.

    Raises:
        ValueError: если хотя бы один шаг — freeform.
    """
    result: list[PlanStep] = []
    errors: list[str] = []
    for i, raw in enumerate(steps):
        try:
            result.append(PlanStep(**raw))
        except ValueError as e:
            errors.append(f"  step[{i}]: {e}")
    if errors:
        raise ValueError("Plan validation failed:\n" + "\n".join(errors))
    return result


def check_oracle_naebal(
    plan_attempts: int,
    last_error: str | None = None,
) -> IntegrityFlag | None:
    """Проверяет, не НАЕБАЛ ли Oracle.

    Если Oracle 3 раза подряд выдал invalid plan — это НАЕБАЛ от Oracle.

    Args:
        plan_attempts: сколько раз Oracle уже пытался сгенерировать план.
        last_error: текст последней ошибки валидации.

    Returns:
        IntegrityFlag.NAEBAL если plan_attempts > MAX_REPLAN_ATTEMPTS, иначе None.
    """
    if plan_attempts > MAX_REPLAN_ATTEMPTS:
        from aria.core.integrity import IntegrityFlag

        return IntegrityFlag.NAEBAL(
            reason=(
                f"Oracle НАЕБАЛ: {plan_attempts} invalid plans подряд. "
                f"Последняя ошибка: {last_error or 'unknown'}"
            ),
        )
    return None

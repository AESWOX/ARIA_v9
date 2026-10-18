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
    path: str | None = None
    status: Literal["pending", "in_progress", "done", "failed"] = "pending"
    tool_call_ids: list[uuid.UUID] = []
    # PR-4: план — это DAG, а не список. depends_on — id шагов-предшественников,
    # covers — id критериев приёмки TaskSpec, которые закрывает шаг,
    # write_scope — пути/ресурсы, которые шаг пишет (по ним PR-5 решает, что можно
    # исполнять параллельно, а что обязано идти последовательно).
    # П.1.1 ТЗ: None = «зависимости не указаны» (шаг идёт за предыдущим),
    # [] = «шаг независим явно» (разрешена параллель). Различие принципиально:
    # без него старый план из БД исполнялся волной и читал файл до его создания.
    depends_on: list[str] | None = None
    covers: list[str] = []
    write_scope: list[str] = []

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

# PR-4: лимит 7 шагов пересмотрен — у DAG несколько ветвей идут параллельно, поэтому
# 12 узлов дают примерно то же время прогона, что прежние 7 последовательных.
MAX_PLAN_STEPS = 12


_STEP_NS = uuid.UUID("6f1c2e3a-5b7d-4c8e-9a10-2b3c4d5e6f70")


def _uuid_of(step_id: str) -> str:
    """Детерминированный uuid5 для не-UUID id шага (см. _normalize_step_ids)."""
    return str(uuid.uuid5(_STEP_NS, step_id.strip()))


def _normalize_step_ids(steps: list[dict]) -> list[dict]:
    """Модели пишут step_id как «step-1-clone-repo», а схема требует UUID; три такие попытки подряд ломали план.

    Настоящие UUID остаются как есть, любая другая непустая строка превращается в детерминированный uuid5
    (в пределах плана одинаковая строка + номер шага = разные, но стабильные идентификаторы).
    """
    out: list[dict] = []
    # PR-4: depends_on ссылается на те же id, что и step_id, поэтому нормализуем
    # их одним и тем же правилом, иначе валидатор не увидит связей вообще.
    raw_ids: dict[str, str] = {}
    for i, raw in enumerate(steps):
        if not isinstance(raw, dict):
            continue
        sid = raw.get("step_id")
        if isinstance(sid, str) and sid.strip():
            key = sid.strip()
            try:
                resolved = str(uuid.UUID(key))
            except ValueError:
                resolved = str(uuid.uuid5(_STEP_NS, f"{i}:{key}"))
            # depends_on ссылается на первый шаг с таким id; повтор того же id (Oracle
            # иногда отдаёт «step» дважды) получает собственный детерминированный id
            # по индексу — иначе два разных шага склеивались бы в один.
            raw_ids.setdefault(key, resolved)
            raw_ids[f"{key}#{i}"] = resolved

    for i, raw in enumerate(steps):
        item = dict(raw) if isinstance(raw, dict) else raw
        sid = item.get("step_id") if isinstance(item, dict) else None
        if isinstance(sid, str) and sid.strip():
            item["step_id"] = raw_ids.get(
                f"{sid.strip()}#{i}",
                raw_ids.get(sid.strip(), str(uuid.uuid5(_STEP_NS, f"{i}:{sid.strip()}"))),
            )
        if isinstance(item, dict):
            deps = item.get("depends_on")
            if isinstance(deps, list):
                item["depends_on"] = [
                    raw_ids.get(str(d).strip(), str(d).strip()) for d in deps if str(d).strip()
                ]
        out.append(item)
    return out


def find_cycles(steps: list[dict]) -> list[str]:
    """Ищет циклы в depends_on. Возвращает список id шагов, образующих цикл.

    Пустой список — граф ацикличен (в т.ч. когда зависит только от внешних/неизвестных id).
    """
    ids = {str(s.get("step_id")): s for s in steps}
    state: dict[str, int] = {}  # 0 — в обходе, 1 — завершён
    cycles: list[str] = []

    def visit(node: str, stack: list[str]) -> None:
        if state.get(node) == 1:
            return
        if node in stack:
            cycles.extend(stack[stack.index(node):] + [node])
            return
        state[node] = 0
        for dep in (ids[node].get("depends_on") or []):
            dep_id = str(dep)
            if dep_id in ids:
                visit(dep_id, stack + [node])
        state[node] = 1

    for node_id in list(ids):
        if state.get(node_id) is None:
            visit(node_id, [])
    return cycles


def _is_write_without_scope(step: dict) -> bool:
    """П.1.4 ТЗ: шаг, пишущий наружу (shell_execute/file_write) без write_scope,
    не имеет права делить волну с другими шагами."""
    tool = str(step.get("tool_ref") or step.get("skill_ref") or "").strip().lower()
    if tool not in ("shell_execute", "file_write"):
        return False
    return not [w for w in (step.get("write_scope") or []) if str(w).strip()]


def effective_dependencies(steps: list[dict]) -> list[set[int]]:
    """П.1.3 ТЗ: различает «зависимости не указаны» и «независим явно».

    * ``depends_on is None`` — поле не указано: шаг зависит от ПРЕДЫДУЩЕГО шага
      плана (цепочка). Так исполняется любой старый план из БД, где Oracle поле
      не заполнил, и он больше не обгоняет своего предшественника.
    * ``depends_on == []`` — шаг явно объявлен независимым: может идти первым.
    * непустой список — явные предшественники (id вне плана игнорируются).
    """
    ids = {str(s.get("step_id")): i for i, s in enumerate(steps)}
    out: list[set[int]] = []
    for i, step in enumerate(steps):
        raw = step.get("depends_on")
        if raw is None:
            out.append({i - 1} if i > 0 else set())
            continue
        out.append({d for d in ({ids[str(dep)] for dep in raw if str(dep) in ids}) if d != i})
    return out


def topological_waves(steps: list[dict]) -> list[list[int]]:
    """Разбивает план на волны исполнения (PR-5 использует это напрямую).

    Волна N — шаги, все зависимости которых лежат в волнах < N: внутри волны узлы
    независимы и могут идти параллельно. П.1 ТЗ закрывает регрессию: шаг без
    указанных `depends_on` больше НЕ считается независимым — он идёт за предыдущим
    шагом. Параллельность включается только явным `depends_on: []` или явными
    зависимостями. Дополнительно шаг `shell_execute`/`file_write` без `write_scope`
    исполняется в одиночной волне. Ссылки на несуществующие шаги игнорируются,
    цикл не подвешивает разбор — остаток уходит последней волной.
    """
    count = len(steps)
    deps = effective_dependencies(steps)
    unsocial = {i for i in range(count) if _is_write_without_scope(steps[i])}
    pending = set(range(count))
    done: set[int] = set()
    waves: list[list[int]] = []
    while pending:
        ready = sorted(i for i in pending if deps[i] <= done)
        if not ready:
            waves.append(sorted(pending))
            break
        if set(ready) & unsocial:
            # пишущий шаг без write_scope — строго один в своей волне
            ready = [min(ready)]
        waves.append(ready)
        done.update(ready)
        pending -= set(ready)
    return waves


def validate_plan(steps: list[dict], criteria_ids: list[str] | None = None) -> list[PlanStep]:
    """Валидирует список словарей как PlanStep[] и проверяет целостность DAG.

    Args:
        steps: сырой список словарей от Oracle.
        criteria_ids: id критериев приёмки TaskSpec. Если переданы, каждый шаг обязан
            иметь covers, а каждый критерий — быть покрыт хотя бы одним шагом.

    Returns:
        list[PlanStep]: валидированные шаги.

    Raises:
        ValueError: freeform-шаг, цикл в depends_on, превышен MAX_PLAN_STEPS,
            непокрытый критерий или шаг без covers при заданных критериях.
    """
    normalized = _normalize_step_ids(steps)
    result: list[PlanStep] = []
    errors: list[str] = []
    for i, raw in enumerate(normalized):
        try:
            result.append(PlanStep(**raw))
        except ValueError as e:
            errors.append(f"  step[{i}]: {e}")
    if errors:
        raise ValueError("Plan validation failed:\n" + "\n".join(errors))

    if len(result) > MAX_PLAN_STEPS:
        raise ValueError(f"Plan validation failed: {len(result)} шагов > MAX_PLAN_STEPS={MAX_PLAN_STEPS}")

    cycles = find_cycles(normalized)
    if cycles:
        raise ValueError(f"Plan validation failed: цикл в depends_on: {' -> '.join(cycles)}")

    if criteria_ids is not None:
        known = {str(c) for c in criteria_ids}
        covered: set[str] = set()
        for step in result:
            if not step.covers:
                raise ValueError(
                    f"Plan validation failed: step {step.step_id} не объявляет covers[] — "
                    "каждый шаг обязан быть привязан к критерию приёмки"
                )
            unknown = [c for c in step.covers if c not in known]
            if unknown:
                raise ValueError(
                    f"Plan validation failed: step {step.step_id} покрывает неизвестные критерии {unknown}"
                )
            covered.update(step.covers)
        missing = sorted(known - covered)
        if missing:
            raise ValueError(f"Plan validation failed: критерии не покрыты ни одним шагом: {missing}")

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

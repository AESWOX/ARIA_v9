"""core/plan_validator.py — §2.3 PlanStep валидатор + replan policy.

Строгая схема шага плана. Freeform-шаг (без tool_ref и skill_ref) запрещён.
Oracle 3× invalid plan → НАЕБАЛ, escalation.
"""

from __future__ import annotations

import os
import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aria.core.integrity import IntegrityFlag
from aria.tools.validators import command_rejection_reason, is_windows_command, validate_tool_input


class PlanStep(BaseModel):
    """Один шаг плана задачи. Обязателен tool_ref или skill_ref."""

    # D-24: аргументы инструмента могут лежать в input (dict), а модель часто кладёт их
    # в корень шага. extra="allow" сохраняет такие ключи до PlanStep.model_dump.
    model_config = ConfigDict(extra="allow")

    step_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    objective: str
    role: str = "coder"
    tool_ref: str | None = None
    skill_ref: str | None = None
    path: str | None = None
    command: str | None = None
    url: str | None = None
    input: dict[str, Any] | None = None
    status: Literal["pending", "in_progress", "done", "failed", "skipped"] = "pending"
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


class WebFetchUrlAmbiguous(ValueError):
    """objective содержит несколько разных URL, а step.url/input пусты — выбрать нельзя."""


_URL_RE = re.compile(r"https?://\S+")
_URL_TRAIL = ".,;:)\"\u00bb"


def _is_http_url(url: str) -> bool:
    return url.startswith(("http://", "https://"))


def resolve_web_fetch_url(step: dict) -> str | None:
    """Находит URL для web_fetch: step.url -> input["url"] -> первый https?:// из objective.

    Только схемы http/https (R6/SSRF: ftp://, file:// и т.п. не принимаются).
    Возвращает None, если URL нет нигде. Бросает WebFetchUrlAmbiguous, если objective
    содержит несколько разных http(s)-URL и их нельзя выбрать однозначно.
    """
    url = step.get("url")
    if url is not None and str(url).strip():
        url = str(url).strip()
        return url if _is_http_url(url) else None

    inp = step.get("input")
    if isinstance(inp, dict):
        url = inp.get("url")
        if url is not None and str(url).strip():
            url = str(url).strip()
            return url if _is_http_url(url) else None

    objective = str(step.get("objective") or "")
    urls = {u.rstrip(_URL_TRAIL) for u in _URL_RE.findall(objective)}
    if len(urls) > 1:
        raise WebFetchUrlAmbiguous(
            f"objective содержит несколько разных URL, а step.url/input пусты: {sorted(urls)}"
        )
    return urls.pop() if urls else None


# D-24: поля шага, которые никогда не являются аргументами инструмента.
# status исключён всегда: у log_decision есть одноимённый аргумент, и служебный
# Literal шага не должен попадать в handler (аргумент — только через input).
_STRUCTURAL_STEP_KEYS = frozenset({
    "step_id", "objective", "role", "tool_ref", "skill_ref", "depends_on", "covers",
    "write_scope", "tool_call_ids", "index", "status", "input", "input_json",
})
# Явные ветви executor.py: у них свои источники аргументов, step_tool_input им не нужен.
_EXPLICIT_BRANCH_TOOLS = frozenset({
    "llm_task", "file_write", "shell_execute", "file_read", "file_search", "web_search", "web_fetch",
})


def step_tool_input(step: dict, spec=None) -> dict:
    """Аргументы инструмента из шага плана (D-24).

    Порядок: корневые ключи шага, объявленные в схеме инструмента → ``input_json`` →
    ``input`` (явный input побеждает). Служебные поля шага не копируются никогда.
    Без spec читаются только ``input``/``input_json``.
    """
    props: dict = {}
    if spec is not None:
        props = (getattr(spec, "input_schema", None) or {}).get("properties", {}) or {}
    merged: dict = {}
    for key, value in (step or {}).items():
        if key in _STRUCTURAL_STEP_KEYS or key not in props or value is None:
            continue
        merged[key] = value
    for key in ("input_json", "input"):
        extra = (step or {}).get(key)
        if isinstance(extra, dict):
            merged.update(extra)
    return merged


# Максимум replan-попыток для Oracle до НАЕБАЛ.
# R5: один replan (первая генерация + одна повторная с feedback), затем НАЕБАЛ.
MAX_REPLAN_ATTEMPTS = 1

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


def _ancestors(deps: list[set[int]], idx: int) -> set[int]:
    """Транзитивное замыкание зависимостей шага idx (индексы всех предков)."""
    seen: set[int] = set()
    stack = list(deps[idx])
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(deps[n])
    return seen


def _normalize_abs(path: str, root: str) -> str:
    p = os.path.normcase(os.path.normpath(path))
    if not os.path.isabs(p):
        p = os.path.normcase(os.path.abspath(os.path.join(root, p)))
    return p


def _in_sandbox(path: str, root: str) -> bool:
    p = _normalize_abs(path, root)
    return p == root or p.startswith(root + os.sep)


def validate_plan_paths(steps: list[dict], sandbox_root: str | None) -> None:
    """R4: файловые шаги плана не должны выходить за sandbox и читать несуществующее.

    Только инструменты ``file_*``. ``sandbox_root`` пуст/None — проверка явно пропущена
    (вызывающий решает, когда есть что проверять). Vault-инструменты не проверяются.

    * ``file_read`` допустим, если файл существует на диске ИЛИ его пишет шаг-предок
      (``file_write`` с тем же ``path`` или ``write_scope``).
    * ``file_write``: путь внутри sandbox, новый файл допустим.
    * ``file_search``/``file_grep``: glob не требует существования; только запрет выхода
      за sandbox (``..``, абсолютные пути вне root).

    Пути нормализуются (регистр, слэши Windows). Нарушение — ValueError с причиной.
    """
    if not sandbox_root:
        return
    root = _normalize_abs(str(sandbox_root), str(sandbox_root))
    deps = effective_dependencies(steps)
    ancestors = [_ancestors(deps, i) for i in range(len(steps))]

    def written_by_ancestor(idx: int, path: str) -> bool:
        for a in ancestors[idx]:
            s = steps[a]
            if str(s.get("tool_ref") or "").strip().lower() != "file_write":
                continue
            targets = [s.get("path"), *list(s.get("write_scope") or [])]
            for t in targets:
                if not t:
                    continue
                tn = _normalize_abs(str(t), root)
                if path == tn or path.startswith(tn + os.sep):
                    return True
        return False

    for i, step in enumerate(steps):
        tool = str(step.get("tool_ref") or "").strip().lower()
        if tool not in ("file_read", "file_write", "file_search", "file_grep"):
            continue

        if tool == "file_write":
            path = step.get("path")
            if path and not _in_sandbox(str(path), root):
                raise ValueError(f"Plan validation failed: file_write path outside sandbox: {path!r}")
            continue

        if tool in ("file_search", "file_grep"):
            glob_pat = step.get("glob") or step.get("path") or ""
            if glob_pat and not _in_sandbox(str(glob_pat), root):
                raise ValueError(f"Plan validation failed: {tool} glob escapes sandbox: {glob_pat!r}")
            continue

        # file_read
        path = step.get("path")
        if not path:
            continue
        pn = _normalize_abs(str(path), root)
        if os.path.exists(pn):
            continue
        if not written_by_ancestor(i, pn):
            raise ValueError(f"Plan validation failed: file_read of non-existent file: {path!r}")


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
    # A4 (19.4a): web_fetch без url (ни в step.url, ни в input, ни в objective) —
    # заведомо неисполнимый шаг; отклоняем с причиной для feedback в replan (R5).
    for i, raw in enumerate(normalized):
        if not isinstance(raw, dict):
            continue
        if str(raw.get("tool_ref") or "").strip().lower() != "web_fetch":
            continue
        try:
            resolved = resolve_web_fetch_url(raw)
        except WebFetchUrlAmbiguous as e:
            raise ValueError(f"Plan validation failed: step[{i}] web_fetch: {e}")
        if resolved is None:
            raise ValueError(
                f"Plan validation failed: step[{i}] web_fetch: url не указан ни в поле url, "
                "ни в input, ни в objective. Укажи полный http(s) URL в поле url шага — "
                "шаг web_fetch без URL не создаётся."
            )
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

    # R3(a): команда в плане обязана быть выполнимой на Windows — иначе шаг заведомо
    # не исполнится, и повтор с теми же входными данными ничего не изменит.
    for step in result:
        if step.command is not None and not is_windows_command(step.command):
            raise ValueError(
                f"Plan validation failed: step {step.step_id} command is not runnable on Windows "
                f"({command_rejection_reason(step.command)}): {step.command!r}"
            )

    # D-24: аргументы else-ветки проверяются до исполнения. Иначе неполный план
    # доходил до executor, падал кодом вне R6 и уходил в платный ZABYL-replan.
    # Здесь ошибка сразу уходит в feedback единственного replan (R5).
    from aria.tools.registry import get_tool

    for idx, step in enumerate(result):
        tool_ref = str(step.tool_ref or "").strip().lower()
        if not tool_ref or step.skill_ref or tool_ref in _EXPLICIT_BRANCH_TOOLS:
            continue
        try:
            spec = get_tool(tool_ref)
        except KeyError:
            continue  # неизвестный/алиасный tool_ref — решение за executor
        if spec.handler is None:
            continue
        merged = step_tool_input(step.model_dump(mode="json"), spec)
        arg_errors = validate_tool_input(spec.input_schema, merged)
        if arg_errors:
            raise ValueError(
                f"Plan validation failed: step[{idx}] {tool_ref}: " + "; ".join(arg_errors)
            )

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
        return IntegrityFlag.NAEBAL(
            reason=(
                f"Oracle НАЕБАЛ: {plan_attempts} invalid plans подряд. "
                f"Последняя ошибка: {last_error or 'unknown'}"
            ),
        )
    return None

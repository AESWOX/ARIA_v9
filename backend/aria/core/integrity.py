"""core/integrity.py — §4 Integrity-аудит: чистые функции детекторов.

НАЕБАЛ / ЗАБЫЛ / ПРОЕБАЛ детекторы.
Только чистые функции, без LLM.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ═══════════════════════════════════════════════════════════════════
# IntegrityFlag — результат работы детектора
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class IntegrityFlag:
    """Один integrity-вердикт от детектора.

    - naebal: claimed ≠ actual → hard fail, retry запрещён
    - zabyl:  план покрыт не полностью → fail
    - proebal: честная ошибка → retry (из correctness audit, не отсюда)
    """

    kind: str = "naebal"  # 'naebal' | 'zabyl'
    reason: str = ""
    tool_call_id: str | None = None
    missing_steps: list[str] = field(default_factory=list)

    @classmethod
    def NAEBAL(cls, reason: str, tool_call_id: str | None = None) -> "IntegrityFlag":
        return cls(kind="naebal", reason=reason, tool_call_id=tool_call_id)

    @classmethod
    def ZABYL(cls, reason: str, missing_steps: list[str] | None = None) -> "IntegrityFlag":
        return cls(kind="zabyl", reason=reason, missing_steps=missing_steps or [])


# ═══════════════════════════════════════════════════════════════════
# Чистые функции — основа детекторов
# ═══════════════════════════════════════════════════════════════════


def file_content_hash(path: Path) -> str:
    """sha256 содержимого файла. Используется до и после write/patch.

    Args:
        path: абсолютный путь к файлу.

    Returns:
        hex digest строки, или '' если файл не существует.
    """
    if not path.exists() or not path.is_file():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def assert_file_changed(tc_input: dict, tc_output: dict) -> bool:
    """True только если hash_before != hash_after. Иначе НАЕБАЛ.

    Проверяет поле hash_before / hash_after в input_json и output_json
    tool_call. Если hash_before отсутствует — считаем, что проверка
    невозможна, возвращаем False (НАЕБАЛ по умолчанию).

    Args:
        tc_input: input_json из ToolCall.
        tc_output: output_json из ToolCall.

    Returns:
        False — если файл не изменился или хеш недоступен.
    """
    h_before = tc_input.get("hash_before")
    h_after = tc_output.get("hash_after")
    if not h_before or not h_after:
        return False
    return h_before != h_after


def get_exit_code(tc_output: dict) -> int | None:
    """Из output_json / stderr. None = артефакт отсутствует → НАЕБАЛ.

    Проверяет:
    1. output_json.get('exit_code')
    2. output_json.get('returncode')

    Returns:
        int exit code, или None если артефакт отсутствует.
    """
    for key in ("exit_code", "returncode"):
        val = tc_output.get(key)
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
    return None


def has_junit_artifact(tc_output: dict, junit_path: str | None = None) -> bool:
    """True только если junit-файл реально существует и не пустой.

    Args:
        tc_output: output_json из ToolCall.
        junit_path: опциональный явный путь к junit-файлу.

    Returns:
        False если:
        - junit_path не указан и output_json не содержит пути к junit
        - файл не существует
        - файл пустой (0 байт)
    """
    if junit_path:
        p = Path(junit_path)
    else:
        junit_path = tc_output.get("junit_path") or tc_output.get("junit_file") or ""
        if not junit_path:
            return False
        p = Path(junit_path)

    return p.exists() and p.is_file() and p.stat().st_size > 0


def extract_covered_step_ids(
    plan_json: list[dict],
    tool_calls: list[dict],
) -> set[str]:
    """Строго по tool_call_ids и step_id. Semantic match запрещён.

    Для каждого шага плана (PlanStep) проверяет: есть ли у шага
    tool_call_ids, и присутствуют ли эти id в списке tool_calls.

    Args:
        plan_json: список словарей PlanStep.
        tool_calls: список словарей ToolCall (должны иметь 'id').

    Returns:
        set[str] — step_id шагов, которые считаются покрытыми.
    """
    tc_ids = {tc.get("id") for tc in tool_calls if tc.get("id")}
    covered: set[str] = set()

    for step in plan_json:
        sid = step.get("step_id") or step.get("step_id_str", "")
        if not sid:
            continue
        step_tc_ids = set(step.get("tool_call_ids", []))
        if step_tc_ids and step_tc_ids.intersection(tc_ids):
            covered.add(str(sid))

    return covered


# ═══════════════════════════════════════════════════════════════════
# Детекторы
# ═══════════════════════════════════════════════════════════════════


def detect_naebal(tool_calls: list[dict]) -> list[IntegrityFlag]:
    """Сверяет claimed result из output_json с фактическим состоянием.

    Args:
        tool_calls: список словарей ToolCall.

    Returns:
        list[IntegrityFlag]: пустой список если всё чисто.
    """
    flags: list[IntegrityFlag] = []

    for tc in tool_calls:
        tool_name = tc.get("tool_name", "")
        inp = tc.get("input_json", {}) or {}
        out = tc.get("output_json", {}) or {}
        tc_id = tc.get("id")

        # 1. terminal с pytest
        if tool_name in ("terminal",) and "pytest" in json.dumps(inp):
            exit_code = get_exit_code(out)
            junit = has_junit_artifact(out)
            claimed_pass = "passed" in json.dumps(out)

            if claimed_pass and (exit_code != 0 or not junit):
                flags.append(IntegrityFlag.NAEBAL(
                    reason=f"заявлен passed, реальный exit_code={exit_code}, junit={junit}",
                    tool_call_id=str(tc_id) if tc_id else None,
                ))

        # 2. write_file / patch — файл должен измениться
        if tool_name in ("write_file", "patch"):
            if not assert_file_changed(inp, out):
                flags.append(IntegrityFlag.NAEBAL(
                    reason="write_file/patch выполнен, но hash_before == hash_after",
                    tool_call_id=str(tc_id) if tc_id else None,
                ))

    return flags


def detect_zabyl(
    plan_json: list[dict],
    tool_calls: list[dict],
) -> list[IntegrityFlag]:
    """Сверяет план (чек-лист по step_id) с выполненными tool_calls.

    Semantic matching исключён. Только строго по step_id / tool_call_ids.

    Args:
        plan_json: список PlanStep.
        tool_calls: список ToolCall.

    Returns:
        list[IntegrityFlag]: пустой если всё покрыто.
    """
    if not plan_json:
        return []

    covered = extract_covered_step_ids(plan_json, tool_calls)
    missing_steps = [
        s.get("step_id") or s.get("step_id_str", "")
        for s in plan_json
        if (s.get("step_id") or s.get("step_id_str", "")) not in covered
    ]

    if missing_steps:
        return [IntegrityFlag.ZABYL(
            reason=f"{len(missing_steps)}/{len(plan_json)} шагов не закрыты tool_calls",
            missing_steps=missing_steps,
        )]

    return []


def run_integrity_audit(
    tool_calls: list[dict],
    plan_json: list[dict] | None = None,
) -> list[IntegrityFlag]:
    """Запускает все integrity-детекторы.

    Args:
        tool_calls: список ToolCall.
        plan_json: опциональный список PlanStep для ЗАБЫЛ-детектора.

    Returns:
        list[IntegrityFlag]: все найденные нарушения.
    """
    flags: list[IntegrityFlag] = []
    flags.extend(detect_naebal(tool_calls))
    if plan_json is not None:
        flags.extend(detect_zabyl(plan_json, tool_calls))
    return flags

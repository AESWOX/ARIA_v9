"""core/criteria_audit.py — PR-6: проверка критериев приёмки.

Порядок намеренно такой: сначала детерминированные проверки по фактам tool_calls
(что реально записано, с каким exit_code, существует ли артефакт), и только то, что
структурно проверить нельзя, отдаётся модели. Иначе аудит превращается в «модель
согласилась сама с собой», а критерий приёмки — в украшение отчёта.
"""
from __future__ import annotations

import json
import re

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_UNKNOWN = "unknown"

# Путь в формулировке критерия: generated/report.md, src/app.py, /tmp/out.json
_PATH_RE = re.compile(r"[\w./-]*[\w-]+\.(?:md|txt|json|csv|py|log|ya?ml)\b")


def _criterion_dict(criterion) -> dict:
    if isinstance(criterion, dict):
        return criterion
    if hasattr(criterion, "model_dump"):
        return criterion.model_dump(mode="json")
    return {"id": str(criterion), "statement": ""}


def _output_blob(tool_call: dict) -> str:
    try:
        return json.dumps(tool_call.get("output_json") or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(tool_call.get("output_json"))


def check_criteria(criteria, tool_calls: list[dict]) -> dict[str, dict]:
    """Детерминированная проверка критериев по фактам tool_calls.

    Args:
        criteria: список критериев приёмки (dict или pydantic-модель).
        tool_calls: tool_calls_raw из Stage 3.

    Returns:
        {criterion_id: {"status": pass|fail|unknown, "note": str}}.
        ``unknown`` — структурных признаков нет, критерий уходит к модели.
    """
    result: dict[str, dict] = {}
    for raw in criteria or []:
        criterion = _criterion_dict(raw)
        cid = str(criterion.get("id") or "")
        if not cid:
            continue
        evidence = criterion.get("evidence") or {}
        status, note = STATUS_UNKNOWN, "нет структурного признака — проверяет модель"

        if evidence:
            tool = evidence.get("tool")
            field = evidence.get("field")
            expected = evidence.get("equals")
            matched = [
                tc for tc in tool_calls
                if (not tool or tc.get("tool_name") == tool) and tc.get("status") == "ok"
            ]
            if field:
                ok = any(str((tc.get("output_json") or {}).get(field)) == str(expected) for tc in matched)
            else:
                ok = bool(matched)
            status = STATUS_PASS if ok else STATUS_FAIL
            note = f"evidence {tool or 'любой тул'}.{field or '-'}={expected!r}: {'совпало' if ok else 'не найдено'}"

        if status == STATUS_UNKNOWN:
            paths = _PATH_RE.findall(str(criterion.get("statement") or ""))
            if paths:
                ok = any(
                    tc.get("status") == "ok" and any(p in _output_blob(tc) for p in paths)
                    for tc in tool_calls
                )
                if ok:
                    status, note = STATUS_PASS, f"артефакт {paths[0]} подтверждён выводом инструмента"
                elif tool_calls:
                    status, note = STATUS_FAIL, f"артефакт {paths[0]} не найден в результатах инструментов"

        if status == STATUS_UNKNOWN and tool_calls:
            failed = [tc for tc in tool_calls if tc.get("status") != "ok"]
            if failed:
                status, note = STATUS_FAIL, f"{len(failed)} tool call(s) завершились ошибкой"
            else:
                # Критерий без артефакта и без evidence: подтверждаем только то,
                # что все инструменты отработали без ошибок (exit_code=0 в выводе).
                bad_exit = [
                    tc for tc in tool_calls
                    if isinstance(tc.get("output_json"), dict)
                    and tc["output_json"].get("exit_code") not in (None, 0)
                ]
                if bad_exit:
                    status, note = STATUS_FAIL, "есть ненулевой exit_code в результатах инструментов"
                else:
                    status = STATUS_UNKNOWN

        result[cid] = {"status": status, "note": note}
    return result


def merge_statuses(deterministic: dict[str, dict], from_model: dict[str, dict]) -> dict[str, dict]:
    """Структурный fail не перекрывается мнением модели; pass модели уточняет unknown."""
    merged = dict(deterministic)
    for cid, data in (from_model or {}).items():
        current = merged.get(cid)
        if current and current.get("status") == STATUS_FAIL:
            continue
        if current and current.get("status") == STATUS_PASS and data.get("status") != STATUS_FAIL:
            continue
        merged[cid] = data
    return merged


def failed_criteria(statuses: dict[str, dict]) -> list[str]:
    return sorted(cid for cid, data in (statuses or {}).items() if data.get("status") == STATUS_FAIL)


def criteria_summary(statuses: dict[str, dict]) -> str:
    """Строка для отчёта: ac-1=pass, ac-2=fail."""
    if not statuses:
        return ""
    return ", ".join(f"{cid}={data.get('status')}" for cid, data in sorted(statuses.items()))

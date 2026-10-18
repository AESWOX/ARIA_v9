"""clarify_eval.py — П.4.3 ТЗ: метрики триажа уточнений на размеченном наборе.

Считает на `tests/fixtures/clarify_eval.jsonl`:
  * recall_questions_on_vague  — доля расплывчатых запросов, на которых система задаст вопросы;
  * extra_questions_on_clear   — доля лишних вопросов на ясных запросах;
  * missed_mandatory_slots     — доля пропущенных обязательных слотов.

CI-режим (`--ci`): скриптовый fake-провайдер — только детерминированный триаж
(`clarifier.gate_required` + вывод критериев). Живой режим (`--live`) гоняет
clarifier через LLM и требует ключей; в CI не запускается.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from aria.core import clarifier  # noqa: E402
from aria.core import taskspec  # noqa: E402

FIXTURE = BACKEND / "tests" / "fixtures" / "clarify_eval.jsonl"


def _predict_ask(text: str, mode: str = "agent") -> bool:
    """Детерминированный триаж: вопрос задаётся, если гейт прошёл, но критериев нет.

    Гейт отвечает «нужна ли формализация» (маркер задачи / plan-режим), а
    `derive_criteria` — «есть ли уже проверяемая формулировка». Вопрос уместен,
    когда формализация нужна, а измеримого критерия владелец не дал.
    """
    if not clarifier.gate_required(text, mode):
        return False
    return not taskspec.derive_criteria(text)


def _slots_missing(text: str) -> list[str]:
    """Обязательные слоты, которых не хватает в запросе (детерминированно)."""
    missing: list[str] = []
    if not taskspec.derive_criteria(text):
        missing.append("acceptance_criteria")
    if len((text or "").strip()) < taskspec.MIN_GOAL_CHARS:
        missing.append("goal")
    return missing


def evaluate(path: Path = FIXTURE) -> dict[str, float]:
    items = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    vague = [i for i in items if i.get("vague")]
    clear = [i for i in items if not i.get("vague")]

    asked_on_vague = sum(1 for i in vague if _predict_ask(i["text"]))
    asked_on_clear = sum(1 for i in clear if _predict_ask(i["text"]))

    total_required = 0
    total_missed = 0
    for item in items:
        required = list(item.get("must_slots") or [])
        total_required += len(required)
        missing = set(_slots_missing(item["text"]))
        total_missed += sum(1 for slot in required if slot in missing)

    return {
        "items": float(len(items)),
        "vague": float(len(vague)),
        "clear": float(len(clear)),
        "recall_questions_on_vague": round(asked_on_vague / len(vague), 4) if vague else 0.0,
        "extra_questions_on_clear": round(asked_on_clear / len(clear), 4) if clear else 0.0,
        "missed_mandatory_slots": round(total_missed / total_required, 4) if total_required else 0.0,
    }


def evaluate_detailed(path: Path = FIXTURE) -> dict:
    """Раунд 2 (п.4.1): метрики + разбор — какие ясные запросы получили лишние вопросы
    и какие обязательные слоты пропущены (для отчёта и правки derive_criteria/gate)."""
    items = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    clear = [i for i in items if not i.get("vague")]
    extra = [
        {"lang": i.get("lang"), "text": i["text"], "reason": "gate passed, no verifiable criteria derived"}
        for i in clear if _predict_ask(i["text"])
    ]
    missed = []
    for item in items:
        required = list(item.get("must_slots") or [])
        missing = set(_slots_missing(item["text"]))
        hit = [slot for slot in required if slot in missing]
        if hit:
            missed.append({"lang": item.get("lang"), "text": item["text"], "missed_slots": hit})
    return {"metrics": evaluate(path), "extra_questions_on_clear_list": extra, "missed_slots_list": missed}


def main() -> int:
    parser = argparse.ArgumentParser(description="clarify triage eval (П.4.3 ТЗ)")
    parser.add_argument("--ci", action="store_true", help="скриптовый режим, без сети (по умолчанию)")
    parser.add_argument("--live", action="store_true", help="живой режим через LLM (нужны ключи)")
    parser.add_argument("--fixture", default=str(FIXTURE))
    parser.add_argument("--detailed", action="store_true", help="раунд 2 п.4.1: выводить разбор лишних вопросов и пропусков")
    args = parser.parse_args()

    if args.live:
        print(json.dumps({"error": "live mode requires provider keys; run outside CI"}, ensure_ascii=False))
        # живой режим отдельно: в CI он сознательно не запускается
        return 0

    if args.detailed:
        print(json.dumps(evaluate_detailed(Path(args.fixture)), ensure_ascii=False, indent=2))
        return 0

    metrics = evaluate(Path(args.fixture))
    print(json.dumps(metrics, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

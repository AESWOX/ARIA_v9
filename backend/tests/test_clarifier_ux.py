"""Раунд 2 (UX): кларифаер не показывает внутренние имена полей и не блокирует по умолчанию."""
from __future__ import annotations

from pathlib import Path

from aria.core import clarifier


def test_question_message_does_not_leak_internal_field_names():
    msg = clarifier._question_message([
        {"id": "q-1", "text": "Нужен ли только статичный меш?", "field": "acceptance_criteria", "blocking": True},
        {"id": "q-2", "text": "Нужны ли аддоны?", "field": "goal", "blocking": True},
    ])
    assert "поле" not in msg
    assert "acceptance_criteria" not in msg and "(goal" not in msg
    assert "1. Нужен ли только статичный меш?" in msg and "2. Нужны ли аддоны?" in msg


def test_question_without_blocking_flag_is_blocking():
    """Решение владельца 19.10: вопросы блокирующие по умолчанию (без ответа или «Принять допущения» не стартуем)."""
    out = clarifier._normalise_questions([{"text": "Какой стиль?"}])
    assert out and out[0]["blocking"] is True


def test_explicit_blocking_flag_is_respected():
    out = clarifier._normalise_questions([{"text": "Какой квартал?", "blocking": True}])
    assert out[0]["blocking"] is True


def test_plain_string_question_stays_blocking():
    """Обратная совместимость: строка вместо объекта — как раньше, блокирующий вопрос."""
    out = clarifier._normalise_questions(["Какой квартал?"])
    assert out[0]["blocking"] is True


def test_clarifier_prompt_demands_questions_with_five_options():
    prompt = (Path(clarifier.__file__).parent.parent / "prompts" / "clarifier.md").read_text(encoding="utf-8")
    assert "ВСЕГДА" in prompt and "5 вариантов" in prompt and "recommended" in prompt
    assert "assumptions" in prompt

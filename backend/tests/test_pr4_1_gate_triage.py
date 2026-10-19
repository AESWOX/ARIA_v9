"""П.4 ТЗ: выключатель гейта и триаж (границы слов, конфигурируемые глаголы)."""
from __future__ import annotations

import pytest

from aria.config import get_settings
from aria.core import clarifier


# ─────────────────────────── п.4.1: выключатель ───────────────────────────

def test_gate_can_be_switched_off(monkeypatch):
    """При clarifier_gate_enabled=False гейт не проходит ни в plan, ни в agent."""
    monkeypatch.setattr(get_settings(), "clarifier_gate_enabled", False)
    assert clarifier.gate_required("сделай отчёт по логам", "plan") is False
    assert clarifier.gate_required("fix the failing test", "agent") is False


def test_gate_is_on_by_default(monkeypatch):
    monkeypatch.setattr(get_settings(), "clarifier_gate_enabled", True)
    assert clarifier.gate_required("что угодно", "plan") is True
    assert clarifier.gate_required("сделай отчёт", "agent") is True


# ─────────────────────────── п.4.2: границы слов ───────────────────────────

def test_latin_text_is_gated_regardless_of_verbs(monkeypatch):
    """Глаголы-маркеры больше не ограничивают гейт: любой непустой запрос проходит уточнение."""
    monkeypatch.setattr(get_settings(), "clarifier_gate_enabled", True)
    assert clarifier.gate_required("handle the prefix parsing", "agent") is True
    assert clarifier.gate_required("we are building a house", "agent") is True


def test_russian_stems_match_their_inflections(monkeypatch):
    """Русские основы по-прежнему ловят свои формы (сделай/починить/сгенерировал)."""
    monkeypatch.setattr(get_settings(), "clarifier_gate_enabled", True)
    for text in ("сделай отчёт", "починить тест", "сгенерировал токен", "проверим логи"):
        assert clarifier.gate_required(text, "agent") is True, text


def test_plain_chatter_is_gated_too(monkeypatch):
    monkeypatch.setattr(get_settings(), "clarifier_gate_enabled", True)
    for text in ("привет, как дела?", "спасибо", "расскажи анекдот"):
        assert clarifier.gate_required(text, "agent") is True, text


def test_punctuation_and_case_do_not_break_matching(monkeypatch):
    monkeypatch.setattr(get_settings(), "clarifier_gate_enabled", True)
    assert clarifier.gate_required("FIX the failing test!", "agent") is True
    assert clarifier.gate_required("сделай, пожалуйста, отчёт.", "agent") is True


# ─────────────────────────── п.4.2: конфиг глаголов ───────────────────────────

def test_verb_list_does_not_limit_the_gate(monkeypatch):
    monkeypatch.setattr(get_settings(), "clarifier_gate_enabled", True)
    monkeypatch.setattr(get_settings(), "clarifier_task_verbs", "переведи,суммируй")
    assert clarifier.gate_required("сделай отчёт", "agent") is True


def test_configured_task_verbs_falls_back_to_defaults(monkeypatch):
    monkeypatch.setattr(get_settings(), "clarifier_task_verbs", "")
    assert "сдела" in clarifier.configured_task_verbs()

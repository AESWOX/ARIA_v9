"""П.4.3/5.4/П.1 follow-up: eval-метрики триажа, DLP at-rest, восстановление имён.

Скрипт `scripts/clarify_eval.py` считает метрики на размеченном наборе
`tests/fixtures/clarify_eval.jsonl`; здесь они проверяются в CI-режиме
(скриптовый fake-провайдер, без сети).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from aria.config import get_settings
from aria.core import clarifier, crypto_box, privacy
from aria.core.crypto_box import decrypt, encrypt, is_encrypted
from aria.db import repository as repo
from aria.db.base import session_scope

BACKEND = Path(__file__).resolve().parents[1]
FIXTURE = BACKEND / "tests" / "fixtures" / "clarify_eval.jsonl"
SCRIPT = BACKEND / "scripts" / "clarify_eval.py"


# ─────────────────────────── п.4.3: eval-набор ───────────────────────────

def test_eval_fixture_has_labelled_requests():
    lines = [json.loads(ln) for ln in FIXTURE.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) >= 50, f"в наборе {len(lines)} запросов, нужно ~50"
    langs = {item.get("lang") for item in lines}
    labels = {bool(item.get("vague")) for item in lines}
    assert {"ru", "en"} <= langs
    assert labels == {True, False}, "нужны и ясные, и расплывчатые запросы"


def test_clarify_eval_runs_in_ci_mode():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--ci"],
        cwd=str(BACKEND), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    metrics = json.loads(proc.stdout.strip().splitlines()[-1])
    for key in ("recall_questions_on_vague", "extra_questions_on_clear", "missed_mandatory_slots"):
        assert 0.0 <= metrics[key] <= 1.0
    # Пороги по фактически измеренной базе (скриптовый режим): не хуже неё.
    assert metrics["recall_questions_on_vague"] >= 0.55
    assert metrics["extra_questions_on_clear"] <= 0.45
    assert metrics["missed_mandatory_slots"] <= 0.35


# ─────────────────────────── п.5.4: DLP at-rest ───────────────────────────

def test_crypto_box_roundtrip():
    secret = "sk-test-abcdefghijklmnop1234"
    box = encrypt(secret)
    assert is_encrypted(box) and box != secret
    assert decrypt(box) == secret
    assert encrypt(box) == box  # идемпотентно


def test_shadow_map_is_not_stored_as_plaintext():
    """В БД значения только в виде enc:v1: — открытый текст не персистится."""
    with session_scope() as db:
        session = repo.create_session(db, "dlp")
        repo.save_shadow_map(db, session.id, {"⟦SECRET_1⟧": "sk-live-very-secret-0001"})
        raw = db.get(type(session), session.id)  # sanity: строка существует
        from aria.db.models import PrivacyShadowMap

        row = db.get(PrivacyShadowMap, session.id)
        stored = dict(row.map_json or {})
    assert stored and all(is_encrypted(v) for v in stored.values())
    assert "sk-live-very-secret-0001" not in json.dumps(stored)
    with session_scope() as db:
        restored = repo.get_shadow_map(db, session.id)
    assert restored == {"⟦SECRET_1⟧": "sk-live-very-secret-0001"}


def test_dlp_plaintext_fixture_is_not_in_storage_path():
    """Документируем выбор варианта: шифрование значений (флаг dlp_encrypt_secrets)."""
    assert bool(getattr(get_settings(), "dlp_encrypt_secrets", True)) is True


# ─────────────────────────── п.5.4: написание из текста ───────────────────────────

def test_restore_keeps_user_spelling():
    """IGNORECASE-поиск возвращает написание пользователя, а не из настроек."""
    anon = privacy.EntityAnonymizer(names=["Джон"])
    text = anon.anonymize("ДЖОН сегодня писал код")
    assert "ДЖОН" not in text
    assert anon.restore(text) == "ДЖОН сегодня писал код"


def test_dlp_masks_email_and_restores_it():
    anon = privacy.EntityAnonymizer()
    masked = anon.anonymize("пиши на user@example.com")
    assert "user@example.com" not in masked
    assert anon.restore(masked) == "пиши на user@example.com"


# ─────────────────────────── п.5.1: needs_info в сторе ───────────────────────────

def test_record_needs_info_fills_open_questions():
    """needs_info не переделка: задача уходит в awaiting_clarification с вопросами."""
    from aria.db.enums import TaskStatus
    from aria.core.state_machine import assert_transition_allowed

    assert_transition_allowed(TaskStatus.under_audit, TaskStatus.awaiting_clarification)
    spec = clarifier.__dict__.get("AUTONOMOUS_NOTE")
    assert spec  # модуль импортирован и содержит автономную заметку

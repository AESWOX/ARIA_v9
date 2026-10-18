"""PR-7 tests, robust version."""
from __future__ import annotations

from types import SimpleNamespace

from aria.core.privacy import EntityAnonymizer
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.llm.providers.base import ChatMessage

KEY = "sk-abcdef1234567890abcdef"
EMAIL = "owner@example.com"


def test_secret_and_email_are_masked():
    anon = EntityAnonymizer()
    masked = anon.anonymize(f"ключ {KEY} и почта {EMAIL}")
    assert KEY not in masked and EMAIL not in masked
    assert "⟦SECRET_1⟧" in masked and "⟦EMAIL_1⟧" in masked


def test_explicit_names_are_masked():
    anon = EntityAnonymizer(names=["Иванов"])
    assert "⟦NAME_1⟧" in anon.anonymize("клиент Иванов оплатил")


def test_placeholder_is_stable_between_messages():
    anon = EntityAnonymizer()
    first = anon.anonymize(f"пиши на {EMAIL}")
    second = anon.anonymize(f"напомни про {EMAIL} завтра")
    assert "⟦EMAIL_1⟧" in first and "⟦EMAIL_1⟧" in second
    assert "⟦EMAIL_2⟧" not in second


def test_round_trip_restores_the_original():
    anon = EntityAnonymizer(names=["Иванов"])
    original = f"отправь {KEY} на {EMAIL}, исполнитель Иванов"
    masked = anon.anonymize(original)
    assert masked != original
    assert anon.restore(masked) == original


def test_shadow_map_reuses_existing_placeholders():
    seed = EntityAnonymizer()
    first = seed.anonymize(f"почта {EMAIL}")
    assert "⟦EMAIL_1⟧" in first
    fresh = EntityAnonymizer(shadow_map=seed.shadow_map)
    second = fresh.anonymize(f"снова {EMAIL}")
    assert "⟦EMAIL_1⟧" in second  # тот же плейсхолдер, что и в первой сессии
    assert fresh.restore(second) == f"снова {EMAIL}"


def test_agent_loop_is_not_masked():
    """Агентский цикл не маскируется: плейсхолдеры сломали бы пути и команды."""
    from aria.core import privacy

    assert not hasattr(privacy, "anonymize_tool_calls")


def test_dlp_is_disabled_by_default():
    from aria.config import get_settings

    assert get_settings().dlp_chat_enabled is False


# ─────────────────── интеграция с чатом ───────────────────

def _fake_settings(enabled: bool, names: str = ""):
    return SimpleNamespace(
        dlp_chat_enabled=enabled,
        dlp_names=names,
        dlp_names_list=[n for n in names.split(",") if n.strip()],
    )


def test_mask_is_a_noop_when_the_flag_is_off(monkeypatch):
    from aria.routers import chat

    monkeypatch.setattr(chat, "get_settings", lambda: _fake_settings(False))
    with session_scope() as db:
        s = repo.create_session(db, "pr7-off")
        sid = s.id
    prompt = [ChatMessage(role="user", content=f"мой ключ {KEY}")]
    masked, applied = chat._dlp_mask(prompt, sid)
    assert applied is False and masked[0].content == f"мой ключ {KEY}"
    with session_scope() as db:
        assert repo.get_privacy_shadow_map(db, sid) is None


def test_mask_and_restore_through_the_chat_helpers(monkeypatch):
    from aria.routers import chat

    monkeypatch.setattr(chat, "get_settings", lambda: _fake_settings(True, "Иванов"))
    with session_scope() as db:
        s = repo.create_session(db, "pr7-on")
        sid = s.id

    prompt = [ChatMessage(role="user", content=f"отправь {KEY} владельцу {EMAIL}, копия Иванов")]
    masked, applied = chat._dlp_mask(prompt, sid)
    assert applied is True
    assert KEY not in masked[0].content and EMAIL not in masked[0].content and "Иванов" not in masked[0].content

    with session_scope() as db:
        row = repo.get_privacy_shadow_map(db, sid)
        assert row is not None and len(row.map_json) == 3

    restored = chat._dlp_restore(masked[0].content, sid)
    assert KEY in restored and EMAIL in restored and "Иванов" in restored
    assert "⟦" not in restored


def test_shadow_map_row_is_upserted_not_duplicated(monkeypatch):
    from aria.routers import chat

    monkeypatch.setattr(chat, "get_settings", lambda: _fake_settings(True))
    with session_scope() as db:
        s = repo.create_session(db, "pr7-upsert")
        sid = s.id

    chat._dlp_mask([ChatMessage(role="user", content=f"a {KEY}")], sid)
    chat._dlp_mask([ChatMessage(role="user", content=f"b {EMAIL}")], sid)
    with session_scope() as db:
        row = repo.get_privacy_shadow_map(db, sid)
        assert set(row.map_json) == {"⟦SECRET_1⟧", "⟦EMAIL_1⟧"}

"""UX-1: hover meta on chat messages is wired to i18n and translated in every locale."""
from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "desktop" / "src"
PAGE = SRC / "pages" / "NativeChatPage.tsx"


def test_page_shows_send_time_and_processing_time():
    page = PAGE.read_text(encoding="utf-8")
    assert "formatLocalTime(m.created_at, locale)" in page
    assert "processingSeconds(" in page
    assert "t.chatMeta.sentAt" in page
    assert "t.chatMeta.tookSeconds" in page
    assert "title={meta || undefined}" in page


def test_every_locale_has_chat_meta_keys():
    locale_files = [p for p in (SRC / "i18n").glob("*.ts") if p.name not in ("index.ts", "types.ts")]
    assert len(locale_files) == 16
    for path in locale_files:
        text = path.read_text(encoding="utf-8")
        assert "chatMeta:" in text, path.name
        assert "{time}" in text, path.name
        assert "{seconds}" in text, path.name

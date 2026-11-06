"""UI-4: chat copy, right-sidebar task details and calm code selection."""
from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "desktop" / "src"


def test_chat_copies_only_the_transcript_and_has_a_right_sidebar():
    page = (SRC / "pages" / "NativeChatPage.tsx").read_text(encoding="utf-8")
    assert "copyChatText" in page
    assert "onCopyChat" in page
    assert "w-[360px]" in page
    assert "<WorkPanel" in page
    work = (SRC / "components" / "WorkPanel.tsx").read_text(encoding="utf-8")
    assert "<TaskSpecPanel" in work
    assert "<SessionInspector" in work


def test_topbar_has_the_copy_button():
    bar = (SRC / "components" / "ChatTopBar.tsx").read_text(encoding="utf-8")
    assert "Copy chat text" in bar
    assert "onCopyChat" in bar


def test_sessions_page_drops_the_stats_dashboard():
    sessions = (SRC / "pages" / "SessionsPage.tsx").read_text(encoding="utf-8")
    assert "Active in store" not in sessions
    assert "getSessionStats" not in sessions


def test_code_blocks_are_not_a_constant_glow():
    css = (SRC / "index.css").read_text(encoding="utf-8")
    assert "code {\n  background: transparent;" in css
    assert "*::selection" in css

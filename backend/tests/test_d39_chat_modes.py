"""D-39: в чате только Chat и Agent; старый режим plan молча читается как agent."""
from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "desktop" / "src"
MODES = SRC / "lib" / "chat-modes.ts"
PAGE = SRC / "pages" / "NativeChatPage.tsx"


def test_p0_chat_offers_only_chat_and_agent():
    assert MODES.is_file(), "desktop/src/lib/chat-modes.ts is missing"
    src = MODES.read_text(encoding="utf-8")
    assert re.findall(r'id:\s*"([a-z]+)"', src) == ["chat", "agent"]
    assert 'id: "plan"' not in src


def test_p1_no_plan_tab_or_plan_mode_type_in_the_page():
    page = PAGE.read_text(encoding="utf-8")
    assert 'id: "plan"' not in page
    assert 'type Mode = "chat" | "agent" | "plan"' not in page
    assert 'from "@/lib/chat-modes"' in page
    assert "CHAT_MODES" in page


def test_p2_saved_plan_mode_is_read_as_agent():
    src = MODES.read_text(encoding="utf-8")
    _, body = src.split("export function normalizeChatMode", 1)
    assert '"plan"' in body
    assert '? "agent" : "chat"' in body
    assert "normalizeChatMode(" in PAGE.read_text(encoding="utf-8")


def test_p4_no_plan_tabs_anywhere_in_the_ui():
    offenders = [
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.tsx")
        if 'id: "plan"' in path.read_text(encoding="utf-8")
    ]
    assert offenders == []

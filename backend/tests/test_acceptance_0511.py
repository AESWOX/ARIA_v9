"""Acceptance fixes 05.11: modes/roles, memory import, vision wording, clarifier context."""
from __future__ import annotations

from pathlib import Path

from aria.config import get_settings
from aria.core import clarifier, loop
from aria.core.roles import ROLE_REGISTRY
from aria.routers.chat import SYSTEM_PROMPT_BASE
from aria.tools.handlers.vision import _VISION_TEMPLATE
from aria.tools.registry import TOOL_REGISTRY


def test_agent_loop_imports_memory_store_as_module() -> None:
    import aria.memory.store as store

    assert callable(store.recall_block)
    source = Path(loop.__file__).read_text(encoding="utf-8")
    assert "from aria.memory.store import memory_store" not in source
    assert "from aria.memory import store as _memory_store" in source


def test_general_role_has_telegram_and_shell_when_toggle_is_on(monkeypatch, tmp_path) -> None:
    role = ROLE_REGISTRY["general"]
    assert "telegram_channel" in role.tool_whitelist

    settings = get_settings()
    monkeypatch.setattr(settings, "agent_shell_enabled", True)
    monkeypatch.setattr(settings, "agent_project_root", str(tmp_path))

    assert loop._agent_shell_target("general", settings) == (True, str(tmp_path))
    assert loop._agent_shell_target("worker", settings) == (False, None)
    shell = TOOL_REGISTRY["shell_execute"]
    assert shell.requires_approval is True
    assert "general" in shell.allowed_roles


def test_chat_prompt_names_chat_mode_and_points_to_agent() -> None:
    assert "Chat mode" in SYSTEM_PROMPT_BASE
    assert "no tools" in SYSTEM_PROMPT_BASE.lower()
    assert "switch to Agent" in SYSTEM_PROMPT_BASE


def test_vision_template_marks_unclear_and_keeps_scene_first() -> None:
    lowered = _VISION_TEMPLATE.lower()
    assert "first 2-3 sentences" in lowered
    assert "possible minor" in lowered
    assert "unclear" in lowered
    assert "user's question" in lowered


def test_clarifier_recent_context_is_bounded_and_visible() -> None:
    text = clarifier._recent_context_text(
        [
            ("user", "посмотри вложение Attached files: - chat-uploads/a.png"),
            ("assistant", "На картинке кот."),
        ]
    )
    assert "Последние сообщения сессии" in text
    assert "Attached files" in text
    assert "На картинке кот" in text
    assert len(text) < 5000


def test_response_language_follows_the_message_not_the_ui() -> None:
    assert "Russian" in clarifier.language_instruction("привет, как дела")
    assert "Ukrainian" in clarifier.language_instruction("привіт, як справи")
    assert "English" in clarifier.language_instruction("hello, how are you")


def test_memory_honesty_is_in_the_persona() -> None:
    persona = (Path(__file__).resolve().parents[1] / "aria" / "prompts" / "persona.md").read_text(
        encoding="utf-8"
    )
    lowered = persona.lower()
    assert "before denying past work" in lowered
    assert "search memory and vault" in lowered
    assert "could not search chats" in lowered


def test_accept_assumptions_has_no_empty_clarification_bubble() -> None:
    from aria.routers.tasks import _clarify_echo

    assert _clarify_echo([]) == "Приняты допущения."
    assert _clarify_echo([{"field": "format", "value": "PDF"}]) == "Уточнения: format: PDF"


def test_creative_prompt_keeps_scope_and_non_consensual_limit() -> None:
    prompt = (Path(__file__).resolve().parents[1] / "aria" / "prompts" / "content_creative.md").read_text(
        encoding="utf-8"
    ).lower()
    assert "scenes and stories" in prompt
    assert "image prompts" in prompt
    assert "non-consensual" in prompt
    assert "adult mode" in prompt


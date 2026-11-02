"""L1 (TZ_PRODUCTION_LINE): persona in chat and agent prompts, no extra provider calls."""
from __future__ import annotations

import asyncio
import math

from aria.config import get_settings
from aria.core.compliance import COMPLIANCE_TEXT
from aria.core.persona import PERSONA_TEXT
from aria.routers import chat as chat_mod
from aria.routers.chat import SYSTEM_PROMPT_BASE
from tests.test_compliance_v2 import (
    FakeProvider,
    _cleanup,
    _client,
    _new_session,
    _run_agent_once,
)

PERSONA_MARKER = "ARIA persona (owner-local assistant)"


def test_persona_is_in_chat_prompt_in_required_order_and_stable():
    prompt = chat_mod._system_prompt()
    assert PERSONA_MARKER in prompt
    assert prompt == chat_mod._system_prompt()
    assert chat_mod._build_prompt([("user", "hi")])[0].content == prompt
    assert prompt.index(PERSONA_TEXT) < prompt.index(COMPLIANCE_TEXT)


def test_chat_persona_off_equals_l0_prompt(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "persona_prompt_enabled", False)
    prompt = chat_mod._build_prompt([("user", "hi")])[0].content
    assert PERSONA_TEXT not in prompt
    assert COMPLIANCE_TEXT in prompt
    assert prompt == SYSTEM_PROMPT_BASE + "\n\n" + COMPLIANCE_TEXT


def test_persona_flag_defaults_on():
    assert get_settings().persona_prompt_enabled is True


def test_persona_plus_compliance_fit_the_token_budget():
    # Эвристика ТЗ: оценка токенов по символам, 4.2 символа на токен.
    assert math.ceil((len(PERSONA_TEXT) + len(COMPLIANCE_TEXT)) / 4.2) <= 450


def test_chat_turn_still_uses_one_provider_call():
    provider = FakeProvider(text="pong")
    client = _client(provider)
    sid = _new_session(client)
    try:
        assert client.post(f"/chat/sessions/{sid}/send", json={"content": "do the task"}).status_code == 200
        assert provider.calls == 1
        assert PERSONA_TEXT in provider.seen[0][0].content
    finally:
        _cleanup(sid)


def test_agent_role_prompt_has_persona_before_compliance_and_one_call():
    provider = asyncio.run(_run_agent_once("pong"))
    system_prompt = provider.seen[0][0].content
    assert PERSONA_TEXT in system_prompt
    assert system_prompt.index(PERSONA_TEXT) < system_prompt.index(COMPLIANCE_TEXT)
    assert provider.calls == 1


def test_agent_persona_off_keeps_l0_contract(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "persona_prompt_enabled", False)
    provider = asyncio.run(_run_agent_once("pong"))
    system_prompt = provider.seen[0][0].content
    assert PERSONA_TEXT not in system_prompt
    assert COMPLIANCE_TEXT in system_prompt
    assert provider.calls == 1


def test_persona_file_has_no_adult_samples_just_boundaries():
    lowered = PERSONA_TEXT.lower()
    assert "hard limits" in lowered
    assert "no sexual content involving minors" in lowered

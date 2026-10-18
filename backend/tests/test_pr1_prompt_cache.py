"""PR-1: стабильный префикс промпта (messages[0]) + телеметрия prompt-кэша."""
from __future__ import annotations

import logging

from aria.llm.providers.base import LlmResponse
from aria.llm.router import log_cache_usage
from aria.routers.chat import SYSTEM_PROMPT, _attach_dynamic, _build_prompt

SUMMARY = "[compression summary of 4 messages]\nстарая часть диалога"
HISTORY = [
    ("system", SUMMARY),
    ("user", "первый вопрос"),
    ("assistant", "ответ"),
    ("user", "новый вопрос"),
]


def test_messages0_is_byte_stable():
    a = _build_prompt(HISTORY, "")
    b = _build_prompt(HISTORY, "ПАМЯТЬ: владелец любит кофе")
    c = _build_prompt(HISTORY, "ПАМЯТЬ: совсем другой факт")
    assert a[0].content == b[0].content == c[0].content == SYSTEM_PROMPT


def test_dynamic_block_is_a_separate_system_message():
    b = _build_prompt(HISTORY, "ПАМЯТЬ: владелец любит кофе")
    assert b[-1].role == "user"
    dynamic = [mm for mm in b[1:] if mm.role == "system"]
    assert any("ПАМЯТЬ: владелец любит кофе" in mm.content for mm in dynamic)
    assert any("Сводка более ранней" in mm.content for mm in dynamic)
    assert sum(1 for mm in b if mm.role == "system" and mm.content == SYSTEM_PROMPT) == 1


def test_thinking_hint_does_not_touch_messages0():
    base = _build_prompt(HISTORY, "")
    p = _attach_dynamic(list(base), "Думай глубже.")
    assert p[0].content == SYSTEM_PROMPT
    assert p[-1].role == "user"
    assert p[-2].role == "system" and "Думай глубже." in p[-2].content


def test_empty_dynamic_keeps_prompt_identical():
    base = _build_prompt(HISTORY, "")
    assert [mm.content for mm in _attach_dynamic(list(base), "   ")] == [mm.content for mm in base]


def test_cache_hit_and_miss_are_logged(caplog):
    resp = LlmResponse(text="hi", usage={"prompt_cache_hit_tokens": 900, "prompt_cache_miss_tokens": 100})
    with caplog.at_level(logging.INFO, logger="local_agent.router"):
        stats = log_cache_usage("deepseek-chat", resp)
    assert stats == {"hit": 900, "miss": 100}
    assert "hit_ratio=0.900" in caplog.text


def test_provider_without_cache_fields_is_quiet(caplog):
    resp = LlmResponse(text="hi", usage={})
    with caplog.at_level(logging.INFO, logger="local_agent.router"):
        stats = log_cache_usage("gemini-flash", resp)
    assert stats == {"hit": None, "miss": None}
    assert "prompt_cache:" not in caplog.text

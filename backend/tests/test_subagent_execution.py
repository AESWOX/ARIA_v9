"""Tests for role-to-provider-class binding in sub-agent delegation (B4/v13).

Verifies that each sub-agent role (coder, vision, devops_infra, etc.)
has default_model_policy="subagent_execution" so delegated tasks use Groq.
"""
from __future__ import annotations

from aria.core.roles import ROLE_REGISTRY, mvp_active_roles


SUBAGENT_ROLES = {
    "coder", "devops_infra", "image_gen", "vision",
    "qa_auditor", "obsidian_keeper", "housekeeping", "research",
}


def test_all_subagent_roles_use_subagent_execution() -> None:
    """Every role that gets delegated to must explicitly opt into subagent_execution."""
    for role_id in SUBAGENT_ROLES:
        role = ROLE_REGISTRY[role_id]
        assert role.default_model_policy == "subagent_execution", (
            f"{role_id} has policy={role.default_model_policy}, "
            f"expected subagent_execution"
        )


def test_orchestrator_stays_premium() -> None:
    """Orchestrator plans and delegates — keeps premium_reasoning."""
    role = ROLE_REGISTRY["orchestrator"]
    assert role.default_model_policy == "premium_reasoning"


def test_router_has_no_stub_providers_by_default() -> None:
    """Волна 1 (A9): без ключей маршрутизатор пуст, а не «отвечает» заглушкой."""
    from aria.llm.router import build_default_router

    router = build_default_router()
    stub_ids = [
        p.provider_id
        for providers in router.providers_by_class.values()
        for p in providers
        if p.provider_id.startswith("stub")
    ]
    assert stub_ids == [], f"тихие заглушки вернулись в боевой путь: {stub_ids}"


def test_empty_router_reports_unavailable_not_an_answer() -> None:
    """Пустой маршрутизатор обязан честно падать, а не выдумывать ответ."""
    import asyncio

    import pytest

    from aria.llm.providers.base import ChatMessage
    from aria.llm.router import ProviderRouter, ProviderUnavailable

    with pytest.raises(ProviderUnavailable):
        asyncio.run(
            ProviderRouter().route_chat(
                "standard_reasoning", [ChatMessage(role="user", content="hi")], [], allow_degrade=True
            )
        )

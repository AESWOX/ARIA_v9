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


def test_subagent_execution_is_registered() -> None:
    """Sanity: the provider_class subagent_execution must exist in the router."""
    from aria.llm.router import build_default_router
    router = build_default_router()
    providers = router.providers_by_class.get("subagent_execution", [])
    # In CI without .env, this will be a StubProvider; with Groq keys, a real one.
    # Either is fine — what matters is the class is registered.
    assert len(providers) >= 1, (
        f"expected at least 1 provider with provider_class=subagent_execution, "
        f"got {len(providers)}. Router has classes: {list(router.providers_by_class.keys())}"
    )
    # Verify the provider is properly initialised
    assert providers[0].provider_class == "subagent_execution"

"""Роли и саб-агенты — §7 ТЗ v7.1.

Роли — не процессы, а конфигурируемые пресеты (принцип §4.3). MVP subset для
Фазы 1-3: general, orchestrator, coder, devops_infra, image_gen, vision,
qa_auditor, obsidian_keeper, housekeeping. Остальные роли присутствуют в
контракте, но enabled=False — включаются добавлением записи в реестр без
изменения кода ядра, как и требует §7.1.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RoleDefinition:
    role_id: str
    description: str
    system_prompt_asset: str
    tool_whitelist: tuple[str, ...]
    default_model_policy: str  # provider class из §12.1
    can_delegate: bool = False
    max_subagents: int = 0
    enabled: bool = True


ROLE_REGISTRY: dict[str, RoleDefinition] = {
    "general": RoleDefinition(
        role_id="general",
        description="Базовая роль общего назначения, диспетчеризация простых задач.",
        system_prompt_asset="prompts/general.md",
        tool_whitelist=("read_note", "search_vault", "list_vault", "file_read", "file_search"),
        default_model_policy="standard_reasoning",
    ),
    "oracle": RoleDefinition(
        role_id="oracle",
        description="Генерация плана задачи в формате PlanStep. Результат проходит validate_plan().",
        system_prompt_asset="prompts/oracle.md",
        tool_whitelist=(),
        default_model_policy="premium_reasoning",
        can_delegate=False,
        max_subagents=0,
    ),
    "orchestrator": RoleDefinition(
        role_id="orchestrator",
        description="Строит план, выбирает роль/модель/tool-стратегию, может делегировать.",
        system_prompt_asset="prompts/orchestrator.md",
        tool_whitelist=("delegate_task", "file_read", "file_search", "read_note", "search_vault", "list_vault", "web_search"),
        default_model_policy="premium_reasoning",
        can_delegate=True,
        max_subagents=5,
    ),
    "coder": RoleDefinition(
        role_id="coder",
        description="Пишет и правит код, работает с shell и файловой системой.",
        system_prompt_asset="prompts/coder.md",
        tool_whitelist=("shell_execute", "file_read", "file_write", "file_search"),
        default_model_policy="subagent_execution",
    ),
    "devops_infra": RoleDefinition(
        role_id="devops_infra",
        description="RunPod/B2/инфраструктурные операции, включая high-risk shell.",
        system_prompt_asset="prompts/devops_infra.md",
        tool_whitelist=("shell_execute", "file_read", "file_write"),
        default_model_policy="subagent_execution",
    ),
    "image_gen": RoleDefinition(
        role_id="image_gen",
        description="SDXL/ComfyUI генерация.",
        system_prompt_asset="prompts/image_gen.md",
        tool_whitelist=("shell_execute", "file_write"),
        default_model_policy="subagent_execution",
    ),
    "vision": RoleDefinition(
        role_id="vision",
        description="Анализ изображений.",
        system_prompt_asset="prompts/vision.md",
        tool_whitelist=("file_read",),
        default_model_policy="subagent_execution",
    ),
    "qa_auditor": RoleDefinition(
        role_id="qa_auditor",
        description="Независимая структурная проверка результата (audit-loop, §6.1 п.7).",
        system_prompt_asset="prompts/qa_auditor.md",
        tool_whitelist=("file_read", "file_search", "search_vault"),
        default_model_policy="subagent_execution",
    ),
    "obsidian_keeper": RoleDefinition(
        role_id="obsidian_keeper",
        description="Ведение Obsidian vault, Draft TZ storage (§13.3).",
        system_prompt_asset="prompts/obsidian_keeper.md",
        tool_whitelist=("read_note", "write_note", "search_vault", "list_vault", "file_search"),
        default_model_policy="subagent_execution",
    ),
    "housekeeping": RoleDefinition(
        role_id="housekeeping",
        description="Служебные/уборочные задачи низкого риска.",
        system_prompt_asset="prompts/housekeeping.md",
        tool_whitelist=("file_read", "file_search"),
        default_model_policy="subagent_execution",
    ),
    # --- присутствуют в контракте, выключены до Фазы 4+ (§7.1) ---
    "research": RoleDefinition(
        role_id="research", description="Веб-исследования — поиск в интернете через DuckDuckGo.",
        system_prompt_asset="prompts/research.md",
        tool_whitelist=("web_search", "file_read", "file_search"),
        default_model_policy="subagent_execution",
    ),
    "social_monitor": RoleDefinition(
        role_id="social_monitor", description="Мониторинг соцсетей.", system_prompt_asset="prompts/social_monitor.md",
        tool_whitelist=(), default_model_policy="free_tier_reasoning", enabled=False,
    ),
    "github": RoleDefinition(
        role_id="github", description="GitHub-операции.", system_prompt_asset="prompts/github.md",
        tool_whitelist=(), default_model_policy="standard_reasoning", enabled=False,
    ),
    "api_debug": RoleDefinition(
        role_id="api_debug", description="Отладка внешних API.", system_prompt_asset="prompts/api_debug.md",
        tool_whitelist=(), default_model_policy="standard_reasoning", enabled=False,
    ),
    "data_mlops": RoleDefinition(
        role_id="data_mlops", description="Data/MLOps задачи.", system_prompt_asset="prompts/data_mlops.md",
        tool_whitelist=(), default_model_policy="standard_reasoning", enabled=False,
    ),
    "content_creative": RoleDefinition(
        role_id="content_creative", description="Креативный контент.", system_prompt_asset="prompts/content_creative.md",
        tool_whitelist=(), default_model_policy="standard_reasoning", enabled=False,
    ),
}


def get_role(role_id: str) -> RoleDefinition:
    role = ROLE_REGISTRY.get(role_id)
    if role is None:
        raise KeyError(f"unknown role_id={role_id}")
    if not role.enabled:
        raise PermissionError(f"role {role_id} is disabled until Phase 4+ per §7.1")
    return role


def mvp_active_roles() -> list[str]:
    return [r.role_id for r in ROLE_REGISTRY.values() if r.enabled]

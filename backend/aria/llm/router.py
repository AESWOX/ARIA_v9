"""llm/router.py — §12 ТЗ v7.1.

Провайдеры группируются по provider_class (§12.1). Оркестратор/Audit просят
premium/standard reasoning, простые подзадачи и sub-agents — free/cheap tiers
(§12.2). Роутер обязан:
- проверить connectivity с timeout 2 сек / 1 retry, без бесконечного перебора (§12.3)
- уважать budget thresholds 80%/100% (§12.4)
- делать round-robin по ключам одного провайдера (§12.5)
"""
from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session as OrmSession

from aria.config import get_settings
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import ProviderStatus
from aria.llm.providers.base import ChatMessage, LlmProvider, LlmResponse

logger = logging.getLogger("local_agent.router")


class ProviderUnavailable(Exception):
    pass


@dataclass
class RoutingResult:
    response: LlmResponse
    provider_id: str
    degraded_to_free: bool = False


@dataclass
class ProviderRouter:
    providers_by_class: dict[str, list[LlmProvider]] = field(default_factory=dict)

    def register(self, provider: LlmProvider) -> None:
        self.providers_by_class.setdefault(provider.provider_class, []).append(provider)

    # ---------- budget policy (§12.4) ----------

    def _budget_status(self) -> dict:
        with session_scope() as db:
            state = repo.get_agent_state(db, "budget_status")
        return state or {"daily_pct": 0, "weekly_pct": 0}

    def budget_gate(self, provider_class: str) -> tuple[bool, bool]:
        """Возвращает (allowed, warn). При >=100% премиум/standard блокируются кодом,
        запрос деградирует на free_tier; при >=80% только warning-флаг, запрос идёт."""
        settings = get_settings()
        status = self._budget_status()
        pct = max(status.get("daily_pct", 0), status.get("weekly_pct", 0))
        warn = pct >= settings.budget_warn_threshold_pct
        if provider_class in ("premium_reasoning", "standard_reasoning") and pct >= settings.budget_block_threshold_pct:
            return False, warn
        return True, warn

    # ---------- connectivity / selection (§12.3, §12.5) ----------

    async def _pick_available(self, provider_class: str, db: OrmSession | None = None) -> LlmProvider | None:
        settings = get_settings()
        candidates = self.providers_by_class.get(provider_class, [])
        for provider in candidates:
            ok = await provider.check_connectivity(settings.providers_connectivity_timeout_sec)
            if not ok:
                # 1 retry, затем provider_status=offline и немедленное завершение
                ok = await provider.check_connectivity(settings.providers_connectivity_timeout_sec)
            self._record_provider_status(provider, ProviderStatus.active if ok else ProviderStatus.offline, db=db)
            if ok:
                return provider
        return None

    def _record_provider_status(self, provider: LlmProvider, status: ProviderStatus, db: OrmSession | None = None) -> None:
        if db is None:
            with session_scope() as session:
                repo.upsert_provider_health(
                    session,
                    provider.provider_id,
                    label=provider.provider_id,
                    provider_class=provider.provider_class,
                    status=status,
                )
        else:
            repo.upsert_provider_health(
                db,
                provider.provider_id,
                label=provider.provider_id,
                provider_class=provider.provider_class,
                status=status,
            )

    # ---------- entrypoint ----------

    async def route_chat(
        self,
        provider_class: str,
        messages: list[ChatMessage],
        tools: list[dict],
        timeout_sec: float = 60,
        allow_degrade: bool = True,
        db: OrmSession | None = None,
    ) -> RoutingResult:
        allowed, warn = self.budget_gate(provider_class)
        target_class = provider_class
        degraded = False

        if not allowed:
            if not allow_degrade:
                raise ProviderUnavailable(f"budget_block_threshold_pct reached, {provider_class} blocked (§12.4)")
            target_class = "free_tier_reasoning"
            degraded = True
            logger.warning("budget block: degrading %s -> free_tier_reasoning", provider_class)

        provider = await self._pick_available(target_class, db=db)
        if provider is None and target_class != "free_tier_reasoning" and allow_degrade:
            logger.warning("%s unavailable, falling back to free_tier_reasoning", target_class)
            provider = await self._pick_available("free_tier_reasoning", db=db)
            degraded = True

        if provider is None:
            raise ProviderUnavailable(f"no available provider for class={target_class} (§12.3)")

        response = await provider.chat(messages, tools, timeout_sec)
        return RoutingResult(response=response, provider_id=provider.provider_id, degraded_to_free=degraded)


def build_default_router() -> ProviderRouter:
    """Собирает роутер из env-конфигурации: DeepSeek как standard/premium,
    Gemini/Groq с ротацией ключей. Всегда есть deterministic stub для smoke/MVP."""
    from aria.llm.key_pool import KeyPool
    from aria.llm.providers.openai_compatible import OpenAICompatibleProvider
    from aria.llm.providers.stub import StubProvider

    settings = get_settings()
    router = ProviderRouter()

    has_any_real_provider = False

    if settings.deepseek_api_key_resolved:
        has_any_real_provider = True
        router.register(
            OpenAICompatibleProvider(
                provider_id="deepseek-chat",
                provider_class="standard_reasoning",
                base_url=settings.deepseek_base_url,
                model="deepseek-chat",
                api_key=settings.deepseek_api_key_resolved,
            )
        )
        router.register(
            OpenAICompatibleProvider(
                provider_id="deepseek-reasoner",
                provider_class="premium_reasoning",
                base_url=settings.deepseek_base_url,
                model="deepseek-reasoner",
                api_key=settings.deepseek_api_key_resolved,
            )
        )

    # Порядок регистрации внутри одного provider_class = порядок fallback
    # в _pick_available(): Gemini первый, Groq — если Gemini недоступен
    # или весь его пул ключей исчерпан (see KeyPool.mark_rate_limited/mark_dead).
    gemini_keys = settings.gemini_api_keys_list
    if gemini_keys:
        has_any_real_provider = True
        gemini_pool_flash = KeyPool(gemini_keys, name="gemini-answers")
        gemini_pool_pro = KeyPool(gemini_keys, name="gemini-answers")  # свой курсор на класс
        router.register(
            OpenAICompatibleProvider(
                provider_id="gemini-flash",
                provider_class="free_tier_reasoning",
                base_url=settings.gemini_base_url,
                model="gemini-2.5-flash",
                key_pool=gemini_pool_flash,
            )
        )
        router.register(
            OpenAICompatibleProvider(
                provider_id="gemini-pro",
                provider_class="standard_reasoning",
                base_url=settings.gemini_base_url,
                model="gemini-3.1-pro",
                key_pool=gemini_pool_pro,
            )
        )
        # DeepSeek приоритет для premium_reasoning; если DeepSeek не задан/недоступен —
        # полный фолбэк на Gemini, тем же объектом что и standard_reasoning (свой key_pool).
        router.providers_by_class.setdefault("premium_reasoning", []).append(
            OpenAICompatibleProvider(
                provider_id="gemini-pro-premium-fallback",
                provider_class="premium_reasoning",
                base_url=settings.gemini_base_url,
                model="gemini-3.1-pro",
                key_pool=KeyPool(gemini_keys, name="gemini-answers"),
            )
        )

    groq_keys = settings.groq_api_keys_list
    if groq_keys:
        has_any_real_provider = True
        groq_pool_free = KeyPool(groq_keys, name="groq-answers")
        groq_pool_standard = KeyPool(groq_keys, name="groq-answers")
        router.register(
            OpenAICompatibleProvider(
                provider_id="groq-llama-fast",
                provider_class="free_tier_reasoning",
                base_url=settings.groq_base_url,
                model="llama-3.1-8b-instant",
                key_pool=groq_pool_free,
            )
        )
        router.register(
            OpenAICompatibleProvider(
                provider_id="groq-llama-versatile",
                provider_class="standard_reasoning",
                base_url=settings.groq_base_url,
                model="llama-3.3-70b-versatile",
                key_pool=groq_pool_standard,
            )
        )
        # Последнее звено цепи "DeepSeek -> Gemini -> Groq" для premium_reasoning.
        router.providers_by_class.setdefault("premium_reasoning", []).append(
            OpenAICompatibleProvider(
                provider_id="groq-llama-versatile-premium-fallback",
                provider_class="premium_reasoning",
                base_url=settings.groq_base_url,
                model="llama-3.3-70b-versatile",
                key_pool=KeyPool(groq_keys, name="groq-answers"),
            )
        )

    # --- Vision (multimodal): отдельный пул, не разделяет состояние с main pool ---
    vision_keys = settings.vision_gemini_api_keys_list
    if vision_keys:
        has_any_real_provider = True
        vision_pool = KeyPool(vision_keys, name="vision")
        router.register(
            OpenAICompatibleProvider(
                provider_id="gemini-vision",
                provider_class="vision_multimodal",
                base_url=settings.gemini_base_url,
                model="gemini-2.5-flash",
                key_pool=vision_pool,
            )
        )

    # --- Sub-agent execution (Groq): дешёвый быстрый провайдер для delegate_task ---
    if groq_keys:
        subagent_pool = KeyPool(groq_keys, name="groq-subagent")
        router.register(
            OpenAICompatibleProvider(
                provider_id="groq-subagent-fast",
                provider_class="subagent_execution",
                base_url=settings.groq_base_url,
                model="llama-3.1-8b-instant",
                key_pool=subagent_pool,
            )
        )

    if not has_any_real_provider:
        stub_free = StubProvider(provider_id="stub-free")
        stub_free.provider_class = "free_tier_reasoning"
        router.register(stub_free)

        stub_standard = StubProvider(provider_id="stub-standard")
        stub_standard.provider_class = "standard_reasoning"
        router.register(stub_standard)

        stub_premium = StubProvider(provider_id="stub-premium")
        stub_premium.provider_class = "premium_reasoning"
        router.register(stub_premium)

    # subagent_execution стопроцентно гарантирован: если нет Groq — ставим стаб
    if not groq_keys:
        stub_subagent = StubProvider(provider_id="stub-subagent")
        stub_subagent.provider_class = "subagent_execution"
        router.register(stub_subagent)

    return router

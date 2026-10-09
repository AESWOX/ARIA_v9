"""llm/router.py — §12 ТЗ v7.1.

Провайдеры группируются по provider_class (§12.1). Оркестратор/Audit просят
premium/standard reasoning, простые подзадачи и sub-agents — free/cheap tiers
(§12.2). Роутер обязан:
- проверить connectivity с timeout 2 сек / 1 retry, без бесконечного перебора (§12.3)
- уважать budget thresholds 80%/100% (§12.4)
- делать round-robin по ключам одного провайдера (§12.5)
"""
from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass, field

import httpx
from sqlalchemy.orm import Session as OrmSession

from aria.config import get_settings
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import ProviderStatus
from aria.llm.key_pool import NoAvailableKeys
from aria.llm.providers.base import ChatMessage, LlmProvider, LlmResponse

logger = logging.getLogger("local_agent.router")


class ProviderUnavailable(Exception):
    pass


# Transient upstream statuses: worth one more try / another provider.
RETRYABLE_STATUS = frozenset({408, 425, 500, 502, 503, 504})
RETRY_ATTEMPTS_PER_PROVIDER = 2      # same provider, on 5xx only
RETRY_BACKOFF_SEC = 1.0              # 1s, then 2s


@dataclass
class RoutingResult:
    response: LlmResponse
    provider_id: str
    degraded_to_free: bool = False
    fallback: bool = False  # answered by a provider other than the first choice


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

    def find_provider(self, provider_id: str) -> LlmProvider | None:
        for providers in self.providers_by_class.values():
            for provider in providers:
                if provider.provider_id == provider_id:
                    return provider
        return None

    async def route_chat(
        self,
        provider_class: str,
        messages: list[ChatMessage],
        tools: list[dict],
        timeout_sec: float = 60,
        allow_degrade: bool = True,
        db: OrmSession | None = None,
        resilient: bool = False,
        fallback_classes: tuple[str, ...] = (),
        retry_backoff_sec: float = RETRY_BACKOFF_SEC,
        prefer_provider_id: str | None = None,
    ) -> RoutingResult:
        """``prefer_provider_id`` — конкретная модель, выбранная владельцем. Если она недоступна, упёрлась
        в бюджет (§12.4) или ответила ошибкой, запрос идёт обычной цепочкой класса, а результат помечен
        ``fallback=True`` (UI может сказать, что ответила другая модель)."""
        preferred_failed = False
        if prefer_provider_id:
            preferred = self.find_provider(prefer_provider_id)
            if preferred is not None and self.budget_gate(preferred.provider_class)[0]:
                try:
                    if await self._connectivity_ok(preferred, db):
                        response = await preferred.chat(messages, tools, timeout_sec)
                        return RoutingResult(response=response, provider_id=preferred.provider_id)
                except Exception as exc:  # noqa: BLE001 — выбранная модель не должна ронять задачу
                    logger.warning("preferred model %s failed (%s); using the class chain", prefer_provider_id, type(exc).__name__)
            preferred_failed = True
        result = await self._route_by_class(
            provider_class, messages, tools, timeout_sec, allow_degrade, db, resilient, fallback_classes, retry_backoff_sec
        )
        if preferred_failed:
            result.fallback = True
        return result

    async def _route_by_class(
        self,
        provider_class: str,
        messages: list[ChatMessage],
        tools: list[dict],
        timeout_sec: float,
        allow_degrade: bool,
        db: OrmSession | None,
        resilient: bool,
        fallback_classes: tuple[str, ...],
        retry_backoff_sec: float,
    ) -> RoutingResult:
        if resilient:
            return await self._route_resilient(
                provider_class, messages, tools, timeout_sec, db, fallback_classes, retry_backoff_sec
            )
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


    # ---------- resilient path (A20): retry on 5xx, fail over across providers ----------

    async def _connectivity_ok(self, provider: LlmProvider, db: OrmSession | None) -> bool:
        settings = get_settings()
        ok = await provider.check_connectivity(settings.providers_connectivity_timeout_sec)
        if not ok:
            ok = await provider.check_connectivity(settings.providers_connectivity_timeout_sec)
        self._record_provider_status(provider, ProviderStatus.active if ok else ProviderStatus.offline, db=db)
        return ok

    async def _route_resilient(
        self,
        provider_class: str,
        messages: list[ChatMessage],
        tools: list[dict],
        timeout_sec: float,
        db: OrmSession | None,
        fallback_classes: tuple[str, ...],
        backoff_sec: float,
    ) -> RoutingResult:
        """Walk every provider of the class, then each fallback class.

        - 5xx from a provider: retry it (backoff 1s, 2s), then move on.
        - 429 / exhausted keys / timeout / network error: move on at once
          (the provider already rotated its own keys; retrying only burns time).
        - Anything else (400, 401 with no keys left handled inside the provider,
          parse errors): raised as is, another provider would fail the same way.
        Budget gate applies to every class tried.
        """
        classes = [provider_class, *[c for c in fallback_classes if c != provider_class]]
        last_exc: Exception | None = None
        tried = 0
        degraded = False
        for cls_index, cls in enumerate(classes):
            allowed, _warn = self.budget_gate(cls)
            if not allowed:
                if cls_index == 0:
                    degraded = True
                    cls = "free_tier_reasoning"  # same degrade rule as the plain path
                else:
                    continue
            for provider in self.providers_by_class.get(cls, []):
                if not await self._connectivity_ok(provider, db):
                    continue
                for attempt in range(RETRY_ATTEMPTS_PER_PROVIDER):
                    tried += 1
                    try:
                        response = await provider.chat(messages, tools, timeout_sec)
                    except httpx.HTTPStatusError as exc:
                        status = exc.response.status_code
                        last_exc = exc
                        if status in RETRYABLE_STATUS and attempt + 1 < RETRY_ATTEMPTS_PER_PROVIDER:
                            logger.warning("%s: HTTP %s, retry %d", provider.provider_id, status, attempt + 1)
                            await asyncio.sleep(backoff_sec * (2 ** attempt))
                            continue
                        if status == 429 or status in RETRYABLE_STATUS:
                            logger.warning("%s: HTTP %s, switching provider", provider.provider_id, status)
                            break
                        raise
                    except (NoAvailableKeys, httpx.TimeoutException, httpx.TransportError) as exc:
                        logger.warning("%s: %s, switching provider", provider.provider_id, type(exc).__name__)
                        last_exc = exc
                        break
                    else:
                        return RoutingResult(
                            response=response,
                            provider_id=provider.provider_id,
                            degraded_to_free=degraded or (cls != provider_class and cls == "free_tier_reasoning"),
                            fallback=tried > 1,
                        )
        if last_exc is not None:
            # keep the original error so callers map it as before (429 -> 429, timeout -> 504, ...)
            raise last_exc
        raise ProviderUnavailable(f"no available provider for class={provider_class} (§12.3)")


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
                model=settings.gemini_flash_model,
                key_pool=gemini_pool_flash,
            )
        )
        router.register(
            OpenAICompatibleProvider(
                provider_id="gemini-pro",
                provider_class="standard_reasoning",
                base_url=settings.gemini_base_url,
                model=settings.gemini_pro_model,
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
                model=settings.gemini_pro_model,
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
                model="qwen/qwen3.6-27b",
                key_pool=groq_pool_free,
            )
        )
        router.register(
            OpenAICompatibleProvider(
                provider_id="groq-llama-versatile",
                provider_class="standard_reasoning",
                base_url=settings.groq_base_url,
                model="qwen/qwen3.6-27b",
                key_pool=groq_pool_standard,
            )
        )
        # Последнее звено цепи "DeepSeek -> Gemini -> Groq" для premium_reasoning.
        router.providers_by_class.setdefault("premium_reasoning", []).append(
            OpenAICompatibleProvider(
                provider_id="groq-llama-versatile-premium-fallback",
                provider_class="premium_reasoning",
                base_url=settings.groq_base_url,
                model="qwen/qwen3.6-27b",
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
                model=settings.gemini_flash_model,
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
                model="qwen/qwen3.6-27b",
                key_pool=subagent_pool,
            )
        )

    if not router.providers_by_class:
        # Волна 1 (A9): раньше здесь молча регистрировались 4 заглушки
        # (stub-free/standard/premium/subagent), и UI показывал «ответ модели»,
        # которого не было. Теперь честно: без ключей маршрутизатор пуст,
        # route_chat поднимает ProviderUnavailable, /chat/status говорит
        # «не настроено», задача падает с provider_unavailable.
        logging.getLogger("local_agent.llm.router").warning(
            "no LLM provider configured — router is empty (add keys in Keys / .env)"
        )

    return router

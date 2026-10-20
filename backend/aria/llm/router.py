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
import re
import time
from dataclasses import dataclass, field

import httpx
from sqlalchemy.orm import Session as OrmSession

from aria.config import get_settings
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import ProviderStatus
from aria.core import timing
from aria.llm.key_pool import NoAvailableKeys
from aria.llm.providers.base import ChatMessage, LlmProvider, LlmResponse

logger = logging.getLogger("local_agent.router")


async def _timed_chat(provider, messages, tools, timeout_sec):
    """Один вызов модели с замером (этап ``provider_call``): сеть и ответ провайдера без очереди и повторов."""
    with timing.stage("provider_call"):
        return await provider.chat(messages, tools, timeout_sec)


class ProviderUnavailable(Exception):
    pass


def _is_empty_reply(response) -> bool:
    """Пустой ответ модели: нет текста и нет вызовов тулов."""
    return not (getattr(response, "text", None) or "").strip() and not getattr(response, "tool_calls", None)


def _finish_hint(response) -> str:
    """' (finish_reason=length)' из сырого ответа, если он есть: по нему видно, почему content пустой."""
    try:
        reason = response.raw["choices"][0].get("finish_reason")
    except Exception:  # noqa: BLE001 - сырой ответ может быть любым
        return ""
    return f" (finish_reason={reason})" if reason else ""


def tried_suffix(exc: Exception) -> str:
    """' (tried: gemini-flash HTTP 429; deepseek-chat HTTP 402; ...)' — для сообщений об ошибках."""
    attempts = getattr(exc, "attempts", None)
    return f" (tried: {'; '.join(attempts)})" if attempts else ""


# Transient upstream statuses: worth one more try / another provider.
RETRYABLE_STATUS = frozenset({408, 425, 500, 502, 503, 504})
# Ошибки, привязанные к ПРОВАЙДЕРУ (аккаунту или имени модели), а не к запросу: 402 (нет средств,
# «Insufficient Balance»), 403 (ключ не имеет доступа), 404 (имени модели нет у провайдера: «models/x is
# not found»). Повтор бесполезен, а другой провайдер отвечает нормально: переходим дальше, а этого
# провайдера на время откладываем в конец очереди (404 лечится правкой .env и перезапуском).
ACCOUNT_STATUS = frozenset({402, 403, 404})
ACCOUNT_COOLDOWN_SEC = 600.0
# Любой другой отказ провайдера (400/401/413/422..., битый ответ): идём дальше по цепочке, а провайдера на
# две минуты уводим в конец очереди: если он не тянет эту модель/схему тулов, не тратим на него первый запрос.
OTHER_ERROR_COOLDOWN_SEC = 120.0
# Предохранитель: после 429 / исчерпанных 5xx / нет ключей / таймаута провайдер уходит в КОНЕЦ очереди на короткое
# время (порядок, а не отказ: единственного провайдера всё равно пробуем). Без этого каждый запрос заново платил
# за заведомо провальную попытку: 2 повтора после 503 с паузой или до 60 с на таймаут.
RATE_LIMIT_COOLDOWN_SEC = 30.0
TRANSIENT_COOLDOWN_SEC = 30.0
TIMEOUT_COOLDOWN_SEC = 60.0
NO_KEYS_COOLDOWN_SEC = 60.0          # если пул ключей не сообщил, когда освободится первый
NO_KEYS_COOLDOWN_MIN_SEC = 15.0
NO_KEYS_COOLDOWN_MAX_SEC = 300.0
RETRY_ATTEMPTS_PER_PROVIDER = 2      # same provider, on 5xx only
RETRY_BACKOFF_SEC = 1.0              # 1s, then 2s


def log_cache_usage(provider_id: str, response) -> dict[str, int | None]:
    """PR-1: логирует попадание/промах prompt-кэша провайдера.

    DeepSeek и OpenAI-совместимые API возвращают в usage
    ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``; до PR-1 они не
    читались нигде, и «кэш работает или нет» нельзя было проверить по логам.
    Провайдер без этих полей (Gemini/Groq) молча пропускается.
    """
    usage = getattr(response, "usage", None) or {}
    if not isinstance(usage, dict):
        return {"hit": None, "miss": None}
    hit = usage.get("prompt_cache_hit_tokens", usage.get("prompt_cache_hit"))
    miss = usage.get("prompt_cache_miss_tokens", usage.get("prompt_cache_miss"))
    if hit is None and miss is None:
        return {"hit": None, "miss": None}
    try:
        hit_i = int(hit) if hit is not None else 0
        miss_i = int(miss) if miss is not None else 0
    except (TypeError, ValueError):
        return {"hit": None, "miss": None}
    total = hit_i + miss_i
    ratio = (hit_i / total) if total else 0.0
    logger.info("prompt_cache: provider=%s hit=%d miss=%d hit_ratio=%.3f", provider_id, hit_i, miss_i, ratio)
    return {"hit": hit_i, "miss": miss_i}


@dataclass
class RoutingResult:
    response: LlmResponse
    provider_id: str
    degraded_to_free: bool = False
    fallback: bool = False  # answered by a provider other than the first choice


@dataclass
class ProviderRouter:
    providers_by_class: dict[str, list[LlmProvider]] = field(default_factory=dict)
    # provider_id -> момент (по self.clock), до которого провайдер откладывается в конец очереди
    cooldown_until: dict[str, float] = field(default_factory=dict)
    clock: callable = time.monotonic
    # П.5.2 ТЗ: семафоры по классам провайдеров (квоты Gemini free-tier / Groq).
    semaphores: dict[str, object] = field(default_factory=dict)

    def _cooling(self, provider: LlmProvider) -> bool:
        return self.cooldown_until.get(provider.provider_id, 0.0) > self.clock()

    def _cool(self, provider: LlmProvider, seconds: float) -> None:
        """Отложить провайдера в конец очереди; более длинный уже действующий карантин не укорачиваем."""
        until = self.clock() + seconds
        if until > self.cooldown_until.get(provider.provider_id, 0.0):
            self.cooldown_until[provider.provider_id] = until

    @staticmethod
    def _no_keys_cooldown(provider: LlmProvider) -> float:
        pool = getattr(provider, "key_pool", None)
        if pool is not None:
            try:
                wait = pool.status().get("retry_in_sec")
            except Exception:
                wait = None
            if wait is not None:
                return min(max(float(wait), NO_KEYS_COOLDOWN_MIN_SEC), NO_KEYS_COOLDOWN_MAX_SEC)
        return NO_KEYS_COOLDOWN_SEC

    def _ordered(self, providers: list[LlmProvider]) -> list[LlmProvider]:
        """Здоровые провайдеры вперёд, «на карантине» (402/403) в конец: последняя попытка, а не отказ."""
        return [p for p in providers if not self._cooling(p)] + [p for p in providers if self._cooling(p)]

    def register(self, provider: LlmProvider) -> None:
        self.providers_by_class.setdefault(provider.provider_class, []).append(provider)

    # ---------- budget policy (§12.4) ----------

    def _budget_status(self) -> dict:
        from aria.llm.budget import compute_budget_status

        settings = get_settings()
        if settings.budget_daily_usd > 0 or settings.budget_weekly_usd > 0:
            return compute_budget_status(settings.budget_daily_usd, settings.budget_weekly_usd)
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

    def provider_concurrency_limits(self) -> dict[str, int]:
        """П.5.2 ТЗ: лимиты из provider_max_concurrency ("класс=лимит,класс=лимит")."""
        settings = get_settings()
        return dict(getattr(settings, "provider_concurrency_limits", {}) or {})

    def provider_slot(self, provider_class: str):
        """Асинхронный контекст-менеджер семафора класса провайдера (П.5.2 ТЗ)."""
        import asyncio
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _slot():
            settings = get_settings()
            limits = dict(getattr(settings, "provider_concurrency_limits", {}) or {})
            limit = max(1, int(limits.get(provider_class, getattr(settings, "delegate_max_parallel", 5) or 5)))
            holder = self.semaphores.get(provider_class)
            if holder is None or getattr(holder, "aria_limit", None) != limit:
                holder = asyncio.Semaphore(limit)
                try:
                    holder.aria_limit = limit
                except Exception:  # noqa: BLE001
                    pass
                self.semaphores[provider_class] = holder
            async with holder:
                yield

        return _slot()

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
        reject_empty: bool = False,
    ) -> RoutingResult:
        """П.5.2 ТЗ: запрос держит слот класса провайдера — параллельные волны EXECUTE
        и делегирование не выбивают квоты free-tier одним залпом."""
        t_wait = time.monotonic()
        async with self.provider_slot(provider_class):
            timing.record("provider_slot_wait", (time.monotonic() - t_wait) * 1000.0)
            with timing.stage("route_chat"):
                return await self._route_chat_impl(
                    provider_class,
                    messages,
                    tools,
                    timeout_sec=timeout_sec,
                    allow_degrade=allow_degrade,
                    db=db,
                    resilient=resilient,
                    fallback_classes=fallback_classes,
                    retry_backoff_sec=retry_backoff_sec,
                    prefer_provider_id=prefer_provider_id,
                    reject_empty=reject_empty,
                )

    async def _route_chat_impl(
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
        reject_empty: bool = False,
    ) -> RoutingResult:
        """``reject_empty`` — пустой текст без вызова тулов считается отказом звена (идём к следующему),
        а не успехом. Нужно чату: 200 OK с пустым content раньше обрывал всю цепочку (502).
        ``prefer_provider_id`` — конкретная модель, выбранная владельцем. Если она недоступна, упёрлась
        в бюджет (§12.4) или ответила ошибкой, запрос идёт обычной цепочкой класса, а результат помечен
        ``fallback=True`` (UI может сказать, что ответила другая модель)."""
        preferred_failed = False
        if prefer_provider_id:
            preferred = self.find_provider(prefer_provider_id)
            if preferred is not None and self.budget_gate(preferred.provider_class)[0]:
                try:
                    if await self._connectivity_ok(preferred, db):
                        response = await _timed_chat(preferred, messages, tools, timeout_sec)
                        if reject_empty and _is_empty_reply(response):
                            logger.warning("preferred model %s returned an empty reply; using the class chain", prefer_provider_id)
                        else:
                            log_cache_usage(preferred.provider_id, response)
                            return RoutingResult(response=response, provider_id=preferred.provider_id)
                except Exception as exc:  # noqa: BLE001 — выбранная модель не должна ронять задачу
                    logger.warning("preferred model %s failed (%s); using the class chain", prefer_provider_id, type(exc).__name__)
            preferred_failed = True
        result = await self._route_by_class(
            provider_class, messages, tools, timeout_sec, allow_degrade, db, resilient, fallback_classes, retry_backoff_sec,
            reject_empty,
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
        reject_empty: bool = False,
    ) -> RoutingResult:
        if resilient:
            return await self._route_resilient(
                provider_class, messages, tools, timeout_sec, db, fallback_classes, retry_backoff_sec, reject_empty
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

        response = await _timed_chat(provider, messages, tools, timeout_sec)
        log_cache_usage(provider.provider_id, response)
        return RoutingResult(response=response, provider_id=provider.provider_id, degraded_to_free=degraded)


    # ---------- resilient path (A20): retry on 5xx, fail over across providers ----------

    async def _connectivity_ok(self, provider: LlmProvider, db: OrmSession | None) -> bool:
        settings = get_settings()
        with timing.stage("connectivity_check"):
            ok = await provider.check_connectivity(settings.providers_connectivity_timeout_sec)
            if not ok:
                ok = await provider.check_connectivity(settings.providers_connectivity_timeout_sec)
        self._record_provider_status(provider, ProviderStatus.active if ok else ProviderStatus.offline, db=db)
        return ok

    @staticmethod
    def _unavailable_reason(provider: LlmProvider) -> str:
        """Почему провайдер пропущен без запроса: нет живых ключей (и когда освободится первый) или нет связи."""
        pool = getattr(provider, "key_pool", None)
        if pool is not None:
            st = pool.status()
            if st["dead"] + st["cooling_down"] >= st["total"]:
                wait = st.get("retry_in_sec")
                return f"no usable key (first frees up in ~{wait:.0f}s)" if wait is not None else "no usable key (all dead)"
        return "unreachable"

    async def _route_resilient(
        self,
        provider_class: str,
        messages: list[ChatMessage],
        tools: list[dict],
        timeout_sec: float,
        db: OrmSession | None,
        fallback_classes: tuple[str, ...],
        backoff_sec: float,
        reject_empty: bool = False,
    ) -> RoutingResult:
        """Walk every provider of the class, then each fallback class.

        - 5xx from a provider: retry it (backoff 1s, 2s), then move on.
        - 429 / exhausted keys / timeout / network error: move on at once
          (the provider already rotated its own keys; retrying only burns time).
        - Anything else (400, 401 with no keys left, битый ответ): тоже идём дальше (0022): провайдер мог не
          потянуть именно эту модель или схему тулов. Остановка только когда погасла вся цепочка; тогда
          поднимается последняя ошибка со списком всех попыток (`tried: ...`).
        Budget gate applies to every class tried.
        """
        classes = [provider_class, *[c for c in fallback_classes if c != provider_class]]
        last_exc: Exception | None = None
        tried = 0
        degraded = False
        attempts: list[str] = []  # кого и с чем пробовали: корень (429 у Flash) не должен прятаться за последней ошибкой
        for cls_index, cls in enumerate(classes):
            allowed, _warn = self.budget_gate(cls)
            if not allowed:
                if cls_index == 0:
                    degraded = True
                    cls = "free_tier_reasoning"  # same degrade rule as the plain path
                else:
                    continue
            for provider in self._ordered(self.providers_by_class.get(cls, [])):
                if not await self._connectivity_ok(provider, db):
                    attempts.append(f"{provider.provider_id} {self._unavailable_reason(provider)}")
                    continue
                for attempt in range(RETRY_ATTEMPTS_PER_PROVIDER):
                    tried += 1
                    try:
                        response = await _timed_chat(provider, messages, tools, timeout_sec)
                    except httpx.HTTPStatusError as exc:
                        status = exc.response.status_code
                        last_exc = exc
                        exc.provider_id = provider.provider_id  # чтобы вызывающий мог назвать виновника
                        attempts.append(f"{provider.provider_id} HTTP {status}")
                        if status in ACCOUNT_STATUS:
                            self.cooldown_until[provider.provider_id] = self.clock() + ACCOUNT_COOLDOWN_SEC
                            logger.warning("%s: HTTP %s (account/billing), switching provider for %.0fs", provider.provider_id, status, ACCOUNT_COOLDOWN_SEC)
                            break
                        if status in RETRYABLE_STATUS and attempt + 1 < RETRY_ATTEMPTS_PER_PROVIDER:
                            logger.warning("%s: HTTP %s, retry %d", provider.provider_id, status, attempt + 1)
                            await asyncio.sleep(backoff_sec * (2 ** attempt))
                            continue
                        if status == 429 or status in RETRYABLE_STATUS:
                            self._cool(provider, RATE_LIMIT_COOLDOWN_SEC if status == 429 else TRANSIENT_COOLDOWN_SEC)
                            logger.warning("%s: HTTP %s, switching provider", provider.provider_id, status)
                            break
                        # 400/401/413/422...: этот провайдер не обслужил запрос (схема тулов, параметры, все ключи
                        # мертвы). Не останавливаемся на первой же ошибке: идём к следующему, пока цепочка не кончится.
                        self.cooldown_until[provider.provider_id] = self.clock() + OTHER_ERROR_COOLDOWN_SEC
                        logger.warning("%s: HTTP %s, trying the next provider", provider.provider_id, status)
                        break
                    except (NoAvailableKeys, httpx.TimeoutException, httpx.TransportError) as exc:
                        self._cool(
                            provider,
                            self._no_keys_cooldown(provider) if isinstance(exc, NoAvailableKeys) else TIMEOUT_COOLDOWN_SEC,
                        )
                        logger.warning("%s: %s, switching provider", provider.provider_id, type(exc).__name__)
                        last_exc = exc
                        attempts.append(f"{provider.provider_id} {type(exc).__name__}")
                        break
                    except (KeyError, IndexError, TypeError, ValueError) as exc:  # пустой/битый ответ (нет choices, не JSON)
                        logger.warning("%s: unusable response (%s), switching provider", provider.provider_id, type(exc).__name__)
                        last_exc = exc
                        attempts.append(f"{provider.provider_id} bad response ({type(exc).__name__})")
                        self.cooldown_until[provider.provider_id] = self.clock() + OTHER_ERROR_COOLDOWN_SEC
                        break
                    else:
                        if reject_empty and _is_empty_reply(response):
                            # 200 OK, но ни текста, ни вызова тула (reasoning съел лимит, фильтр, сбой): это отказ звена.
                            # Раньше такой ответ возвращался как успех, и чат падал 502, не дойдя до остальной цепочки.
                            logger.warning("%s: empty reply (no text, no tool calls), switching provider", provider.provider_id)
                            attempts.append(f"{provider.provider_id} empty reply{_finish_hint(response)}")
                            last_exc = ProviderUnavailable(f"{provider.provider_id} returned an empty reply")
                            break
                        log_cache_usage(provider.provider_id, response)
                        self.cooldown_until.pop(provider.provider_id, None)  # живой ответ снимает карантин
                        return RoutingResult(
                            response=response,
                            provider_id=provider.provider_id,
                            degraded_to_free=degraded or (cls != provider_class and cls == "free_tier_reasoning"),
                            fallback=tried > 1,
                        )
        if last_exc is not None and isinstance(last_exc, (httpx.HTTPError, NoAvailableKeys, ProviderUnavailable)):
            # keep the original error so callers map it as before (429 -> 429, timeout -> 504, ...)
            last_exc.attempts = attempts
            raise last_exc
        unavailable = ProviderUnavailable(
            f"no available provider for class={provider_class} (§12.3)"
            + (f" (last error: {type(last_exc).__name__}: {last_exc})" if last_exc is not None else "")
            + (f" (tried: {'; '.join(attempts)})" if attempts else "")
        )
        unavailable.attempts = attempts
        raise unavailable from last_exc


def build_default_router() -> ProviderRouter:
    """Собирает роутер из env-конфигурации: DeepSeek как standard/premium,
    Gemini/Groq с ротацией ключей. Всегда есть deterministic stub для smoke/MVP."""
    from aria.llm import gemini_pools
    from aria.llm.key_pool import KeyPool
    from aria.llm.providers.openai_compatible import OpenAICompatibleProvider, thinking_payload
    from aria.llm.providers.stub import StubProvider

    settings = get_settings()
    router = ProviderRouter()
    gemini_pools.reset()  # новая сборка роутера (старт или смена .env): cooldown пулов начинается с нуля

    has_any_real_provider = False

    if settings.deepseek_api_key_resolved:
        has_any_real_provider = True
        router.register(
            OpenAICompatibleProvider(
                provider_id="deepseek-chat",
                provider_class="standard_reasoning",
                base_url=settings.deepseek_base_url,
                model=settings.deepseek_chat_model,
                api_key=settings.deepseek_api_key_resolved,
                extra_payload=thinking_payload(settings.deepseek_chat_thinking),
            )
        )
        router.register(
            OpenAICompatibleProvider(
                provider_id="deepseek-reasoner",
                provider_class="premium_reasoning",
                base_url=settings.deepseek_base_url,
                model=settings.deepseek_reasoner_model,
                api_key=settings.deepseek_api_key_resolved,
                extra_payload=thinking_payload(settings.deepseek_reasoner_thinking),
            )
        )

    # Порядок регистрации внутри одного provider_class = порядок fallback
    # в _pick_available(): Gemini первый, Groq — если Gemini недоступен
    # или весь его пул ключей исчерпан (see KeyPool.mark_rate_limited/mark_dead).
    gemini_keys = settings.gemini_api_keys_list
    if gemini_keys:
        has_any_real_provider = True
        # Квота Gemini считается на проект И МОДЕЛЬ (0020): пул ключей общий для всех, кто ходит в одну
        # модель этими ключами (реестр gemini_pools), и раздельный для разных моделей. С 0021 на класс
        # можно задать ЦЕПОЧКУ моделей (GEMINI_FLASH_MODELS / GEMINI_PRO_MODELS): у каждой модели своя
        # квота на тех же ключах, и когда одна исчерпана, запрос идёт на следующую по порядку.
        def _short(model: str) -> str:
            return model[len("gemini-"):] if model.startswith("gemini-") else model

        def _register_chain(base_id: str, provider_class: str, models: list[str], *, direct: bool) -> None:
            for i, model in enumerate(models):
                provider = OpenAICompatibleProvider(
                    # первое звено сохраняет прежний id (профили запуска и выбор модели в UI ссылаются на него)
                    provider_id=base_id if i == 0 else f"{base_id}-{_short(model)}",
                    provider_class=provider_class,
                    base_url=settings.gemini_base_url,
                    model=model,
                    key_pool=gemini_pools.pool_for(gemini_keys, model),
                )
                if direct:
                    router.register(provider)
                else:
                    router.providers_by_class.setdefault(provider_class, []).append(provider)

        # 0027: цепочки не заданы в .env -> подобрать по списку моделей ключа (кэш на 12 ч; сеть недоступна = как раньше).
        if settings.gemini_models_auto and not (settings.gemini_flash_models and settings.gemini_pro_models):
            from aria.llm import gemini_discovery

            gemini_discovery.refresh_if_stale(gemini_keys, settings.gemini_base_url)
        flash_chain = settings.gemini_flash_chain
        pro_chain = settings.gemini_pro_chain
        _register_chain("gemini-flash", "free_tier_reasoning", flash_chain, direct=True)
        _register_chain("gemini-pro", "standard_reasoning", pro_chain, direct=True)
        # DeepSeek приоритет для premium_reasoning; если DeepSeek не задан/недоступен — полный фолбэк
        # на Gemini (пулы общие с gemini-pro: та же модель — та же квота).
        _register_chain("gemini-pro-premium-fallback", "premium_reasoning", pro_chain, direct=False)

    groq_keys = settings.groq_api_keys_list
    groq_main: list[str] = []
    groq_fast: list[str] = []
    groq_pools: dict[str, KeyPool] = {}
    if groq_keys:
        has_any_real_provider = True
        # Цепочки не заданы в .env -> подобрать по списку моделей пула (кэш на 12 ч; сеть недоступна = GROQ_MODEL).
        if settings.groq_models_auto and not settings.groq_models:
            from aria.llm import groq_discovery

            groq_discovery.refresh_if_stale(groq_keys, settings.groq_base_url)
        groq_main = settings.groq_main_chain
        groq_fast = settings.groq_fast_chain

    def _groq_pool(model: str) -> KeyPool:
        # одна квота Groq на ключ и модель: одна модель = один пул на все классы
        if model not in groq_pools:
            groq_pools[model] = KeyPool(groq_keys, name=f"groq-{model}")
        return groq_pools[model]

    def _register_groq_chain(base_id: str, provider_class: str, models: list[str], *, direct: bool) -> None:
        for i, model in enumerate(models):
            provider = OpenAICompatibleProvider(
                # первое звено сохраняет прежний id (профили запуска и выбор модели в UI ссылаются на него)
                provider_id=base_id if i == 0 else f"{base_id}-{re.sub(r'[^a-z0-9.]+', '-', model.lower()).strip('-')}",
                provider_class=provider_class,
                base_url=settings.groq_base_url,
                model=model,
                key_pool=_groq_pool(model),
            )
            if direct:
                router.register(provider)
            else:
                router.providers_by_class.setdefault(provider_class, []).append(provider)

    if groq_keys:
        _register_groq_chain("groq-llama-fast", "free_tier_reasoning", groq_fast, direct=True)
        _register_groq_chain("groq-llama-versatile", "standard_reasoning", groq_main, direct=True)
        # Последнее звено цепи "DeepSeek -> Gemini -> Groq" для premium_reasoning.
        _register_groq_chain("groq-llama-versatile-premium-fallback", "premium_reasoning", groq_main, direct=False)

    # --- Vision (multimodal): отдельный пул, не разделяет состояние с main pool ---
    vision_keys = settings.vision_gemini_api_keys_list
    if vision_keys:
        has_any_real_provider = True
        # Те же ключи и та же модель, что у чата, = тот же пул (одна квота, один cooldown). Отдельные
        # VISION_GEMINI_API_KEYS дают отдельный набор ключей и потому свой пул.
        vision_model = settings.gemini_flash_chain[0]
        router.register(
            OpenAICompatibleProvider(
                provider_id="gemini-vision",
                provider_class="vision_multimodal",
                base_url=settings.gemini_base_url,
                model=vision_model,
                key_pool=gemini_pools.pool_for(vision_keys, vision_model),
            )
        )

    # --- Sub-agent execution (Groq): дешёвый быстрый провайдер для delegate_task ---
    if groq_keys:
        _register_groq_chain("groq-subagent-fast", "subagent_execution", groq_fast, direct=True)

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

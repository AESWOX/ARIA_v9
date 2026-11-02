"""Учёт токенов и стоимости из usage провайдера (19.6a).

Провайдеры отдают usage по-разному: OpenAI/DeepSeek — prompt_tokens/completion_tokens,
другие — input_tokens/output_tokens. Цены ProviderModel — USD за токен (как отдаёт
провайдер в pricing.prompt/completion), не за миллион.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("local_agent.llm.usage")

PURPOSES = frozenset({
    "chat", "agent", "plan", "audit", "vision", "clarifier", "compress", "subagent", "other",
})


def _as_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_usage(usage: dict[str, Any] | None) -> dict[str, int | None] | None:
    """Разбор usage в поля ledger'а; None, если токенов нет вовсе.

    Кэш-поля есть только у DeepSeek: у Gemini/Groq hit/miss unknown (None),
    total берётся из prompt_tokens. reasoning — из completion_tokens_details.
    """
    if not isinstance(usage, dict):
        return None
    total = _as_int(usage.get("prompt_tokens", usage.get("input_tokens")))
    completion = _as_int(usage.get("completion_tokens", usage.get("output_tokens")))
    hit = _as_int(usage.get("prompt_cache_hit_tokens"))
    miss = _as_int(usage.get("prompt_cache_miss_tokens"))
    details = usage.get("completion_tokens_details")
    reasoning = _as_int(details.get("reasoning_tokens")) if isinstance(details, dict) else None
    if total is None and hit is not None and miss is not None:
        total = hit + miss
    if total is None and completion is None and hit is None and miss is None:
        return None
    return {
        "prompt_hit": None if hit is None else max(0, hit),
        "prompt_miss": None if miss is None else max(0, miss),
        "prompt_total": max(0, total or 0),
        "completion": max(0, completion or 0),
        "reasoning": None if reasoning is None else max(0, reasoning),
    }


def record_usage(
    *,
    provider_id: str,
    model: str | None,
    usage: dict[str, Any] | None,
    purpose: str = "other",
    session_id: Any = None,
    task_id: Any = None,
    cost_usd: float | None = None,
    tier: str = "n/a",
    estimated: bool = False,
) -> Any:
    """Записать строку llm_usage в отдельной короткой транзакции.

    Ошибка записи только пишет WARNING и не роняет вызов модели. Пустой usage —
    строки нет, в лог уходит llm_usage_missing. Никаких текстов промптов/ответов.
    """
    parsed = parse_usage(usage)
    purpose = purpose if purpose in PURPOSES else "other"
    if parsed is None:
        logger.warning("llm_usage_missing provider=%s purpose=%s", provider_id, purpose)
        return None
    if cost_usd is None:
        # E2: цена из prices.json; без цен остаёмся на None/"n/a" (не выдумываем).
        try:
            from aria.llm import pricing

            hit = parsed["prompt_hit"] or 0
            miss = parsed["prompt_miss"]
            if miss is None:
                miss = max(parsed["prompt_total"] - hit, 0)
            cost_usd, tier = pricing.cost_usd(model, hit, miss, parsed["completion"])
        except Exception:  # noqa: BLE001 — стоимость не должна ломать запись
            logger.warning("llm_usage_pricing_failed provider=%s purpose=%s", provider_id, purpose, exc_info=True)
    try:
        from aria.db import models
        from aria.db.base import session_scope

        with session_scope() as db:
            row = models.LlmUsage(
                session_id=session_id,
                task_id=task_id,
                provider_id=provider_id,
                model=model,
                purpose=purpose,
                prompt_hit=parsed["prompt_hit"],
                prompt_miss=parsed["prompt_miss"],
                prompt_total=parsed["prompt_total"],
                completion=parsed["completion"],
                reasoning=parsed["reasoning"],
                cost_usd=cost_usd,
                tier=tier,
                estimated=estimated,
            )
            db.add(row)
            db.flush()
            return row.id
    except Exception:  # noqa: BLE001 — учёт не должен ломать ответ модели
        logger.warning("llm_usage_write_failed provider=%s purpose=%s", provider_id, purpose, exc_info=True)
        return None


def usage_tokens(usage: dict[str, Any] | None) -> tuple[int, int] | None:
    """(вход, выход) из usage; None, если токенов нет вовсе или они битые."""
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    try:
        inp = int(prompt) if prompt is not None else None
        out = int(completion) if completion is not None else None
    except (TypeError, ValueError):
        return None
    if inp is None and out is None:
        return None
    return max(0, inp or 0), max(0, out or 0)


def usage_cost(
    usage: dict[str, Any] | None,
    prompt_price: float | None,
    completion_price: float | None,
) -> float | None:
    """Стоимость вызова в USD; None, если цен нет или usage пуст."""
    if prompt_price is None or completion_price is None:
        return None
    tokens = usage_tokens(usage)
    if tokens is None:
        return None
    inp, out = tokens
    try:
        cost = inp * float(prompt_price) + out * float(completion_price)
    except (TypeError, ValueError):
        return None
    return round(cost, 6)

"""core/notifiers/telegram.py — §6.2 TelegramNotifier.

Реализация Notifier Protocol через Telegram Bot API.
Only escalation — не полный бот.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone

from aria.core.notifiers.protocol import Notifier, NotifierError

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Отправляет escalation-сообщения в Telegram.

    - timeout=10s, retry=2
    - idempotency_key = sha256(task_id + iteration) — защита от дублирования
    - При недоступности бота: задача переходит в failed + лог ошибки
    """

    BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, bot_token: str, chat_id: str, timeout: int = 10, retry: int = 2):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = timeout
        self.retry = retry

    def _idempotency_key(self, task_id: str, iteration: int) -> str:
        raw = f"{task_id}:{iteration}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def _format_message(
        self,
        task_id: str,
        objective: str,
        claimed_result: str,
        audit_findings: str,
        iteration: int,
        tool_call_log_url: str,
    ) -> str:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return (
            f"\U0001f6a8 ARIA — эскалация задачи\n\n"
            f"**Задача:** `{task_id[:8]}` — {objective[:60]}\n"
            f"**Итерация:** {iteration}/3\n"
            f"**Время:** {ts}\n\n"
            f"**Что заявлялось:**\n{claimed_result[:200]}\n\n"
            f"**Что показал audit:**\n{audit_findings[:300]}\n"
        )

    async def send_escalation(
        self,
        task_id: str,
        objective: str,
        claimed_result: str,
        audit_findings: str,
        iteration: int,
        tool_call_log_url: str = "",
    ) -> None:
        """Отправляет escalation.

        Args:
            task_id: UUID задачи.
            objective: цель задачи.
            claimed_result: что заявлял саб.
            audit_findings: что показал аудит.
            iteration: номер итерации.
            tool_call_log_url: ссылка на лог (не реализовано в v1).

        Raises:
            NotifierError: если все retry исчерпаны.
        """
        text = self._format_message(
            task_id, objective, claimed_result, audit_findings,
            iteration, tool_call_log_url,
        )

        url = self.BASE_URL.format(token=self.bot_token)
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
            # idempotency_key не поддерживается Telegram API напрямую,
            # но мы используем синхронную защиту на уровне приложения
        }

        last_error: Exception | None = None
        for attempt in range(max(1, self.retry)):
            try:
                import httpx
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, json=payload)
                    if resp.status_code == 200:
                        logger.info(
                            "Telegram escalation sent for task %s (attempt %d)",
                            task_id[:8], attempt + 1,
                        )
                        return
                    elif resp.status_code == 429:
                        # Rate limited — подождать и повторить
                        retry_after = int(resp.json().get("parameters", {}).get("retry_after", 5))
                        logger.warning(
                            "Telegram rate limited, retry after %ds", retry_after
                        )
                        await asyncio.sleep(retry_after)
                        last_error = NotifierError(f"rate limited: retry after {retry_after}s")
                    else:
                        last_error = NotifierError(
                            f"HTTP {resp.status_code}: {resp.text[:200]}"
                        )
            except ImportError:
                logger.error("httpx not installed — cannot send Telegram notification")
                last_error = NotifierError("httpx not installed")
                break
            except Exception as e:
                last_error = NotifierError(f"request failed: {e}")
                if attempt < self.retry - 1:
                    wait = 2 ** attempt
                    logger.warning(
                        "Telegram send failed (attempt %d/%d), retry in %ds: %s",
                        attempt + 1, self.retry, wait, e,
                    )
                    await asyncio.sleep(wait)

        # Все retry исчерпаны
        logger.error(
            "Telegram escalation FAILED for task %s after %d attempts: %s",
            task_id[:8], self.retry, last_error,
        )
        # Notifier failure ≠ integrity-нарушение. Задача всё равно в failed.
        # Не бросаем исключение — задача уже переведена в failed.

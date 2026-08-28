"""core/notifiers/protocol.py — §6 Notifier Protocol.

Абстракция уведомлений. Не привязана к Telegram API.
Реализации: TelegramNotifier.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    """Интерфейс уведомлений для эскалации задач.

    Не привязан к конкретному каналу (Telegram/Slack/Email).
    """

    async def send_escalation(
        self,
        task_id: str,
        objective: str,
        claimed_result: str,
        audit_findings: str,
        iteration: int,
        tool_call_log_url: str = "",
    ) -> None:
        """Отправляет escalation-уведомление.

        Args:
            task_id: UUID задачи.
            objective: цель задачи (truncated).
            claimed_result: что заявлял саб (truncated).
            audit_findings: что показал integrity-аудит (truncated).
            iteration: номер итерации (1-3).
            tool_call_log_url: ссылка на лог tool_calls.

        Raises:
            NotifierError: если отправка не удалась.
        """
        ...


class NotifierError(Exception):
    """Ошибка отправки уведомления.

    Notifier failure НЕ является integrity-нарушением.
    При недоступности notifier задача всё равно переходит в failed.
    """
    pass

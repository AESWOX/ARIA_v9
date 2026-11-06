"""Telegram gate: personal chats are unavailable until the bot is linked.

Public channels (`t.me/s`) stay readable through the `telegram_channel` tool; this
gate only stops the model from answering about the owner's own account/messages
from memory when no bot token and chat id are configured.
"""
from __future__ import annotations

import re

_PERSONAL_RE = re.compile(
    r"(?:мой|моем|моём|моего|мою|мои|моих|мне)\s+(?:телеграм|телеграме|телеграма|тг|телеге)"
    r"|(?:мои|моих)\s+(?:сообщени|переписк|чат|канал)"
    r"|(?:что|кто)\s+(?:мне\s+)?(?:пишет|писал|написал|прислал).{0,40}(?:телеграм|телеге|тг)"
    r"|(?:прочитай|покажи|проверь|посмотри|глянь).{0,30}(?:телеграм|телеге|тг)"
    r"|my\s+telegram"
    r"|telegram\s+(?:messages|chats|account|inbox)"
    , re.IGNORECASE,
)
_PUBLIC_RE = re.compile(r"t\.me/|(?:^|\s)@[A-Za-z0-9_]{4,}")


def is_personal_request(text: str) -> bool:
    content = (text or "").strip()
    if not content:
        return False
    if _PUBLIC_RE.search(content):
        return False
    return bool(_PERSONAL_RE.search(content))


def is_linked(settings: object) -> bool:
    token = str(getattr(settings, "telegram_bot_token", "") or "").strip()
    chat_id = str(getattr(settings, "telegram_chat_id", "") or "").strip()
    return bool(token and chat_id)


def notice_text(lang: str = "ru") -> str:
    if str(lang).lower().startswith("en"):
        return (
            "Telegram is not connected: I have no access to your account or messages.\n"
            "How to connect:\n"
            "1. Open @BotFather → /newbot → copy the token.\n"
            "2. Add the bot as an admin to your channel or chat.\n"
            "3. Enter the token and the channel as @name below.\n"
            "Public channels already work via t.me/s — send a link."
        )
    return (
        "Telegram не подключён: у меня нет доступа к твоему аккаунту и сообщениям.\n"
        "Как подключить:\n"
        "1. Открой @BotFather → /newbot → скопируй токен.\n"
        "2. Добавь бота админом в свой канал или чат.\n"
        "3. Введи токен и канал через @ (например @my_channel) в форме ниже.\n"
        "Публичные каналы уже работают через t.me/s — просто пришли ссылку."
    )


def blocked_notice(text: str, settings: object) -> dict | None:
    if is_linked(settings) or not is_personal_request(text):
        return None
    from aria.core.clarifier import detect_language

    return {
        "notice": "telegram_not_connected",
        "text": notice_text(detect_language(text)),
    }

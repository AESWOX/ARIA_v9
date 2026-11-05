"""Honest recall: never deny past work after an empty memory/vault search."""
from __future__ import annotations

import re

_RECALL_RE = re.compile(
    r"мы\s+(?:такое|это|так|уже)\s+(?:делали|генерили|писали|создавали|обсуждали)"
    r"|мы\s+(?:делали|генерили|писали)\s+(?:такое|это)"
    r"|(?:ты|вы)\s+помнишь"
    r"|помнишь,?\s+(?:мы|как)"
    r"|это\s+уже\s+было"
    r"|did\s+we\s+(?:do|make|build|write|generate)"
    r"|have\s+we\s+(?:done|made|built|written|generated)"
    r"|do\s+you\s+remember"
    , re.IGNORECASE,
)


def is_recall_question(text: str) -> bool:
    return bool(_RECALL_RE.search(text or ""))


def missing_note(text: str, blocks: list[str]) -> str:
    """An explicit instruction for the model after an empty recall search."""
    if not is_recall_question(text):
        return ""
    if any((block or "").strip() for block in blocks):
        return ""
    from aria.core.clarifier import detect_language

    if detect_language(text) == "en":
        return (
            "Recall check: memory and notes were searched and nothing matched. "
            "Do not deny past work — say plainly that it was not found in memory or notes "
            "and that chats cannot be searched."
        )
    return (
        "Проверка памяти: в памяти и заметках совпадений нет. "
        "Не отрицай прошлую работу — скажи прямо, что не нашёл в памяти и заметках, "
        "а искать по чатам не умеешь."
    )

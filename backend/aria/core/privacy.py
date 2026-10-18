"""core/privacy.py — PR-7: DLP только для чата.

Маскировка применяется к исходящему промпту чата и обратима на ответе модели:
владелец видит свои данные, провайдер — плейсхолдеры. Агентский цикл (tool calls)
намеренно НЕ трогается: подстановка плейсхолдеров в пути, команды и аргументы
инструментов ломает исполнение, а не защищает данные.

«Narrative framing» и обёртки «вымышленный фрейм» здесь не делаются: это обход
поведения модели, а не качество защиты.
"""
from __future__ import annotations

import re

PLACEHOLDER_TPL = "⟦{kind}_{n}⟧"
_PLACEHOLDER_RE = re.compile(r"⟦[A-ZА-Я]+_\d+⟧")

# Секреты и PII, которые видны в промпте текстом.
_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("SECRET", re.compile(
        r"\b(?:sk-[A-Za-z0-9_\-]{8,}"
        r"|AKIA[0-9A-Z]{16}"
        r"|gh[pousr]_[A-Za-z0-9]{16,}"
        r"|xox[baprs]-[A-Za-z0-9\-]{10,}"
        r"|AIza[0-9A-Za-z_\-]{20,})\b"
    )),
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
)


class EntityAnonymizer:
    """Обратимая маскировка сущностей в тексте.

    ``shadow_map`` живёт в БД по сессии и передаётся в каждый вызов: плейсхолдеры
    стабильны между сообщениями (одно и то же значение всегда даёт один и тот же
    плейсхолдер), и это единственное место, где хранится соответствие.
    """

    def __init__(self, names: list[str] | None = None, shadow_map: dict[str, str] | None = None):
        self.names = [n.strip() for n in (names or []) if n and n.strip()]
        self.shadow_map: dict[str, str] = dict(shadow_map or {})
        self._reverse: dict[str, str] = {value: key for key, value in self.shadow_map.items()}
        self._counters: dict[str, int] = {}

    # ---------- маскировка ----------

    def _placeholder(self, kind: str, value: str) -> str:
        existing = self._reverse.get(value)
        if existing:
            return existing
        n = self._counters.get(kind, 0) + 1
        while PLACEHOLDER_TPL.format(kind=kind, n=n) in self.shadow_map:
            n += 1
        self._counters[kind] = n
        placeholder = PLACEHOLDER_TPL.format(kind=kind, n=n)
        self.shadow_map[placeholder] = value
        self._reverse[value] = placeholder
        return placeholder

    def anonymize(self, text: str) -> str:
        out = text or ""
        for kind, pattern in _PATTERNS:
            out = pattern.sub(lambda m, k=kind: self._placeholder(k, m.group(0)), out)
        for name in self.names:
            # П.5.4 ТЗ: в плейсхолдер идёт НАПИСАНИЕ ИЗ ТЕКСТА пользователя (m.group(0)),
            # а не значение из настроек — иначе IGNORECASE-поиск возвращал чужое написание.
            out = re.sub(
                rf"(?<!\w){re.escape(name)}(?!\w)",
                lambda m: self._placeholder("NAME", m.group(0)),
                out,
                flags=re.IGNORECASE,
            )
        return out

    def anonymize_messages(self, messages: list) -> list:
        """Маскирует список сообщений, сохраняя роль и тип объекта."""
        out = []
        for msg in messages:
            content = self.anonymize(getattr(msg, "content", "") or "")
            if hasattr(msg, "content"):
                try:
                    msg.content = content
                    out.append(msg)
                    continue
                except Exception:  # noqa: BLE001 — slots/dataclass без setattr
                    pass
            out.append(type(msg)(role=getattr(msg, "role", "user"), content=content) if hasattr(msg, "role") else content)
        return out

    # ---------- восстановление ----------

    def restore(self, text: str) -> str:
        out = text or ""
        for placeholder, value in self.shadow_map.items():
            out = out.replace(placeholder, value)
        return out

    @staticmethod
    def has_placeholders(text: str) -> bool:
        return bool(_PLACEHOLDER_RE.search(text or ""))

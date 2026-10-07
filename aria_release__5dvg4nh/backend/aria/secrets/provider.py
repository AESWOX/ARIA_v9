"""SecretProvider interface + EnvSecretProvider.

EnvSecretProvider — реализация по умолчанию: читает os.environ.
Не требует .env на месте (чистый extract без ключей → пустые списки → SKIPPED).
Для новых источников (encrypted file, system keyring, vault) — наследовать SecretProvider.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod


class SecretProvider(ABC):
    """Абстрактный источник секретов.

    Единственное место в приложении, которое знает, где лежат ключи.
    KeyPool / OpenAICompatibleProvider вызывают SecretProvider, не читают env сами.
    """

    @abstractmethod
    def get_key(self, name: str) -> str | None:
        """Один ключ (например DEEPSEEK_API_KEY). None если не найден."""
        ...

    @abstractmethod
    def get_key_list(self, name: str) -> list[str]:
        """Список ключей (GEMINI_API_KEYS, GROQ_API_KEYS). Пустой список если нет."""
        ...


class EnvSecretProvider(SecretProvider):
    """Читает os.environ. Не требует .env — работает и на чистом extract.

    В чистом архиве без .env: все get_key/get_key_list возвращают None/[].
    Это правильное поведение — dod_verify.py видит SKIPPED для провайдеров.
    """

    def get_key(self, name: str) -> str | None:
        return os.environ.get(name) or None

    def get_key_list(self, name: str) -> list[str]:
        raw = os.environ.get(name, "")
        return [k.strip() for k in raw.split(",") if k.strip()]

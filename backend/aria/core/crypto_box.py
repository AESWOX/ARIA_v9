"""core/crypto_box.py — П.5.4 ТЗ + раунд 2 п.3: шифрование карты DLP (вариант Б).

Схема (вариант Б ТЗ round2): HMAC-SHA256-CTR + encrypt-then-MAC (MAC на HMAC-SHA256
над nonce||body), префикс ``enc:v2:``. Целостность проверяется при расшифровке:
подмена байта шифртекста/nonce/тега отклоняется (InvalidToken).

Ключ: секрет-провайдер (``ARIA_DLP_KEY``) либо файл ``<home>/.local-agent-ui/dlp.key``
с правами 0600. Раунд 2: молчаливый детерминированный запасной ключ УБРАН — если ключ
получить нельзя, поднимается :class:`KeyUnavailableError`; DLP отключается с
предупреждением в логе (ограничение зафиксировано в DOD.md).

Миграция «лениво» (совместимость с раундом 1): значения ``enc:v1:`` расшифровываются
и при следующей записи перешифровываются в ``enc:v2:`` (decrypt читает обе версии,
encrypt повторно шифрует всё, что пришло с префиксом v1).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PREFIX = "enc:v2:"
LEGACY_PREFIX = "enc:v1:"
_NONCE_BYTES = 12
_MAC_BYTES = 32
_cached_key: bytes | None = None


class KeyUnavailableError(RuntimeError):
    """Раунд 2 (п.3 Б): ключ DLP недоступен — шифрование невозможно, DLP отключается."""


def _key_path() -> Path:
    return Path.home() / ".local-agent-ui" / "dlp.key"


def _load_key() -> bytes:
    """Ключ из секрет-провайдера или файла 0600. Иначе — исключение, не «предсказуемый» ключ."""
    global _cached_key
    if _cached_key is not None:
        return _cached_key
    try:
        from aria.secrets import secret_provider

        raw = secret_provider.get_key("ARIA_DLP_KEY")
        if raw:
            _cached_key = hashlib.sha256(raw.encode("utf-8")).digest()
            return _cached_key
    except KeyUnavailableError:
        raise
    except Exception:  # noqa: BLE001 — секрет-провайдер недоступен, пробуем файл
        logger.warning("crypto_box: секрет-провайдер недоступен, пробую файл ключа")
    path = _key_path()
    try:
        if path.exists():
            data = path.read_bytes().strip()
            if data:
                _cached_key = hashlib.sha256(data).digest()
                return _cached_key
        path.parent.mkdir(parents=True, exist_ok=True)
        material = base64.urlsafe_b64encode(os.urandom(32))
        path.write_bytes(material)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        _cached_key = hashlib.sha256(material).digest()
        return _cached_key
    except OSError as exc:
        # Раунд 2 (п.3 Б): детерминированная производная от хоста УДАЛЕНА —
        # такой ключ фактически предсказуем. Честный отказ: DLP отключается.
        logger.error("crypto_box: ключ DLP недоступен (%s) — DLP шифрование отключено", exc)
        raise KeyUnavailableError("DLP key unavailable: no secret provider and key file cannot be used") from exc


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def _mac(key: bytes, nonce: bytes, body: bytes) -> bytes:
    """Encrypt-then-MAC: тег над nonce||body (раунд 2, п.3 Б)."""
    return hmac.new(key, nonce + body, hashlib.sha256).digest()


def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and (value.startswith(PREFIX) or value.startswith(LEGACY_PREFIX))


def encrypt(value: str) -> str:
    """Шифрует строку (enc:v2:, AES-независимая схема с MAC). Идемпотентно для v2.

    Значения ``enc:v1:`` (раунд 1) при повторной записи перешифровываются в v2 —
    ленивая миграция без отдельного скрипта. Значение с недоступным ключом НЕ
    сохраняется открытым текстом: поднимается KeyUnavailableError.
    """
    if not isinstance(value, str) or not value:
        return value
    if value.startswith(PREFIX):
        return value
    if value.startswith(LEGACY_PREFIX):
        # Ленивая миграция: расшифровываем v1 и ниже шифруем заново в v2.
        value = decrypt(value)
    key = _load_key()
    nonce = os.urandom(_NONCE_BYTES)
    raw = value.encode("utf-8")
    body = bytes(a ^ b for a, b in zip(raw, _keystream(key, nonce, len(raw))))
    tag = _mac(key, nonce, body)
    return PREFIX + base64.urlsafe_b64encode(nonce + body + tag).decode("ascii")


def decrypt(value: object) -> str:
    """Обратная операция (v2 с проверкой MAC; v1 — легаси без MAC, читается для миграции).

    Подмена любого байта шифртекста/nonce/тега в v2 отклоняется (InvalidToken).
    """
    if not isinstance(value, str):
        return str(value)
    if value.startswith(PREFIX):
        try:
            raw = base64.urlsafe_b64decode(value[len(PREFIX):].encode("ascii"))
        except Exception as exc:  # noqa: BLE001
            raise ValueError("crypto_box: повреждённое значение enc:v2:") from exc
        if len(raw) < _NONCE_BYTES + _MAC_BYTES:
            raise ValueError("crypto_box: повреждённое значение enc:v2: (слишком короткое)")
        nonce, body, tag = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:-_MAC_BYTES], raw[-_MAC_BYTES:]
        key = _load_key()
        if not hmac.compare_digest(_mac(key, nonce, body), tag):
            raise ValueError("crypto_box: проверка целостности не пройдена (подмена шифртекста)")
        plain = bytes(a ^ b for a, b in zip(body, _keystream(key, nonce, len(body))))
        return plain.decode("utf-8", "replace")
    if value.startswith(LEGACY_PREFIX):
        # Легаси раунда 1: CTR без MAC. Читаем для миграции; при недоступном ключе — ошибка.
        try:
            raw = base64.urlsafe_b64decode(value[len(LEGACY_PREFIX):].encode("ascii"))
        except Exception:  # noqa: BLE001
            return str(value)
        nonce, body = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        key = _load_key()
        plain = bytes(a ^ b for a, b in zip(body, _keystream(key, nonce, len(body))))
        return plain.decode("utf-8", "replace")
    return str(value)

"""core/crypto_box.py — П.5.4 ТЗ: значения карты DLP не лежат в БД открытым текстом.

Вариант, выбранный для п.5.4: шифровать значения целиком (префикс ``enc:v1:``),
а не отказываться от персиста вида ``SECRET``. Ключ берётся у секрет-провайдера
(``ARIA_DLP_KEY``), иначе создаётся файл ``<home>/.local-agent-ui/dlp.key`` с правами 0600.
Схема — HMAC-SHA256-KEYS в режиме счётчика (CTR): без ключа значение не восстановить,
а сам ключ в открытом виде не хранится в SQLite.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path

PREFIX = "enc:v1:"
_NONCE_BYTES = 12
_cached_key: bytes | None = None


def _key_path() -> Path:
    return Path.home() / ".local-agent-ui" / "dlp.key"


def _load_key() -> bytes:
    global _cached_key
    if _cached_key is not None:
        return _cached_key
    from aria.secrets import secret_provider

    raw = secret_provider.get_key("ARIA_DLP_KEY")
    if raw:
        _cached_key = hashlib.sha256(raw.encode("utf-8")).digest()
        return _cached_key
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
    except OSError:
        # Ключ негде хранить: производная от хоста — шифрование деградирует,
        # но значения не остаются открытым текстом в БД.
        _cached_key = hashlib.sha256(("aria-dlp-" + str(Path.home())).encode()).digest()
        return _cached_key


def _keystream(nonce: bytes, length: int) -> bytes:
    key = _load_key()
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def is_encrypted(value: object) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value: str) -> str:
    """Шифрует строку. Уже зашифрованное значение возвращается как есть (идемпотентно)."""
    if not isinstance(value, str) or not value:
        return value
    if is_encrypted(value):
        return value
    nonce = os.urandom(_NONCE_BYTES)
    raw = value.encode("utf-8")
    body = bytes(a ^ b for a, b in zip(raw, _keystream(nonce, len(raw))))
    return PREFIX + base64.urlsafe_b64encode(nonce + body).decode("ascii")


def decrypt(value: object) -> str:
    """Обратная операция. Значение без префикса возвращается как есть (старые строки)."""
    if not is_encrypted(value):
        return str(value)
    try:
        raw = base64.urlsafe_b64decode(str(value)[len(PREFIX):].encode("ascii"))
    except Exception:  # noqa: BLE001
        return str(value)
    nonce, body = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    plain = bytes(a ^ b for a, b in zip(body, _keystream(nonce, len(body))))
    return plain.decode("utf-8", "replace")

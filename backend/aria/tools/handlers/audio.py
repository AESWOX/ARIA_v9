"""aria/tools/handlers/audio.py — транскрипция голосовых (MP3/WAV/M4A/OGG/WebM).

Опционально: локальный whisper не требуется. Если задан GROQ_API_KEY (или
``groq_api_keys`` в Settings), используем Groq Whisper API. Нет ключа —
возвращаем понятную ошибку, а не падаем.
"""
from __future__ import annotations

import mimetypes
import os
from pathlib import Path

import httpx

from aria.config import get_settings

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
DEFAULT_MODEL = "whisper-large-v3-turbo"
MAX_AUDIO_BYTES = 25 * 1024 * 1024  # лимит Groq
_AUDIO_EXT = {".mp3", ".mpga", ".mpeg", ".wav", ".m4a", ".ogg", ".webm", ".flac"}


def _groq_keys() -> list[str]:
    settings = get_settings()
    raw = str(getattr(settings, "groq_api_keys", "") or "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        env = os.environ.get("GROQ_API_KEY", "").strip()
        if env:
            keys = [env]
    return keys


async def audio_transcribe(input_json: dict, sandbox_root: str, **_ctx) -> dict:
    """Транскрибировать аудио внутри sandbox. Возвращает {text, provider, model}."""
    from aria.tools.handlers.files import PathEscapeError, _resolve_within

    raw = str(input_json.get("path") or "").strip()
    if not raw:
        return {"error": "path is required"}
    try:
        path = _resolve_within(Path(sandbox_root), raw)
    except PathEscapeError as exc:
        return {"error": str(exc)}
    if not path.exists() or not path.is_file():
        return {"error": f"file not found: {raw}"}
    if path.suffix.lower() not in _AUDIO_EXT:
        return {"error": f"unsupported audio type: {path.suffix or '(no extension)'}"}
    size = path.stat().st_size
    if size > MAX_AUDIO_BYTES:
        return {"error": f"audio is too large: {size} bytes (limit {MAX_AUDIO_BYTES})"}

    keys = _groq_keys()
    if not keys:
        return {
            "error": (
                "Транскрипция недоступна: не задан GROQ_API_KEY. "
                "Добавь ключ Groq в Settings → Keys (Whisper) или пришли текст голосового."
            )
        }

    model = str(input_json.get("model") or DEFAULT_MODEL)
    data: dict[str, str] = {"model": model, "response_format": "json"}
    language = str(input_json.get("language") or "").strip()
    if language:
        data["language"] = language
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    last_error = ""
    for key in keys:
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                response = await client.post(
                    GROQ_URL,
                    headers={"Authorization": f"Bearer {key}"},
                    data=data,
                    files={"file": (path.name, path.read_bytes(), mime)},
                )
        except Exception as exc:  # noqa: BLE001 — сеть/таймаут не должны ронять задачу
            last_error = f"Groq Whisper request failed: {exc}"
            continue
        if response.status_code >= 400:
            last_error = f"Groq Whisper HTTP {response.status_code}: {response.text[:300]}"
            continue
        try:
            payload = response.json()
        except ValueError:
            last_error = "Groq Whisper returned non-JSON response"
            continue
        text = str(payload.get("text") or "").strip()
        if not text:
            last_error = "Groq Whisper returned an empty transcript"
            continue
        return {"path": raw, "text": text, "provider": "groq", "model": model}
    return {"error": last_error or "Groq Whisper failed"}

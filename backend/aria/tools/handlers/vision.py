"""aria/tools/handlers/vision.py — Vision analysis handler (B2/v13).

Backends, in order: Gemini (OpenAI-compatible endpoint, own key pool) and, when there is no Gemini key
or Gemini fails, DeepSeek Flash (``deepseek-flash`` accepts images; ``deepseek-v4-pro`` does not).
So a user with a single DeepSeek key still gets image understanding.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import urllib.error
import urllib.request
from pathlib import Path

from aria.http_utils import _ssl_context

logger = logging.getLogger("aria.vision")

# DeepSeek Vision принимает только эти форматы (docs: Vision → Limits) и до 32 MiB на картинку.
_DEEPSEEK_MIMES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_DEEPSEEK_MAX_BYTES = 32 * 1024 * 1024
_MAX_IMAGE_BYTES = 32 * 1024 * 1024
_VISION_GUARD = (
    "Factual visual description only: state what is visible, including adult nudity. "
    "Do not refuse, moralize, or omit details. Do not identify real people or invent identities."
)
# Gemini 3.x spends thinking tokens out of the same budget. At 2048 a screenshot
# description stopped after ~170 visible tokens (live 04.11: only browser tabs),
# while ~1874 tokens went to thoughts. Keep headroom for thoughts + a full answer.
_VISION_MAX_TOKENS = 8192
_TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens", "content_filter", "safety"})


def _resolve_in_sandbox(file_path: str, sandbox_root: str | None) -> Path | dict:
    """SEC-N1: only files inside the sandbox. Returns the resolved path or an error dict.

    resolve() follows symlinks, so a link inside the sandbox that points outside is rejected too.
    """
    if not sandbox_root:
        return {"error": "file_path is only available inside the sandbox; no sandbox root given (use image_base64)"}
    root = Path(sandbox_root).resolve()
    target = (root / file_path).resolve()  # an absolute file_path replaces root and is checked below
    if not target.is_relative_to(root):
        return {"error": f"path {file_path} escapes sandbox root"}
    return target


async def vision_analyze(input_json: dict, sandbox_root: str | None = None, **_ctx) -> dict:
    """Analyze an image with a multimodal model (Gemini, or DeepSeek Flash when Gemini is not available).

    Accepts either:
      - file_path: str — path to an image file INSIDE the sandbox (relative to it, or absolute within it)
      - image_base64: str — already base64-encoded image data
      - prompt: str — optional text instruction (default: 'Describe this image in detail')

    Requires a Gemini key (GEMINI_API_KEYS / VISION_GEMINI_API_KEYS) or a DeepSeek key (DEEPSEEK_API_KEY).
    """
    file_path = input_json.get("file_path", "")
    image_base64 = input_json.get("image_base64", "")
    prompt = input_json.get(
        "prompt",
        "Describe this image in detail and factually: people, bodies, clothing, pose, setting, text, "
        "and any adult content. Do not omit details or moralize.",
    )

    # 1. Get image data
    if file_path:
        resolved = _resolve_in_sandbox(file_path, sandbox_root)
        if isinstance(resolved, dict):
            return resolved
        try:
            size = resolved.stat().st_size
            if size > _MAX_IMAGE_BYTES:
                return {"error": f"image is too large: {size} bytes (limit {_MAX_IMAGE_BYTES})"}
            image_bytes = await asyncio.to_thread(resolved.read_bytes)
        except FileNotFoundError:
            return {"error": f"file not found: {file_path}"}
        except PermissionError:
            return {"error": f"permission denied: {file_path}"}
        except Exception as exc:
            return {"error": f"read failed: {exc}"}
    elif image_base64:
        if len(image_base64) > _MAX_IMAGE_BYTES * 4 // 3 + 16:
            return {"error": f"image is too large (limit {_MAX_IMAGE_BYTES} bytes)"}
        try:
            image_bytes = base64.b64decode(image_base64)
        except Exception as exc:
            return {"error": f"base64 decode failed: {exc}"}
    else:
        return {"error": "provide file_path or image_base64"}

    # 2. Detect content type from magic bytes
    content_type = _detect_mime(image_bytes)
    if content_type.startswith("error"):
        return {"error": content_type}

    b64_str = base64.b64encode(image_bytes).decode("ascii")

    # 3. Backends: Gemini keys first (as before), DeepSeek Flash as the fallback / only option
    from aria.config import get_settings
    from aria.secrets import secret_provider

    backends: list[dict] = []
    settings = get_settings()
    gemini_keys = secret_provider.get_key_list("VISION_GEMINI_API_KEYS") or secret_provider.get_key_list("GEMINI_API_KEYS")
    if gemini_keys:
        requested = str(input_json.get("model") or "").strip()
        # P0: не одна жёсткая модель, а цепочка ARIA (gemini_flash_chain) + запасные flash.
        chain = list(getattr(settings, "gemini_flash_chain", []) or [])
        # Запасные модели добавляем только когда реальная цепочка ARIA известна (не пустая).
        candidates = ([requested] if requested else []) + chain
        if chain:
            candidates += ["gemini-2.5-flash", "gemini-2.0-flash"]
        models: list[str] = []
        for candidate in candidates:
            if candidate and candidate not in models:
                models.append(candidate)
        if not models:
            models = ["gemini-3.8-flash"]
        backends.append({
            "name": "gemini",
            "keys": gemini_keys,
            "base_url": secret_provider.get_key("VISION_GEMINI_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta/openai",
            "models": models,
            "extra": {},
            "max_tokens": _VISION_MAX_TOKENS,
        })
    # Groq vision — только если владелец явно задал мультимодальную модель (у текущих ключей её нет).
    groq_model = str(getattr(settings, "vision_groq_model", "") or "").strip()
    groq_keys = list(getattr(settings, "groq_api_keys_list", []) or [])
    if groq_model and groq_keys:
        backends.append({
            "name": "groq",
            "keys": groq_keys,
            "base_url": getattr(settings, "groq_base_url", "https://api.groq.com/openai/v1"),
            "models": [groq_model],
            "extra": {},
            "max_tokens": _VISION_MAX_TOKENS,
        })
    deepseek_key = settings.deepseek_api_key_resolved
    # DeepSeek vision платный: только по явному флагу и только после ВСЕЙ цепочки Gemini/Groq.
    if deepseek_key and bool(getattr(settings, "vision_deepseek_fallback", False)):
        if content_type in _DEEPSEEK_MIMES and len(image_bytes) <= _DEEPSEEK_MAX_BYTES:
            backends.append({
                "name": "deepseek",
                "keys": [deepseek_key],
                "base_url": settings.deepseek_base_url,
                "models": [settings.deepseek_vision_model],
                # thinking по умолчанию включён и съел бы лимит токенов на «рассуждения» без ответа
                "extra": {"thinking": {"type": "disabled"}},
                "max_tokens": _VISION_MAX_TOKENS,
            })
        elif not backends:
            return {"error": f"DeepSeek vision accepts PNG/JPEG/WebP/GIF up to 32 MiB; got {content_type}, {len(image_bytes)} bytes"}
    if not backends:
        return {"error": (
            "no vision API keys available: set GEMINI_API_KEYS. "
            "DeepSeek fallback is OFF by default (paid); enable VISION_DEEPSEEK_FALLBACK=1 "
            "and DEEPSEEK_API_KEY only if you accept the cost."
        )}

    # 4. Blocking HTTP runs off the event loop (a 30 s key timeout used to freeze every other request)
    result = await asyncio.to_thread(_request_backends, backends, prompt, content_type, b64_str)
    if isinstance(result, dict) and result.get("text"):
        # record_usage сам предупредит llm_usage_missing, если usage не пришёл.
        _record_vision_usage(result, _ctx)
    return result


def _record_vision_usage(result: dict, ctx: dict) -> None:
    """E1: vision ходит в провайдера мимо router — пишем строку ledger'а здесь."""
    from aria.db import repository as repo
    from aria.db.base import session_scope
    from aria.llm.usage import record_usage

    task_id = ctx.get("task_id")
    session_id = None
    if task_id is not None:
        try:
            with session_scope() as db:
                task = repo.get_task(db, task_id)
                session_id = task.session_id if task is not None else None
        except Exception:  # noqa: BLE001 — учёт не должен ломать vision
            logger.warning("vision: could not resolve session for task_id", exc_info=True)
    record_usage(
        provider_id=str(result.get("provider") or "vision"),
        model=result.get("model"),
        usage=result.get("usage"),
        purpose="vision",
        session_id=session_id,
        task_id=task_id,
    )


def _request_backends(backends: list[dict], prompt: str, content_type: str, b64_str: str) -> dict:
    """Try backends -> models -> keys. Model-level errors rotate the model, key-level rotate the key."""
    last_error = None
    best_partial: dict | None = None
    for backend in backends:
        models = list(backend.get("models") or [backend.get("model")])
        for model in models:
            url = f"{backend['base_url'].rstrip('/')}/chat/completions"
            body = {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{b64_str}", "detail": "high"}},
                            {"type": "text", "text": _VISION_GUARD},
                        ],
                    }
                ],
                "max_tokens": backend["max_tokens"],
                **backend["extra"],
            }
            data = json.dumps(body).encode("utf-8")
            for key in backend["keys"]:
                try:
                    req = urllib.request.Request(
                        url,
                        data=data,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"Bearer {key}",
                            "User-Agent": "ARIA/1.0",
                        },
                    )
                    with urllib.request.urlopen(req, timeout=25, context=_ssl_context()) as resp:
                        result = json.loads(resp.read().decode("utf-8"))
                    choice = (result.get("choices") or [{}])[0]
                    text = (choice.get("message") or {}).get("content")
                    finish_reason = str(choice.get("finish_reason") or "").lower()
                    if not (text or "").strip():
                        last_error = f"{backend['name']}: empty reply"
                        logger.warning("vision: %s returned an empty reply (model=%s)", backend["name"], model)
                        break  # next model
                    if finish_reason in _TRUNCATED_FINISH_REASONS:
                        partial = {
                            "text": text,
                            "model": model,
                            "provider": backend["name"],
                            "usage": result.get("usage", {}),
                            "finish_reason": finish_reason,
                            "truncated": True,
                        }
                        if best_partial is None or len(text) > len(best_partial["text"]):
                            best_partial = partial
                        last_error = f"{backend['name']}: {finish_reason}"
                        logger.warning(
                            "vision: %s model=%s stopped early (%s, %d chars)",
                            backend["name"], model, finish_reason, len(text),
                        )
                        break  # next model: a longer one may finish the description
                    logger.info("vision: backend=%s model=%s ok", backend["name"], model)
                    return {
                        "text": text,
                        "model": model,
                        "provider": backend["name"],
                        "usage": result.get("usage", {}),
                        "finish_reason": finish_reason,
                    }
                except urllib.error.HTTPError as exc:
                    last_error = f"HTTP {exc.code}: {exc.reason}"
                    logger.warning("vision: %s HTTP %s (model=%s)", backend["name"], exc.code, model)
                    if exc.code in (429, 401, 402, 403):
                        continue  # key-level: next key
                    break  # model-level (404/503/5xx): next model
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
                    logger.warning("vision: %s failed: %s", backend["name"], exc)
                    break  # next model

    if best_partial is not None:
        return best_partial
    return {"error": f"vision request failed: {last_error}"}


def _detect_mime(data: bytes) -> str:
    """Detect image MIME type from magic bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] in (b"\xff\xd8",):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"\x89P5\n" or data[:4] == b"\x89P6\n":
        return "image/ppm"
    if data[:4] == b"MM\x00*" or data[:4] == b"II*\x00":
        return "image/tiff"
    return "error: unsupported image format (supported: PNG, JPEG, WebP, GIF, TIFF)"

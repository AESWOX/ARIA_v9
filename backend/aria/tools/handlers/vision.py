"""aria/tools/handlers/vision.py — Vision analysis handler (B2/v13).

Uses Gemini's OpenAI-compatible endpoint with image_url (base64) format.
Separate key pool from main agent answers.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path

logger = logging.getLogger("aria.vision")


async def vision_analyze(input_json: dict, **_ctx) -> dict:
    """Analyze an image using Gemini multimodal vision.

    Accepts either:
      - file_path: str — absolute path to an image file on disk
      - image_base64: str — already base64-encoded image data
      - prompt: str — optional text instruction (default: 'Describe this image in detail')

    Requires a working Gemini API key configured via GEMINI_API_KEYS or
    VISION_GEMINI_API_KEYS env vars (SecretProvider).
    """
    file_path = input_json.get("file_path", "")
    image_base64 = input_json.get("image_base64", "")
    prompt = input_json.get("prompt", "Describe this image in detail. Be concise but thorough.")

    # 1. Get image data
    if file_path:
        try:
            with open(file_path, "rb") as f:
                image_bytes = f.read()
        except FileNotFoundError:
            return {"error": f"file not found: {file_path}"}
        except PermissionError:
            return {"error": f"permission denied: {file_path}"}
        except Exception as exc:
            return {"error": f"read failed: {exc}"}
    elif image_base64:
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

    # 3. Get API keys from SecretProvider
    from aria.secrets import secret_provider
    keys = secret_provider.get_key_list("VISION_GEMINI_API_KEYS") or secret_provider.get_key_list("GEMINI_API_KEYS")
    if not keys:
        return {"error": "no vision API keys available (VISION_GEMINI_API_KEYS or GEMINI_API_KEYS)"}

    # 4. Build request
    import json
    import urllib.request
    import urllib.error

    base_url = secret_provider.get_key("VISION_GEMINI_BASE_URL") or "https://generativelanguage.googleapis.com/v1beta/openai"
    model = input_json.get("model", "gemini-2.5-flash")
    url = f"{base_url.rstrip('/')}/chat/completions"

    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{b64_str}", "detail": "high"}},
                ],
            }
        ],
        "max_tokens": 1024,
    }
    data = json.dumps(body).encode("utf-8")

    # 5. Try keys round-robin
    last_error = None
    for key in keys:
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
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            text = result["choices"][0]["message"]["content"]
            return {
                "text": text,
                "model": model,
                "usage": result.get("usage", {}),
            }
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}: {exc.reason}"
            if exc.code in (429, 401, 403):
                continue  # try next key
            break
        except Exception as exc:
            last_error = str(exc)
            break

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

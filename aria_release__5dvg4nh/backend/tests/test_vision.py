"""Tests for vision_analyze handler (B2/v13).

Offline: mocks urllib.request.urlopen to test request/response cycle.
Live smoke: in live_smoke.py (Блок C).
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from aria.tools.handlers.vision import vision_analyze, _detect_mime

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
TEST_IMAGE = FIXTURE_DIR / "test_image.png"

SAMPLE_GEMINI_RESPONSE = {
    "choices": [{"message": {"content": "This is a red square pixel."}}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
}

SAMPLE_ERROR_RESPONSE = json.dumps({"error": {"message": "API key not valid"}})


class TestVisionMimeDetection:
    def test_detect_png(self):
        with open(TEST_IMAGE, "rb") as f:
            data = f.read()
        assert _detect_mime(data) == "image/png"

    def test_detect_unsupported(self):
        assert _detect_mime(b"not an image").startswith("error")


class TestVisionAnalyzeOffline:
    async def test_no_input_returns_error(self):
        result = await vision_analyze({})
        assert "error" in result

    async def test_file_not_found_returns_error(self):
        result = await vision_analyze({"file_path": "/nonexistent/image.png"})
        assert "error" in result

    async def test_missing_both_params_returns_error(self):
        result = await vision_analyze({"prompt": "desc"})
        assert "error" in result

    async def test_successful_analysis_with_file_path(self):
        """Mock the Gemini API call and verify response is parsed correctly."""
        with open(TEST_IMAGE, "rb") as f:
            img_data = f.read()
        b64 = base64.b64encode(img_data).decode("ascii")

        with patch("aria.secrets.secret_provider.get_key_list", return_value=["fake-key"]):
            with patch("urllib.request.urlopen", new_callable=MagicMock) as mock:
                mock.return_value.__enter__.return_value.read.return_value = json.dumps(SAMPLE_GEMINI_RESPONSE).encode("utf-8")
                result = await vision_analyze({"file_path": str(TEST_IMAGE)})

        assert "text" in result
        assert result["text"] == "This is a red square pixel."
        assert result["model"] == "gemini-2.5-flash"

    async def test_successful_analysis_with_base64(self):
        with open(TEST_IMAGE, "rb") as f:
            img_data = f.read()
        b64 = base64.b64encode(img_data).decode("ascii")

        with patch("aria.secrets.secret_provider.get_key_list", return_value=["fake-key"]):
            with patch("urllib.request.urlopen", new_callable=MagicMock) as mock:
                mock.return_value.__enter__.return_value.read.return_value = json.dumps(SAMPLE_GEMINI_RESPONSE).encode("utf-8")
                result = await vision_analyze({"image_base64": b64})

        assert "text" in result
        assert result["text"] == "This is a red square pixel."

    async def test_http_error_triggers_key_rotation(self):
        from urllib.error import HTTPError
        with patch("aria.secrets.secret_provider.get_key_list", return_value=["bad-key", "bad-key-2"]):
            with patch("urllib.request.urlopen", side_effect=[
                HTTPError("http://test", 429, "Too Many Requests", {}, None),
                HTTPError("http://test", 429, "Too Many Requests", {}, None),
            ]):
                result = await vision_analyze({"file_path": str(TEST_IMAGE)})

        assert "error" in result
        assert "429" in result["error"] or "Too Many" in result.get("error", "")

    async def test_no_keys_returns_error(self):
        with patch("aria.secrets.secret_provider.get_key_list", return_value=[]):
            result = await vision_analyze({"file_path": str(TEST_IMAGE)})
        assert "error" in result

"""Tests for vision_analyze handler (B2/v13).

Offline: mocks urllib.request.urlopen to test request/response cycle.
Live smoke: in live_smoke.py (Блок C).
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from aria.tools.handlers.vision import vision_analyze, _detect_mime

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
TEST_IMAGE = FIXTURE_DIR / "test_image.png"
SANDBOX = str(FIXTURE_DIR)

SAMPLE_GEMINI_RESPONSE = {
    "choices": [{"message": {"content": "This is a red square pixel."}}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110},
}

SAMPLE_ERROR_RESPONSE = json.dumps({"error": {"message": "API key not valid"}})


class _Resp:
    def __init__(self, payload: dict):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body


@pytest.fixture
def no_deepseek_key():
    """Settings without a DeepSeek key, whatever other tests left in env or the key store.

    vision.py reads the DeepSeek key through get_settings(), not through secret_provider,
    so patching get_key_list alone does not isolate it (CI py3.12 failure, run #17).
    """
    stub = SimpleNamespace(
        deepseek_api_key_resolved="",
        deepseek_base_url="https://api.deepseek.com",
        deepseek_vision_model="deepseek-flash",
    )
    with patch("aria.config.get_settings", return_value=stub):
        yield


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
        result = await vision_analyze({"file_path": "/nonexistent/image.png"}, sandbox_root=SANDBOX)
        assert "error" in result

    async def test_missing_both_params_returns_error(self):
        result = await vision_analyze({"prompt": "desc"})
        assert "error" in result

    async def test_successful_analysis_with_file_path(self):
        """Mock the Gemini API call and verify response is parsed correctly."""
        with patch("aria.secrets.secret_provider.get_key_list", return_value=["fake-key"]):
            with patch("urllib.request.urlopen", new_callable=MagicMock) as mock:
                mock.return_value.__enter__.return_value.read.return_value = json.dumps(SAMPLE_GEMINI_RESPONSE).encode("utf-8")
                result = await vision_analyze({"file_path": str(TEST_IMAGE)}, sandbox_root=SANDBOX)

        assert "text" in result
        assert result["text"] == "This is a red square pixel."
        assert result["model"] == "gemini-3.8-flash"

    async def test_default_prompt_allows_factual_adult_description(self):
        with patch("aria.secrets.secret_provider.get_key_list", return_value=["fake-key"]):
            with patch("urllib.request.urlopen", new_callable=MagicMock) as mock:
                mock.return_value.__enter__.return_value.read.return_value = json.dumps(SAMPLE_GEMINI_RESPONSE).encode("utf-8")
                await vision_analyze({"file_path": str(TEST_IMAGE)}, sandbox_root=SANDBOX)
        body = json.loads(mock.call_args.args[0].data.decode("utf-8"))
        texts = [p.get("text", "") for p in body["messages"][0]["content"] if p.get("type") == "text"]
        assert any("adult nudity" in text for text in texts)
        # Gemini 3.x thinking tokens share this budget; 2048 truncated the answer.
        assert body["max_tokens"] >= 8192

    async def test_tool_timeout_covers_the_model_fallback_chain(self):
        from aria.tools.registry import TOOL_REGISTRY

        assert TOOL_REGISTRY["vision_analyze"].timeout_sec >= 90

    async def test_truncated_reply_tries_next_model(self):
        truncated = {
            "choices": [{"message": {"content": "only browser tabs"}, "finish_reason": "length"}],
            "usage": {},
        }
        complete = {
            "choices": [{"message": {"content": "full page description"}, "finish_reason": "stop"}],
            "usage": {},
        }
        with patch("aria.secrets.secret_provider.get_key_list", return_value=["fake-key"]):
            with patch("urllib.request.urlopen", side_effect=[_Resp(truncated), _Resp(complete)]) as mock:
                result = await vision_analyze({"file_path": str(TEST_IMAGE)}, sandbox_root=SANDBOX)

        assert result["text"] == "full page description"
        assert result["finish_reason"] == "stop"
        assert mock.call_count == 2

    async def test_all_truncated_returns_longest_partial_with_flag(self):
        first = {
            "choices": [{"message": {"content": "short"}, "finish_reason": "length"}],
            "usage": {},
        }
        second = {
            "choices": [{"message": {"content": "much longer partial description"}, "finish_reason": "content_filter"}],
            "usage": {},
        }
        third = {
            "choices": [{"message": {"content": "medium partial"}, "finish_reason": "safety"}],
            "usage": {},
        }
        with patch("aria.secrets.secret_provider.get_key_list", return_value=["fake-key"]):
            with patch("urllib.request.urlopen", side_effect=[_Resp(first), _Resp(second), _Resp(third)]):
                result = await vision_analyze({"file_path": str(TEST_IMAGE)}, sandbox_root=SANDBOX)

        assert result["truncated"] is True
        assert result["text"] == "much longer partial description"
        assert result["finish_reason"] == "content_filter"

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

    async def test_http_error_triggers_key_rotation(self, no_deepseek_key):
        from urllib.error import HTTPError

        def always_429(req, **kwargs):
            raise HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

        with patch("aria.secrets.secret_provider.get_key_list", return_value=["bad-key", "bad-key-2"]):
            with patch("urllib.request.urlopen", side_effect=always_429):
                result = await vision_analyze({"file_path": str(TEST_IMAGE)}, sandbox_root=SANDBOX)

        assert "error" in result
        assert "429" in result["error"]

    async def test_no_keys_returns_error(self, no_deepseek_key):
        with patch("aria.secrets.secret_provider.get_key_list", return_value=[]):
            result = await vision_analyze({"file_path": str(TEST_IMAGE)}, sandbox_root=SANDBOX)
        assert "error" in result


# ── DeepSeek Flash как бэкенд vision (deepseek-flash принимает изображения, deepseek-v4-pro нет) ──────────────

import pytest  # noqa: E402
from urllib.error import HTTPError  # noqa: E402

SAMPLE_DEEPSEEK_RESPONSE = {
    "choices": [{"message": {"content": "A red square."}}],
    "usage": {"prompt_tokens": 400, "completion_tokens": 5, "total_tokens": 405},
}


def _ok(payload):
    resp = MagicMock()
    resp.__enter__.return_value.read.return_value = json.dumps(payload).encode("utf-8")
    return resp


def test_main_model_refusal_does_not_hide_the_vision_result():
    from aria.core.loop import _last_successful_tool_text

    history = [
        SimpleNamespace(
            content_json={
                "tool_name": "vision_analyze",
                "status": "ok",
                "output": {"text": "Detailed factual description."},
            }
        )
    ]
    assert _last_successful_tool_text(history, "vision_analyze") == "Detailed factual description."


def test_render_tool_result_keeps_full_vision_description():
    from aria.core import loop as loop_mod

    text = "Полное фактическое описание кадра. " * 400
    assert len(text) > loop_mod.MAX_TOOL_RESULT_CHARS
    message = SimpleNamespace(
        content_json={
            "tool_name": "vision_analyze",
            "status": "ok",
            "output": {"text": text, "model": "gemini-3.6-flash"},
        }
    )

    rendered = loop_mod._render_tool_result(message)

    assert text in rendered
    assert "truncated" not in rendered


def test_render_tool_result_caps_plain_text_at_its_own_limit():
    from aria.core import loop as loop_mod

    text = "x" * (loop_mod.MAX_TEXT_TOOL_RESULT_CHARS + 500)
    message = SimpleNamespace(
        content_json={"tool_name": "vision_analyze", "status": "ok", "output": {"text": text}}
    )

    rendered = loop_mod._render_tool_result(message)

    assert "truncated 500 chars" in rendered
    assert len(rendered) < len(text)


def test_verbatim_request_is_detected():
    from aria.core.loop import _asks_for_verbatim

    assert _asks_for_verbatim("какой ответ тебе пришёл дословно?") is True
    assert _asks_for_verbatim("Give it to me verbatim, word for word") is True
    assert _asks_for_verbatim("что на картинке?") is False


def test_prefer_tool_text_rules():
    from aria.core.loop import _prefer_tool_text

    assert _prefer_tool_text("short", "a" * 2000, refused=False) is True
    assert _prefer_tool_text("a" * 2000, "b" * 2000, refused=False) is False
    assert _prefer_tool_text("short", "a" * 2000, refused=True) is True
    assert _prefer_tool_text("short", "small", refused=False) is False
    assert _prefer_tool_text("short", None, refused=True) is False


async def test_model_summary_is_replaced_by_full_vision_text():
    from aria.core import loop as loop_mod
    from aria.db import models as m
    from aria.db import repository as repo
    from aria.db.base import session_scope
    from aria.db.enums import TaskStatus
    from aria.llm.router import ProviderRouter
    from tests.test_compliance_v2 import FakeProvider

    vision_text = "Полное описание содержимого изображения. " * 100
    summary = "Ответ обрезан: виден только верх браузера."
    provider = FakeProvider(text=summary)
    router = ProviderRouter()
    router.register(provider)
    sandbox = str(FIXTURE_DIR)

    with session_scope() as db:
        session = repo.create_session(db, "vision relay")
        task = repo.create_task(db, session, role="general", objective="describe the image")
        repo.set_task_status(db, task, TaskStatus.approved)
        repo.append_message(
            db,
            session,
            role="tool",
            content="vision_analyze -> ok",
            content_json={
                "tool_name": "vision_analyze",
                "status": "ok",
                "output": {"text": vision_text},
            },
        )
        task_id, session_id = task.id, session.id

    async def _no_finalize(*_args, **_kwargs):
        return None

    original = loop_mod.finalize_task
    loop_mod.finalize_task = _no_finalize
    try:
        await loop_mod.execute_agent_loop(task_id, router, sandbox)
        with session_scope() as db:
            answers = [
                msg.content
                for msg in repo.list_messages(db, session_id, limit=1000)
                if msg.role == "assistant"
            ]
        assert answers and answers[-1] == vision_text.strip()
    finally:
        loop_mod.finalize_task = original
        with session_scope() as db:
            db.query(m.Task).filter(m.Task.id == task_id).delete()
            db.query(m.Session).filter(m.Session.id == session_id).delete()


@pytest.fixture
def deepseek_key(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-deepseek-vision-1234567890")
    from aria import config

    # DeepSeek vision теперь платный last-resort и по умолчанию выключен.
    monkeypatch.setattr(config.get_settings(), "vision_deepseek_fallback", True)


class TestVisionDeepSeekBackend:
    async def test_single_deepseek_key_is_enough(self, deepseek_key):
        with patch("aria.secrets.secret_provider.get_key_list", return_value=[]):
            with patch("urllib.request.urlopen", return_value=_ok(SAMPLE_DEEPSEEK_RESPONSE)) as mock:
                result = await vision_analyze({"file_path": str(TEST_IMAGE), "prompt": "what is it"}, sandbox_root=SANDBOX)
        assert result["text"] == "A red square."
        assert result["provider"] == "deepseek" and result["model"] == "deepseek-flash"
        req = mock.call_args.args[0]
        assert req.full_url.endswith("/chat/completions") and "deepseek" in req.full_url
        assert req.get_header("Authorization") == "Bearer sk-test-deepseek-vision-1234567890"
        body = json.loads(req.data.decode("utf-8"))
        assert body["model"] == "deepseek-flash", "v4-pro изображения не принимает"
        assert body["thinking"] == {"type": "disabled"}, "thinking по умолчанию съел бы лимит токенов"
        parts = body["messages"][0]["content"]
        assert parts[0] == {"type": "text", "text": "what is it"}
        assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")

    async def test_gemini_stays_first_when_its_key_exists(self, deepseek_key):
        with patch("aria.secrets.secret_provider.get_key_list", return_value=["gem-key"]):
            with patch("urllib.request.urlopen", return_value=_ok(SAMPLE_GEMINI_RESPONSE)) as mock:
                result = await vision_analyze({"file_path": str(TEST_IMAGE)}, sandbox_root=SANDBOX)
        assert result["provider"] == "gemini"
        assert mock.call_count == 1

    async def test_falls_back_to_deepseek_when_gemini_is_rate_limited(self, deepseek_key):
        def route(req, **kwargs):
            if "deepseek" in req.full_url:
                return _ok(SAMPLE_DEEPSEEK_RESPONSE)
            raise HTTPError(req.full_url, 429, "Too Many Requests", {}, None)

        with patch("aria.secrets.secret_provider.get_key_list", return_value=["gem-key"]):
            with patch("urllib.request.urlopen", side_effect=route) as mock:
                result = await vision_analyze({"file_path": str(TEST_IMAGE)}, sandbox_root=SANDBOX)
        assert result["provider"] == "deepseek" and result["text"] == "A red square."
        assert mock.call_count >= 2

    async def test_both_backends_failing_reports_the_last_error(self, deepseek_key):
        def route(req, **kwargs):
            code = 402 if "deepseek" in req.full_url else 429
            raise HTTPError(req.full_url, code, "err", {}, None)

        with patch("aria.secrets.secret_provider.get_key_list", return_value=["gem-key"]):
            with patch("urllib.request.urlopen", side_effect=route):
                result = await vision_analyze({"file_path": str(TEST_IMAGE)}, sandbox_root=SANDBOX)
        assert "error" in result and "402" in result["error"]

    async def test_empty_deepseek_reply_is_an_error_not_an_empty_answer(self, deepseek_key):
        with patch("aria.secrets.secret_provider.get_key_list", return_value=[]):
            with patch("urllib.request.urlopen", return_value=_ok({"choices": [{"message": {"content": ""}}]})):
                result = await vision_analyze({"file_path": str(TEST_IMAGE)}, sandbox_root=SANDBOX)
        assert "error" in result and "empty" in result["error"]

    async def test_formats_deepseek_cannot_read_are_rejected_without_a_request(self, deepseek_key):
        tiff = base64.b64encode(b"II*\x00" + b"\x00" * 64).decode("ascii")
        with patch("aria.secrets.secret_provider.get_key_list", return_value=[]):
            with patch("urllib.request.urlopen") as mock:
                result = await vision_analyze({"image_base64": tiff})
        assert "error" in result and "PNG/JPEG/WebP/GIF" in result["error"]
        mock.assert_not_called()

    async def test_no_keys_at_all_names_every_option(self, monkeypatch, no_deepseek_key):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        with patch("aria.secrets.secret_provider.get_key_list", return_value=[]):
            result = await vision_analyze({"file_path": str(TEST_IMAGE)}, sandbox_root=SANDBOX)
        assert "DEEPSEEK_API_KEY" in result["error"] and "GEMINI_API_KEYS" in result["error"]

    def test_vision_model_is_flash_not_pro(self):
        from aria.config import get_settings

        assert get_settings().deepseek_vision_model == "deepseek-flash"
        assert "pro" not in get_settings().deepseek_vision_model


class TestVisionSandbox:
    """SEC-N1: vision_analyze reads images only inside the sandbox."""

    def _png(self, directory: Path, name: str = "pic.png") -> Path:
        path = directory / name
        path.write_bytes(TEST_IMAGE.read_bytes())
        return path

    async def test_absolute_path_outside_sandbox_is_rejected_without_a_request(self, tmp_path):
        sandbox = tmp_path / "sb"
        sandbox.mkdir()
        outside = self._png(tmp_path)
        with patch("aria.secrets.secret_provider.get_key_list", return_value=["k"]):
            with patch("urllib.request.urlopen") as mock:
                result = await vision_analyze({"file_path": str(outside)}, sandbox_root=str(sandbox))
        assert "escapes sandbox" in result["error"]
        mock.assert_not_called()

    async def test_dotdot_traversal_is_rejected(self, tmp_path):
        sandbox = tmp_path / "sb"
        sandbox.mkdir()
        self._png(tmp_path)
        with patch("urllib.request.urlopen") as mock:
            result = await vision_analyze({"file_path": "../pic.png"}, sandbox_root=str(sandbox))
        assert "escapes sandbox" in result["error"]
        mock.assert_not_called()

    async def test_symlink_pointing_outside_is_rejected(self, tmp_path):
        import pytest

        sandbox = tmp_path / "sb"
        sandbox.mkdir()
        outside = self._png(tmp_path)
        try:
            (sandbox / "link.png").symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks are not available here")
        with patch("urllib.request.urlopen") as mock:
            result = await vision_analyze({"file_path": "link.png"}, sandbox_root=str(sandbox))
        assert "escapes sandbox" in result["error"]
        mock.assert_not_called()

    async def test_no_sandbox_root_means_no_file_access(self, tmp_path):
        outside = self._png(tmp_path)
        result = await vision_analyze({"file_path": str(outside)})
        assert "sandbox" in result["error"]

    async def test_relative_and_absolute_paths_inside_the_sandbox_work(self, tmp_path):
        sandbox = tmp_path / "sb"
        (sandbox / "img").mkdir(parents=True)
        inside = self._png(sandbox / "img")
        for given in ("img/pic.png", str(inside)):
            with patch("aria.secrets.secret_provider.get_key_list", return_value=["fake-key"]):
                with patch("urllib.request.urlopen", new_callable=MagicMock) as mock:
                    mock.return_value.__enter__.return_value.read.return_value = json.dumps(SAMPLE_GEMINI_RESPONSE).encode("utf-8")
                    result = await vision_analyze({"file_path": given}, sandbox_root=str(sandbox))
            assert result.get("text") == "This is a red square pixel."

    async def test_oversized_file_is_refused_before_reading(self, tmp_path, monkeypatch):
        from aria.tools.handlers import vision as vision_mod

        sandbox = tmp_path / "sb"
        sandbox.mkdir()
        self._png(sandbox)
        monkeypatch.setattr(vision_mod, "_MAX_IMAGE_BYTES", 10)
        result = await vision_analyze({"file_path": "pic.png"}, sandbox_root=str(sandbox))
        assert "too large" in result["error"]

    async def test_slow_provider_call_does_not_block_the_event_loop(self, tmp_path):
        import asyncio
        import time

        sandbox = tmp_path / "sb"
        sandbox.mkdir()
        self._png(sandbox)
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        def slow(*a, **k):
            time.sleep(0.4)
            return _ok(SAMPLE_GEMINI_RESPONSE)

        task = asyncio.create_task(ticker())
        with patch("aria.secrets.secret_provider.get_key_list", return_value=["fake-key"]):
            with patch("urllib.request.urlopen", side_effect=slow):
                result = await vision_analyze({"file_path": "pic.png"}, sandbox_root=str(sandbox))
        task.cancel()
        assert "text" in result
        assert ticks >= 8  # loop kept running during the 0.4 s blocking call

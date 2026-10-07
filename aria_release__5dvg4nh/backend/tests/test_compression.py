"""Tests for compression 429 rotation and dead-key handling (B3/v13).

Uses httpx.Response with _request set so raise_for_status() works correctly.
Tests the actual KeyPool transitions in _summarize().
"""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from aria.llm.compression import _summarize
from aria.llm.key_pool import KeyPool


@pytest.fixture
def pool():
    """Fresh KeyPool with 3 keys for each test."""
    p = KeyPool(["key-a", "key-b", "key-c"], name="compression-test")
    return p


def _req():
    return httpx.Request("POST", "http://test.local/chat/completions")


def _ok_response(text: str) -> httpx.Response:
    """httpx.Response with _request set so raise_for_status() works."""
    r = httpx.Response(
        status_code=200,
        json={"choices": [{"message": {"content": text}}]},
        request=_req(),
    )
    return r


def _err_response(status: int, reason: str = "") -> httpx.Response:
    """httpx.Response that produces an HTTPStatusError on raise_for_status."""
    return httpx.Response(status_code=status, request=_req())


class TestCompressionKeyRotation:
    """429 → rotate, 403 → dead, all-dead → NoAvailableKeys."""

    async def test_success_first_try(self, pool):
        """Первый ключ работает → возвращаем результат, остальные не трогаем."""
        with patch("aria.llm.compression._get_compression_pool", return_value=pool):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_instance = mock_cls.return_value.__aenter__.return_value
                mock_instance.post.return_value = _ok_response("summary text")

                result = await _summarize("test dialog")

        assert result == "summary text"
        # available: total - dead - cooling_down = 3 - 0 - 0
        assert pool.status()["dead"] == 0
        assert pool.status()["cooling_down"] == 0

    async def test_429_rotation(self, pool):
        """Первый ключ 429 → rate_limited, второй работает."""
        with patch("aria.llm.compression._get_compression_pool", return_value=pool):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_instance = mock_cls.return_value.__aenter__.return_value
                rl = _err_response(429)
                ok = _ok_response("summary after 429")
                mock_instance.post.side_effect = [rl, ok]

                result = await _summarize("test dialog")

        assert result == "summary after 429"
        assert pool.status()["dead"] == 0
        assert pool.status()["cooling_down"] == 1  # key-a rate limited

    async def test_403_dead_key(self, pool):
        """Первый ключ 403 → dead, второй работает."""
        with patch("aria.llm.compression._get_compression_pool", return_value=pool):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_instance = mock_cls.return_value.__aenter__.return_value
                err = _err_response(403)
                ok = _ok_response("summary after 403")
                mock_instance.post.side_effect = [err, ok]

                result = await _summarize("test dialog")

        assert result == "summary after 403"
        assert pool.status()["dead"] == 1

    async def test_all_keys_rate_limited(self, pool):
        """Все ключи 429 → raises HTTPStatusError (последняя ошибка)."""
        with patch("aria.llm.compression._get_compression_pool", return_value=pool):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_instance = mock_cls.return_value.__aenter__.return_value
                mock_instance.post.side_effect = [
                    _err_response(429), _err_response(429), _err_response(429),
                ]

                with pytest.raises(httpx.HTTPStatusError):
                    await _summarize("test dialog")

        assert pool.status()["cooling_down"] == 3  # all rate limited

    async def test_all_keys_dead(self, pool):
        """Все ключи 401 → все dead, raises HTTPStatusError (последняя ошибка)."""
        with patch("aria.llm.compression._get_compression_pool", return_value=pool):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_instance = mock_cls.return_value.__aenter__.return_value
                mock_instance.post.side_effect = [
                    _err_response(401), _err_response(401), _err_response(401),
                ]

                with pytest.raises(httpx.HTTPStatusError):
                    await _summarize("test dialog")

        assert pool.status()["dead"] == 3

    async def test_mixed_errors(self, pool):
        """429 → 403 → 200: третий ключ спасает."""
        with patch("aria.llm.compression._get_compression_pool", return_value=pool):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_instance = mock_cls.return_value.__aenter__.return_value
                mock_instance.post.side_effect = [
                    _err_response(429, "RL1"),
                    _err_response(403, "Dead2"),
                    _ok_response("third time lucky"),
                ]

                result = await _summarize("test dialog")

        assert result == "third time lucky"
        assert pool.status()["dead"] == 1
        assert pool.status()["cooling_down"] == 1  # only key-c left

    async def test_non_http_error_propagates(self, pool):
        """ConnectionError, таймаут и т.п. — не swallow (только HTTPStatusError)."""
        with patch("aria.llm.compression._get_compression_pool", return_value=pool):
            with patch("httpx.AsyncClient") as mock_cls:
                mock_instance = mock_cls.return_value.__aenter__.return_value
                mock_instance.post.side_effect = RuntimeError("network partition")

                with pytest.raises(RuntimeError, match="network partition"):
                    await _summarize("test dialog")

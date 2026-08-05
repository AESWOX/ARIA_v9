"""Tests for web_search handler (B1/v13).

Offline: mocks urllib.request.urlopen to test parsing logic.
Live smoke: in live_smoke.py (Блок C).
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from aria.tools.handlers.web import web_search

SAMPLE_HTML = """<!DOCTYPE html>
<html><body>
<div class="result">
    <a href="https://example.com/article-1" class="result-link">Test Result One</a>
    <span class="snippet">This is a test snippet for result one.</span>
</div>
<div class="result">
    <a href="https://example.com/article-2" class="result-link">Test Result Two</a>
    <span class="snippet">Snippet for second result.</span>
</div>
<div class="result">
    <a href="https://example.com/article-3" class="result-link">Another Test</a>
    <span class="snippet">Third result snippet here.</span>
</div>
</body></html>"""

EMPTY_HTML = "<html><body><p>No results found.</p></body></html>"


class TestWebSearchOffline:
    """Offline tests with mocked HTTP — no external calls."""

    async def test_returns_results(self):
        with patch("urllib.request.urlopen", new_callable=MagicMock) as mock:
            mock.return_value.__enter__.return_value.read.return_value = SAMPLE_HTML.encode("utf-8")
            result = await web_search({"query": "test query", "max_results": 5})
        assert "results" in result
        assert len(result["results"]) == 3
        assert result["results"][0]["title"] == "Test Result One"
        assert result["results"][0]["url"] == "https://example.com/article-1"

    async def test_empty_query_returns_error(self):
        result = await web_search({"query": ""})
        assert "error" in result
        assert "empty" in result["error"]

    async def test_max_results_limits_output(self):
        with patch("urllib.request.urlopen", new_callable=MagicMock) as mock:
            mock.return_value.__enter__.return_value.read.return_value = SAMPLE_HTML.encode("utf-8")
            result = await web_search({"query": "test", "max_results": 2})
        assert len(result["results"]) == 2

    async def test_no_results_returns_empty_list(self):
        with patch("urllib.request.urlopen", new_callable=MagicMock) as mock:
            mock.return_value.__enter__.return_value.read.return_value = EMPTY_HTML.encode("utf-8")
            result = await web_search({"query": "zzzznotfoundxxxxx"})
        assert result["results"] == []

    async def test_network_error_returns_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            result = await web_search({"query": "test"})
            assert "error" in result

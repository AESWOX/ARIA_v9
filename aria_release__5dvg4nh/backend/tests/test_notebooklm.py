"""Tests for notebook integration — config, client degradation, tool registration."""
from __future__ import annotations

import pytest
from aria.config import get_settings
from aria.integrations.notebooklm.client import NotebookClient
from aria.integrations.notebooklm.tool import notebook_query
from aria.tools.registry import TOOL_REGISTRY


class TestNotebookConfig:
    def test_notebook_disabled_by_default(self):
        """Fresh config instance must have notebook_enabled == False."""
        settings = get_settings()
        assert settings.notebook_enabled is False
        assert settings.notebook_auth_path == ""
        assert settings.notebook_timeout_sec == 30

    def test_notebook_tool_registered(self):
        """notebook_query must be in TOOL_REGISTRY."""
        assert "notebook_query" in TOOL_REGISTRY
        spec = TOOL_REGISTRY["notebook_query"]
        assert spec.tool_name == "notebook_query"
        assert "prompt" in spec.input_schema.get("properties", {})
        assert spec.timeout_sec == 60
        assert spec.risk_level.value == "low"


class TestNotebookClient:
    def test_health_when_disabled(self):
        """notebook_query must return error when disabled."""
        result = notebook_query(prompt="test query")
        assert result["success"] is False
        assert "disabled" in result.get("error", "")

    def test_client_degrades_gracefully(self):
        """Client must not raise on missing library — return degraded result."""
        client = NotebookClient(auth_path="/nonexistent/auth.json")
        health = client.health()
        assert "ok" in health
        assert health["ok"] is False  # no cookies file, no library

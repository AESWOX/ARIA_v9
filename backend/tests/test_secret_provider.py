"""Tests for SecretProvider layer (D1/v13)."""
import os
from unittest.mock import patch

import pytest

from aria.secrets import EnvSecretProvider, SecretProvider, secret_provider
from aria.secrets.provider import SecretProvider as SPABC


class TestEnvSecretProvider:
    def test_get_key_returns_env_value(self):
        with patch.dict(os.environ, {"TEST_SECRET": "super-secret-value"}, clear=False):
            sp = EnvSecretProvider()
            assert sp.get_key("TEST_SECRET") == "super-secret-value"

    def test_get_key_returns_none_for_missing(self):
        sp = EnvSecretProvider()
        assert sp.get_key("DOES_NOT_EXIST_XYZ") is None

    def test_get_key_list_parses_comma_separated(self):
        with patch.dict(os.environ, {"TEST_KEYS": "key1,key2,key3"}, clear=False):
            sp = EnvSecretProvider()
            assert sp.get_key_list("TEST_KEYS") == ["key1", "key2", "key3"]

    def test_get_key_list_handles_empty(self):
        sp = EnvSecretProvider()
        assert sp.get_key_list("DOES_NOT_EXIST_XYZ") == []

    def test_get_key_list_strips_whitespace(self):
        with patch.dict(os.environ, {"TEST_KEYS": " key1 , key2 , key3 "}, clear=False):
            sp = EnvSecretProvider()
            assert sp.get_key_list("TEST_KEYS") == ["key1", "key2", "key3"]

    def test_get_key_list_single_item(self):
        with patch.dict(os.environ, {"TEST_KEYS": "only-one-key"}, clear=False):
            sp = EnvSecretProvider()
            assert sp.get_key_list("TEST_KEYS") == ["only-one-key"]


class TestSecretProviderInterface:
    def test_abstract_methods_exist(self):
        # Verify EnvSecretProvider implements the full interface
        sp = EnvSecretProvider()
        assert hasattr(sp, "get_key")
        assert hasattr(sp, "get_key_list")

    def test_module_instance_is_env_provider(self):
        assert isinstance(secret_provider, EnvSecretProvider)

    def test_settings_integration(self):
        """Verify settings properties delegate to SecretProvider."""
        from aria.config import get_settings

        with patch.dict(os.environ, {"GEMINI_API_KEYS": "test-key-1,test-key-2"}, clear=False):
            s = get_settings()
            keys = s.gemini_api_keys_list
            assert "test-key-1" in keys
            assert "test-key-2" in keys

    def test_settings_deepseek_resolved(self):
        from aria.config import get_settings

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test-deepseek"}, clear=False):
            s = get_settings()
            assert s.deepseek_api_key_resolved == "sk-test-deepseek"

    def test_env_provider_is_swappable(self):
        """SecretProvider is a class, not a module — can be subclassed."""
        assert issubclass(EnvSecretProvider, SPABC)
        # Verify you can create a custom provider
        class MockProvider(SecretProvider):
            def get_key(self, name: str) -> str | None:
                return f"mock-{name}"
            def get_key_list(self, name: str) -> list[str]:
                return [f"mock-{name}-v1"]
        mp = MockProvider()
        assert mp.get_key("TEST") == "mock-TEST"
        assert mp.get_key_list("TEST") == ["mock-TEST-v1"]

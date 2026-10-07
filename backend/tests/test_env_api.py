"""Keys/env page: saved values reach the running backend and survive a restart."""
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.routers import env as env_mod


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(env_mod, "_ENV_FILE", tmp_path / ".env")
    app = FastAPI()
    app.include_router(env_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "test"
    yield TestClient(app)
    os.environ.pop("OBSIDIAN_VAULT_PATH", None)
    get_settings.cache_clear()


def test_vault_path_change_applies_immediately_and_is_persisted(client, tmp_path):
    from aria.storage import obsidian_vault as ov

    default_vault = ov.vault_root()
    my_vault = tmp_path / "MyObsidian"
    assert client.put("/env", json={"key": "OBSIDIAN_VAULT_PATH", "value": str(my_vault)}).json() == {"ok": True}
    assert get_settings().OBSIDIAN_VAULT_PATH == str(my_vault)
    assert ov.vault_root() == my_vault and my_vault.is_dir()
    assert f"OBSIDIAN_VAULT_PATH={my_vault}" in (tmp_path / ".env").read_text(encoding="utf-8")

    # clearing the value falls back to the default instead of resolving to "."
    assert client.request("DELETE", "/env", json={"key": "OBSIDIAN_VAULT_PATH"}).json() == {"ok": True}
    assert ov.vault_root() == default_vault

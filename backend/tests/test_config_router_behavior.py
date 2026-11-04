"""Config router (/config*, /auth*, /dashboard*, /llm/* helpers): behaviour.

Each test asserts status + response shape, and for mutating routes that the
change is visible on the following GET. No network, no real LLM.
Secrets must never appear in any payload (contract: keys live in secret_provider).
"""
from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from aria.api.auth import token_store
from aria.main import app

LOWERCASE_SECRETS = (
    "deepseek_api_key",
    "gemini_api_key",
    "groq_api_key",
    "openai_api_key",
    "telegram_bot_token",
)
UPPERCASE_SECRETS = ("B2_KEY_ID", "B2_APPLICATION_KEY", "LOCAL_AGENT_UI_PIN")


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _h():
    if not token_store.current_token():
        token_store.issue()
    return {"X-Local-Agent-Token": token_store.current_token()}


def _assert_no_secret_fields(payload: dict, names: tuple[str, ...] = LOWERCASE_SECRETS):
    leaked = [name for name in names if name in payload]
    assert leaked == [], f"secret fields in payload: {leaked}"


def test_get_config_hides_secrets_and_has_defaults(client):
    r = client.get("/config", headers=_h())
    assert r.status_code == 200
    body = r.json()
    _assert_no_secret_fields(body)
    assert body["loop_max_iterations"] == 15
    assert body["audit_max_attempts"] == 3


def test_uppercase_secret_fields_are_not_exposed(client):
    for path, key in (("/config", None), ("/config/defaults", None), ("/config/schema", "fields")):
        body = client.get(path, headers=_h()).json()
        payload = body[key] if key else body
        leaked = [name for name in UPPERCASE_SECRETS if name in payload]
        assert leaked == [], f"{path} leaks {leaked}"
    raw = client.get("/config/raw", headers=_h()).json()["yaml"]
    leaked = [name for name in UPPERCASE_SECRETS if name in raw]
    assert leaked == [], f"/config/raw leaks {leaked}"


def test_config_endpoints_do_not_write_secret_values_to_logs(client, monkeypatch):
    from aria import paths
    from aria.config import get_settings

    canary = "zzq-log-canary-42"
    monkeypatch.setattr(get_settings(), "B2_APPLICATION_KEY", canary, raising=False)
    client.get("/config", headers=_h())
    client.get("/config/raw", headers=_h())
    log_dir = paths.log_dir()
    blob = ""
    if log_dir.is_dir():
        blob = "".join(
            p.read_text(encoding="utf-8", errors="replace") for p in log_dir.glob("*.log")
        )
    assert canary not in blob


def test_diagnostics_zip_masks_uppercase_secret_values(client, monkeypatch):
    import io
    import zipfile

    from aria import paths
    from aria.config import get_settings

    values = {
        "B2_KEY_ID": "keyid-zzq-987",
        "B2_APPLICATION_KEY": "appkey-zzq-654",
        "LOCAL_AGENT_UI_PIN": "pin-zzq-321",
    }
    settings = get_settings()
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value, raising=False)
        monkeypatch.setenv(name, value)
    log_dir = paths.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    probe = log_dir / "zzq-diagnostics.log"
    probe.write_text(
        "B2_APPLICATION_KEY=appkey-zzq-654 B2_KEY_ID=keyid-zzq-987 LOCAL_AGENT_UI_PIN=pin-zzq-321\n",
        encoding="utf-8",
    )
    try:
        r = client.get("/logs/diagnostics", headers=_h())
        assert r.status_code == 200, r.text
        archive = zipfile.ZipFile(io.BytesIO(r.content))
        names = archive.namelist()
        assert "system.json" in names and "zzq-diagnostics.log" in [n.split("/")[-1] for n in names]
        blob = "\n".join(
            archive.read(name).decode("utf-8", errors="replace") for name in names
        )
        leaked = [name for name, value in values.items() if value in blob]
        assert leaked == [], f"diagnostics zip leaks {leaked}"
    finally:
        probe.unlink(missing_ok=True)


def test_defaults_match_current_core_values(client):
    defaults = client.get("/config/defaults", headers=_h()).json()
    current = client.get("/config", headers=_h()).json()
    for key in ("loop_max_iterations", "delegate_max_parallel", "budget_warn_threshold_pct"):
        assert defaults[key] == current[key]


def test_schema_shape_and_no_secrets(client):
    body = client.get("/config/schema", headers=_h()).json()
    assert isinstance(body["fields"], dict) and body["fields"]
    assert body["category_order"]
    _assert_no_secret_fields(body["fields"])
    spec = body["fields"]["loop_max_iterations"]
    assert spec["type"] in {"int", "integer", "number"} and spec["category"] == "agent"


def test_put_config_requires_object(client):
    assert client.put("/config", json={"config": "nope"}, headers=_h()).status_code == 400
    assert client.put("/config", json={}, headers=_h()).status_code == 400


def test_put_config_applies_visible_change(client):
    r = client.put("/config", json={"config": {"loop_max_iterations": 17}}, headers=_h())
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.get("/config", headers=_h()).json()["loop_max_iterations"] == 17


def test_raw_get_returns_parseable_yaml_and_path(client):
    body = client.get("/config/raw", headers=_h()).json()
    parsed = yaml.safe_load(body["yaml"])
    assert isinstance(parsed, dict) and parsed["loop_max_iterations"] == 15
    assert body["path"].endswith("config.yaml")


def test_raw_put_roundtrip(client):
    r = client.put(
        "/config/raw", json={"yaml_text": "loop_max_iterations: 18\n"}, headers=_h()
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    assert client.get("/config", headers=_h()).json()["loop_max_iterations"] == 18


@pytest.mark.parametrize("yaml_text", ["loop: [unclosed", "- just\n- a list\n"])
def test_raw_put_rejects_invalid_or_non_object_yaml(client, yaml_text):
    assert client.put("/config/raw", json={"yaml_text": yaml_text}, headers=_h()).status_code == 400


def test_public_config_shape(client):
    body = client.get("/config/public", headers=_h()).json()
    for key in ("current_session_id", "provider_health", "feature_flags", "scheduler_jobs"):
        assert key in body
    assert body["feature_flags"]["demo_mode"] is False


def test_auth_me_shape(client):
    body = client.get("/auth/me", headers=_h()).json()
    assert body["user_id"] == "local-dev"
    assert body["provider"] == "loopback"
    assert body["pin_required"] is False


def test_verify_pin_returns_bool(client):
    body = client.post("/auth/verify-pin", json={"pin": ""}, headers=_h()).json()
    assert set(body) == {"ok"} and isinstance(body["ok"], bool)


def test_themes_catalog(client):
    body = client.get("/dashboard/themes", headers=_h()).json()
    names = {t["name"] for t in body["themes"]}
    assert len(names) == 8 and "default" in names
    assert body["active"] in names
    assert all(t["label"] and t["description"] for t in body["themes"])


def test_theme_set_is_visible_and_restorable(client):
    previous = client.get("/dashboard/themes", headers=_h()).json()["active"]
    target = "mono" if previous != "mono" else "ember"
    r = client.put("/dashboard/theme", json={"name": target}, headers=_h())
    assert r.status_code == 200 and r.json()["theme"] == target
    assert client.get("/dashboard/themes", headers=_h()).json()["active"] == target
    assert client.put("/dashboard/theme", json={"name": previous}, headers=_h()).status_code == 200


def test_theme_set_rejects_unknown_name(client):
    r = client.put("/dashboard/theme", json={"name": "neon-void"}, headers=_h())
    assert r.status_code == 400


def test_font_roundtrip(client):
    previous = client.get("/dashboard/font", headers=_h()).json()["font"]
    target = "serif" if previous != "serif" else "mono"
    assert client.put("/dashboard/font", json={"font": target}, headers=_h()).json()["font"] == target
    assert client.get("/dashboard/font", headers=_h()).json()["font"] == target
    client.put("/dashboard/font", json={"font": previous}, headers=_h())


def test_dashboard_plugins_is_empty_list(client):
    r = client.get("/dashboard/plugins", headers=_h())
    assert r.status_code == 200 and r.json() == []


def test_inline_complete_uses_local_heuristic(client):
    body = client.post(
        "/llm/inline-complete", json={"before": "items:\n-", "after": ""}, headers=_h()
    ).json()
    assert body == {"suggestion": "[ ] ", "source": "local-heuristic"}


def test_inline_complete_empty_input_is_empty_suggestion(client):
    body = client.post(
        "/llm/inline-complete", json={"before": "", "after": ""}, headers=_h()
    ).json()
    assert body["suggestion"] == "" and body["source"] == "local-heuristic"


def test_transform_selection_upper(client):
    body = client.post(
        "/llm/transform",
        json={"selectedText": "abc", "instruction": "upper"},
        headers=_h(),
    ).json()
    assert body == {"text": "ABC", "source": "local-heuristic"}


def test_transform_selection_unknown_instruction_keeps_text(client):
    body = client.post(
        "/llm/transform",
        json={"selectedText": "как есть", "instruction": "rotate"},
        headers=_h(),
    ).json()
    assert body["text"] == "как есть"


def test_requires_runtime_token(client):
    assert client.get("/config").status_code in (401, 403)
    assert client.put("/config", json={"config": {}}).status_code in (401, 403)

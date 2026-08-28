"""Config / auth / dashboard / llm helper routes (moved from aria.main).

Config ext (§WORK_NOTES): GET /config/defaults, /config/schema,
GET/PUT /config/raw, PUT /config, plus real dashboard theme/font prefs.
The UI edits a nested view (agent.*, dashboard.*, ...) while flat keys map
1:1 onto the pydantic Settings model. Overrides are persisted to
data/config.yaml and applied to the cached Settings instance immediately.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException

from aria.api.auth import require_runtime_token, token_store
from aria.config import get_settings
from aria.http_utils import public_config_payload

router = APIRouter(tags=["config"])

# ---------------------------------------------------------------------------
# Config store — nested overrides persisted to data/config.yaml.
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_CONFIG_PATH = _DATA_DIR / "config.yaml"
_DASHBOARD_PATH = _DATA_DIR / "dashboard.json"

_LOAD_LOCK = threading.Lock()

_BUILTIN_THEMES: tuple[tuple[str, str, str], ...] = (
    ("default", "ARIA Teal", "Classic dark teal — the canonical ARIA look"),
    ("default-large", "ARIA Teal (Large)", "ARIA Teal with bigger fonts and roomier spacing"),
    ("nous-blue", "Nous Blue", "Light mode — vivid Nous-blue accents on cream canvas"),
    ("midnight", "Midnight", "Deep blue-violet with cool accents"),
    ("ember", "Ember", "Warm crimson and bronze — forge vibes"),
    ("mono", "Mono", "Clean grayscale — minimal and focused"),
    ("cyberpunk", "Cyberpunk", "Neon green on black — matrix terminal"),
    ("rose", "Rosé", "Soft pink and warm ivory — easy on the eyes"),
)

_BUILTIN_THEME_NAMES = {name for name, _, _ in _BUILTIN_THEMES}

# Nested (dotted) keys the UI reads/writes that have no Settings counterpart.
CONFIG_EXTRAS: list[dict[str, Any]] = [
    {
        "key": "agent.reasoning_effort",
        "type": "select",
        "options": ["low", "medium", "high"],
        "default": "medium",
        "category": "agent",
        "description": "Reasoning effort for the active model",
    },
    {
        "key": "dashboard.show_token_analytics",
        "type": "boolean",
        "default": False,
        "category": "general",
        "description": "Show token/cost analytics in the sidebar",
    },
]

_CATEGORY_PREFIXES: tuple[tuple[str, str], ...] = (
    ("compression_", "compression"),
    ("delegate_", "delegation"),
    ("approval_", "security"),
    ("security_", "security"),
    ("codex_", "agent"),
    ("notebook_", "browser"),
    ("lock_", "general"),
    ("audit_", "tool_loop_guardrails"),
    ("tools_", "tool_loop_guardrails"),
    ("budget_", "general"),
    ("backup_", "general"),
    ("watchdog_", "general"),
    ("runtime_", "security"),
    ("loop_", "agent"),
    ("b2_", "general"),
    ("providers_", "general"),
    ("http_", "general"),
    ("ws_", "general"),
)

_CATEGORY_ORDER = [
    "general", "agent", "terminal", "display", "delegation", "memory",
    "compression", "security", "auxiliary", "browser", "voice", "tts", "stt",
    "logging", "discord", "tool_loop_guardrails", "tool_output",
    "model_catalog", "openrouter", "sessions", "curator", "kanban", "updates",
    "bedrock",
]

_SELECT_FIELDS: dict[str, list[str]] = {
    "codex_sandbox_mode": ["read-only", "workspace-write", "danger-full-access"],
}


def _is_secret(name: str) -> bool:
    return any(tok in name for tok in ("api_key", "apikey", "application_key", "key_id", "token", "pin"))


def _category_for(name: str) -> str:
    for prefix, category in _CATEGORY_PREFIXES:
        if name.startswith(prefix):
            return category
    return "general"


def _field_type(name: str, annotation: Any) -> str:
    if annotation is bool:
        return "boolean"
    if annotation in (int, float):
        return "number"
    return "text"


def _flatten(d: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    out: list[tuple[str, Any]] = []
    for key, value in d.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.extend(_flatten(value, path))
        else:
            out.append((path, value))
    return out


def _set_nested(d: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node = d
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def _get_nested(d: dict[str, Any], path: str, default: Any = None) -> Any:
    node = d
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_overrides() -> dict[str, Any]:
    with _LOAD_LOCK:
        return _load_yaml(_CONFIG_PATH)


def _coerce(name: str, value: Any) -> Any:
    settings_cls = get_settings().__class__
    finfo = settings_cls.model_fields.get(name)
    if finfo is not None and finfo.annotation is bool:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if finfo is not None and finfo.annotation in (int, float):
        try:
            return finfo.annotation(value)
        except (TypeError, ValueError):
            return value
    return value


def _apply_overrides() -> None:
    settings = get_settings()
    for path, value in _flatten(_load_overrides()):
        if path in settings.__class__.model_fields:
            setattr(settings, path, _coerce(path, value))


_apply_overrides()


def _settings_snapshot() -> dict[str, Any]:
    settings = get_settings()
    snapshot: dict[str, Any] = {}
    for name in settings.__class__.model_fields:
        if _is_secret(name):
            continue
        snapshot[name] = getattr(settings, name)
    for extra in CONFIG_EXTRAS:
        if _get_nested(snapshot, extra["key"]) is None:
            _set_nested(snapshot, extra["key"], extra["default"])
    return snapshot


def _settings_defaults() -> dict[str, Any]:
    settings_cls = get_settings().__class__
    defaults: dict[str, Any] = {}
    for name, finfo in settings_cls.model_fields.items():
        if _is_secret(name):
            continue
        defaults[name] = finfo.default
    for extra in CONFIG_EXTRAS:
        _set_nested(defaults, extra["key"], extra["default"])
    return defaults


def _settings_schema() -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    for name, finfo in get_settings().__class__.model_fields.items():
        if _is_secret(name):
            continue
        spec: dict[str, Any] = {
            "type": _field_type(name, finfo.annotation),
            "category": _category_for(name),
        }
        if name in _SELECT_FIELDS:
            spec = {
                "type": "select",
                "options": _SELECT_FIELDS[name],
                "category": spec["category"],
            }
        if finfo.description:
            spec["description"] = finfo.description
        fields[name] = spec
    for extra in CONFIG_EXTRAS:
        spec = {
            "type": extra["type"],
            "category": extra.get("category", "general"),
        }
        if extra.get("options"):
            spec["options"] = extra["options"]
        if extra.get("description"):
            spec["description"] = extra["description"]
        fields[extra["key"]] = spec
    return fields


def _persist_config(config: dict[str, Any]) -> None:
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _LOAD_LOCK:
        with _CONFIG_PATH.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(config, fh, allow_unicode=True, sort_keys=False)


def _apply_config(config: dict[str, Any]) -> None:
    settings = get_settings()
    for path, value in _flatten(config):
        if path in settings.__class__.model_fields:
            setattr(settings, path, _coerce(path, value))


def _load_dashboard_prefs() -> dict[str, Any]:
    if _DASHBOARD_PATH.exists():
        try:
            with _DASHBOARD_PATH.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"theme": "default", "font": "theme"}


def _save_dashboard_prefs(prefs: dict[str, Any]) -> None:
    _DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _DASHBOARD_PATH.open("w", encoding="utf-8") as fh:
        json.dump(prefs, fh, indent=2)


# ---------------------------------------------------------------------------
# Config routes
# ---------------------------------------------------------------------------


@router.get("/config")
async def get_config(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return _settings_snapshot()


@router.get("/config/defaults")
async def get_config_defaults(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return _settings_defaults()


@router.get("/config/schema")
async def get_config_schema(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"fields": _settings_schema(), "category_order": _CATEGORY_ORDER}


@router.put("/config")
async def put_config(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    config = payload.get("config")
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="body.config must be an object")
    _persist_config(config)
    _apply_config(config)
    return {"ok": True}


@router.get("/config/raw")
async def get_config_raw(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    yaml_text = yaml.safe_dump(_settings_snapshot(), allow_unicode=True, sort_keys=False)
    return {"yaml": yaml_text, "path": str(_CONFIG_PATH)}


@router.put("/config/raw")
async def put_config_raw(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    yaml_text = str(payload.get("yaml_text", ""))
    try:
        config = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"invalid YAML: {exc}")
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="yaml_text must parse to an object")
    _persist_config(config)
    _apply_config(config)
    return {"ok": True}


@router.get("/config/public")
async def get_public_config(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return public_config_payload()


# ---------------------------------------------------------------------------
# Auth / profiles
# ---------------------------------------------------------------------------


@router.get("/auth/me")
async def auth_me_alias(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {
        "user_id": "local-dev",
        "display_name": "Local Dev",
        "email": "dev@local.host",
        "org_id": "local",
        "provider": "loopback",
        "expires_at": 9999999999,
        "pin_required": False,
    }


@router.post("/auth/verify-pin")
async def verify_pin(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    pin = str(payload.get("pin", ""))
    ok = token_store.verify_pin(pin)
    return {"ok": ok}


# ---------------------------------------------------------------------------
# Dashboard theme / font
# ---------------------------------------------------------------------------


@router.get("/dashboard/themes")
async def dashboard_themes_alias(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    prefs = _load_dashboard_prefs()
    return {
        "active": prefs.get("theme", "default"),
        "themes": [
            {"name": name, "label": label, "description": description}
            for name, label, description in _BUILTIN_THEMES
        ],
    }


@router.put("/dashboard/theme")
async def dashboard_theme_set(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    name = str(payload.get("name", "default"))
    if name not in _BUILTIN_THEME_NAMES:
        raise HTTPException(status_code=400, detail=f"unknown theme: {name}")
    prefs = _load_dashboard_prefs()
    prefs["theme"] = name
    _save_dashboard_prefs(prefs)
    return {"ok": True, "theme": name}


@router.get("/dashboard/font")
async def dashboard_font_alias(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    prefs = _load_dashboard_prefs()
    return {"font": prefs.get("font", "theme")}


@router.put("/dashboard/font")
async def dashboard_font_set(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    font = str(payload.get("font", "theme"))
    prefs = _load_dashboard_prefs()
    prefs["font"] = font
    _save_dashboard_prefs(prefs)
    return {"ok": True, "font": font}


@router.get("/dashboard/plugins")
async def dashboard_plugins_alias(_: str = Depends(require_runtime_token)) -> list[dict[str, Any]]:
    return []


# ---------------------------------------------------------------------------
# LLM helpers (local heuristics — no external calls)
# ---------------------------------------------------------------------------


def _local_inline_completion(before: str, after: str) -> str:
    lines_before = before.splitlines()
    lines_after = after.splitlines()
    line_before = lines_before[-1] if lines_before else before
    line_after = lines_after[0] if lines_after else after
    if line_before.endswith('[[') and not line_after.startswith(']]'):
        return ']]'
    if re.fullmatch(r'-\s*', line_before):
        return '[ ] '
    if re.match(r'- \[[ xX]\] .+', line_before):
        return '\n- [ ] '
    if re.match(r'#{1,6} .+', line_before):
        return '\n\n'
    if line_before.rstrip().endswith('```'):
        return '\n\n```'
    if re.search(r'[:：]\s*$', line_before.strip()):
        return '\n- '
    if re.match(r'- .+', line_before):
        return '\n- '
    return ''


def _local_transform(selected_text: str, instruction: str) -> str:
    instruction_lower = instruction.lower()
    if 'upper' in instruction_lower or 'верх' in instruction_lower:
        return selected_text.upper()
    if 'lower' in instruction_lower or 'ниж' in instruction_lower:
        return selected_text.lower()
    if 'todo' in instruction_lower or 'задач' in instruction_lower:
        lines = [line.strip() for line in selected_text.splitlines() if line.strip()]
        return '\n'.join(f'- [ ] {line.lstrip("-* ")}' for line in lines)
    if 'summary' in instruction_lower or 'крат' in instruction_lower or 'суммар' in instruction_lower:
        chunks = re.split(r'(?<=[.!?])\s+', selected_text.strip())
        return ' '.join(chunks[:2]) if chunks else selected_text
    return selected_text


@router.post("/llm/inline-complete")
async def inline_complete(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    before = str(payload.get('before', ''))
    after = str(payload.get('after', ''))
    suggestion = _local_inline_completion(before, after)
    return {'suggestion': suggestion, 'source': 'local-heuristic'}


@router.post("/llm/transform")
async def transform_selection(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    selected_text = str(payload.get('selectedText', ''))
    instruction = str(payload.get('instruction', ''))
    return {'text': _local_transform(selected_text, instruction), 'source': 'local-heuristic'}

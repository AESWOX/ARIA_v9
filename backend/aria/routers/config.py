"""Config / auth / dashboard / llm helper routes (moved from aria.main).

Config ext: GET /config/defaults, /config/schema,
GET/PUT /config/raw, PUT /config, plus real dashboard theme/font prefs.
The UI edits a nested view (agent.*, dashboard.*, ...) while flat keys map
1:1 onto the pydantic Settings model. Overrides are persisted to
data/config.yaml and applied to the cached Settings instance immediately.
"""
from __future__ import annotations

from aria import paths

import json
import os
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

_DATA_DIR = paths.data_dir()
_CONFIG_PATH = _DATA_DIR / "config.yaml"
_DASHBOARD_PATH = _DATA_DIR / "dashboard.json"

_LOAD_LOCK = threading.Lock()

_BUILTIN_THEMES: tuple[tuple[str, str, str], ...] = (
    ("default", "ARIA Teal", "Classic dark teal — the canonical ARIA look"),
    ("default-large", "ARIA Teal (Large)", "ARIA Teal with bigger fonts and roomier spacing"),
    ("aria-blue", "ARIA Blue", "Light mode — vivid ARIA-blue accents on cream canvas"),
    ("midnight", "Midnight", "Deep blue-violet with cool accents"),
    ("ember", "Ember", "Warm crimson and bronze — forge vibes"),
    ("mono", "Mono", "Clean grayscale — minimal and focused"),
    ("cyberpunk", "Cyberpunk", "Neon green on black — matrix terminal"),
    ("rose", "Rosé", "Soft pink and warm ivory — easy on the eyes"),
    ("black", "Black", "Deep black — the default 0.2.1 look"),
    ("white", "White", "Light canvas with dark text"),
    ("gray", "Gray", "Neutral gray without a colour accent"),
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



def _is_secret(name: str) -> bool:
    lowered = name.lower()
    return any(tok in lowered for tok in ("api_key", "apikey", "application_key", "key_id", "token", "pin"))


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


_SURFACE_MODES = {"theme", "white", "textured", "custom", "image"}
_SURFACE_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$", re.IGNORECASE)
_SURFACE_IMAGE_RE = re.compile(r"^data:image/(png|jpeg|webp|gif);base64,[A-Za-z0-9+/=\s]+$", re.IGNORECASE)
_MAX_SURFACE_IMAGE_CHARS = 4_200_000
_DASHBOARD_DEFAULTS: dict[str, Any] = {
    "theme": "black",
    "font": "theme",
    "surface_mode": "theme",
    "surface_color": "#101418",
    "surface_image": "",
    "surface_dim": 0,
    "overrides": {"textColor": "", "windowTextColor": "", "accentColor": ""},
    "open_tabs": [],
    "active_tab": None,
}


def _dashboard_defaults() -> dict[str, Any]:
    return {
        "theme": _DASHBOARD_DEFAULTS["theme"],
        "font": _DASHBOARD_DEFAULTS["font"],
        "surface_mode": _DASHBOARD_DEFAULTS["surface_mode"],
        "surface_color": _DASHBOARD_DEFAULTS["surface_color"],
        "surface_image": _DASHBOARD_DEFAULTS["surface_image"],
        "surface_dim": _DASHBOARD_DEFAULTS["surface_dim"],
        "overrides": dict(_DASHBOARD_DEFAULTS["overrides"]),
        "open_tabs": list(_DASHBOARD_DEFAULTS["open_tabs"]),
        "active_tab": _DASHBOARD_DEFAULTS["active_tab"],
    }


def _dashboard_prefs_payload(raw: dict[str, Any]) -> dict[str, Any]:
    prefs = _dashboard_defaults()
    for key in ("theme", "font"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            prefs[key] = value.strip()[:120]
    mode = raw.get("surface_mode")
    if isinstance(mode, str) and mode in _SURFACE_MODES:
        prefs["surface_mode"] = mode
    color = raw.get("surface_color")
    if isinstance(color, str) and _SURFACE_COLOR_RE.fullmatch(color):
        prefs["surface_color"] = color
    image = raw.get("surface_image")
    if isinstance(image, str):
        if not image:
            prefs["surface_image"] = ""
        elif len(image) <= _MAX_SURFACE_IMAGE_CHARS and _SURFACE_IMAGE_RE.fullmatch(image):
            prefs["surface_image"] = image
    dim = raw.get("surface_dim")
    if isinstance(dim, (int, float)) and not isinstance(dim, bool):
        prefs["surface_dim"] = max(0, min(100, int(dim)))
    overrides = raw.get("overrides")
    if isinstance(overrides, dict):
        for key in ("textColor", "windowTextColor", "accentColor"):
            value = overrides.get(key)
            if isinstance(value, str) and (value == "" or _SURFACE_COLOR_RE.fullmatch(value)):
                prefs["overrides"][key] = value
    tabs = raw.get("open_tabs")
    if isinstance(tabs, list):
        clean: list[str] = []
        for value in tabs:
            if isinstance(value, str) and value.strip() and len(value) <= 64 and value not in clean:
                clean.append(value.strip())
        prefs["open_tabs"] = clean[:50]
    active = raw.get("active_tab")
    if isinstance(active, str) and len(active) <= 64:
        prefs["active_tab"] = active
    return prefs


def _validate_dashboard_prefs(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="prefs must be an object")
    mode = raw.get("surface_mode")
    if mode is not None and mode not in _SURFACE_MODES:
        raise HTTPException(status_code=400, detail=f"unknown surface mode: {mode}")
    color = raw.get("surface_color")
    if color is not None and (not isinstance(color, str) or not _SURFACE_COLOR_RE.fullmatch(color)):
        raise HTTPException(status_code=400, detail="surface_color must be #rrggbb")
    image = raw.get("surface_image")
    if image is not None:
        if not isinstance(image, str):
            raise HTTPException(status_code=400, detail="surface_image must be a string")
        if image and (len(image) > _MAX_SURFACE_IMAGE_CHARS or not _SURFACE_IMAGE_RE.fullmatch(image)):
            raise HTTPException(status_code=400, detail="surface_image must be a png/jpeg/webp/gif data URL under 4 MB")
    dim = raw.get("surface_dim")
    if dim is not None and (isinstance(dim, bool) or not isinstance(dim, (int, float)) or dim < 0 or dim > 100):
        raise HTTPException(status_code=400, detail="surface_dim must be 0..100")
    overrides = raw.get("overrides")
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise HTTPException(status_code=400, detail="overrides must be an object")
        for key in ("textColor", "windowTextColor", "accentColor"):
            value = overrides.get(key)
            if value is not None and (not isinstance(value, str) or (value and not _SURFACE_COLOR_RE.fullmatch(value))):
                raise HTTPException(status_code=400, detail=f"overrides.{key} must be #rrggbb or empty")
    tabs = raw.get("open_tabs")
    if tabs is not None:
        if not isinstance(tabs, list) or any(not isinstance(item, str) for item in tabs):
            raise HTTPException(status_code=400, detail="open_tabs must be a list of strings")
        if len(tabs) > 50 or any(len(item) > 64 for item in tabs):
            raise HTTPException(status_code=400, detail="open_tabs is limited to 50 ids of 64 chars")
    active = raw.get("active_tab")
    if active is not None and (not isinstance(active, str) or len(active) > 64):
        raise HTTPException(status_code=400, detail="active_tab must be a string or null")
    return _dashboard_prefs_payload(raw)


def _load_dashboard_prefs() -> dict[str, Any]:
    raw: dict[str, Any] = {}
    if _DASHBOARD_PATH.exists():
        try:
            with _DASHBOARD_PATH.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                raw = data
        except Exception:
            raw = {}
    return _dashboard_prefs_payload(raw)


def _save_dashboard_prefs(prefs: dict[str, Any]) -> None:
    with _LOAD_LOCK:
        _DASHBOARD_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DASHBOARD_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(prefs, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, _DASHBOARD_PATH)


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
        "email": "dev@example.com",
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
        "active": prefs.get("theme", "black"),
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


@router.get("/dashboard/prefs")
async def dashboard_prefs_get(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    """Full UI preferences: theme, font, surface image/dim, color overrides.

    The browser keeps a localStorage copy for a flash-free start; this file is
    the durable source of truth across app restarts and profiles.
    """
    return _load_dashboard_prefs()


@router.put("/dashboard/prefs")
async def dashboard_prefs_put(
    payload: dict[str, Any], _: str = Depends(require_runtime_token)
) -> dict[str, Any]:
    patch = _validate_dashboard_prefs(payload)
    prefs = _load_dashboard_prefs()
    for key, value in patch.items():
        if key in payload:
            prefs[key] = value
    _save_dashboard_prefs(prefs)
    return {"ok": True, **prefs}


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

"""Profile management routes (§WORK_NOTES profiles ext).

Profiles persist under data/profiles/<name>/profile.yaml (description,
description_auto, provider/model, soul, setup-command). "default" is the
built-in profile backed by the backend working dir and cannot be deleted.
"""
from __future__ import annotations

from aria import paths

import re
import shutil
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException

from aria.api.auth import require_runtime_token
from aria.config import get_settings

router = APIRouter(tags=["profiles"])

_PROFILES_ROOT = paths.data_dir() / "profiles"
_STATE_FILE = paths.data_dir() / "profile_state.json"
_SKILLS_ROOT = paths.skills_dir()

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")


def _load_state() -> dict[str, Any]:
    if _STATE_FILE.exists():
        try:
            import json

            with _STATE_FILE.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {"active": "default"}


def _save_state(state: dict[str, Any]) -> None:
    import json

    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _STATE_FILE.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def _profile_dir(name: str) -> Path:
    return _PROFILES_ROOT / name


def _profile_file(name: str) -> Path:
    return _profile_dir(name) / "profile.yaml"


def _load_profile(name: str) -> dict[str, Any]:
    path = _profile_file(name)
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def _save_profile(name: str, data: dict[str, Any]) -> None:
    path = _profile_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True, sort_keys=False)


def _skill_count() -> int:
    if not _SKILLS_ROOT.exists():
        return 0
    return sum(1 for d in _SKILLS_ROOT.iterdir() if d.is_dir() and (d / "SKILL.md").exists())


def _list_profile_names() -> list[str]:
    names = ["default"]
    if _PROFILES_ROOT.exists():
        for d in sorted(_PROFILES_ROOT.iterdir()):
            if d.is_dir() and (d / "profile.yaml").exists():
                names.append(d.name)
    return names


def _profile_info(name: str) -> dict[str, Any]:
    data = _load_profile(name)
    is_default = name == "default"
    return {
        "name": name,
        "path": str(_profile_dir(name) if not is_default else Path.cwd()),
        "is_default": is_default,
        "model": data.get("model"),
        "provider": data.get("provider"),
        "has_env": (Path(".env").exists()) if is_default else _profile_dir(name).joinpath(".env").exists(),
        "skill_count": _skill_count(),
        "gateway_running": False,
        "description": data.get("description", ""),
        "description_auto": bool(data.get("description_auto", False)),
        "distribution_name": None,
        "distribution_version": None,
        "distribution_source": None,
        "has_alias": False,
    }


def _validate_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="invalid profile name")
    return name


@router.get("/profiles")
async def profiles_list(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"profiles": [_profile_info(name) for name in _list_profile_names()]}


@router.get("/profiles/active")
async def profiles_active(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    active = _load_state().get("active", "default")
    return {"active": active, "current": active}


@router.post("/profiles/active")
async def profiles_set_active(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    name = str(payload.get("name", ""))
    if name not in _list_profile_names():
        raise HTTPException(status_code=404, detail="profile not found")
    state = _load_state()
    state["active"] = name
    _save_state(state)
    return {"ok": True, "active": name}


@router.post("/profiles")
async def profiles_create(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    name = _validate_name(str(payload.get("name", "")))
    if name in _list_profile_names():
        raise HTTPException(status_code=409, detail="profile already exists")
    _PROFILES_ROOT.mkdir(parents=True, exist_ok=True)
    _profile_dir(name).mkdir(parents=True, exist_ok=True)

    data: dict[str, Any] = {}
    clone_from = payload.get("clone_from")
    if clone_from and clone_from in _list_profile_names() and clone_from != name:
        data = dict(_load_profile(clone_from))
        if not payload.get("clone_all"):
            data.pop("soul", None)
    elif payload.get("clone_from_default"):
        data = dict(_load_profile("default"))

    if payload.get("no_skills"):
        pass
    if payload.get("description"):
        data["description"] = str(payload["description"])
        data["description_auto"] = False
    if payload.get("provider"):
        data["provider"] = str(payload["provider"])
    if payload.get("model"):
        data["model"] = str(payload["model"])
    data.setdefault("created_at", "")

    _save_profile(name, data)

    mcp_servers = payload.get("mcp_servers")
    mcp_written = 0
    if isinstance(mcp_servers, list) and mcp_servers:
        mcp_written = len(mcp_servers)

    hub_installs: list[dict[str, Any]] = []
    hub_skills = payload.get("hub_skills")
    if isinstance(hub_skills, list):
        for identifier in hub_skills:
            hub_installs.append({"identifier": str(identifier), "pid": None})

    skills_disabled = 0
    keep_skills = payload.get("keep_skills")
    if not isinstance(keep_skills, list) and not isinstance(hub_skills, list):
        skills_disabled = 0

    return {
        "ok": True,
        "name": name,
        "path": str(_profile_dir(name)),
        "model_set": bool(payload.get("model")),
        "mcp_written": mcp_written,
        "skills_disabled": skills_disabled,
        "hub_installs": hub_installs,
    }


@router.put("/profiles/{name}/description")
async def profiles_update_description(
    name: str, payload: dict[str, Any], _: str = Depends(require_runtime_token)
) -> dict[str, Any]:
    name = _validate_name(name)
    if name not in _list_profile_names():
        raise HTTPException(status_code=404, detail="profile not found")
    data = _load_profile(name)
    data["description"] = str(payload.get("description", ""))
    data["description_auto"] = False
    _save_profile(name, data)
    return {"ok": True, "description": data["description"], "description_auto": False}


@router.post("/profiles/{name}/describe-auto")
async def profiles_describe_auto(
    name: str, payload: dict[str, Any], _: str = Depends(require_runtime_token)
) -> dict[str, Any]:
    name = _validate_name(name)
    if name not in _list_profile_names():
        raise HTTPException(status_code=404, detail="profile not found")
    data = _load_profile(name)
    overwrite = bool(payload.get("overwrite", True))
    if not overwrite and data.get("description") and not data.get("description_auto"):
        return {"ok": False, "reason": "manual description exists; overwrite=false", "description": data.get("description"), "description_auto": False}

    parts = [f"{_skill_count()} skills"]
    if data.get("provider"):
        parts.append(f"provider {data['provider']}")
    if data.get("model"):
        parts.append(f"model {data['model']}")
    description = f"Profile '{name}' - " + ", ".join(parts) + "."
    data["description"] = description
    data["description_auto"] = True
    _save_profile(name, data)
    return {"ok": True, "reason": "generated from local metadata", "description": description, "description_auto": True}


@router.put("/profiles/{name}/model")
async def profiles_set_model(
    name: str, payload: dict[str, Any], _: str = Depends(require_runtime_token)
) -> dict[str, Any]:
    name = _validate_name(name)
    if name not in _list_profile_names():
        raise HTTPException(status_code=404, detail="profile not found")
    data = _load_profile(name)
    data["provider"] = str(payload.get("provider", ""))
    data["model"] = str(payload.get("model", ""))
    _save_profile(name, data)
    return {"ok": True, "provider": data["provider"], "model": data["model"]}


@router.patch("/profiles/{name}")
async def profiles_rename(
    name: str, payload: dict[str, Any], _: str = Depends(require_runtime_token)
) -> dict[str, Any]:
    name = _validate_name(name)
    if name == "default":
        raise HTTPException(status_code=400, detail="cannot rename default profile")
    new_name = _validate_name(str(payload.get("new_name", "")))
    if name not in _list_profile_names():
        raise HTTPException(status_code=404, detail="profile not found")
    if new_name in _list_profile_names():
        raise HTTPException(status_code=409, detail="target profile already exists")
    _profile_dir(name).rename(_profile_dir(new_name))
    state = _load_state()
    if state.get("active") == name:
        state["active"] = new_name
        _save_state(state)
    return {"ok": True, "name": new_name, "path": str(_profile_dir(new_name))}


@router.delete("/profiles/{name}")
async def profiles_delete(name: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    name = _validate_name(name)
    if name == "default":
        raise HTTPException(status_code=400, detail="cannot delete default profile")
    if name not in _list_profile_names():
        raise HTTPException(status_code=404, detail="profile not found")
    shutil.rmtree(_profile_dir(name), ignore_errors=True)
    state = _load_state()
    if state.get("active") == name:
        state["active"] = "default"
        _save_state(state)
    return {"ok": True}


@router.get("/profiles/{name}/setup-command")
async def profiles_setup_command(name: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    name = _validate_name(name)
    if name not in _list_profile_names():
        raise HTTPException(status_code=404, detail="profile not found")
    settings = get_settings()
    command = (
        f"python run_backend.py --profile {name} "
        f"--host {settings.http_host} --port {settings.http_port}"
    )
    return {"command": command}


@router.get("/profiles/{name}/soul")
async def profiles_get_soul(name: str, _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    name = _validate_name(name)
    if name not in _list_profile_names():
        raise HTTPException(status_code=404, detail="profile not found")
    data = _load_profile(name)
    content = data.get("soul", "")
    return {"content": content, "exists": bool(content)}


@router.put("/profiles/{name}/soul")
async def profiles_put_soul(
    name: str, payload: dict[str, Any], _: str = Depends(require_runtime_token)
) -> dict[str, Any]:
    name = _validate_name(name)
    if name not in _list_profile_names():
        raise HTTPException(status_code=404, detail="profile not found")
    data = _load_profile(name)
    data["soul"] = str(payload.get("content", ""))
    _save_profile(name, data)
    return {"ok": True}

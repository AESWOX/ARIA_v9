"""Model routes: /api/model/options, /api/model/auxiliary, /api/model/set.

Model catalog comes from the ``provider_models`` table (populated by the
provider catalog refresh) and the LLM router's provider registry. The
active main/auxiliary assignment persists to ``data/model_assignment.json``
so a restart keeps the chosen models.
"""
from __future__ import annotations

from aria import paths

import json
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from aria.api.auth import require_runtime_token
from aria.db import repository as repo
from aria.db.base import session_scope

router = APIRouter(tags=["model"])

_STATE_FILE = paths.data_dir() / "model_assignment.json"
_state_lock = threading.Lock()

# Auxiliary task slots surfaced by the UI (ModelsPage AUX_TASKS).
AUX_TASKS = [
    "vision", "web_extract", "compression", "skills_hub", "approval", "mcp",
    "title_generation", "triage_specifier", "kanban_decomposer",
    "profile_describer", "curator",
]


def _load_state() -> dict[str, Any]:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
    return {"main": {"provider": "", "model": ""}, "aux": {}}


def _save_state(state: dict[str, Any]) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _catalog(request: Request) -> dict[str, list[str]]:
    """provider_id -> sorted model ids, plus which provider is current."""
    llm_router = getattr(request.app.state, "router", None)
    provider_ids: list[str] = []
    if llm_router is not None and hasattr(llm_router, "providers_by_class"):
        for providers in llm_router.providers_by_class.values():
            provider_ids.extend(p.provider_id for p in providers)
    with session_scope() as db:
        rows = repo.list_provider_models(db)
    by_provider: dict[str, set[str]] = {}
    for row in rows:
        by_provider.setdefault(row.provider_id, set()).add(row.model_id)
    for pid in provider_ids:
        by_provider.setdefault(pid, set())
    return {pid: sorted(models) for pid, models in by_provider.items()}


@router.get("/model/options")
async def model_options(
    request: Request,
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    catalog = _catalog(request)
    state = _load_state()
    main = state.get("main", {}) or {}
    providers: list[dict[str, Any]] = []
    for pid in sorted(catalog):
        providers.append({
            "name": pid,
            "slug": pid,
            "models": catalog[pid],
            "total_models": len(catalog[pid]),
            "is_current": pid == main.get("provider"),
            "is_user_defined": False,
            "source": None,
            "warning": None,
        })
    return {
        "model": main.get("model") or "",
        "provider": main.get("provider") or "",
        "providers": providers,
    }


@router.get("/model/auxiliary")
async def model_auxiliary(
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    state = _load_state()
    main = state.get("main", {}) or {}
    aux = state.get("aux", {}) or {}
    tasks = []
    for key in AUX_TASKS:
        entry = aux.get(key) or {}
        tasks.append({
            "task": key,
            "provider": entry.get("provider", ""),
            "model": entry.get("model", ""),
            "base_url": entry.get("base_url", ""),
        })
    return {
        "tasks": tasks,
        "main": {"provider": main.get("provider", ""), "model": main.get("model", "")},
    }


@router.post("/model/set")
async def model_set(
    body: dict[str, Any],
    _: str = Depends(require_runtime_token),
) -> dict[str, Any]:
    scope = body.get("scope")
    provider = (body.get("provider") or "").strip()
    model = (body.get("model") or "").strip()
    task = (body.get("task") or "").strip()
    if scope not in ("main", "auxiliary"):
        raise HTTPException(status_code=400, detail="scope must be 'main' or 'auxiliary'")

    with _state_lock:
        state = _load_state()
        if scope == "main":
            if not provider or not model:
                raise HTTPException(status_code=400, detail="provider and model are required")
            state["main"] = {"provider": provider, "model": model}
            changed = f"main → {provider}/{model}"
        else:
            aux = state.setdefault("aux", {})
            if task == "__reset__":
                state["aux"] = {}
                changed = "auxiliary → all auto"
            else:
                if not task:
                    raise HTTPException(status_code=400, detail="task is required for auxiliary scope")
                if not provider or not model:
                    raise HTTPException(status_code=400, detail="provider and model are required")
                aux[task] = {"provider": provider, "model": model,
                             "base_url": (body.get("base_url") or "").strip()}
                changed = f"aux:{task} → {provider}/{model}"
        _save_state(state)

    return {"ok": True, "scope": scope, "provider": provider, "model": model,
            "tasks": list((_load_state().get("aux") or {}).keys()),
            "message": f"assigned {changed}"}

"""Config / auth / dashboard / llm helper routes (moved from aria.main)."""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends

from aria.api.auth import require_runtime_token, token_store
from aria.http_utils import public_config_payload

router = APIRouter(tags=["config"])


@router.get("/config")
async def config_alias(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return await get_public_config(_=_)


@router.get("/config/public")
async def get_public_config(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return public_config_payload()


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


@router.get("/profiles")
async def profiles_list_alias(_: str = Depends(require_runtime_token)) -> list[dict[str, Any]]:
    return [{"name": "default", "label": "Default", "is_active": True}]


@router.get("/profiles/active")
async def profiles_active_alias(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"name": "default", "label": "Default", "is_active": True}


@router.get("/dashboard/themes")
async def dashboard_themes_alias(_: str = Depends(require_runtime_token)) -> list[dict[str, Any]]:
    return []


@router.get("/dashboard/font")
async def dashboard_font_alias(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    return {"family": "Inter", "size": 14}


@router.get("/dashboard/plugins")
async def dashboard_plugins_alias(_: str = Depends(require_runtime_token)) -> list[dict[str, Any]]:
    return []


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

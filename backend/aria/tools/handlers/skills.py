"""tools/handlers/skills.py — чтение тела навыка (SKILL.md) для агента.

``skill_lookup`` отдавал только имена из skills_meta: прочитать, ЧТО навык велит делать, агент не мог,
а шаги плана со ``skill_ref`` падали «unknown tool_ref». Здесь — безопасное чтение SKILL.md
(имя без путей, только каталог навыков).
"""
from __future__ import annotations

import re
from pathlib import Path

from aria import paths

MAX_SKILL_CHARS = 20_000
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,119}$")


def read_skill_body(name: str) -> str | None:
    """Тело SKILL.md или None, если навыка нет / имя небезопасно."""
    name = (name or "").strip()
    if not _SAFE_NAME.match(name) or name in (".", "..") or Path(name).name != name:
        return None
    md = paths.skills_dir() / name / "SKILL.md"
    try:
        return md.read_text(encoding="utf-8", errors="replace")[:MAX_SKILL_CHARS] if md.is_file() else None
    except OSError:
        return None


async def skill_read(input_json: dict, **_ctx) -> dict:
    name = str(input_json.get("name", "")).strip()
    body = read_skill_body(name)
    if body is None:
        return {"found": False, "name": name, "content": None,
                "error": f"навык «{name}» не найден (список имён: skill_lookup)"}
    return {"found": True, "name": name, "content": body}

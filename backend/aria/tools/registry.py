"""tools/registry.py — §11 Tool Registry contract.

Любой tool call обязан ссылаться на registry entry. Здесь же — единственное
место, где перечислены input/output schema, timeout, risk_level,
null_output_allowed, requires_approval, allowed_roles, idempotency_class.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from aria.db.enums import IdempotencyClass, RiskLevel
from aria.storage import obsidian_vault
from aria.tools.handlers import files as files_handler
from aria.tools.handlers import shell as shell_handler
from aria.tools.handlers import web as web_handler
from aria.tools.handlers import vision as vision_handler
from aria.integrations.notebooklm.tool import notebook_query


@dataclass(frozen=True)
class ToolSpec:
    tool_name: str
    description: str
    input_schema: dict
    output_schema: dict
    timeout_sec: int
    risk_level: RiskLevel
    null_output_allowed: bool
    requires_approval: bool
    allowed_roles: tuple[str, ...]
    idempotency_class: IdempotencyClass
    handler: Callable[..., Awaitable[dict]]


async def _read_note(input_json: dict, **_ctx) -> dict:
    return obsidian_vault.read_note(input_json["note_name"])


async def _write_note(input_json: dict, **_ctx) -> dict:
    return obsidian_vault.write_note(input_json["note_name"], input_json.get("content", ""), input_json.get("folder", "00-TASKS"))


async def _search_vault(input_json: dict, **_ctx) -> dict:
    return obsidian_vault.search_vault(input_json["pattern"], input_json.get("max_results", 20))


async def _list_vault(input_json: dict, **_ctx) -> dict:
    return obsidian_vault.list_vault_tree(input_json.get("subdir", ""))


async def _shell_execute(input_json: dict, timeout_sec: int, sandbox_root: str, **_ctx) -> dict:
    shell_handler.validate_shell_input(input_json)
    return await shell_handler.shell_execute(input_json, timeout_sec=timeout_sec, cwd=sandbox_root)


async def _file_read(input_json: dict, sandbox_root: str, **_ctx) -> dict:
    return await files_handler.file_read(input_json, sandbox_root)


async def _file_write(input_json: dict, sandbox_root: str, **_ctx) -> dict:
    return await files_handler.file_write(input_json, sandbox_root)


async def _file_search(input_json: dict, sandbox_root: str, **_ctx) -> dict:
    return await files_handler.file_search(input_json, sandbox_root)




async def _notebook_query(input_json: dict, **_ctx) -> dict:
    """Async wrapper around notebook_query."""
    result = notebook_query(
        prompt=input_json.get("prompt", ""),
        source_ids=input_json.get("source_ids"),
    )
    return result


# ── MCP allowlist tools (read-only, no side effects) ──────────────────────


async def _skill_lookup(input_json: dict, **_ctx) -> dict:
    """Search skills_meta by name or category."""
    from aria.db.base import session_scope
    from aria.db.models import SkillMeta
    from sqlalchemy import select
    pattern = input_json.get("pattern", "")
    category = input_json.get("category", "")
    limit = min(input_json.get("limit", 10), 100)
    with session_scope() as session:
        q = select(SkillMeta)
        if pattern:
            q = q.where(SkillMeta.skill_name.ilike(f"%{pattern}%"))
        if category:
            q = q.where(SkillMeta.category == category)
        q = q.limit(limit)
        rows = session.execute(q).scalars().all()
        return {
            "skills": [{"name": r.skill_name, "category": r.category} for r in rows],
            "total": len(rows),
        }


async def _task_status(input_json: dict, **_ctx) -> dict:
    """Query task by ID."""
    from aria.db.base import session_scope
    from aria.db.models import Task
    from sqlalchemy import select
    import uuid
    task_id = input_json.get("task_id", "")
    with session_scope() as session:
        try:
            tid = uuid.UUID(task_id) if task_id else None
        except (ValueError, TypeError):
            tid = None
        if tid is None:
            return {"status": "not_found", "task_id": task_id}
        row = session.execute(select(Task).where(Task.id == tid)).scalar_one_or_none()
        if row is None:
            return {"status": "not_found", "task_id": task_id}
        return {
            "status": row.status.value if hasattr(row.status, 'value') else str(row.status),
            "role": row.role or "",
            "objective": (row.objective or "")[:500],
            "summary": (row.summary or "")[:1000],
            "error": row.error or "",
            "created_at": str(row.created_at) if row.created_at else "",
            "completed_at": str(row.completed_at) if row.completed_at else "",
        }


async def _friend_memory_read(input_json: dict, **_ctx) -> dict:
    """Read friend_memory entries."""
    from aria.db.base import session_scope
    from aria.db.models import FriendMemory
    from sqlalchemy import select
    category = input_json.get("category", "")
    limit = min(input_json.get("limit", 20), 200)
    with session_scope() as session:
        q = select(FriendMemory)
        if category:
            q = q.where(FriendMemory.category == category)
        q = q.order_by(FriendMemory.updated_at.desc()).limit(limit)
        rows = session.execute(q).scalars().all()
        return {
            "entries": [
                {"id": r.id, "category": r.category, "key": r.key,
                 "value": r.value_json, "updated_at": str(r.updated_at), "source": r.source}
                for r in rows
            ],
            "total": len(rows),
        }


# ── Tool Registry (§11) ────────────────────────────────────────────────────


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "read_note": ToolSpec(
        tool_name="read_note",
        description="Читает заметку из Obsidian vault по имени.",
        input_schema={"type": "object", "properties": {"note_name": {"type": "string"}}, "required": ["note_name"]},
        output_schema={"type": "object", "properties": {"found": {"type": "boolean"}, "content": {"type": ["string", "null"]}}},
        timeout_sec=10,
        risk_level=RiskLevel.low,
        null_output_allowed=True,
        requires_approval=False,
        allowed_roles=("general", "orchestrator", "obsidian_keeper", "qa_auditor"),
        idempotency_class=IdempotencyClass.safe_read,
        handler=_read_note,
    ),
    "write_note": ToolSpec(
        tool_name="write_note",
        description="Пишет/обновляет заметку в Obsidian vault.",
        input_schema={
            "type": "object",
            "properties": {
                "note_name": {"type": "string"},
                "content": {"type": "string"},
                "folder": {"type": "string"},
            },
            "required": ["note_name", "content"],
        },
        output_schema={"type": "object", "properties": {"path": {"type": "string"}, "bytes_written": {"type": "integer"}}},
        timeout_sec=10,
        risk_level=RiskLevel.low,
        null_output_allowed=False,
        requires_approval=False,
        allowed_roles=("obsidian_keeper", "orchestrator"),
        idempotency_class=IdempotencyClass.safe_write,
        handler=_write_note,
    ),
    "search_vault": ToolSpec(
        tool_name="search_vault",
        description="Ищет текст по всем заметкам Obsidian vault (case-insensitive substring), возвращает совпадения с контекстом в 2 строки.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["pattern"],
        },
        output_schema={"type": "object", "properties": {"matches": {"type": "array"}, "total": {"type": "integer"}}},
        timeout_sec=30,
        risk_level=RiskLevel.low,
        null_output_allowed=False,
        requires_approval=False,
        allowed_roles=("general", "orchestrator", "obsidian_keeper", "qa_auditor", "coder"),
        idempotency_class=IdempotencyClass.safe_read,
        handler=_search_vault,
    ),
    "list_vault": ToolSpec(
        tool_name="list_vault",
        description="Список подпапок и заметок в директории Obsidian vault (без рекурсии). Пустой subdir = корень vault.",
        input_schema={
            "type": "object",
            "properties": {"subdir": {"type": "string"}},
            "required": [],
        },
        output_schema={"type": "object", "properties": {"dirs": {"type": "array"}, "notes": {"type": "array"}}},
        timeout_sec=15,
        risk_level=RiskLevel.low,
        null_output_allowed=False,
        requires_approval=False,
        allowed_roles=("general", "orchestrator", "obsidian_keeper", "qa_auditor", "coder"),
        idempotency_class=IdempotencyClass.safe_read,
        handler=_list_vault,
    ),
    "shell_execute": ToolSpec(
        tool_name="shell_execute",
        description="Выполняет shell/терминал команду в ОС в пределах sandbox-директории.",
        input_schema={"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
        output_schema={
            "type": "object",
            "properties": {"returncode": {"type": "integer"}, "stdout": {"type": "string"}, "stderr": {"type": "string"}},
        },
        timeout_sec=120,
        risk_level=RiskLevel.high,
        null_output_allowed=False,
        requires_approval=True,  # финально решает validators.is_high_risk_command по конкретной команде
        allowed_roles=("coder", "devops_infra", "image_gen"),
        idempotency_class=IdempotencyClass.unsafe_write,
        handler=_shell_execute,
    ),
    "file_read": ToolSpec(
        tool_name="file_read",
        description="Читает содержимое файла или список директории в пределах sandbox root.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        output_schema={"type": "object", "properties": {"exists": {"type": "boolean"}, "content": {"type": ["string", "null"]}}},
        timeout_sec=15,
        risk_level=RiskLevel.low,
        null_output_allowed=True,
        requires_approval=False,
        allowed_roles=("general", "orchestrator", "coder", "devops_infra", "vision", "qa_auditor", "housekeeping"),
        idempotency_class=IdempotencyClass.safe_read,
        handler=_file_read,
    ),
    "file_write": ToolSpec(
        tool_name="file_write",
        description="Пишет содержимое файла (overwrite|append) в пределах sandbox root.",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "mode": {"type": "string", "enum": ["overwrite", "append"]}},
            "required": ["path", "content"],
        },
        output_schema={"type": "object", "properties": {"path": {"type": "string"}, "bytes_written": {"type": "integer"}}},
        timeout_sec=30,
        risk_level=RiskLevel.medium,
        null_output_allowed=False,
        requires_approval=False,
        allowed_roles=("coder", "devops_infra", "image_gen"),
        idempotency_class=IdempotencyClass.unsafe_write,
        handler=_file_write,
    ),
    "file_search": ToolSpec(
        tool_name="file_search",
        description="Ищет файлы по glob-паттерну в пределах sandbox root.",
        input_schema={"type": "object", "properties": {"glob": {"type": "string"}}, "required": []},
        output_schema={"type": "object", "properties": {"matches": {"type": "array"}}},
        timeout_sec=15,
        risk_level=RiskLevel.low,
        null_output_allowed=True,
        requires_approval=False,
        allowed_roles=("general", "orchestrator", "coder", "devops_infra", "qa_auditor", "housekeeping", "obsidian_keeper"),
        idempotency_class=IdempotencyClass.safe_read,
        handler=_file_search,
    ),
    "delegate_task": ToolSpec(
        tool_name="delegate_task",
        description="Делегирует под-задачу саб-агенту в указанной роли. "
                    "Только orchestrator может делегировать (§7.2). "
                    "Глубина вложенности не более MAX_DEPTH=1.",
        input_schema={
            "type": "object",
            "properties": {
                "role": {"type": "string", "description": "Роль суб-агента (coder, devops_infra, research, ...)"},
                "objective": {"type": "string", "description": "Чёткая цель для суб-агента"},
            },
            "required": ["role", "objective"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "summary": {"type": "string"},
                "error": {"type": ["string", "null"]},
            },
        },
        timeout_sec=120,
        risk_level=RiskLevel.medium,
        null_output_allowed=False,
        requires_approval=False,
        allowed_roles=("orchestrator",),
        idempotency_class=IdempotencyClass.unsafe_write,
        handler=None,  # handled as special case in loop.py
    ),
    "web_search": ToolSpec(
        tool_name="web_search",
        description="DuckDuckGo web search — ищет в интернете без API-ключа. "
                    "Берёт query и max_results(1-15). Возвращает список {title, url}.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "results": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "url": {"type": "string"},
                            "snippet": {"type": "string"},
                        },
                    },
                },
                "error": {"type": "string"},
            },
        },
        timeout_sec=15,
        risk_level=RiskLevel.medium,
        null_output_allowed=False,
        requires_approval=False,
        allowed_roles=("orchestrator", "research", "coder"),
        idempotency_class=IdempotencyClass.safe_read,
        handler=web_handler.web_search,
    ),
    "vision_analyze": ToolSpec(
        tool_name="vision_analyze",
        description="Analyze an image using Gemini multimodal vision. "
                    "Accepts file_path or image_base64. "
                    "Returns text description of the image contents.",
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to image file"},
                "image_base64": {"type": "string", "description": "Base64-encoded image data"},
                "prompt": {"type": "string", "description": "Optional text instruction (default: describe)"},
                "model": {"type": "string", "description": "Gemini model (default: gemini-2.5-flash)"},
            },
            "oneOf": [
                {"required": ["file_path"]},
                {"required": ["image_base64"]},
            ],
        },
        output_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "error": {"type": "string"},
                "model": {"type": "string"},
            },
        },
        timeout_sec=35,
        risk_level=RiskLevel.medium,
        null_output_allowed=False,
        requires_approval=False,
        allowed_roles=("orchestrator", "vision", "coder", "research", "qa_auditor"),
        idempotency_class=IdempotencyClass.safe_read,
        handler=vision_handler.vision_analyze,
    ),
    "notebook_query": ToolSpec(
        tool_name="notebook_query",
        description="Запрашивает исследовательский notebook по вопросу. "
        "Требует cookies Google-аккаунта. Неофициальный API.",
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Исследовательский вопрос"},
                "source_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "ID источников для сужения области поиска (опционально)",
                },
            },
            "required": ["prompt"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "data": {"type": "string"},
                "error": {"type": "string"},
            },
        },
        timeout_sec=60,
        risk_level=RiskLevel.low,
        null_output_allowed=True,
        requires_approval=False,
        allowed_roles=("general", "research", "orchestrator"),
        idempotency_class=IdempotencyClass.safe_read,
        handler=_notebook_query,
    ),
    "skill_lookup": ToolSpec(
        tool_name="skill_lookup",
        description="Ищет навыки (skills_meta) по имени или категории. Read-only.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Substring search in skill name or description"},
                "category": {"type": "string", "description": "Filter by category (optional)"},
                "limit": {"type": "integer", "description": "Max results (default 10)"},
            },
            "required": [],
        },
        output_schema={
            "type": "object",
            "properties": {
                "skills": {"type": "array"},
                "total": {"type": "integer"},
            },
        },
        timeout_sec=10,
        risk_level=RiskLevel.low,
        null_output_allowed=False,
        requires_approval=False,
        allowed_roles=("orchestrator", "research", "obsidian_keeper", "coder"),
        idempotency_class=IdempotencyClass.safe_read,
        handler=_skill_lookup,
    ),
    "task_status": ToolSpec(
        tool_name="task_status",
        description="Запрашивает статус задачи по task_id. Read-only.",
        input_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "UUID задачи"},
            },
            "required": ["task_id"],
        },
        output_schema={
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "role": {"type": "string"},
                "objective": {"type": "string"},
                "summary": {"type": "string"},
                "error": {"type": "string"},
                "created_at": {"type": "string"},
                "completed_at": {"type": "string"},
            },
        },
        timeout_sec=10,
        risk_level=RiskLevel.low,
        null_output_allowed=True,
        requires_approval=False,
        allowed_roles=("orchestrator", "research", "coder", "qa_auditor"),
        idempotency_class=IdempotencyClass.safe_read,
        handler=_task_status,
    ),
    "friend_memory_read": ToolSpec(
        tool_name="friend_memory_read",
        description="Читает friend_memory — заметки о стиле, предпочтениях, повторяющемся контексте пользователя. Read-only.",
        input_schema={"type": "object", "properties": {"category": {"type": "string"}, "limit": {"type": "integer"}}, "required": []},
        output_schema={"type": "object", "properties": {"entries": {"type": "array"}, "total": {"type": "integer"}}},
        timeout_sec=10,
        risk_level=RiskLevel.low,
        null_output_allowed=False,
        requires_approval=False,
        allowed_roles=("orchestrator", "research", "obsidian_keeper"),
        idempotency_class=IdempotencyClass.safe_read,
        handler=_friend_memory_read,
    ),
}


def get_tool(tool_name: str) -> ToolSpec:
    spec = TOOL_REGISTRY.get(tool_name)
    if spec is None:
        raise KeyError(f"unknown tool_name={tool_name}; любой tool call обязан ссылаться на registry entry (§11)")
    return spec


def list_tools() -> list[ToolSpec]:
    return list(TOOL_REGISTRY.values())


# ---------- _delegate_task (standalone, direct call from tests & loop) ----------


async def _delegate_task(
    input_json: dict,
    timeout_sec: int = 60,
    sandbox_root: str = "",
    router=None,
    session_id=None,
    parent_task_id=None,
    delegation_depth: int = 0,
) -> dict:
    """§7.2: делегирование суб-агенту.

    Вызывается loop.py как special case для tool_name='delegate_task'.
    Также импортируется напрямую тестами (test_delegate_task.py).
    """
    from aria.core.delegate import MAX_DEPTH
    from aria.core.loop import execute_agent_loop as _run_task
    from aria.core.roles import get_role as _get_role
    from aria.db import repository as _repo
    from aria.db.base import session_scope as _session_scope
    from aria.db.enums import TaskStatus

    role_name = input_json.get("role", "")
    objective = input_json.get("objective", "")

    # Проверка: нельзя делегировать самому себе
    if role_name == "orchestrator":
        return {"status": "failed", "summary": "", "error": "orchestrator cannot delegate to itself (§7.2)"}

    # Проверка: роль должна существовать
    try:
        role_def = _get_role(role_name)
    except (KeyError, PermissionError):
        return {"status": "failed", "summary": "", "error": f"unknown or disabled role: {role_name}"}

    # Проверка: глубина делегирования
    if delegation_depth >= MAX_DEPTH:
        return {"status": "failed", "summary": "", "error": f"max delegation depth ({MAX_DEPTH}) exceeded, §7.2"}

    # Если router не передан — не можем выполнить суб-задачу
    if router is None:
        return {"status": "failed", "summary": "", "error": "no router provided for sub-task execution"}

    # Создаём суб-задачу в БД
    with _session_scope() as db:
        session = _repo.get_session(db, session_id) if session_id else None
        if session is None:
            # Пробуем получить сессию через parent_task_id
            if parent_task_id:
                parent = _repo.get_task(db, parent_task_id)
                if parent:
                    session = _repo.get_session(db, parent.session_id)
        if session is None:
            return {"status": "failed", "summary": "", "error": "session not found"}

        parent = _repo.get_task(db, parent_task_id) if parent_task_id else None

        sub_task = _repo.create_task(
            db,
            session,
            role=role_name,
            objective=objective,
            parent_task_id=parent_task_id,
            delegation_depth=delegation_depth + 1,
        )
        _repo.set_task_status(db, sub_task, TaskStatus.approved)
        sub_task_id = sub_task.id

    # Запускаем суб-задачу в том же event loop
    try:
        await _run_task(sub_task_id, router, sandbox_root)
    except Exception as exc:
        return {"status": "failed", "summary": "", "error": f"sub-task crashed: {exc}"}

    # Собираем результат
    with _session_scope() as db:
        sub_task = _repo.get_task(db, sub_task_id)
        if sub_task is None:
            return {"status": "failed", "summary": "", "error": "sub-task disappeared"}
        status = sub_task.status.value if hasattr(sub_task.status, "value") else str(sub_task.status)
        summary = sub_task.objective[:200]
        error = sub_task.error_message if sub_task.status in (TaskStatus.failed, TaskStatus.cancelled) else None

    return {
        "status": "done" if status in ("done", "done_unaudited") else status,
        "summary": summary,
        "error": error,
    }

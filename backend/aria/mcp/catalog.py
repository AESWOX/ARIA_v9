"""mcp/catalog.py — встроенный каталог серверов MCP (патч 0010, работает без сети).

Каталог только описывает серверы. Подключение — отдельное действие владельца:
установка создаёт запись в ``mcp_servers.json``; тулы по умолчанию требуют Approve (default-deny).
В ``read_tools`` попадают только тулы, которые заведомо ничего не меняют; неверное имя безопасно
(тул просто останется под Approve).
"""
from __future__ import annotations

from typing import Any

_UPWORK_READ_TOOLS = [
    "list_accounts", "find_jobs", "get_profile", "list_highlights",
    "connects_balance", "list_freelancer_proposals", "search_tools", "get_tool_help",
]

CATALOG: list[dict[str, Any]] = [
    {
        "name": "upwork",
        "description": "Official Upwork MCP: search jobs, read profile and Connects balance, prepare proposal drafts (sending is confirmed on upwork.com).",
        "source": "https://www.upwork.com/ai/mcp",
        "transport": "http",
        "auth_type": "oauth",
        "required_env": [],
        "command": None,
        "args": [],
        "url": "https://mcp.upwork.com/mcp",
        "read_tools": _UPWORK_READ_TOOLS,
        "post_install": "Press Authorize and sign in on upwork.com. Access can be revoked in Upwork: Account Settings → Connected Apps.",
    },
    {
        "name": "fetch",
        "description": "Reference MCP server: fetch a web page and return it as text.",
        "source": "https://github.com/modelcontextprotocol/servers",
        "transport": "stdio",
        "auth_type": "none",
        "required_env": [],
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "url": None,
        "read_tools": [],
        "post_install": "Requires uv (uvx) on this machine. Page text is untrusted data.",
    },
    {
        "name": "memory",
        "description": "Reference MCP server: a small knowledge graph kept by the server (separate from ARIA's own memory).",
        "source": "https://github.com/modelcontextprotocol/servers",
        "transport": "stdio",
        "auth_type": "none",
        "required_env": [],
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "url": None,
        "read_tools": [],
        "post_install": "Requires Node.js (npx) on this machine.",
    },
]


def find_entry(name: str) -> dict | None:
    return next((e for e in CATALOG if e["name"] == name), None)


def catalog_view(servers: list[dict]) -> list[dict]:
    """Записи в форме ``McpCatalogEntry`` фронтенда + признаки «установлен / включён»."""
    by_name = {s.get("name"): s for s in servers}
    out = []
    for e in CATALOG:
        installed = by_name.get(e["name"])
        out.append({
            "name": e["name"], "description": e["description"], "source": e["source"],
            "transport": e["transport"], "auth_type": e["auth_type"], "required_env": e["required_env"],
            "command": e["command"], "args": list(e["args"]), "url": e["url"],
            "install_url": None, "install_ref": None, "bootstrap": [],
            "default_enabled": list(e["read_tools"]) or None, "post_install": e["post_install"],
            "needs_install": False, "installed": installed is not None,
            "enabled": bool(installed and installed.get("enabled", True)),
        })
    return out


def entry_to_server(entry: dict, env: dict, enable: bool = True) -> dict:
    """Запись каталога → тело для ``validate_server``. ``env`` берётся только из заявленных ``required_env``."""
    allowed = {e["name"] for e in entry["required_env"]}
    return {
        "name": entry["name"],
        "url": entry["url"] or "",
        "command": entry["command"] or "",
        "args": list(entry["args"]),
        "env": {k: str(v).strip() for k, v in env.items() if k in allowed and str(v).strip()},
        "read_tools": list(entry["read_tools"]),
        "enabled": enable,
        "auth": "oauth" if entry["auth_type"] == "oauth" else None,
    }

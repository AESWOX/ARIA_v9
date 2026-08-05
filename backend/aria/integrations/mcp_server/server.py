"""server.py — ARIA MCP server (stdio transport).

Implements the Model Context Protocol (MCP) over stdin/stdout, exposing
a curated allowlist of read-only ARIA tools for VS Code Agent Mode.

Protocol: JSON-RPC 2.0 over stdio
  - tools/list          -> list of available tools with schemas
  - tools/call          -> invoke a tool by name with arguments
  - initialize          -> handshake (MCP spec §3.1)
  - notifications/initialized -> ack

Allowlist (read-only, no side effects):
  - search_vault        full-text search in Obsidian vault
  - skill_lookup        query skills_meta by name/category
  - task_status         query task by ID
  - friend_memory_read  read friend_memory entries

Approval gate: all tools in the allowlist are read-only and safe.
If allowlist is expanded to include write tools in the future, each
expansion must be a separate TZ item with explicit approval gate wiring.
"""

import asyncio
import json
import sys
from typing import Any

# ── allowlist ─────────────────────────────────────────────────────────────

ALLOWLIST: frozenset[str] = frozenset({
    "search_vault",
    "skill_lookup",
    "task_status",
    "friend_memory_read",
})

# ── JSON-RPC helpers ──────────────────────────────────────────────────────


def _rpc_result(msg_id: int | str | None, result: dict[str, Any]) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _rpc_error(msg_id: int | str | None, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}})


# ── handlers ──────────────────────────────────────────────────────────────


async def _handle_initialize(params: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocolVersion": params.get("protocolVersion", "2024-11-05"),
        "capabilities": {},
        "serverInfo": {"name": "aria-mcp", "version": "0.1.0"},
    }


async def _handle_tools_list() -> dict[str, Any]:
    from aria.tools.registry import list_tools

    all_specs = list_tools()
    allowed = [s for s in all_specs if s.tool_name in ALLOWLIST]
    allowed.sort(key=lambda s: s.tool_name)
    return {
        "tools": [
            {"name": s.tool_name, "description": s.description, "inputSchema": s.input_schema}
            for s in allowed
        ]
    }


async def _handle_tools_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from aria.tools.registry import get_tool

    if name not in ALLOWLIST:
        msg = f"tool '{name}' is not in the allowlist"
        return {"isError": True, "content": [{"type": "text", "text": msg}]}

    try:
        spec = get_tool(name)
    except KeyError:
        msg = f"unknown tool: {name}"
        return {"isError": True, "content": [{"type": "text", "text": msg}]}

    try:
        result = await spec.handler(input_json=arguments)
        text = json.dumps(result, ensure_ascii=False, default=str)
        return {"content": [{"type": "text", "text": text}]}
    except Exception as exc:
        msg = f"error calling {name}: {exc}"
        return {"isError": True, "content": [{"type": "text", "text": msg}]}


# ── sync dispatcher (no asyncio I/O — stdio blocks naturally) ─────────────


def _process_request(msg: dict) -> dict:
    """Synchronous dispatcher for a single JSON-RPC request."""
    method = msg.get("method", "")
    msg_id = msg.get("id")
    params = msg.get("params", {})

    if method == "initialize":
        # Run the async handler in a fresh event loop
        return asyncio.run(_handle_initialize(params))
    elif method == "notifications/initialized":
        return {"_notification": True}
    elif method == "tools/list":
        return asyncio.run(_handle_tools_list())
    elif method == "tools/call":
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        return asyncio.run(_handle_tools_call(name, arguments))
    else:
        return {"error": {"code": -32601, "message": f"Method not found: {method}"}}


def main() -> None:
    """Read JSON-RPC requests from stdin, write responses to stdout.
    Uses sync I/O — `asyncio.run()` per request for async tool handlers.
    """
    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}) + "\n")
            sys.stdout.flush()
            continue

        msg_id = msg.get("id")
        result = _process_request(msg)

        # Skip notifications — no response
        if result.get("_notification"):
            continue

        # If result has error key, it's a method error
        if "error" in result:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "error": result["error"]}) + "\n")
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

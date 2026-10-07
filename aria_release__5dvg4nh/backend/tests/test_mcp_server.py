"""test_mcp_server.py — MCP server process-level tests."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent.parent  # backend/
MCP_SERVER = BACKEND / "aria" / "integrations" / "mcp_server" / "server.py"
PYTHON = Path(sys.executable)


def _send(proc, msg: dict) -> dict:
    """Send a JSON-RPC request, read one JSON-RPC response."""
    line = json.dumps(msg) + "\n"
    proc.stdin.write(line)
    proc.stdin.flush()
    resp = proc.stdout.readline()
    return json.loads(resp)


@pytest.fixture
def mcp_proc():
    """Start MCP server subprocess with PYTHONPATH set to backend/."""
    base_env = os.environ.copy()
    base_env["PYTHONPATH"] = str(BACKEND)
    proc = subprocess.Popen(
        [str(PYTHON), str(MCP_SERVER)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=base_env,
    )
    yield proc
    proc.terminate()
    proc.wait(timeout=5)


def test_mcp_server_starts(mcp_proc):
    """Server starts, handles initialize + tools/list, returns 4 allowlisted tools."""
    proc = mcp_proc

    # 1. Initialize
    init_resp = _send(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "0.1.0"}},
    })
    assert "result" in init_resp, f"Initialize failed: {init_resp}"
    assert init_resp["result"]["serverInfo"]["name"] == "aria-mcp"

    # 2. tools/list
    list_resp = _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert "result" in list_resp, f"tools/list failed: {list_resp}"
    tools = list_resp["result"]["tools"]
    tool_names = {t["name"] for t in tools}
    assert tool_names == {"search_vault", "skill_lookup", "task_status", "friend_memory_read"}, (
        f"Unexpected tools: {tool_names}"
    )
    assert len(tools) == 4

    # 3. tools/call — search_vault (read-only)
    call_resp = _send(proc, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "search_vault", "arguments": {"pattern": "test", "max_results": 3}},
    })
    assert "result" in call_resp, f"tools/call search_vault failed: {call_resp}"
    content = call_resp["result"]["content"]
    assert isinstance(content, list)
    assert len(content) >= 1

    # 5. tools/call — skill_lookup (read-only)
    skill_resp = _send(proc, {
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "skill_lookup", "arguments": {"pattern": "", "limit": 5}},
    })
    assert "result" in skill_resp, f"tools/call skill_lookup failed: {skill_resp}"
    skill_data = json.loads(skill_resp["result"]["content"][0]["text"])
    assert "skills" in skill_data
    assert isinstance(skill_data["total"], int)

    # 6. tools/call — task_status (read-only, nonexistent task returns not_found)
    task_resp = _send(proc, {
        "jsonrpc": "2.0", "id": 6, "method": "tools/call",
        "params": {"name": "task_status", "arguments": {"task_id": "00000000-0000-0000-0000-000000000000"}},
    })
    assert "result" in task_resp, f"tools/call task_status failed: {task_resp}"
    task_text = task_resp["result"]["content"][0]["text"]
    # if error, dump it for debugging
    if task_resp["result"].get("isError"):
        pytest.fail(f"task_status error: {task_text}")
    task_data = json.loads(task_text)
    assert task_data["status"] == "not_found"

    # 7. tools/call — friend_memory_read (read-only)
    fm_resp = _send(proc, {
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "friend_memory_read", "arguments": {"category": "", "limit": 5}},
    })
    assert "result" in fm_resp, f"tools/call friend_memory_read failed: {fm_resp}"
    fm_text = fm_resp["result"]["content"][0]["text"]
    if fm_resp["result"].get("isError"):
        pytest.fail(f"friend_memory_read error: {fm_text}")
    fm_data = json.loads(fm_text)
    assert "entries" in fm_data

    # 4. tools/call — reject non-allowlisted tool
    reject_resp = _send(proc, {
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "delegate_task", "arguments": {}},
    })
    assert "result" in reject_resp
    assert reject_resp["result"].get("isError") is True
    assert "not in the allowlist" in reject_resp["result"]["content"][0]["text"]


def test_mcp_server_unknown_tool(mcp_proc):
    """Call to non-existent tool returns error."""
    proc = mcp_proc
    _send(proc, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "test", "version": "0.1.0"}},
    })
    resp = _send(proc, {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "does_not_exist", "arguments": {}},
    })
    assert "result" in resp
    assert resp["result"].get("isError") is True
    assert "not in the allowlist" in resp["result"]["content"][0]["text"].lower()


def test_mcp_server_unknown_method(mcp_proc):
    """Unknown method returns -32601."""
    proc = mcp_proc
    resp = _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "unknown_method", "params": {}})
    assert "error" in resp
    assert resp["error"]["code"] == -32601

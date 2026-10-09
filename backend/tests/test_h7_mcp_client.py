"""H7 (патч 0007): клиент MCP — транспорты, политика default-deny, роутер, подключение к циклу агента.

Сервер для проверок — ``tests/fixtures/fake_mcp_server.py`` (stdio, настоящий подпроцесс) и
``httpx.MockTransport`` для streamable HTTP. Живых серверов тесты не трогают.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria import paths
from aria.api.auth import require_runtime_token
from aria.core.loop import execute_agent_loop
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import TaskStatus
from aria.llm.providers.stub import StubProvider, final_answer, tool_call
from aria.llm.router import ProviderRouter
from aria.mcp import manager as mcp_manager
from aria.mcp.client import HttpTransport, McpAuthRequired, McpClient, McpError, McpTimeout, StdioTransport
from aria.mcp.manager import McpManager, make_tool_name, validate_server
from aria.routers import mcp as mcp_router
from aria.tools.registry import TOOL_REGISTRY

FAKE = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """Каждый тест — свой каталог данных: mcp_servers.json не пересекается с другими тестами."""
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    return tmp_path


def _fake_cfg(**extra) -> dict:
    payload = {"name": "fake", "command": sys.executable, "args": [FAKE]}
    payload.update(extra)
    return validate_server(payload)


def _stdio_client() -> McpClient:
    return McpClient(StdioTransport(sys.executable, [FAKE]))


# ── клиент поверх stdio ──────────────────────────────────────────────────

def test_stdio_client_lists_all_pages_and_calls_tools():
    async def go():
        c = _stdio_client()
        await c.connect(15)
        try:
            names = [t["name"] for t in await c.list_tools()]
            res = await c.call_tool("echo", {"text": "hi"})
            with pytest.raises(McpError, match="unknown tool"):
                await c.call_tool("nope")
            return c.server_info, c.protocol_version, names, res
        finally:
            await c.close()

    info, proto, names, res = _run(go())
    assert info["name"] == "fake" and proto == "2025-06-18"
    assert names == ["echo", "write_thing", "reader_note"], "список должен собираться со всех страниц (nextCursor)"
    assert res["content"][0]["text"] == "echo:hi"


def test_server_noise_in_stdout_does_not_break_the_client():
    """Сервер печатает не-JSON строки и шлёт ping клиенту — клиент обязан продолжать работать."""
    async def go():
        c = _stdio_client()
        await c.connect(15)
        try:
            return [await c.call_tool("echo", {"text": str(i)}) for i in range(3)]
        finally:
            await c.close()

    assert [r["content"][0]["text"] for r in _run(go())] == ["echo:0", "echo:1", "echo:2"]


def test_start_failure_is_a_clear_mcp_error():
    with pytest.raises(McpError, match="cannot start"):
        _run(McpClient(StdioTransport("definitely-not-a-binary-xyz")).connect(5))


def test_call_timeout_raises_mcp_timeout():
    async def go():
        c = _stdio_client()
        await c.connect(15)
        try:
            await c.call_tool("sleepy", {}, timeout=0.5)
        finally:
            await c.close()

    with pytest.raises(McpTimeout):
        _run(go())


# ── клиент поверх streamable HTTP ────────────────────────────────────────

def _http_client(handler) -> McpClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return McpClient(HttpTransport("https://mcp.example/mcp", client=http))


def test_http_json_responses_session_id_and_headers():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(200)
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return httpx.Response(
                200, headers={"mcp-session-id": "S1"},
                json={"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2025-06-18", "serverInfo": {"name": "h"}, "capabilities": {}}},
            )
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"tools": [{"name": "t1"}]}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": "ok"}]}})

    async def go():
        c = _http_client(handler)
        await c.connect(10)
        tools = await c.list_tools()
        res = await c.call_tool("t1", {})
        await c.close()
        return tools, res

    tools, res = _run(go())
    assert [t["name"] for t in tools] == ["t1"] and res["content"][0]["text"] == "ok"
    later = [r for r in seen if r.method == "POST" and json.loads(r.content).get("method") in ("tools/list", "tools/call")]
    assert later and all(r.headers["mcp-session-id"] == "S1" for r in later), "Mcp-Session-Id должен уходить в следующие запросы"
    assert all(r.headers["mcp-protocol-version"] == "2025-06-18" for r in later)
    assert "text/event-stream" in later[0].headers["accept"]
    assert any(r.method == "DELETE" for r in seen), "при закрытии сессия должна завершаться"


def test_http_sse_response_is_parsed_and_notifications_skipped():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        result = {"protocolVersion": "2025-06-18", "serverInfo": {"name": "sse"}} if body["method"] == "initialize" else {"tools": [{"name": "a"}]}
        sse = (
            'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
            f'event: message\ndata: {json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})}\n\n'
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())

    async def go():
        c = _http_client(handler)
        await c.connect(10)
        try:
            return await c.list_tools()
        finally:
            await c.close()

    assert [t["name"] for t in _run(go())] == ["a"]


def test_http_401_requires_auth_and_5xx_is_an_error():
    def unauthorized(request):
        return httpx.Response(401, headers={"www-authenticate": 'Bearer resource_metadata="https://x/.well-known"'})

    with pytest.raises(McpAuthRequired) as exc:
        _run(_http_client(unauthorized).connect(5))
    assert "resource_metadata" in exc.value.www_authenticate

    with pytest.raises(McpError, match="HTTP 500"):
        _run(_http_client(lambda r: httpx.Response(500, text="boom")).connect(5))


# ── менеджер: политика default-deny ──────────────────────────────────────

def _with_manager(data_dir, cfg, body):
    """Прогнать ``body(mgr)`` и гарантированно убрать тулы из глобального реестра."""
    mcp_manager.save_servers([cfg])

    async def go():
        mgr = McpManager()
        try:
            return await body(mgr)
        finally:
            await mgr.close_all()

    out = _run(go())
    assert not [n for n in TOOL_REGISTRY if n.startswith("mcp__")], "тулы MCP не должны оставаться в реестре"
    return out


def test_by_default_every_mcp_tool_is_a_write_tool_and_is_not_called(data_dir):
    async def body(mgr):
        details = await mgr.refresh("fake")
        spec = TOOL_REGISTRY["mcp__fake__echo"]
        blocked = await spec.handler({"text": "x"}, 60, "/tmp")
        return details, spec.requires_approval, spec.risk_level.value, blocked

    details, needs_approval, risk, blocked = _with_manager(data_dir, _fake_cfg(), body)
    assert [d["read_only"] for d in details] == [False, False, False], "аннотациям сервера по умолчанию не верим"
    assert needs_approval is True and risk == "high"
    assert blocked["status"] == "approval_required" and "text" not in blocked, "сервер не должен был быть вызван"


def test_read_tools_allowlist_makes_tool_callable_without_approval(data_dir):
    async def body(mgr):
        details = await mgr.refresh("fake")
        spec = TOOL_REGISTRY["mcp__fake__echo"]
        out = await spec.handler({"text": "hi", "approved": True}, 60, "/tmp")
        write = await TOOL_REGISTRY["mcp__fake__write_thing"].handler({"v": "1"}, 60, "/tmp")
        return details, spec.requires_approval, out, write

    details, needs_approval, out, write = _with_manager(data_dir, _fake_cfg(read_tools=["echo"]), body)
    assert {d["name"]: d["read_only"] for d in details} == {"echo": True, "write_thing": False, "reader_note": False}
    assert needs_approval is False and out["text"] == "echo:hi" and out["is_error"] is False
    assert write["status"] == "approval_required", "write_thing не в read_tools — остаётся под запретом"


def test_readonly_hint_is_trusted_only_when_server_is_trusted(data_dir):
    async def body(mgr):
        return {d["name"]: d["read_only"] for d in await mgr.refresh("fake")}

    assert _with_manager(data_dir, _fake_cfg(trust_annotations=True), body) == {"echo": True, "write_thing": False, "reader_note": False}


def test_approved_write_tool_runs_and_model_args_cannot_self_approve(data_dir):
    async def body(mgr):
        await mgr.refresh("fake")
        spec = TOOL_REGISTRY["mcp__fake__write_thing"]
        self_approved = await spec.handler({"v": "1", "approved": True}, 60, "/tmp")  # approved в аргументах — данные модели
        real = await spec.handler({"v": "2"}, 60, "/tmp", approved=True)  # системный kwarg после Approve
        return self_approved, real

    self_approved, real = _with_manager(data_dir, _fake_cfg(), body)
    assert self_approved["status"] == "approval_required"
    assert real["text"] == "wrote:2" and real["structured"] == {"ok": True}


def test_only_allowed_roles_see_mcp_tools(data_dir):
    async def body(mgr):
        await mgr.refresh("fake")
        return mgr.tool_names_for_role("general"), mgr.tool_names_for_role("coder"), mgr.tool_names_for_role("qa_auditor")

    general, coder, auditor = _with_manager(data_dir, _fake_cfg(), body)
    assert "mcp__fake__echo" in general and coder == () and auditor == ()


def test_big_and_error_results_are_normalized(data_dir):
    async def body(mgr):
        await mgr.refresh("fake")
        return await mgr.call("fake", "big", {}), await mgr.call("fake", "boom", {})

    big, boom = _with_manager(data_dir, _fake_cfg(), body)
    assert len(big["text"]) == mcp_manager.MAX_TEXT and big["truncated"] is True
    assert boom["is_error"] is True and boom["text"] == "kaboom"


def test_disabled_server_is_not_callable_and_test_leaves_no_tools(data_dir):
    cfg = _fake_cfg()
    cfg["enabled"] = False

    async def body(mgr):
        probe = await mgr.test("fake")
        with pytest.raises(McpError, match="disabled"):
            await mgr.call("fake", "echo", {"text": "x"})
        return probe

    probe = _with_manager(data_dir, cfg, body)
    assert probe["ok"] is True, "проверка соединения выключенного сервера допустима"


def test_failed_server_reports_error_instead_of_raising(data_dir):
    async def body(mgr):
        return await mgr.test("fake")

    probe = _with_manager(data_dir, _fake_cfg(command="definitely-not-a-binary-xyz"), body)
    assert probe["ok"] is False and "cannot start" in probe["error"] and probe["tools"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "bad name", "command": "x"},
        {"name": "ok"},
        {"name": "ok", "url": "ftp://x"},
        {"name": "ok", "url": "https://x", "command": "y"},
        {"name": "ok", "command": "x", "args": "not-a-list"},
    ],
)
def test_validate_server_rejects_bad_input(payload):
    with pytest.raises(ValueError):
        validate_server(payload)


def test_tool_names_fit_provider_limit_and_stay_unique():
    a = make_tool_name("s" * 40, "t" * 60)
    b = make_tool_name("s" * 40, "t" * 59 + "x")
    assert len(a) <= 64 and len(b) <= 64 and a != b
    assert make_tool_name("my server", "do.it") == "mcp__my_server__do_it"


# ── роутер ───────────────────────────────────────────────────────────────

def test_router_crud_masks_secrets_and_tests_server(data_dir, monkeypatch):
    monkeypatch.setattr(mcp_manager, "_manager", McpManager())
    app = FastAPI()
    app.include_router(mcp_router.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"

    with TestClient(app) as c:  # один event loop на все запросы: подпроцесс живёт между ними
        r = c.post("/mcp/servers", json={"name": "fake", "command": sys.executable, "args": [FAKE], "env": {"TOKEN": "s3cret"}})
        assert r.status_code == 200, r.text
        assert r.json()["env"] == {"TOKEN": "***"}, "значения env в ответе скрыты"
        assert "s3cret" in (data_dir / "mcp_servers.json").read_text(encoding="utf-8")
        assert c.post("/mcp/servers", json={"name": "fake", "command": "x"}).status_code == 409
        assert c.post("/mcp/servers", json={"name": "bad name", "command": "x"}).status_code == 400

        probe = c.post("/mcp/servers/fake/test").json()
        assert probe["ok"] is True and probe["tools"] == ["echo", "write_thing", "reader_note"]
        listed = c.get("/mcp/servers").json()["servers"][0]
        assert listed["connected"] is True and listed["tools"] == probe["tools"]

        assert c.put("/mcp/servers/fake/enabled", json={"enabled": False}).json()["enabled"] is False
        assert c.get("/mcp/servers").json()["servers"][0]["connected"] is False
        assert c.post("/mcp/servers/nope/test").status_code == 404
        assert c.delete("/mcp/servers/fake").json() == {"ok": True}
        assert c.get("/mcp/servers").json()["servers"] == []
    assert not [n for n in TOOL_REGISTRY if n.startswith("mcp__")]


# ── цикл агента ──────────────────────────────────────────────────────────

class _Recording(StubProvider):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.seen: list[list] = []

    async def chat(self, messages, tools, timeout_sec):
        self.seen.append(list(messages))
        return await super().chat(messages, tools, timeout_sec)


def _agent_run(tmp_path, monkeypatch, cfg, calls):
    """Запустить execute_agent_loop для роли general с подключённым фейковым MCP-сервером."""
    mcp_manager.save_servers([cfg])
    mgr = McpManager()
    monkeypatch.setattr(mcp_manager, "_manager", mgr)
    std = _Recording(provider_id="stub-std")
    std.provider_class = "standard_reasoning"
    for c in calls:
        std.push(c)
    std.push(final_answer("done"))
    for _ in range(3):
        std.push(final_answer("OK"))
    router = ProviderRouter()
    router.register(std)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    with session_scope() as db:
        session = repo.create_session(db, title=f"h7-{uuid.uuid4().hex[:6]}")
        task = repo.create_task(db, session, role="general", objective="use mcp")
        repo.set_task_status(db, task, TaskStatus.approved)
        task_id = task.id

    async def go():
        try:
            await execute_agent_loop(task_id, router, str(sandbox))
        finally:
            await mgr.close_all()

    _run(go())
    with session_scope() as db:
        rows = [(c.tool_name, c.status.value, c.error_code) for c in repo.list_tool_calls(db, task_id)]
    return std, rows


def test_agent_calls_read_only_mcp_tool_and_sees_its_result(data_dir, tmp_path, monkeypatch):
    std, rows = _agent_run(tmp_path, monkeypatch, _fake_cfg(read_tools=["echo"]), [tool_call("mcp__fake__echo", {"text": "hello"})])
    assert ("mcp__fake__echo", "ok", None) in rows
    after_tool = "\n".join(m.content for m in std.seen[1])
    assert "echo:hello" in after_tool and 'source_trust="untrusted"' in after_tool, "результат MCP — недоверенные данные в контексте модели"


def test_agent_cannot_run_mcp_write_tool_without_approval(data_dir, tmp_path, monkeypatch):
    std, rows = _agent_run(tmp_path, monkeypatch, _fake_cfg(), [tool_call("mcp__fake__write_thing", {"v": "x"})])
    assert ("mcp__fake__write_thing", "blocked_policy", "approval_required") in rows
    assert "wrote:x" not in "\n".join(m.content for m in std.seen[1]), "запись не должна была исполниться"

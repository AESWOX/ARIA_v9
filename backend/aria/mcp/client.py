"""mcp/client.py — минимальный клиент Model Context Protocol (H7).

Транспорты:
  * ``StdioTransport`` — подпроцесс, JSON-RPC построчно (newline-delimited);
  * ``HttpTransport`` — streamable HTTP: POST на единый URL, ответ либо JSON,
    либо SSE-поток; ``Mcp-Session-Id`` сохраняется и отправляется дальше.

Клиент намеренно маленький и без внешних зависимостей (только httpx, уже в
requirements): нужные методы — ``initialize``, ``tools/list`` (с пагинацией),
``tools/call``. Запросы сервера к клиенту (``ping``) отвечаются; остальные
(sampling, roots, elicitation) отклоняются «method not found» — мы их не заявляем.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import shutil
from typing import Any, Callable

import httpx

logger = logging.getLogger("local_agent.mcp")

PROTOCOL_VERSION = "2025-06-18"
MAX_LIST_PAGES = 20


class McpError(Exception):
    """Ошибка протокола или транспорта."""


class McpTimeout(McpError):
    """Сервер не ответил вовремя."""


class McpAuthRequired(McpError):
    """Сервер ответил 401: нужна авторизация (OAuth, патч 0008)."""

    def __init__(self, message: str, www_authenticate: str = "") -> None:
        super().__init__(message)
        self.www_authenticate = www_authenticate


def _ok(msg_id: Any, result: dict | None = None) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result or {}}


def _err(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _server_request_reply(msg: dict) -> dict | None:
    """Ответ на запрос сервера к клиенту (есть и ``id``, и ``method``)."""
    if msg.get("method") == "ping":
        return _ok(msg.get("id"))
    return _err(msg.get("id"), -32601, "method not supported by this client")


# ── stdio ────────────────────────────────────────────────────────────────

class StdioTransport:
    def __init__(self, command: str, args: list[str] | None = None, env: dict[str, str] | None = None, cwd: str | None = None) -> None:
        self.command = command
        self.args = [str(a) for a in (args or [])]
        self.env = {str(k): str(v) for k, v in (env or {}).items()}
        self.cwd = cwd
        self.stderr_tail = ""
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[Any, asyncio.Future] = {}
        self._tasks: list[asyncio.Task] = []

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def set_protocol_version(self, _version: str) -> None:  # для единообразия с HttpTransport
        return None

    async def start(self) -> None:
        # На Windows «npx» — это npx.cmd; create_subprocess_exec сам расширение не подставляет.
        exe = shutil.which(self.command) or self.command
        try:
            self._proc = await asyncio.create_subprocess_exec(
                exe, *self.args,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={**os.environ, **self.env}, cwd=self.cwd, limit=16 * 1024 * 1024,
            )
        except (OSError, ValueError) as exc:
            raise McpError(f"cannot start '{self.command}': {exc}") from exc
        self._tasks = [asyncio.create_task(self._read_loop()), asyncio.create_task(self._drain_stderr())]

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue  # не JSON-RPC (лог в stdout) — игнорируем
                if not isinstance(msg, dict):
                    continue
                if "method" in msg and "id" in msg:
                    reply = _server_request_reply(msg)
                    if reply is not None:
                        await self._send(reply)
                elif "id" in msg and ("result" in msg or "error" in msg):
                    fut = self._pending.pop(msg["id"], None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("mcp stdio reader failed")
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(McpError(f"server closed the connection. stderr: {self.stderr_tail[-300:]}"))
            self._pending.clear()

    async def _drain_stderr(self) -> None:
        assert self._proc and self._proc.stderr
        try:
            while True:
                chunk = await self._proc.stderr.read(2048)
                if not chunk:
                    break
                self.stderr_tail = (self.stderr_tail + chunk.decode("utf-8", "replace"))[-4000:]
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    async def _send(self, msg: dict) -> None:
        if self._proc is None or self._proc.stdin is None or self._proc.stdin.is_closing():
            raise McpError("server is not running")
        self._proc.stdin.write((json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def request(self, payload: dict, timeout: float) -> dict:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[payload["id"]] = fut
        try:
            await self._send(payload)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as exc:
            raise McpTimeout(f"no response to {payload.get('method')} in {timeout:.0f}s") from exc
        finally:
            self._pending.pop(payload["id"], None)

    async def notify(self, payload: dict) -> None:
        await self._send(payload)

    async def close(self) -> None:
        proc = self._proc
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except BaseException:  # noqa: BLE001
                pass
        self._tasks = []
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), 3)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), 3)
                except Exception:  # noqa: BLE001
                    pass
        self._proc = None


# ── streamable HTTP ──────────────────────────────────────────────────────

class HttpTransport:
    is_alive = True

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        token_provider: Callable[[], str | None] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self.token_provider = token_provider
        self._client = client
        self._owns_client = client is None
        self._session_id: str | None = None
        self._protocol_version: str | None = None

    def set_protocol_version(self, version: str) -> None:
        self._protocol_version = version

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=None, follow_redirects=False)

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json, text/event-stream", **self.headers}
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        if self._protocol_version:
            h["MCP-Protocol-Version"] = self._protocol_version
        token = self.token_provider() if self.token_provider else None
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async def _post(self, payload: dict, want_id: Any | None, timeout: float) -> dict | None:
        assert self._client is not None
        async with self._client.stream("POST", self.url, json=payload, headers=self._headers(), timeout=timeout) as resp:
            if resp.status_code == 401:
                raise McpAuthRequired("authorization required", resp.headers.get("www-authenticate", ""))
            if resp.status_code >= 400:
                body = (await resp.aread())[:300].decode("utf-8", "replace")
                raise McpError(f"HTTP {resp.status_code} from MCP server: {body}")
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self._session_id = sid
            if want_id is None:  # уведомление: ответ не нужен (обычно 202)
                await resp.aread()
                return None
            ctype = resp.headers.get("content-type", "").lower()
            if "text/event-stream" in ctype:
                return await self._read_sse(resp, want_id)
            raw = await resp.aread()
            try:
                data = json.loads(raw)
            except ValueError as exc:
                raise McpError("MCP server returned a non-JSON response") from exc
            for msg in data if isinstance(data, list) else [data]:
                if isinstance(msg, dict) and msg.get("id") == want_id and ("result" in msg or "error" in msg):
                    return msg
            raise McpError("MCP server response has no matching id")

    @staticmethod
    async def _read_sse(resp: httpx.Response, want_id: Any) -> dict:
        data_lines: list[str] = []

        def _match() -> dict | None:
            if not data_lines:
                return None
            try:
                msg = json.loads("\n".join(data_lines))
            except ValueError:
                return None
            if isinstance(msg, dict) and msg.get("id") == want_id and ("result" in msg or "error" in msg):
                return msg
            return None

        async for line in resp.aiter_lines():
            if line == "":
                found = _match()
                data_lines.clear()
                if found is not None:
                    return found
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        found = _match()
        if found is not None:
            return found
        raise McpError("SSE stream ended without a response")

    async def request(self, payload: dict, timeout: float) -> dict:
        try:
            result = await asyncio.wait_for(self._post(payload, payload["id"], timeout), timeout + 1)
        except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
            raise McpTimeout(f"no response to {payload.get('method')} in {timeout:.0f}s") from exc
        except httpx.HTTPError as exc:
            raise McpError(f"network error: {exc}") from exc
        assert result is not None
        return result

    async def notify(self, payload: dict) -> None:
        try:
            await self._post(payload, None, 30)
        except httpx.HTTPError as exc:
            raise McpError(f"network error: {exc}") from exc

    async def close(self) -> None:
        client = self._client
        if client is None:
            return
        if self._session_id:
            try:
                await client.delete(self.url, headers=self._headers(), timeout=5)
            except Exception:  # noqa: BLE001
                pass
        if self._owns_client:
            await client.aclose()
        self._client = None


# ── клиент ───────────────────────────────────────────────────────────────

class McpClient:
    def __init__(self, transport: StdioTransport | HttpTransport, client_name: str = "aria", client_version: str = "1.0") -> None:
        self.transport = transport
        self.client_name = client_name
        self.client_version = client_version
        self.server_info: dict = {}
        self.protocol_version: str | None = None
        self.capabilities: dict = {}
        self._ids = itertools.count(1)

    @property
    def is_alive(self) -> bool:
        return bool(self.transport.is_alive)

    async def connect(self, timeout: float = 30) -> None:
        await self.transport.start()
        try:
            result = await self._rpc(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": self.client_name, "version": self.client_version},
                },
                timeout,
            )
            self.server_info = result.get("serverInfo") or {}
            self.protocol_version = result.get("protocolVersion") or PROTOCOL_VERSION
            self.capabilities = result.get("capabilities") or {}
            self.transport.set_protocol_version(self.protocol_version)
            await self.transport.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except BaseException:
            await self.transport.close()
            raise

    async def _rpc(self, method: str, params: dict | None, timeout: float) -> dict:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": next(self._ids), "method": method}
        if params is not None:
            payload["params"] = params
        resp = await self.transport.request(payload, timeout)
        if "error" in resp:
            err = resp["error"] if isinstance(resp["error"], dict) else {}
            raise McpError(f"{method}: [{err.get('code')}] {err.get('message') or resp['error']}")
        result = resp.get("result")
        return result if isinstance(result, dict) else {}

    async def list_tools(self, timeout: float = 30) -> list[dict]:
        tools: list[dict] = []
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            result = await self._rpc("tools/list", {"cursor": cursor} if cursor else {}, timeout)
            tools.extend(t for t in result.get("tools") or [] if isinstance(t, dict) and t.get("name"))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict | None = None, timeout: float = 60) -> dict:
        return await self._rpc("tools/call", {"name": name, "arguments": arguments or {}}, timeout)

    async def close(self) -> None:
        await self.transport.close()

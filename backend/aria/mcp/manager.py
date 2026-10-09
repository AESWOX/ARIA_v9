"""mcp/manager.py — серверы MCP, политика default-deny и мост в реестр тулов (H7).

Политика безопасности:
  * Тул стороннего сервера по умолчанию считается ПИШУЩИМ (risk=high, requires_approval):
    без подтверждения владельца сервер НЕ вызывается вообще. Аннотациям сервера
    (``readOnlyHint``) по умолчанию не верим — их пишет автор сервера.
  * Тул становится «чтением» (вызывается без Approve) только если владелец сам внёс его в
    ``read_tools`` сервера, либо явно включил ``trust_annotations`` и сервер пометил тул ``readOnlyHint``.
  * Подтверждение приходит ТОЛЬКО системным kwarg ``approved`` (как у shell_execute);
    ``approved`` в аргументах от модели — просто данные и ничего не разрешает.
  * Тулы MCP видят только роли из ``MCP_ROLES``; результат для модели — недоверенные данные
    (оборачивает ``loop._render_tool_result``).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from typing import Any

from aria import paths
from aria.db.enums import IdempotencyClass, RiskLevel
from aria.mcp import oauth
from aria.mcp.client import HttpTransport, McpAuthRequired, McpClient, McpError, StdioTransport
from aria.tools.registry import TOOL_REGISTRY, ToolSpec

logger = logging.getLogger("local_agent.mcp")

MAX_TEXT = 20000
MAX_TOOL_NAME = 64
CONNECT_TIMEOUT = 45.0
MCP_ROLES: tuple[str, ...] = ("general", "orchestrator")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_CONN_FIELDS = ("url", "command", "args", "env", "headers")


# ── конфигурация ─────────────────────────────────────────────────────────

def servers_path():
    return paths.data_dir() / "mcp_servers.json"


def load_servers() -> list[dict]:
    path = servers_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning("mcp_servers.json is unreadable; treating as empty")
        return []
    return [s for s in data if isinstance(s, dict) and s.get("name")] if isinstance(data, list) else []


def save_servers(servers: list[dict]) -> None:
    path = servers_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(servers, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _str_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, (str, int, float)) for v in value):
        raise ValueError(f"'{field}' must be a list of strings")
    return [str(v) for v in value]


def _str_map(value: Any, field: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"'{field}' must be an object")
    return {str(k): str(v) for k, v in value.items()}


def validate_server(payload: dict) -> dict:
    """Нормализовать вход POST /mcp/servers. ``ValueError`` — ошибка пользователя (HTTP 400)."""
    if not isinstance(payload, dict):
        raise ValueError("body must be an object")
    name = str(payload.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError("name must be 1-40 chars: letters, digits, '_' or '-'")
    url = str(payload.get("url") or "").strip()
    command = str(payload.get("command") or "").strip()
    if bool(url) == bool(command):
        raise ValueError("exactly one of 'url' (streamable HTTP) or 'command' (stdio) is required")
    if url and not re.match(r"^https?://\S+$", url):
        raise ValueError("url must start with http:// or https://")
    return {
        "name": name,
        "transport": "http" if url else "stdio",
        "url": url or None,
        "command": command or None,
        "args": _str_list(payload.get("args"), "args"),
        "env": _str_map(payload.get("env"), "env"),
        "headers": _str_map(payload.get("headers"), "headers"),
        "read_tools": _str_list(payload.get("read_tools"), "read_tools"),
        "trust_annotations": bool(payload.get("trust_annotations", False)),
        "enabled": bool(payload.get("enabled", True)),
        "auth": "oauth" if str(payload.get("auth") or "").strip().lower() == "oauth" else None,
    }


def make_tool_name(server: str, tool: str) -> str:
    """``mcp__<server>__<tool>``: допустимые символы, ≤64 (лимит провайдеров), уникально при усечении."""
    clean = lambda s: re.sub(r"[^A-Za-z0-9_-]", "_", s)  # noqa: E731
    base = f"mcp__{clean(server)}__{clean(tool)}"
    if len(base) <= MAX_TOOL_NAME:
        return base
    digest = hashlib.sha1(f"{server}\0{tool}".encode("utf-8")).hexdigest()[:8]
    return f"{base[: MAX_TOOL_NAME - 9]}_{digest}"


def _fingerprint(cfg: dict) -> str:
    return json.dumps({k: cfg.get(k) for k in _CONN_FIELDS}, sort_keys=True, default=str)


def _mask(mapping: dict | None) -> dict[str, str]:
    return {str(k): "***" for k in (mapping or {})}


def _normalize_result(result: dict) -> dict:
    """Ответ ``tools/call`` → компактный словарь для модели (текст ≤ MAX_TEXT)."""
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            parts.append(str(item.get("text", "")))
        else:
            parts.append(f"[{item.get('type', 'unknown')} content omitted]")
    text = "\n".join(parts)
    truncated = len(text) > MAX_TEXT
    out: dict[str, Any] = {"text": text[:MAX_TEXT], "is_error": bool(result.get("isError")), "truncated": truncated}
    if isinstance(result.get("structuredContent"), (dict, list)):
        out["structured"] = result["structuredContent"]
    return out


def _clean_schema(schema: Any) -> dict:
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return {"type": "object", "properties": {}}
    return {k: v for k, v in schema.items() if k != "$schema"}


# ── менеджер ─────────────────────────────────────────────────────────────

class McpManager:
    def __init__(self) -> None:
        self._clients: dict[str, McpClient] = {}
        self._fingerprints: dict[str, str] = {}
        self._tools: dict[str, list[dict]] = {}       # server -> детали тулов (после классификации)
        self._registered: dict[str, list[str]] = {}   # server -> имена в TOOL_REGISTRY
        self._errors: dict[str, str] = {}
        self._origins: dict[str, tuple[str, str]] = {}  # registry_name -> (server, tool)
        self._challenges: dict[str, str] = {}          # server -> WWW-Authenticate последнего 401
        self._lock = asyncio.Lock()

    # -- конфиг -----------------------------------------------------------
    def get_config(self, name: str) -> dict | None:
        return next((s for s in load_servers() if s.get("name") == name), None)

    def view(self) -> list[dict]:
        out = []
        for cfg in load_servers():
            name = cfg["name"]
            client = self._clients.get(name)
            connected = bool(client and client.is_alive)
            out.append({
                "name": name,
                "transport": cfg.get("transport") or ("http" if cfg.get("url") else "stdio"),
                "url": cfg.get("url"),
                "command": cfg.get("command"),
                "args": list(cfg.get("args") or []),
                "env": _mask(cfg.get("env")),
                "headers": _mask(cfg.get("headers")),
                "read_tools": list(cfg.get("read_tools") or []),
                "trust_annotations": bool(cfg.get("trust_annotations")),
                "enabled": bool(cfg.get("enabled", True)),
                "auth": self._auth_label(cfg),
                "oauth": oauth.status(name) if cfg.get("url") else None,
                "connected": connected,
                "tools": [t["name"] for t in self._tools.get(name, [])] if connected else None,
                "error": self._errors.get(name),
            })
        return out

    @staticmethod
    def _auth_label(cfg: dict) -> str | None:
        if not cfg.get("url"):
            return None
        if cfg.get("auth") == "oauth" or oauth.status(cfg["name"]) != "none":
            return "oauth"
        return None

    def challenge(self, name: str) -> str:
        return self._challenges.get(name, "")

    # -- соединения -------------------------------------------------------
    @staticmethod
    def _build_client(cfg: dict) -> McpClient:
        if cfg.get("url"):
            name = cfg["name"]
            return McpClient(HttpTransport(
                cfg["url"], headers=cfg.get("headers") or {}, token_provider=lambda: oauth.access_token(name),
            ))
        return McpClient(StdioTransport(cfg["command"], cfg.get("args") or [], cfg.get("env") or {}))

    async def _ensure_client(self, cfg: dict) -> McpClient:
        name = cfg["name"]
        client = self._clients.get(name)
        if client is not None and client.is_alive and self._fingerprints.get(name) == _fingerprint(cfg):
            return client
        await self._drop_client_only(name)  # тулы остаются в реестре: переподключение не должно ломать идущую задачу
        if cfg.get("url"):
            await oauth.ensure_fresh(name)
        client = self._build_client(cfg)
        await client.connect(CONNECT_TIMEOUT)
        self._clients[name] = client
        self._fingerprints[name] = _fingerprint(cfg)
        return client

    async def _drop(self, name: str) -> None:
        self._unregister(name)
        self._tools.pop(name, None)
        self._fingerprints.pop(name, None)
        client = self._clients.pop(name, None)
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                logger.warning("mcp '%s': close failed", name, exc_info=True)

    async def _drop_client_only(self, name: str) -> None:
        """Закрыть соединение, оставив зарегистрированные тулы (повторное подключение после refresh)."""
        self._fingerprints.pop(name, None)
        client = self._clients.pop(name, None)
        if client is not None:
            try:
                await client.close()
            except Exception:  # noqa: BLE001
                logger.warning("mcp '%s': close failed", name, exc_info=True)

    async def disconnect(self, name: str) -> None:
        async with self._lock:
            await self._drop(name)
            self._errors.pop(name, None)

    async def close_all(self) -> None:
        async with self._lock:
            for name in list(self._clients):
                await self._drop(name)
            for name in list(self._registered):
                self._unregister(name)

    # -- реестр тулов -----------------------------------------------------
    def _unregister(self, server: str) -> None:
        for reg_name in self._registered.pop(server, []):
            TOOL_REGISTRY.pop(reg_name, None)
            self._origins.pop(reg_name, None)

    @staticmethod
    def _is_read_only(cfg: dict, tool: dict) -> bool:
        if tool["name"] in (cfg.get("read_tools") or []):
            return True
        hints = tool.get("annotations") if isinstance(tool.get("annotations"), dict) else {}
        return bool(cfg.get("trust_annotations")) and hints.get("readOnlyHint") is True

    def _register(self, cfg: dict, raw_tools: list[dict]) -> list[dict]:
        server = cfg["name"]
        self._unregister(server)
        details: list[dict] = []
        taken: dict[str, str] = {}
        for tool in raw_tools:
            tname = str(tool["name"])
            reg_name = make_tool_name(server, tname)
            if reg_name in taken or (reg_name in TOOL_REGISTRY and reg_name not in self._registered.get(server, [])):
                digest = hashlib.sha1(f"{server}\0{tname}".encode("utf-8")).hexdigest()[:8]
                reg_name = f"{reg_name[: MAX_TOOL_NAME - 9]}_{digest}"
            taken[reg_name] = tname
            self._origins[reg_name] = (server, tname)
            read_only = self._is_read_only(cfg, tool)
            desc = str(tool.get("description") or tname)[:500]
            TOOL_REGISTRY[reg_name] = ToolSpec(
                tool_name=reg_name,
                description=f"[MCP:{server}] {desc}",
                input_schema=_clean_schema(tool.get("inputSchema")),
                output_schema={"type": "object", "properties": {"text": {"type": "string"}, "is_error": {"type": "boolean"}}},
                timeout_sec=60,
                risk_level=RiskLevel.low if read_only else RiskLevel.high,
                null_output_allowed=True,
                requires_approval=not read_only,
                allowed_roles=MCP_ROLES,
                idempotency_class=IdempotencyClass.safe_read if read_only else IdempotencyClass.external_side_effect,
                handler=self._make_handler(server, tname, read_only),
            )
            details.append({"name": tname, "registry_name": reg_name, "read_only": read_only, "description": desc})
        self._registered[server] = [d["registry_name"] for d in details]
        self._tools[server] = details
        return details

    def _make_handler(self, server: str, tool: str, read_only: bool):
        async def handler(input_json: dict, timeout_sec: int = 60, sandbox_root: str = "", **ctx: Any) -> dict:
            # Подтверждение — только системный kwarg; "approved" внутри input_json — данные модели.
            if not read_only and not ctx.get("approved"):
                return {
                    "status": "approval_required", "server": server, "tool": tool,
                    "error": f"MCP tool '{tool}' of server '{server}' may change data: owner approval is required, nothing was called",
                }
            return await self.call(server, tool, input_json or {}, timeout=max(5.0, float(timeout_sec) - 2.0))

        return handler

    # -- операции ---------------------------------------------------------
    async def refresh(self, name: str) -> list[dict]:
        """Подключиться (если нужно), перечитать список тулов и зарегистрировать их."""
        async with self._lock:
            return await self._refresh_locked(name)

    async def _refresh_locked(self, name: str) -> list[dict]:
        cfg = self.get_config(name)
        if cfg is None:
            raise McpError(f"MCP server '{name}' not found")
        if not cfg.get("enabled", True):
            raise McpError(f"MCP server '{name}' is disabled")
        try:
            client = await self._ensure_client(cfg)
            raw = await client.list_tools()
        except BaseException as exc:
            await self._drop(name)
            if isinstance(exc, McpAuthRequired):
                self._challenges[name] = exc.www_authenticate
                self._errors[name] = "authorization required: open the MCP tab and press Authorize"
            elif isinstance(exc, Exception):
                self._errors[name] = str(exc)
            raise
        self._errors.pop(name, None)
        return self._register(cfg, raw)

    async def ensure_loaded(self) -> None:
        """Перед задачей: синхронизировать соединения с конфигом. Сбой одного сервера не мешает остальным."""
        async with self._lock:
            configs = {c["name"]: c for c in load_servers()}
            for name in list(self._clients):
                cfg = configs.get(name)
                if cfg is None or not cfg.get("enabled", True):
                    await self._drop(name)
            for name, cfg in configs.items():
                if not cfg.get("enabled", True):
                    continue
                client = self._clients.get(name)
                if client is not None and client.is_alive and self._fingerprints.get(name) == _fingerprint(cfg) and name in self._registered:
                    continue
                try:
                    await self._refresh_locked(name)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("mcp '%s' unavailable: %s", name, exc)

    def origin(self, registry_name: str) -> tuple[str, str] | None:
        """(сервер, исходное имя тула) по имени в реестре."""
        return self._origins.get(registry_name)

    def tool_names_for_role(self, role_id: str) -> tuple[str, ...]:
        if role_id not in MCP_ROLES:
            return ()
        return tuple(n for names in self._registered.values() for n in names)

    async def call(self, server: str, tool: str, arguments: dict, timeout: float = 60) -> dict:
        cfg = self.get_config(server)
        if cfg is None:
            raise McpError(f"MCP server '{server}' not found")
        if not cfg.get("enabled", True):
            raise McpError(f"MCP server '{server}' is disabled")
        async with self._lock:
            client = await self._ensure_client(cfg)
        try:
            try:
                result = await client.call_tool(tool, arguments, timeout=timeout)
            except McpAuthRequired as exc:
                # Токен мог истечь между подключением и вызовом: один раз обновляем и повторяем.
                self._challenges[server] = exc.www_authenticate
                if not (cfg.get("url") and await oauth.refresh(server)):
                    raise McpError("authorization required: open the MCP tab and press Authorize") from exc
                async with self._lock:
                    await self._drop_client_only(server)
                    client = await self._ensure_client(cfg)
                result = await client.call_tool(tool, arguments, timeout=timeout)
        except McpError as exc:
            self._errors[server] = str(exc)
            raise
        return _normalize_result(result)

    async def test(self, name: str) -> dict:
        """Проверка соединения. Включённый сервер остаётся подключённым, выключенный — проверяется и закрывается."""
        cfg = self.get_config(name)
        if cfg is None:
            return {"ok": False, "error": "server not found", "tools": []}
        try:
            if cfg.get("enabled", True):
                details = await self.refresh(name)
                info = self._clients[name].server_info if name in self._clients else {}
                return {"ok": True, "tools": [d["name"] for d in details], "server": info}
            client = self._build_client(cfg)
            await client.connect(CONNECT_TIMEOUT)
            try:
                tools = await client.list_tools()
                return {"ok": True, "tools": [t["name"] for t in tools], "server": client.server_info}
            finally:
                await client.close()
        except McpAuthRequired as exc:
            self._challenges[name] = exc.www_authenticate
            return {"ok": False, "error": "authorization required: press Authorize", "auth_required": True, "tools": []}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc) or exc.__class__.__name__, "tools": []}


_manager: McpManager | None = None


def get_manager() -> McpManager:
    global _manager
    if _manager is None:
        _manager = McpManager()
    return _manager

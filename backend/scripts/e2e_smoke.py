#!/usr/bin/env python3
"""e2e_smoke.py — сквозной смоук ЗАПУЩЕННОГО ARIA по HTTP, без ручных действий.

Только стандартная библиотека: работает любым Python и против установленного приложения.

  python e2e_smoke.py --base http://127.0.0.1:8765 --llm require --agent --ctx

Токен берётся с главной страницы приложения (<meta name="runtime-token">), как это делает UI;
для сборки без фронтенда его можно передать --token или переменной ARIA_E2E_TOKEN.

Стадии (каждая PASS / FAIL / SKIP, код выхода = число FAIL):
  infra        health/status, токен обязателен, self-test
  surface      все основные GET-маршруты отвечают, каталог MCP содержит Upwork
  mcp          локальный фальшивый MCP-сервер: подключение, тулы, default-deny
  plugin       импорт Agent Plugin из временной папки: preview, import, 409, delete
  memory       запись и поиск в долговременной памяти
  chat         живая модель: ответ на запрос, вспоминание факта из памяти (H3)
  agent_shell  агент просит команду -> карточка Approve -> одобрение -> команда выполнена (A22)
  agent_mcp    агент вызывает пишущий тул MCP -> до Approve сервер НЕ тронут -> после Approve вызван (0008)
  ctx          длинный диалог -> появляется сводка сжатия, чат отвечает дальше (0010);
               приложение должно быть запущено с низким COMPRESSION_TOKEN_THRESHOLD (напр. 3000)
Всё созданное стадиями удаляется в конце.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LLM_TIMEOUT = 180


class Skip(Exception):
    pass


# ── HTTP ────────────────────────────────────────────────────────────────

class Api:
    def __init__(self, base: str, token: str | None):
        self.base = base.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, body=None, *, token: bool | str = True, timeout: float = 60):
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if token is True and self.token:
            headers["X-Local-Agent-Token"] = self.token
        elif isinstance(token, str):
            headers["X-Local-Agent-Token"] = token
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            status = exc.code
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw

    def ok(self, method: str, path: str, body=None, *, expect=(200,), **kw):
        status, data = self.call(method, path, body, **kw)
        if status not in expect:
            raise AssertionError(f"{method} {path} -> {status}: {str(data)[:1500]}")
        return data


def discover_token(base: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    env = os.environ.get("ARIA_E2E_TOKEN", "").strip()
    if env:
        return env
    with urllib.request.urlopen(base.rstrip("/") + "/", timeout=20) as resp:
        html = resp.read().decode("utf-8", errors="replace")
    m = re.search(r'<meta name="runtime-token" content="([^"]+)"', html)
    if not m:
        raise RuntimeError("runtime token not found on the index page (frontend not built?) — pass --token")
    return m.group(1)


# ── фальшивый MCP-сервер (streamable HTTP) ──────────────────────────────

class FakeMcp:
    TOOLS = [
        {"name": "echo", "description": "Echo text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
        {"name": "write_note", "description": "Write a note (changes data)", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    ]

    def __init__(self):
        self.calls: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # тишина
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)) or b"{}")
                method = body.get("method")
                if "id" not in body:
                    self.send_response(202)
                    self.end_headers()
                    return
                if method == "initialize":
                    result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "e2e-fake", "version": "1"}}
                elif method == "tools/list":
                    result = {"tools": outer.TOOLS}
                elif method == "tools/call":
                    outer.calls.append(body["params"])
                    result = {"content": [{"type": "text", "text": f"called {body['params']['name']}"}]}
                else:
                    result = {}
                payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                if method == "initialize":
                    self.send_header("Mcp-Session-Id", "e2e-session")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self.send_response(405)
                self.end_headers()

            def do_DELETE(self):
                self.send_response(200)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# ── контекст и реестр стадий ────────────────────────────────────────────

class Ctx:
    def __init__(self, api: Api, args):
        self.api = api
        self.args = args
        self.cleanups: list = []
        self.mcp: FakeMcp | None = None
        self.mcp_name = "e2e-smoke"
        self.llm_ready = False

    def new_chat_session(self) -> str:
        sid = self.api.ok("POST", "/chat/sessions", {})["session_id"]
        self.cleanups.append(lambda s=sid: self.api.call("DELETE", f"/sessions/{s}"))
        return sid

    def new_task_session(self, title: str) -> str:
        # как UI: сессия без задачи; задачу (с корневой ролью из профиля) создаёт первое сообщение
        sid = self.api.ok("POST", "/chat/sessions", {"title": title})["session_id"]
        self.cleanups.append(lambda s=sid: self.api.call("DELETE", f"/sessions/{s}"))
        return sid


STAGES: list[tuple[str, bool, callable]] = []


def stage(name: str, needs_llm: bool = False):
    def deco(fn):
        STAGES.append((name, needs_llm, fn))
        return fn
    return deco


def _hint(code: str | None) -> str:
    """Подсказка владельцу: часть падений — это счёт/квота провайдера, а не баг приложения."""
    if code == "provider_http_404":
        return "  [the provider has no such model: fix GEMINI_*_MODEL in Env, restart; not an app bug]"
    if code in ("provider_http_402", "provider_http_403"):
        return "  [provider account problem: no balance/access at that provider; top it up or remove its key, not an app bug]"
    if code == "provider_http_429":
        return "  [provider quota/rate limit reached; wait or add keys, not an app bug]"
    return ""


def wait_for(fn, timeout: float, what: str, every: float = 1.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"timeout ({timeout:.0f}s) waiting for {what}")


def messages(ctx: Ctx, sid: str) -> list[dict]:
    return ctx.api.ok("GET", f"/sessions/{sid}/messages")["messages"]


def send_chat(ctx: Ctx, sid: str, text: str) -> str:
    try:
        data = ctx.api.ok("POST", f"/chat/sessions/{sid}/send", {"content": text}, timeout=LLM_TIMEOUT)
    except AssertionError as exc:
        if " 502:" in str(exc) and ("account" in str(exc) or "HTTP 402" in str(exc)):
            raise AssertionError(f"{exc}{_hint('provider_http_402')}") from None
        raise
    # ответ ассистента: берём из ответа, при другой форме — из истории
    for key in ("assistant", "message", "reply"):
        v = data.get(key) if isinstance(data, dict) else None
        if isinstance(v, dict) and v.get("content"):
            return str(v["content"])
        if isinstance(v, str) and v:
            return v
    msgs = [m for m in messages(ctx, sid) if m.get("role") == "assistant"]
    if not msgs:
        raise AssertionError(f"no assistant message after send: {str(data)[:200]}")
    return str(msgs[-1].get("content") or "")


# ── стадии ──────────────────────────────────────────────────────────────

@stage("infra")
def s_infra(ctx: Ctx):
    api = ctx.api
    for p in ("/health", "/status"):
        status, _ = api.call("GET", p, token=False)
        assert status == 200, f"{p} -> {status}"
    for tok in (False, "definitely-wrong-token"):
        status, _ = api.call("GET", "/mcp/servers", token=tok)
        assert status in (401, 403), f"/mcp/servers without a valid token -> {status}"
    st = api.ok("GET", "/system/self-test")
    assert st.get("status") != "error", f"self-test error: {st.get('issues')}"
    for issue in st.get("issues") or []:  # «degraded» — конфигурация владельца, не поломка сборки: показываем, не валим
        print(f"      WARN self-test: {issue}")


@stage("surface")
def s_surface(ctx: Ctx):
    api = ctx.api
    for p in ("/chat/status", "/chat/models", "/chat/profile", "/memory", "/ops/hooks", "/ops/checkpoints",
              "/plugins", "/skills", "/tasks/runner/status", "/attention-items", "/sessions", "/mcp/servers"):
        api.ok("GET", p)
    cat = api.ok("GET", "/mcp/catalog")
    names = {e["name"]: e for e in cat["entries"]}
    assert "upwork" in names, f"catalog has no Upwork entry: {sorted(names)}"
    status = api.ok("GET", "/chat/status")
    ctx.llm_ready = bool(status.get("configured"))
    print(f"      providers: {', '.join(status.get('providers') or []) or 'none'}")
    # настроенные модели (не секрет): сразу видно протухшее имя вроде GEMINI_PRO_MODEL
    for m in api.ok("GET", "/chat/models").get("models", []):
        print(f"      model  {m['tier']:<20} {m['id']:<28} -> {m['model']}")
    if ctx.args.llm == "require" and not ctx.llm_ready:
        raise AssertionError(f"no real model provider configured: {status.get('hint')}")


@stage("mcp")
def s_mcp(ctx: Ctx):
    api = ctx.api
    ctx.mcp = FakeMcp()
    ctx.cleanups.append(lambda: (api.call("DELETE", f"/mcp/servers/{ctx.mcp_name}"), ctx.mcp.stop()))
    api.call("DELETE", f"/mcp/servers/{ctx.mcp_name}")  # хвост прошлого запуска
    api.ok("POST", "/mcp/servers", {"name": ctx.mcp_name, "url": ctx.mcp.url, "enabled": True, "read_tools": ["echo"]})
    res = api.ok("POST", f"/mcp/servers/{ctx.mcp_name}/test", {}, timeout=60)
    assert res["ok"], f"test failed: {res}"
    assert sorted(res["tools"]) == ["echo", "write_note"], res
    entry = next(s for s in api.ok("GET", "/mcp/servers")["servers"] if s["name"] == ctx.mcp_name)
    assert entry["connected"] is True and entry["enabled"] is True, entry
    assert api.call("POST", f"/mcp/servers/{ctx.mcp_name}/test", {}, token=False)[0] in (401, 403)
    assert not ctx.mcp.calls, "tools/call must not happen during a connection test"


@stage("plugin")
def s_plugin(ctx: Ctx):
    api = ctx.api
    name = f"e2e{uuid.uuid4().hex[:6]}"
    root = Path(tempfile.mkdtemp(prefix="aria-e2e-plugin-"))
    (root / "skills" / "hello").mkdir(parents=True)
    (root / "skills" / "hello" / "SKILL.md").write_text("---\nname: hello\ndescription: e2e hello skill\n---\n# hello\n", encoding="utf-8")
    (root / "plugin.json").write_text(json.dumps({"name": name, "version": "0.0.1", "license": "MIT"}), encoding="utf-8")
    (root / "mcp.json").write_text(json.dumps({"mcpServers": {"api": {"url": "https://127.0.0.1:9/mcp"}}}), encoding="utf-8")
    src = {"path": str(root)}
    ctx.cleanups.append(lambda: api.call("DELETE", f"/plugins/{name}"))
    pv = api.ok("POST", "/plugins/preview", {"source": src})
    assert pv["name"] == name and pv["skills"][0]["name"] == f"{name}-hello" and not pv["already_installed"], pv
    api.ok("POST", "/plugins/import", {"source": src})
    skills = api.ok("GET", "/skills")
    flat = json.dumps(skills)
    assert f"{name}-hello" in flat, "imported skill is not listed"
    srv = next(s for s in api.ok("GET", "/mcp/servers")["servers"] if s["name"] == f"{name}-api")
    assert srv["enabled"] is False, "plugin MCP server must be imported disabled"
    status, _ = api.call("POST", "/plugins/import", {"source": src})
    assert status == 409, f"second import must conflict, got {status}"
    api.ok("DELETE", f"/plugins/{name}")
    assert f"{name}-api" not in json.dumps(api.ok("GET", "/mcp/servers")), "plugin server must be removed with the plugin"
    assert not any(p["name"] == name for p in api.ok("GET", "/plugins")["plugins"])


@stage("memory")
def s_memory(ctx: Ctx):
    api = ctx.api
    marker = f"zebra-quasar-{uuid.uuid4().hex[:6]}"
    status, item = api.call("POST", "/memory/items", {"layer": "fact", "content": f"e2e smoke marker {marker}"})
    if status == 409:
        raise Skip(f"memory is disabled: {item}")
    assert status == 200, f"{status}: {item}"
    item_id = item["item"]["id"]
    ctx.cleanups.append(lambda: api.call("DELETE", f"/memory/items/{item_id}"))
    found = api.ok("GET", f"/memory/search?q={marker}")
    assert any(marker in json.dumps(i) for i in found["items"]), "stored item is not found by search"
    api.ok("DELETE", f"/memory/items/{item_id}")


@stage("chat", needs_llm=True)
def s_chat(ctx: Ctx):
    api = ctx.api
    sid = ctx.new_chat_session()
    reply = send_chat(ctx, sid, "Reply with exactly the single word PONG and nothing else.")
    assert "pong" in reply.lower(), f"unexpected reply: {reply[:200]}"
    # H3: факт из долговременной памяти попадает в ответ
    secret = f"vermilion-{uuid.uuid4().hex[:4]}"
    status, item = api.call("POST", "/memory/items", {"layer": "fact", "content": f"The user's favourite smoke-test colour is {secret}."})
    if status == 200:
        item_id = item["item"]["id"]
        ctx.cleanups.append(lambda: api.call("DELETE", f"/memory/items/{item_id}"))
        sid2 = ctx.new_chat_session()
        reply2 = send_chat(ctx, sid2, "What is my favourite smoke-test colour? Answer with just the colour word.")
        assert secret.split("-")[0] in reply2.lower(), f"memory recall failed: {reply2[:200]}"
    n = len(messages(ctx, sid))
    assert n >= 2, f"history has {n} messages"


def _pending(ctx: Ctx, sid: str) -> dict:
    return ctx.api.ok("GET", f"/sessions/{sid}/run")


MAX_CLARIFY_ROUNDS_SMOKE = 4


def _answer_clarifications(ctx: Ctx, queued: dict) -> dict:
    """Кларифайер спрашивает всегда: задача встаёт в awaiting_clarification с queued=False.
    Отвечаем рекомендованными значениями (default_value); вопросы без рекомендации закрываем допущениями.
    POST /tasks/{id}/clarify с mode=agent сам ставит задачу в очередь, когда контракт валиден,
    поэтому отдельный /start не нужен (он вернул бы duplicate)."""
    api = ctx.api
    task_id = queued.get("task_id")
    assert task_id, f"awaiting_clarification without task_id: {queued}"
    questions = queued.get("questions") or []
    res: dict = {}
    for _ in range(MAX_CLARIFY_ROUNDS_SMOKE):
        answers = [
            {"id": q["id"], "value": q["default_value"]}
            for q in questions
            if isinstance(q, dict) and q.get("id") and str(q.get("default_value") or "").strip()
        ]
        body = {"answers": answers, "mode": "agent",
                "accept_assumptions": len(answers) < len(questions) or not answers}
        res = api.ok("POST", f"/tasks/{task_id}/clarify", body, timeout=60)
        if res.get("valid"):
            break
        questions = res.get("questions") or []
    else:
        raise AssertionError(f"clarification did not converge in {MAX_CLARIFY_ROUNDS_SMOKE} rounds: {res}")
    return res


def _run_agent(ctx: Ctx, title: str, prompt: str, expect_type: str, profile: dict | None = None):
    api = ctx.api
    sid = ctx.new_task_session(title)
    if profile:
        # профиль живёт на уровне чат-сессии: PUT /chat/sessions/{id}/profile
        api.ok("PUT", f"/chat/sessions/{sid}/profile", profile)
    queued = api.ok("POST", f"/sessions/{sid}/messages", {"content": prompt, "mode": "agent"}, timeout=60)
    if queued.get("status") == "awaiting_clarification":
        queued = _answer_clarifications(ctx, queued)
    assert queued.get("queued") is True, f"agent run was not queued (A10): {queued}"
    last = {"status": None}

    def _poll():
        r = _pending(ctx, sid)
        last["status"] = r["status"]
        return r if r["attention"] or r["terminal"] else None

    try:
        run = wait_for(_poll, LLM_TIMEOUT, "an approval card", every=1.5)
    except AssertionError as exc:
        raise AssertionError(f"{exc}; task status stayed {last['status']!r} (the model did not request the tool?)") from None
    if not run["attention"]:
        why = f" error_code={run.get('error_code')!r} error_message={str(run.get('error_message'))[:300]!r}" if run.get("error_code") or run.get("error_message") else ""
        raise AssertionError(f"task ended without a card: status={run['status']}{why}{_hint(run.get('error_code'))}")
    item = run["attention"][0]
    assert item["type"] == expect_type, f"expected {expect_type}, got {item['type']}"
    return sid, item


def _finish(ctx: Ctx, sid: str, item: dict, until=None) -> dict:
    """Approve и дождаться результата. until() — признак готовности (по умолчанию: корневая задача терминальна)."""
    res = ctx.api.ok("POST", f"/attention-items/{item['id']}/approve", {}, timeout=60)
    assert res.get("resumed") is True, f"approve did not resume the task (A22): {res}"
    try:
        if until is None:
            return wait_for(lambda: (lambda r: r if r["terminal"] else None)(_pending(ctx, sid)), LLM_TIMEOUT, "the task to finish after approve")
        wait_for(until, LLM_TIMEOUT, "the approved action to finish")
    except AssertionError as exc:
        raise AssertionError(f"{exc}; {_snapshot(ctx, sid)}") from None
    return _pending(ctx, sid)


def _snapshot(ctx: Ctx, sid: str) -> str:
    """Что было в сессии в момент таймаута: статус, ошибка, ждёт ли ещё карточка, хвост переписки."""
    try:
        run = _pending(ctx, sid)
        tail = [f"{m.get('role')}: {str(m.get('content') or '')[:120]}" for m in messages(ctx, sid)[-4:]]
        return (f"task status={run['status']!r} error_code={run.get('error_code')!r} "
                f"error_message={str(run.get('error_message'))[:200]!r} pending_cards={len(run['attention'])} tail={tail}")
    except Exception as exc:  # noqa: BLE001
        return f"(snapshot failed: {exc})"


@stage("agent_shell", needs_llm=True)
def s_agent_shell(ctx: Ctx):
    # shell_execute доступен только ролям coder/devops_infra: корневая роль «general» его не видит,
    # поэтому используем стиль boss (orchestrator делегирует coder'у). `git --version` не в allowlist -> Approve.
    sid, item = _run_agent(
        ctx, "e2e shell approval",
        "Delegate this job to the coder role with the delegate_task tool: use the shell_execute tool to run "
        "exactly this command and report its output: git --version",
        "high_risk_shell",
        profile={"style": "boss", "audit": "off", "thinking": "off"},
    )
    assert "git version" not in json.dumps(messages(ctx, sid)), "command output appeared BEFORE approval"
    # boss: корневая задача завершается, пока дочерняя ждёт Approve, поэтому ждём САМ результат команды
    # Сообщение «shell_execute (approved) -> ok» появляется раньше, чем модель перескажет вывод команды,
    # поэтому ждём сам вывод (0024: раньше проверка срабатывала по первому и падала гонкой).
    done = lambda: "git version" in json.dumps(messages(ctx, sid), ensure_ascii=False)  # noqa: E731
    _finish(ctx, sid, item, until=done)
    dump = json.dumps(messages(ctx, sid), ensure_ascii=False)
    assert "git version" in dump, f"approved command did not run / result missing: {dump[-300:]}"


@stage("agent_mcp", needs_llm=True)
def s_agent_mcp(ctx: Ctx):
    if ctx.mcp is None:
        raise Skip("mcp stage did not run")
    ctx.mcp.calls.clear()
    note = f"e2e-note-{uuid.uuid4().hex[:5]}"
    tool = f"mcp__{ctx.mcp_name}__write_note"
    sid, item = _run_agent(
        ctx, "e2e mcp approval",
        f"Call the tool {tool} with the argument text=\"{note}\". Call it first, then report the result.",
        "mcp_tool_approval",
        profile={"style": "solo", "audit": "off", "thinking": "off"},  # проверяем Approve, а не аудит: короче и без лишних вызовов модели
    )
    assert not ctx.mcp.calls, "MCP server was called BEFORE owner approval (default-deny broken)"
    final = _finish(ctx, sid, item)
    assert final["status"] in ("done", "done_unaudited"), f"task ended as {final['status']}"
    calls = [c for c in ctx.mcp.calls if c.get("name") == "write_note"]
    assert len(calls) == 1 and calls[0]["arguments"].get("text") == note, f"server calls: {ctx.mcp.calls}"


@stage("ctx", needs_llm=True)
def s_ctx(ctx: Ctx):
    api = ctx.api
    sid = ctx.new_chat_session()
    secret = f"quokka-{uuid.uuid4().hex[:4]}"
    filler = ("Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor. " * 30)[:2400]
    first = f"Remember this code word for later: {secret}. {filler}"
    found = None
    for i in range(12):
        send_chat(ctx, sid, first if i == 0 else f"Note {i}: {filler}")
        msgs = messages(ctx, sid)
        found = next((m for m in msgs if str(m.get("content") or "").startswith("[compression summary")), None)
        if found:
            break
    assert found, ("no compression summary after 12 long messages — start the app with "
                   "COMPRESSION_TOKEN_THRESHOLD=3000 (or lower) for this stage")
    reply = send_chat(ctx, sid, "What was the code word I asked you to remember? Answer with just the word.")
    assert reply.strip(), "empty reply after compression"
    if secret.split("-")[0] not in reply.lower():
        print(f"      WARN: summary did not carry the code word (model-dependent): {reply[:80]!r}")


# ── запуск ──────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--token", default=None)
    ap.add_argument("--llm", choices=("auto", "require", "skip"), default="auto",
                    help="require: нет модели = FAIL; skip: не трогать модель")
    ap.add_argument("--agent", action="store_true", help="стадии agent_shell и agent_mcp")
    ap.add_argument("--ctx", action="store_true", help="стадия ctx (нужен низкий порог сжатия в приложении)")
    ap.add_argument("--no-cleanup", action="store_true", help="не удалять созданное (для разбора)")
    ap.add_argument("--only", default="", help="через запятую: имена стадий")
    ap.add_argument("--report", default=str(Path(tempfile.gettempdir()) / "aria_e2e_report.json"))
    args = ap.parse_args()

    try:
        token = discover_token(args.base, args.token)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL cannot reach the app at {args.base}: {exc}")
        return 1
    ctx = Ctx(Api(args.base, token), args)
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    results: list[dict] = []
    infra_ok = True
    print("=" * 64)
    print(f"  ARIA e2e smoke  base={args.base}  llm={args.llm}  agent={args.agent}  ctx={args.ctx}")
    print("=" * 64)
    for name, needs_llm, fn in STAGES:
        t0 = time.time()
        reason = None
        if only and name not in only:
            continue
        if not infra_ok and name != "infra":
            reason = "infra failed"
        elif needs_llm and args.llm == "skip":
            reason = "--llm skip"
        elif needs_llm and not ctx.llm_ready and name != "infra":
            reason = "no model provider configured"
        elif name in ("agent_shell", "agent_mcp") and not args.agent:
            reason = "needs --agent"
        elif name == "ctx" and not args.ctx:
            reason = "needs --ctx"
        if reason:
            status, err = "SKIP", reason
        else:
            try:
                fn(ctx)
                status, err = "PASS", None
            except Skip as exc:
                status, err = "SKIP", str(exc)
            except Exception as exc:  # noqa: BLE001
                status, err = "FAIL", f"{type(exc).__name__}: {exc}"
        if name == "infra" and status == "FAIL":
            infra_ok = False
        dur = time.time() - t0
        icon = {"PASS": "OK  ", "FAIL": "FAIL", "SKIP": "skip"}[status]
        print(f"  [{icon}] {name:<12} {dur:6.1f}s" + (f"  {err}" if err else ""))
        results.append({"stage": name, "status": status, "seconds": round(dur, 1), "error": err})
    for fn in ([] if args.no_cleanup else reversed(ctx.cleanups)):
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass
    failed = sum(1 for r in results if r["status"] == "FAIL")
    passed = sum(1 for r in results if r["status"] == "PASS")
    skipped = sum(1 for r in results if r["status"] == "SKIP")
    print("-" * 64)
    print(f"  {passed} passed, {failed} failed, {skipped} skipped")
    Path(args.report).write_text(json.dumps({"base": args.base, "results": results, "passed": passed, "failed": failed, "skipped": skipped}, indent=2), encoding="utf-8")
    print(f"  report: {args.report}")
    return failed


if __name__ == "__main__":
    sys.exit(main())

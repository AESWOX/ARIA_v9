#!/usr/bin/env python3
"""e2e_rehearsal.py — репетиция сквозного смоука БЕЗ ключей и без вашего профиля.

Поднимает изолированный бэкенд (временный ARIA_DATA_DIR, свой порт, свой токен) и ФАЛЬШИВУЮ
модель (локальный OpenAI-совместимый сервер), затем прогоняет e2e_smoke.py со всеми стадиями.
Ни реальные ключи, ни ваши данные не используются. Запуск из backend/:

  .venv\\Scripts\\python scripts\\e2e_rehearsal.py

  .venv\\Scripts\\python scripts\\e2e_rehearsal.py --exe dist\\backend.exe    # то же на собранном PyInstaller-sidecar

Код выхода = число FAIL смоука (0 — всё прошло). Логи бэкенда: <tmp>/backend.log (и data/logs/backend.log)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
SUMMARY_MARK = "Сожми диалог"


def _text(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(str(p.get("text", "")) for p in c if isinstance(p, dict))
    return str(c or "")


class FakeLlm:
    """Детерминированная «модель»: отвечает по сценариям смоука."""

    def __init__(self, debug_log: Path | None = None):
        self.debug_log = debug_log
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # проверка доступности провайдера (GET /models)
                payload = json.dumps({"object": "list", "data": [{"id": "fake-model"}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0") or 0)) or b"{}")
                if os.environ.get("ARIA_FAKE_LLM_FAIL_TOOLS") == "1" and body.get("tools"):
                    # имитация сбоя провайдера на агентских запросах (с тулами): проверка диагностики смоука
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                out = outer.answer(body)
                payload = json.dumps(out).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    @staticmethod
    def _reply(text=None, tool=None, args=None):
        msg = {"role": "assistant", "content": text}
        if tool:
            msg["tool_calls"] = [{"id": "call_1", "type": "function", "function": {"name": tool, "arguments": json.dumps(args or {})}}]
        return {"choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool else "stop"}], "usage": {"total_tokens": 1}}

    def answer(self, body: dict) -> dict:
        msgs = body.get("messages", [])
        tools = {t.get("function", {}).get("name") for t in body.get("tools", []) or []}
        texts = [_text(m) for m in msgs]
        joined = "\n".join(texts)
        if self.debug_log:
            with self.debug_log.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"roles": [m.get("role") for m in msgs], "tools": sorted(t for t in tools if t), "last": texts[-1][:300] if texts else ""}, ensure_ascii=False) + "\n")
        if SUMMARY_MARK in joined:  # запрос суммаризации
            m = re.search(r"quokka-\w+", joined)
            return self._reply(f"Summary of earlier conversation. Code word: {m.group(0) if m else 'none'}.")
        user_texts = [_text(m) for m in msgs if m.get("role") == "user"]
        task = next((t for t in reversed(user_texts) if "shell_execute tool" in t or "mcp__" in t or "delegate_task" in t), "")
        has_result = any(m.get("role") == "tool" or "returncode" in _text(m) or "called write_note" in _text(m) for m in msgs)
        if task and tools:
            if "delegate_task" in tools and "Delegate this job" in task and not has_result:
                objective = "Use the shell_execute tool to run exactly this command and report its output: " + task.split("report its output:")[-1].strip()
                return self._reply(tool="delegate_task", args={"role": "coder", "objective": objective})
            if "shell_execute tool" in task and "shell_execute" in tools and not has_result:
                cmd = task.split("report its output:")[-1].strip()
                return self._reply(tool="shell_execute", args={"command": cmd})
            m = re.search(r"(mcp__[\w-]+__write_note) with the argument text=\"([^\"]+)\"", task)
            if m and m.group(1) in tools and not has_result:
                return self._reply(tool=m.group(1), args={"text": m.group(2)})
            if has_result:
                return self._reply("Done. " + " ".join(t for t in texts if "git version" in t or "called write_note" in t)[:300])
        last_user = user_texts[-1] if user_texts else ""
        if "PONG" in last_user:
            return self._reply("PONG")
        if "favourite smoke-test colour" in last_user:
            m = re.search(r"(vermilion)-\w+", joined)
            return self._reply(m.group(1) if m else "unknown")
        if "code word I asked you to remember" in last_user:
            m = re.search(r"quokka-\w+", joined)
            return self._reply(m.group(0) if m else "unknown")
        return self._reply("OK")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exe", default=None, help="запустить собранный backend.exe вместо исходников (проверка сборки)")
    ap.add_argument("--startup-timeout", type=int, default=None)
    cli = ap.parse_args()
    startup = cli.startup_timeout or (180 if cli.exe else 90)
    tmp = Path(tempfile.mkdtemp(prefix="aria-rehearsal-"))
    fake = FakeLlm(debug_log=tmp / "llm_debug.jsonl")
    port = _free_port()
    token = "rehearsal-token"
    env = dict(os.environ)
    for k in list(env):
        if k.upper().endswith(("_API_KEY", "_API_KEYS")) or k.upper().startswith(("GROQ_", "DEEPSEEK_", "GEMINI_", "COMPRESSION_")):
            env.pop(k)
    base_url = f"http://127.0.0.1:{fake.port}/v1"
    env.update({
        "ARIA_DATA_DIR": str(tmp / "data"),
        "LOCAL_AGENT_RUNTIME_TOKEN": token,
        "LOCAL_AGENT_DISABLE_BOOTSTRAP_WRITE": "1",
        "GEMINI_API_KEYS": "fake-key-1,fake-key-2",
        "GEMINI_BASE_URL": base_url,
        "COMPRESSION_BASE_URL": base_url,
        "COMPRESSION_TOKEN_THRESHOLD": "3000",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    })
    log = (tmp / "backend.log").open("w", encoding="utf-8")
    if cli.exe:
        exe = Path(cli.exe).resolve()
        if not exe.is_file():
            print(f"exe not found: {exe}")
            return 1
        cmd, cwd = [str(exe), "--port", str(port)], str(tmp)
    else:
        cmd, cwd = [sys.executable, "run_backend.py", "--port", str(port)], str(BACKEND)
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + startup
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"backend exited early (code {proc.returncode}); log: {tmp / 'backend.log'}")
                for lp in (tmp / "backend.log", tmp / "data" / "logs" / "backend.log"):
                    if lp.exists():
                        print(lp.read_text(encoding="utf-8", errors="replace")[-1500:])
                return 1
            try:
                with urllib.request.urlopen(base + "/health", timeout=2):
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            print(f"backend did not start in {startup}s; see {tmp}")
            return 1
        rc = subprocess.call([sys.executable, str(BACKEND / "scripts" / "e2e_smoke.py"), "--base", base, "--token", token,
                              "--llm", "require", "--agent", "--ctx", "--report", str(tmp / "report.json")]
                             + (["--no-cleanup"] if os.environ.get("ARIA_KEEP_REHEARSAL") == "1" else []), env=env)
        print(f"rehearsal files: {tmp}")
        return rc
    finally:
        if os.name == "nt":  # onefile-загрузчик PyInstaller: terminate() не трогает дочерний процесс
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()
        fake.stop()
        if os.environ.get("ARIA_KEEP_REHEARSAL") != "1" and rc_ok(tmp):
            shutil.rmtree(tmp, ignore_errors=True)


def rc_ok(tmp: Path) -> bool:
    try:
        return json.loads((tmp / "report.json").read_text(encoding="utf-8"))["failed"] == 0
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    sys.exit(main())

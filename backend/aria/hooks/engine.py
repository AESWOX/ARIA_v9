"""hooks/engine.py — жизненные хуки (H4).

События: ``pre_tool``, ``post_tool``, ``task_start``, ``task_close``, ``on_approval``, ``on_error``.

Хук — команда оболочки из ``data/hooks.json`` (создаётся через ``/ops/hooks``). Правила:

* Выполняется **только** хук с ``allowed=true`` (явное одобрение при создании). Без одобрения — пропуск,
  причина попадает в ``HookDecision.skipped``.
* Контекст события — JSON на stdin и в переменной ``ARIA_HOOK_PAYLOAD``; имя события — ``ARIA_HOOK_EVENT``.
* Таймаут (по умолчанию 10 с, максимум 60 с; по таймауту процесс убивается), вывод ограничен 64 КБ,
  окружение очищено от переменных с ``KEY/TOKEN/SECRET/PASSWORD`` в имени, рабочий каталог — песочница.
* ``matcher`` (регулярное выражение) сопоставляется с именем тула для ``pre_tool``/``post_tool``.
* Блокировка тула возможна **только** в ``pre_tool``: код возврата 2 или JSON ``{"block": true, "reason": "..."}``
  в stdout. Сбой, таймаут или остальные коды возврата хук не блокируют (ошибка пишется в результат),
  а блокирует только явное решение. Для остальных событий блокировка игнорируется.
* ``fire`` никогда не бросает исключений: сбой хука не должен ронять задачу.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from aria import paths

logger = logging.getLogger("local_agent.hooks")

EVENTS = ("pre_tool", "post_tool", "task_start", "task_close", "on_approval", "on_error")
DEFAULT_TIMEOUT_SEC = 10
MAX_TIMEOUT_SEC = 60
MAX_OUTPUT_BYTES = 64 * 1024
BLOCK_EXIT_CODE = 2
_SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)


@dataclass
class HookRun:
    command: str
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    error: str | None = None
    blocked: bool = False
    reason: str | None = None


@dataclass
class HookDecision:
    event: str
    blocked: bool = False
    reason: str | None = None
    runs: list[HookRun] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)


def _hooks_file():
    return paths.data_dir() / "hooks.json"


def load_hooks() -> list[dict[str, Any]]:
    path = _hooks_file()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("hooks.json is unreadable, treating as empty")
        return []
    return data if isinstance(data, list) else []


def save_hooks(hooks: list[dict[str, Any]]) -> None:
    path = _hooks_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(hooks, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _clean_env(event: str, payload_json: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not _SECRET_NAME.search(k)}
    env["ARIA_HOOK_EVENT"] = event
    env["ARIA_HOOK_PAYLOAD"] = payload_json[:30_000]
    return env


def _timeout_of(hook: dict[str, Any]) -> int:
    try:
        value = int(hook.get("timeout") or DEFAULT_TIMEOUT_SEC)
    except (TypeError, ValueError):
        value = DEFAULT_TIMEOUT_SEC
    return max(1, min(value, MAX_TIMEOUT_SEC))


def _matches(hook: dict[str, Any], tool_name: str | None) -> bool:
    matcher = hook.get("matcher")
    if not matcher or tool_name is None:
        return True
    try:
        return re.search(str(matcher), tool_name) is not None
    except re.error:
        return False


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Убить процесс хука вместе с потомками (оболочка + её дети держат пайпы открытыми)."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


async def _run_one(hook: dict[str, Any], event: str, payload_json: str, cwd: str | None) -> HookRun:
    command = str(hook.get("command") or "")
    run = HookRun(command=command, exit_code=None)
    timeout = _timeout_of(hook)
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_clean_env(event, payload_json),
            cwd=cwd if cwd and os.path.isdir(cwd) else None,
            start_new_session=(os.name != "nt"),  # своя группа процессов → можно убить дерево целиком
        )
    except Exception as exc:  # noqa: BLE001
        run.error = f"spawn failed: {exc}"
        return run
    comm = asyncio.ensure_future(proc.communicate(payload_json.encode("utf-8")))
    done, _pending = await asyncio.wait({comm}, timeout=timeout)
    if not done:
        run.timed_out = True
        run.error = f"timeout after {timeout}s"
        _kill_tree(proc)
        try:
            await asyncio.wait_for(comm, timeout=3)
        except Exception:  # noqa: BLE001
            comm.cancel()
        return run
    try:
        out, err = comm.result()
    except Exception as exc:  # noqa: BLE001
        run.error = str(exc)
        return run
    run.exit_code = proc.returncode
    run.stdout = out[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    run.stderr = err[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return run


def _wants_block(run: HookRun) -> tuple[bool, str | None]:
    if run.exit_code == BLOCK_EXIT_CODE:
        reason = (run.stderr or run.stdout).strip()[:500] or "blocked by hook (exit 2)"
        return True, reason
    if run.exit_code == 0 and run.stdout.strip().startswith("{"):
        try:
            data = json.loads(run.stdout)
        except ValueError:
            return False, None
        if isinstance(data, dict) and data.get("block") is True:
            return True, str(data.get("reason") or "blocked by hook")[:500]
    return False, None


async def fire(
    event: str,
    payload: dict[str, Any] | None = None,
    *,
    tool_name: str | None = None,
    cwd: str | None = None,
) -> HookDecision:
    """Выполнить все подходящие хуки события. Не бросает исключений."""
    decision = HookDecision(event=event)
    try:
        if event not in EVENTS:
            return decision
        hooks = [h for h in load_hooks() if h.get("event") == event and h.get("command")]
        if not hooks:
            return decision
        body = {"event": event, "tool": tool_name, "time": datetime.now(timezone.utc).isoformat(), **(payload or {})}
        payload_json = json.dumps(body, ensure_ascii=False, default=str)
        for hook in hooks:
            if not _matches(hook, tool_name):
                continue
            if not hook.get("allowed"):
                decision.skipped.append({"command": str(hook.get("command")), "reason": "not approved"})
                continue
            run = await _run_one(hook, event, payload_json, cwd)
            if event == "pre_tool":
                run.blocked, run.reason = _wants_block(run)
                if run.blocked and not decision.blocked:
                    decision.blocked, decision.reason = True, run.reason
            if run.error:
                logger.warning("hook %r failed on %s: %s", run.command, event, run.error)
            decision.runs.append(run)
            if decision.blocked:
                break
    except Exception:  # noqa: BLE001
        logger.exception("hook engine crashed on %s", event)
    return decision

"""tools/handlers/shell.py — исполнение shell-команд под default-deny политикой.

Волна 1 (A13). Что было: ``create_subprocess_shell`` исполнял любую строку,
если она не совпала с чёрным списком regex из §14.2 — то есть политика была
«разрешено всё, что не запрещено», и обойти её можно было обычной композицией
(``ls; rm -rf /``, ``echo x | sh``, ``bash -c ...``).

Что стало: перед запуском команда классифицируется
(:func:`aria.tools.validators.classify_shell_command`). Если это не ``allow``
(не в allowlist либо совпало с high-risk-паттерном) — **команда не
запускается вообще**, handler возвращает ``approval_required``, а вызывающий
слой (core/loop.py, approvals.py) создаёт attention item и ждёт владельца.
Явный ``input_json["approved"] = True`` означает, что подтверждение получено
(так его выставляет api-слой после approve attention item).
"""
from __future__ import annotations

import asyncio
import shlex

from aria.tools.validators import classify_shell_command


async def shell_execute(input_json: dict, timeout_sec: int, cwd: str | None = None) -> dict:
    """§14.3 Emergency stop: SIGTERM -> через 3 сек SIGKILL реализован через
    asyncio subprocess + wait_for/terminate/kill каскад."""
    command = input_json["command"]

    if not input_json.get("approved"):
        decision = classify_shell_command(command)
        if decision != "allow":
            return {
                "status": "approval_required",
                "decision": decision,
                "command": command,
                "error": (
                    "shell policy (default-deny): команда не в allowlist — нужно "
                    "подтверждение владельца (§14.2), исполнение не начато"
                ),
            }

    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
        return {
            "returncode": proc.returncode,
            "stdout": stdout.decode(errors="replace")[-20000:],
            "stderr": stderr.decode(errors="replace")[-20000:],
        }
    except asyncio.TimeoutError:
        await _terminate_then_kill(proc)
        raise


async def _terminate_then_kill(proc: asyncio.subprocess.Process) -> None:
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


def validate_shell_input(input_json: dict) -> None:
    if "command" not in input_json or not isinstance(input_json["command"], str) or not input_json["command"].strip():
        raise ValueError("shell_execute требует непустой строковый input.command")
    # синтаксическая проверка, чтобы не улетать в shell с заведомо битой командой
    shlex.split(input_json["command"])

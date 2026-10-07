from __future__ import annotations

import asyncio
import shlex


async def shell_execute(input_json: dict, timeout_sec: int, cwd: str | None = None) -> dict:
    """§14.3 Emergency stop: SIGTERM -> через 3 сек SIGKILL реализован через
    asyncio subprocess + wait_for/terminate/kill каскад."""
    command = input_json["command"]
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

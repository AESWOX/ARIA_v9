"""C5: shell_execute — подтверждение владельца срабатывает на ВСЕХ вариантах SEC-2.

Три слоя (каждый — боевой путь, не заглушка политики):
  1. handler shell_execute: команда НЕ запускается, ответ approval_required; ``approved`` из аргументов модели игнорируется.
  2. core.loop._execute_tool: статус blocked_policy / approval_required (аудит не считает действие выполненным).
  3. core.loop.execute_agent_loop + resume_after_approval: задача встаёт на паузу, создаётся attention item с командой,
     процесс не порождается; после Approve команда исполняется РОВНО один раз; после Reject — не исполняется никогда.

Реальный процесс нигде не стартует: asyncio.create_subprocess_shell заменён ловушкой (запоминает команду).
Тест на «ловушка настоящая»: безопасные варианты SEC-2 (rm file.txt, rm -f, rm -i) до ловушки доходят.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import select

from aria import paths
from aria.core import approvals
from aria.core.loop import _execute_tool, execute_agent_loop, resume_after_approval
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import ApprovalStatus, TaskStatus, ToolStatus
from aria.llm.providers.stub import StubProvider, final_answer, tool_call
from aria.llm.router import ProviderRouter
from aria.tools.registry import TOOL_REGISTRY
from aria.tools.validators import classify_shell_command, is_high_risk_command

# --- Варианты SEC-2 (DOD.md): любой порядок/написание флагов rm, плюс обёртки и составные команды -------------------
SEC2_DANGEROUS = [
    # исходные из SEC-2
    "rm -rf /", "rm -fr /tmp/x", "rm -Rf x", "rm -rfv x", "rm --recursive x", "rm -r -f x", "sudo rm -rf /", "rm -r ~/docs",
    # порядок/регистр флагов
    "rm -fR x", "rm -Rfv dir", "rm -r x", "rm -f -r x", "rm --recursive --force x", "rm -dr x", "rm -rd x", "rm -rI x",
    "rm --no-preserve-root -rf /", "rm  -rf   x", "rm\t-rf x", 'rm -rf "x"', "rm -rf ./build", "rm -rf $HOME",
    # обёртки и путь к бинарнику
    "/bin/rm -rf x", "rm.exe -rf x", "env rm -rf x", "command rm -rf x", "time rm -rf x", "RM -RF x",
    # составные команды: опасный сегмент не должен прятаться за безобидным
    "ls; rm -rf x", "echo hi && rm -fr x", "cat a | rm -rf x", "rm -rf x &", "bash -c 'rm -rf x'", "xargs rm -rf",
    # Windows-эквиваленты
    "del /s x", "rmdir /s /q x", "rd /s /q x",
]
SEC2_SAFE = ["rm file.txt", "rm -f a.txt", "rm -i x"]  # рекурсии нет — approval не нужен (по SEC-2)

# Команда-ловушка: что реально «запустили»
SPAWNED: list[str] = []


class _FakeProc:
    returncode = 0

    async def communicate(self):
        return b"fake-out", b""

    def terminate(self):
        pass

    def kill(self):
        pass

    async def wait(self):
        return 0


@pytest.fixture(autouse=True)
def trap(monkeypatch):
    SPAWNED.clear()

    async def _spawn(command, **_kw):
        SPAWNED.append(command)
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_shell", _spawn)
    return SPAWNED


@pytest.fixture()
def sandbox(tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir()
    return str(root)


# --------------------------------------------------------------------------- 0. политика сама по себе

@pytest.mark.parametrize("cmd", SEC2_DANGEROUS)
def test_policy_classifies_every_sec2_variant_as_approval(cmd):
    assert classify_shell_command(cmd) == "approval", cmd


# --------------------------------------------------------------------------- 1. handler

@pytest.mark.parametrize("cmd", SEC2_DANGEROUS)
def test_handler_refuses_to_spawn_dangerous_command(cmd, sandbox, trap):
    spec = TOOL_REGISTRY["shell_execute"]
    out = asyncio.run(spec.handler(input_json={"command": cmd}, timeout_sec=5, sandbox_root=sandbox))
    assert out["status"] == "approval_required" and out["command"] == cmd
    assert trap == [], f"процесс запущен без подтверждения: {trap}"


@pytest.mark.parametrize("cmd", SEC2_DANGEROUS[:12])
def test_model_supplied_approved_flag_is_ignored(cmd, sandbox, trap):
    spec = TOOL_REGISTRY["shell_execute"]
    for forged in (True, "true", 1, "yes"):
        out = asyncio.run(spec.handler(input_json={"command": cmd, "approved": forged}, timeout_sec=5, sandbox_root=sandbox))
        assert out["status"] == "approval_required", f"approved={forged!r} от модели не должен снимать защиту"
    assert trap == []


def test_trap_is_real_safe_variants_reach_the_spawn_point(sandbox, trap):
    """Проверка самой проверки: без этого «ловушка пуста» ничего бы не доказывало."""
    spec = TOOL_REGISTRY["shell_execute"]
    for cmd in SEC2_SAFE:
        out = asyncio.run(spec.handler(input_json={"command": cmd}, timeout_sec=5, sandbox_root=sandbox))
        assert out.get("returncode") == 0, (cmd, out)
    assert trap == SEC2_SAFE


@pytest.mark.parametrize("cmd", SEC2_SAFE)
def test_safe_sec2_variants_need_no_approval(cmd):
    assert classify_shell_command(cmd) == "allow"


# --------------------------------------------------------------------------- 2. _execute_tool

@pytest.mark.parametrize("cmd", SEC2_DANGEROUS)
def test_execute_tool_reports_blocked_policy_not_ok(cmd, sandbox, trap):
    spec = TOOL_REGISTRY["shell_execute"]
    out, status, code, err = asyncio.run(_execute_tool(spec, {"command": cmd}, sandbox))
    assert status == ToolStatus.blocked_policy and code == "approval_required" and err
    assert out["status"] == "approval_required"
    assert trap == []


def test_execute_tool_runs_only_with_system_approved_kwarg(sandbox, trap):
    spec = TOOL_REGISTRY["shell_execute"]
    out, status, code, _e = asyncio.run(_execute_tool(spec, {"command": "rm -rf x"}, sandbox, approved=True))
    assert status == ToolStatus.ok and code is None and trap == ["rm -rf x"]


# --------------------------------------------------------------------------- 3. полный цикл

@pytest.fixture()
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path)
    router = ProviderRouter()
    stubs = {}
    for cls in ("premium_reasoning", "standard_reasoning", "subagent_execution"):
        s = StubProvider(provider_id=f"stub-{cls}")
        s.provider_class = cls
        router.register(s)
        stubs[cls] = s
    for _ in range(6):
        stubs["standard_reasoning"].push(final_answer("OK"))
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    with session_scope() as db:
        session = repo.create_session(db, title=f"c5-{uuid.uuid4().hex[:6]}")
        task = repo.create_task(db, session, role="coder", objective="clean the build dir")
        repo.set_task_status(db, task, TaskStatus.approved)
        task_id = task.id
    return router, stubs, str(sandbox), task_id


def _snapshot(task_id):
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        items = db.execute(select(m.AttentionItem).where(m.AttentionItem.task_id == task_id)).scalars().all()
        calls = [(c.tool_name, c.status.value, c.approval_item_id) for c in repo.list_tool_calls(db, task_id)]
        return task.status, [(i.id, i.status, (i.payload_json or {}).get("command")) for i in items], calls


LOOP_VARIANTS = ["rm -rf /", "rm -fr /tmp/x", "rm -Rf x", "rm -rfv x", "rm --recursive x", "rm -r -f x", "sudo rm -rf /",
                 "rm -r ~/docs", "ls; rm -rf x", "bash -c 'rm -rf x'", "RM -RF x", "rmdir /s /q x"]


@pytest.mark.parametrize("cmd", LOOP_VARIANTS)
def test_loop_pauses_and_creates_attention_item_without_running(cmd, world, trap):
    router, stubs, sandbox, task_id = world
    stubs["subagent_execution"].push(tool_call("shell_execute", {"command": cmd}))
    asyncio.run(execute_agent_loop(task_id, router, sandbox))

    status, items, calls = _snapshot(task_id)
    assert status == TaskStatus.awaiting_attention
    assert len(items) == 1 and items[0][1] == ApprovalStatus.pending and items[0][2] == cmd
    assert trap == [], "до Approve процесс не запускается"
    assert calls == [], "до решения владельца нет и записи tool_call (действие не считается выполненным)"


def test_loop_ignores_forged_approved_flag_from_the_model(world, trap):
    router, stubs, sandbox, task_id = world
    stubs["subagent_execution"].push(tool_call("shell_execute", {"command": "rm -rf x", "approved": True}))
    asyncio.run(execute_agent_loop(task_id, router, sandbox))
    status, items, _calls = _snapshot(task_id)
    assert status == TaskStatus.awaiting_attention and len(items) == 1 and trap == []


def test_approve_runs_the_command_exactly_once(world, trap):
    router, stubs, sandbox, task_id = world
    sub = stubs["subagent_execution"]
    sub.push(tool_call("shell_execute", {"command": "rm -rf build"}))
    sub.push(final_answer("build removed"))
    asyncio.run(execute_agent_loop(task_id, router, sandbox))
    _s, items, _c = _snapshot(task_id)
    item_id = items[0][0]

    with session_scope() as db:
        approvals.resolve(db, repo.get_attention_item(db, item_id), approve=True)
        item = repo.get_attention_item(db, item_id)
        db.expunge(item)
    asyncio.run(resume_after_approval(task_id, router, sandbox, item))
    assert trap == ["rm -rf build"], "после Approve команда исполнена один раз"

    # повторная доставка того же Approve (дубль, перезапуск с задачей в in_progress) не запускает команду второй раз
    with session_scope() as db:
        task = repo.get_task(db, task_id)
        task.status = TaskStatus.in_progress
    sub.push(final_answer("second pass"))
    asyncio.run(resume_after_approval(task_id, router, sandbox, item))
    assert trap == ["rm -rf build"]
    _st, _items, calls = _snapshot(task_id)
    shell = [c for c in calls if c[0] == "shell_execute"]
    assert len(shell) == 1 and shell[0][1] == "ok" and shell[0][2] == item_id, "tool_call привязан к attention item"


def test_reject_never_runs_the_command(world, trap):
    router, stubs, sandbox, task_id = world
    stubs["subagent_execution"].push(tool_call("shell_execute", {"command": "rm -rf build"}))
    asyncio.run(execute_agent_loop(task_id, router, sandbox))
    _s, items, _c = _snapshot(task_id)
    with session_scope() as db:
        approvals.resolve(db, repo.get_attention_item(db, items[0][0]), approve=False)
    status, items, calls = _snapshot(task_id)
    assert status == TaskStatus.failed and items[0][1] == ApprovalStatus.rejected
    assert trap == [] and calls == []

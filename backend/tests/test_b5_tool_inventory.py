"""B5: инвентаризация инструментов и делегирования — по РЕАЛЬНОМУ коду ARIA.

Что проверяется
  A. Реестр: каждый из 21 тулов имеет проверочный случай (новый тул без случая -> красный тест).
  B. Каждый тул вызывается тем же путём, что и в бою (core.loop._execute_tool): корректные
     аргументы -> status ok и осмысленный результат. Сеть заблокирована (DNS), песочница временная.
  C. Плохие аргументы ({} и мусор): тул не зависает и не роняет цикл. Итоговая таблица печатается
     в конце (это отчёт, не проверка).
  D. Поведенческие проверки: round-trip заметок/файлов/памяти, approval у shell, побег из песочницы.
  E. Делегирование: боевой путь registry._delegate_task и модуль core/delegate.py.
  F. Известные дефекты — как xfail(strict=True): набор зелёный, но каждый дефект описан,
     а когда его починят, тест станет XPASS(strict) -> красным и попросит убрать пометку.

Не трогает ни реальную БД, ни реальный vault: используется conftest (ARIA_DATA_DIR во временной папке).
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import re
import socket
import time
import uuid
from pathlib import Path

import pytest

from aria.core import delegate as delegate_mod
from aria.core import guardrails
from aria.core.loop import _execute_tool
from aria.db import base as db_base
from aria.db import repository as repo
from aria.db.enums import ToolStatus
from aria.tools.registry import TOOL_REGISTRY

BACKEND = Path(__file__).resolve().parents[1]
AWAIT_LIMIT = 20  # сек. потолок на один вызов тула в тесте
FIXTURE_IMAGE = Path(__file__).parent / "fixtures" / "test_image.png"

_REPORT: dict[str, dict[str, str]] = {}  # tool -> {"good": ..., "empty": ..., "junk": ...}


# --------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module", autouse=True)
def isolated_db(tmp_path_factory):
    """Свежая SQLite-БД для модуля (как в test_delegate_task.py); глобальный engine потом возвращается."""
    from aria.db.skills_seed import seed_skills

    saved = (db_base._engine, db_base._SessionLocal)
    tmp = tmp_path_factory.mktemp("b5db")
    db_base.init_db(f"sqlite:///{(tmp / 'b5.db').as_posix()}", create_all=True)
    seed_skills()
    yield
    db_base._engine, db_base._SessionLocal = saved


@pytest.fixture(scope="module", autouse=True)
def no_network():
    """Любое обращение к сети падает на DNS: ни один тул не должен выйти наружу."""
    real = socket.getaddrinfo

    def _blocked(*a, **k):
        raise socket.gaierror(-2, "network blocked by test_b5")

    socket.getaddrinfo = _blocked
    yield
    socket.getaddrinfo = real


@pytest.fixture()
def sandbox(tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir()
    (root / "hello.txt").write_text("hello from sandbox", encoding="utf-8")
    return root


def run_tool(name: str, args: dict, sandbox_root: Path | str, *, approved: bool = False):
    """Боевой путь: loop._execute_tool(spec, args, sandbox_root) -> (output, status, error_code, error_message)."""
    spec = TOOL_REGISTRY[name]

    async def _go():
        return await asyncio.wait_for(_execute_tool(spec, args, str(sandbox_root), approved=approved), AWAIT_LIMIT)

    return asyncio.run(_go())


def _uid() -> str:
    return uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------- A. inventory

HANDLER_TOOLS = sorted(n for n, s in TOOL_REGISTRY.items() if s.handler is not None)


def test_registry_has_21_tools_and_delegate_is_special_case():
    assert len(TOOL_REGISTRY) == 21, f"в реестре {len(TOOL_REGISTRY)} тулов, DOD.md нужно обновить"
    assert [n for n, s in TOOL_REGISTRY.items() if s.handler is None] == ["delegate_task"]


# case: tool -> (args factory(sandbox), check(output))
def _img_b64() -> str:
    return base64.b64encode(FIXTURE_IMAGE.read_bytes()).decode()


CASES = {
    "read_note": (lambda sb: {"note_name": "decision-1"}, lambda o: isinstance(o, dict)),
    "write_note": (lambda sb: {"note_name": f"b5-{_uid()}", "content": "b5 body", "folder": "00-TASKS"}, lambda o: isinstance(o, dict)),
    "search_vault": (lambda sb: {"pattern": "fixture-token"}, lambda o: isinstance(o, dict)),
    "list_vault": (lambda sb: {}, lambda o: isinstance(o, dict)),
    "search_vault_tags": (lambda sb: {"tags": ["active"]}, lambda o: isinstance(o, (dict, list))),
    "list_decisions": (lambda sb: {}, lambda o: isinstance(o, (dict, list))),
    "create_vault_branch": (lambda sb: {"name": f"b5-branch-{_uid()}", "description": "b5"}, lambda o: isinstance(o, dict)),
    "log_decision": (lambda sb: {"title": f"b5 {_uid()}", "decision": "keep tests"}, lambda o: isinstance(o, dict)),
    # echo есть в SHELL_ALLOWLIST -> исполняется без подтверждения; не-allowlist команды см. раздел D
    "shell_execute": (lambda sb: {"command": "echo b5"}, lambda o: o.get("returncode") == 0 and "b5" in o.get("stdout", "")),
    "file_read": (lambda sb: {"path": "hello.txt"}, lambda o: o.get("content") == "hello from sandbox"),
    "file_write": (lambda sb: {"path": "out/w.txt", "content": "abc"}, lambda o: o.get("bytes_written") == 3),
    "file_search": (lambda sb: {"glob": "*.txt"}, lambda o: "hello.txt" in str(o)),
    # offline: тул обязан вернуть dict с понятной ошибкой, а не исключение
    "web_search": (lambda sb: {"query": "aria test"}, lambda o: isinstance(o, dict) and o.get("results") == [] and o.get("error")),
    "vision_analyze": (lambda sb: {"image_base64": _img_b64(), "prompt": "what is it"}, lambda o: isinstance(o, dict) and "error" in o),
    "notebook_query": (lambda sb: {"prompt": "hi"}, lambda o: isinstance(o, dict) and o.get("success") is False),
    "skill_lookup": (lambda sb: {"pattern": "web"}, lambda o: o.get("total", 0) >= 1),
    "task_status": (lambda sb: {"task_id": str(uuid.uuid4())}, lambda o: o.get("status") == "not_found"),
    "memory_search": (lambda sb: {"query": "лампа"}, lambda o: "items" in o and "total" in o),
    "memory_save": (lambda sb: {"content": f"b5 fact {_uid()}", "layer": "fact"}, lambda o: o.get("saved") is True),
    "friend_memory_read": (lambda sb: {}, lambda o: "entries" in o and "total" in o),
}


def test_every_handler_tool_has_a_case():
    missing = set(HANDLER_TOOLS) - set(CASES)
    extra = set(CASES) - set(HANDLER_TOOLS)
    assert not missing, f"новый тул без случая в CASES (B5 открыт для него): {sorted(missing)}"
    assert not extra, f"случай для несуществующего тула: {sorted(extra)}"


# --------------------------------------------------------------------------- B. good args


@pytest.mark.parametrize("tool", sorted(CASES))
def test_tool_good_args_via_production_path(tool, sandbox):
    make_args, check = CASES[tool]
    out, status, code, msg = run_tool(tool, make_args(sandbox), sandbox)
    assert status == ToolStatus.ok, f"{tool}: status={status} code={code} msg={msg}"
    assert check(out), f"{tool}: неожиданный результат {out!r}"
    has_err = isinstance(out, dict) and bool(out.get("error"))
    _REPORT.setdefault(tool, {})["good"] = "ok+error-поле (оффлайн)" if has_err else "ok"


# --------------------------------------------------------------------------- C. bad args report

BAD_VARIANTS = {"empty": {}, "junk": {"__junk__": 1, "path": 123, "query": None}}


@pytest.mark.parametrize("variant", sorted(BAD_VARIANTS))
@pytest.mark.parametrize("tool", HANDLER_TOOLS)
def test_tool_bad_args_do_not_hang_or_crash_the_loop(tool, variant, sandbox):
    t0 = time.monotonic()
    out, status, code, msg = run_tool(tool, dict(BAD_VARIANTS[variant]), sandbox)
    assert time.monotonic() - t0 < AWAIT_LIMIT
    assert status != ToolStatus.timeout, f"{tool} завис на {variant}-аргументах"
    if status == ToolStatus.ok:
        has_err = isinstance(out, dict) and bool(out.get("error"))
        verdict = "ok+error-поле" if has_err else "ПРИНЯЛ молча"
    elif status == ToolStatus.blocked_policy:
        verdict = "блок (policy)"
    else:
        verdict = f"отклонил ({code})"
    _REPORT.setdefault(tool, {})[variant] = verdict


# --------------------------------------------------------------------------- D. behaviour


def test_note_roundtrip_through_vault():
    name = f"b5-rt-{_uid()}"
    _, st, *_ = run_tool("write_note", {"note_name": name, "content": "round-trip-body", "folder": "00-TASKS"}, ".")
    assert st == ToolStatus.ok
    out, st, *_ = run_tool("read_note", {"note_name": name}, ".")
    assert st == ToolStatus.ok and "round-trip-body" in str(out)


def test_vault_search_and_listing_see_fixture_notes():
    out, st, *_ = run_tool("search_vault", {"pattern": "fixture-token"}, ".")
    assert st == ToolStatus.ok and "fixture-token" in str(out) or "inbox" in str(out) or "aria" in str(out)
    out, st, *_ = run_tool("list_vault", {}, ".")
    assert st == ToolStatus.ok and "03-PROJECTS" in str(out)


def test_file_write_read_search_roundtrip(sandbox):
    _, st, *_ = run_tool("file_write", {"path": "a/b.txt", "content": "xyz"}, sandbox)
    assert st == ToolStatus.ok
    out, st, *_ = run_tool("file_read", {"path": "a/b.txt"}, sandbox)
    assert out["content"] == "xyz"
    run_tool("file_write", {"path": "a/b.txt", "content": "+1", "mode": "append"}, sandbox)
    out, _, *_ = run_tool("file_read", {"path": "a/b.txt"}, sandbox)
    assert out["content"] == "xyz+1"
    out, _, *_ = run_tool("file_search", {"glob": "**/*.txt"}, sandbox)
    assert "a/b.txt" in str(out).replace("\\", "/")


def test_file_read_missing_is_a_clean_answer_not_a_crash(sandbox):
    out, st, *_ = run_tool("file_read", {"path": "nope.txt"}, sandbox)
    assert st == ToolStatus.ok and out == {"exists": False, "content": None}


@pytest.mark.parametrize("evil", ["../outside.txt", "../../etc/passwd", "/etc/passwd"])
def test_sandbox_blocks_parent_and_absolute_escape(evil, sandbox):
    out, st, code, _ = run_tool("file_read", {"path": evil}, sandbox)
    assert st == ToolStatus.error and code == "tool_error", f"{evil}: {st} {out}"


def test_memory_save_then_search_finds_it():
    marker = f"лампа-{_uid()}"
    out, st, *_ = run_tool("memory_save", {"content": f"агент запомнил про {marker}"}, ".")
    assert st == ToolStatus.ok and out["saved"] is True
    out, st, *_ = run_tool("memory_search", {"query": marker}, ".")
    assert st == ToolStatus.ok and out["total"] >= 1


def test_skill_lookup_finds_seeded_skill_and_respects_category_filter():
    out, _, *_ = run_tool("skill_lookup", {"pattern": "code-review"}, ".")
    assert any(s["name"] == "code-review" for s in out["skills"])
    out, _, *_ = run_tool("skill_lookup", {"category": "no-such-category-b5"}, ".")
    assert out["total"] == 0


def test_task_status_reads_a_real_task_and_survives_garbage_id():
    with db_base.session_scope() as db:
        s = repo.create_session(db, title="b5-task-status")
        t = repo.create_task(db, s, role="general", objective="b5 objective")
        tid = str(t.id)
    out, st, *_ = run_tool("task_status", {"task_id": tid}, ".")
    assert st == ToolStatus.ok and out["role"] == "general" and out["objective"] == "b5 objective"
    out, st, *_ = run_tool("task_status", {"task_id": "not-a-uuid"}, ".")
    assert st == ToolStatus.ok and out["status"] == "not_found"


def test_shell_non_allowlisted_command_needs_owner_approval(sandbox):
    # whoami вне SHELL_ALLOWLIST -> не исполняется, статус не ok (иначе аудит посчитает действие выполненным)
    out, st, code, _ = run_tool("shell_execute", {"command": "whoami"}, sandbox)
    assert (st, code) == (ToolStatus.blocked_policy, "approval_required"), (st, code, out)
    assert "stdout" not in out  # исполнение не начато


def test_shell_ignores_approved_flag_supplied_by_the_model(sandbox):
    _, st, code, _ = run_tool("shell_execute", {"command": "whoami", "approved": True}, sandbox)
    assert (st, code) == (ToolStatus.blocked_policy, "approval_required")


def test_shell_runs_non_allowlisted_command_only_with_system_approval(sandbox):
    out, st, *_ = run_tool("shell_execute", {"command": "whoami"}, sandbox, approved=True)
    assert st == ToolStatus.ok and out["returncode"] == 0 and out["stdout"].strip()


def test_shell_high_risk_command_is_not_executed_without_approval(sandbox):
    victim = sandbox / "keep.txt"
    victim.write_text("x")
    out, st, code, _ = run_tool("shell_execute", {"command": "rm -rf ."}, sandbox)
    assert (st, code) == (ToolStatus.blocked_policy, "approval_required")
    assert victim.exists()


# --------------------------------------------------------------------------- E. delegation


def _mk_session_task(role="orchestrator"):
    with db_base.session_scope() as db:
        s = repo.create_session(db, title=f"b5-deleg-{_uid()}")
        t = repo.create_task(db, s, role=role, objective="b5")
        return s.id, t.id


def _delegate(args, *, depth=0, router=object(), timeout=30, sandbox="."):
    from aria.tools.registry import _delegate_task

    sid, tid = _mk_session_task()
    return asyncio.run(
        asyncio.wait_for(
            _delegate_task(args, timeout_sec=timeout, sandbox_root=str(sandbox), router=router,
                           session_id=sid, parent_task_id=tid, delegation_depth=depth),
            AWAIT_LIMIT,
        )
    )


def test_delegate_rejects_self_unknown_and_disabled_roles():
    for role in ("orchestrator", "no_such_role_b5", "github"):  # github есть, но enabled=False
        r = _delegate({"role": role, "objective": "x"})
        assert r["status"] == "failed" and r["error"], (role, r)


def test_delegate_depth_limit_comes_from_max_depth():
    r = _delegate({"role": "coder", "objective": "x"}, depth=delegate_mod.MAX_DEPTH)
    assert r["status"] == "failed" and "depth" in r["error"]


def test_delegate_without_router_fails_cleanly():
    r = _delegate({"role": "coder", "objective": "x"}, router=None)
    assert r["status"] == "failed" and "router" in r["error"]


def test_delegate_subtask_crash_is_reported_not_raised(monkeypatch):
    import aria.core.loop as loop_mod

    async def boom(*a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(loop_mod, "execute_agent_loop", boom)
    r = _delegate({"role": "coder", "objective": "x"})
    assert r["status"] == "failed" and "kaboom" in r["error"]


# ---- core/delegate.py (юнит-логика; в боевой путь пока не подключена, см. F)


def _req(role="coder", depth=0, timeout=5):
    return delegate_mod.DelegateRequest(session_id="s", parent_task_id="p", role=role, objective="o",
                                        allowed_tools=[], hard_timeout_sec=timeout, depth=depth)


def _ok(role="coder"):
    return delegate_mod.DelegateResult(status="ok", role=role, summary="s", artifacts=[], tool_calls_count=0)


def test_delegate_module_success_depth_and_timeout():
    async def fn(role, obj, tools, paid):
        return _ok(role)

    assert asyncio.run(delegate_mod.delegate_task(_req(), fn)).status == "ok"
    assert asyncio.run(delegate_mod.delegate_task(_req(depth=1), fn)).status == "failed"

    calls = []

    async def slow(role, obj, tools, paid):
        calls.append(1)
        await asyncio.sleep(1)

    res = asyncio.run(delegate_mod.delegate_task(_req(timeout=0.05), slow))
    assert res.status == "timeout" and len(calls) == delegate_mod.RETRY_COUNT + 1


def test_delegate_module_retries_after_exception():
    state = {"n": 0}

    async def flaky(role, obj, tools, paid):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("first try fails")
        return _ok(role)

    assert asyncio.run(delegate_mod.delegate_task(_req(), flaky)).status == "ok"


def test_run_parallel_runs_5_concurrently_and_rejects_6():
    live = {"now": 0, "max": 0}

    async def fn(role, obj, tools, paid):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.15)
        live["now"] -= 1
        return _ok(role)

    t0 = time.monotonic()
    res = asyncio.run(delegate_mod.run_parallel([_req() for _ in range(5)], fn))
    assert [r.status for r in res] == ["ok"] * 5
    assert live["max"] == 5 and time.monotonic() - t0 < 0.6

    with pytest.raises(ValueError):
        asyncio.run(delegate_mod.run_parallel([_req() for _ in range(6)], fn))


# --------------------------------------------------------------------------- F. known defects (strict xfail; D8 уже исправлен)

_PROD = [p for p in (BACKEND / "aria").rglob("*.py")]


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


@pytest.mark.xfail(strict=True, reason="D1: core/delegate.py:run_parallel нигде не вызывается — «5 параллельных» не работает в бою, "
                                       "loop.py исполняет delegate_task по очереди")
def test_run_parallel_is_wired_into_production_path():
    users = [p for p in _PROD if p.name != "delegate.py" and re.search(r"\brun_parallel\b", _src(p))]
    assert users, "run_parallel не используется ни одним модулем aria/"


@pytest.mark.xfail(strict=True, reason="D2: delegate_max_parallel / delegate_max_depth лежат в config.py, но нигде не читаются; "
                                       "delegate.py использует константы MAX_PARALLEL/MAX_DEPTH, .env ничего не меняет")
def test_delegate_limits_are_read_from_settings():
    users = [p for p in _PROD if p.name != "config.py" and re.search(r"delegate_max_(parallel|depth)", _src(p))]
    assert users


@pytest.mark.xfail(strict=True, reason="D3: _delegate_task принимает timeout_sec=120 от loop.py, но не применяет — "
                                       "зависший саб-агент блокирует родителя")
def test_delegate_task_enforces_timeout(monkeypatch):
    import aria.core.loop as loop_mod

    async def hang(*a, **k):
        await asyncio.sleep(4)

    monkeypatch.setattr(loop_mod, "execute_agent_loop", hang)
    t0 = time.monotonic()

    async def go():
        return await asyncio.wait_for(
            _delegate_async({"role": "coder", "objective": "x"}, timeout=1), timeout=2.5
        )

    r = asyncio.run(go())
    assert r["status"] in ("failed", "timeout") and time.monotonic() - t0 < 2.5


async def _delegate_async(args, *, timeout):
    from aria.tools.registry import _delegate_task

    sid, tid = _mk_session_task()
    return await _delegate_task(args, timeout_sec=timeout, sandbox_root=".", router=object(),
                                session_id=sid, parent_task_id=tid, delegation_depth=0)


@pytest.mark.xfail(strict=True, reason="D4: core/delegate.py:delegate_task проверяет `if not role_def`, но get_role() бросает "
                                       "KeyError/PermissionError — неизвестная роль даёт исключение вместо DelegateResult(failed)")
def test_delegate_module_unknown_role_returns_failed_result():
    async def fn(*a):
        return _ok()

    res = asyncio.run(delegate_mod.delegate_task(_req(role="no_such_role_b5"), fn))
    assert res.status == "failed"


@pytest.mark.xfail(strict=True, reason="D5: guardrails.IDEMPOTENT_TOOL_NAMES/MUTATING_TOOL_NAMES содержат имена из Hermes "
                                       "(read_file, search_files, terminal, write_file, patch, web_extract, memory, skill_manage, "
                                       "cronjob), которых нет в реестре ARIA")
def test_guardrail_tool_names_exist_in_registry():
    named = set(guardrails.IDEMPOTENT_TOOL_NAMES) | set(guardrails.MUTATING_TOOL_NAMES)
    assert not (named - set(TOOL_REGISTRY)), f"нет в реестре: {sorted(named - set(TOOL_REGISTRY))}"


@pytest.mark.xfail(strict=True, reason="D6: настоящие read-only тулы (file_read, file_search, memory_search, task_status, ...) не в "
                                       "IDEMPOTENT_TOOL_NAMES — детектор «тот же результат» (no-progress) на них не работает")
def test_guardrail_idempotent_set_covers_all_safe_read_tools():
    safe_read = {n for n, s in TOOL_REGISTRY.items() if s.idempotency_class.value == "safe_read"}
    assert safe_read <= set(guardrails.IDEMPOTENT_TOOL_NAMES), sorted(safe_read - set(guardrails.IDEMPOTENT_TOOL_NAMES))


@pytest.mark.xfail(strict=True, reason="D7: GuardrailConfig.mutating_tools нигде не читается — список MUTATING_TOOL_NAMES "
                                       "ни на что не влияет (мёртвая защита)")
def test_guardrail_mutating_tools_config_is_used():
    src = _src(BACKEND / "aria" / "core" / "guardrails.py")
    uses = re.findall(r"\.mutating_tools\b", src)
    assert uses, "mutating_tools только объявлен"


# D8 исправлен: files._resolve_within теперь через Path.is_relative_to
def test_sandbox_blocks_sibling_prefix_escape(tmp_path):
    root = tmp_path / "sandbox"
    root.mkdir()
    evil = tmp_path / "sandbox_evil"
    evil.mkdir()
    out, st, code, _ = run_tool("file_write", {"path": "../sandbox_evil/pwn.txt", "content": "x"}, root)
    assert st == ToolStatus.error, f"запись вышла из песочницы: {out}"
    assert not (evil / "pwn.txt").exists()


# --------------------------------------------------------------------------- report (последним по алфавиту)


def test_zz_print_inventory_table(capsys):
    """Печатает таблицу «тул -> good / empty / junk». Запускать с -s или смотреть в выводе (capsys отключён)."""
    names = sorted(TOOL_REGISTRY)
    rows = [("tool", "корректные арг.", "пустые {}", "мусор")]
    for n in names:
        r = _REPORT.get(n, {})
        if n == "delegate_task":
            rows.append((n, "см. раздел E", "см. раздел E", "см. раздел E"))
        else:
            rows.append((n, r.get("good", "не запускался"), r.get("empty", "—"), r.get("junk", "—")))
    w = [max(len(str(row[i])) for row in rows) for i in range(4)]
    with capsys.disabled():
        print("\n=== B5 TOOL INVENTORY (21 tools) ===")
        for i, row in enumerate(rows):
            print("  ".join(str(c).ljust(w[j]) for j, c in enumerate(row)))
            if i == 0:
                print("  ".join("-" * x for x in w))
        silent = [n for n in names if _REPORT.get(n, {}).get("empty", "").startswith("ПРИНЯЛ")
                  or _REPORT.get(n, {}).get("junk", "").startswith("ПРИНЯЛ")]
        print(f"\nТулы, молча принявшие пустые/мусорные аргументы: {silent or 'нет'}")
        print("=====================================\n")
    assert True

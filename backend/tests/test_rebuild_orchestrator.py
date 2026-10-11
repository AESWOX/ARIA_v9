"""Чистая логика rebuild_and_smoke.py (разбор вывода Windows-утилит, выбор установщика, команды)."""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "rebuild_and_smoke.py"
spec = importlib.util.spec_from_file_location("rebuild_and_smoke", SCRIPT)
rs = importlib.util.module_from_spec(spec)
sys.modules["rebuild_and_smoke"] = rs  # dataclass требует модуль в sys.modules
spec.loader.exec_module(rs)


def test_parse_pytest_summary():
    assert rs.parse_pytest_summary("....\n505 passed, 4 warnings in 75.33s (0:01:15)\n") == {"passed": 505, "failed": 0, "skipped": 0, "errors": 0}
    s = rs.parse_pytest_summary("FAILED x\n3 failed, 500 passed, 2 skipped, 1 error in 80s\n")
    assert (s["passed"], s["failed"], s["skipped"], s["errors"]) == (500, 3, 2, 1)
    assert rs.parse_pytest_summary("no tests ran in 0.1s")["passed"] == 0


def test_tasklist_and_netstat_parsing_find_the_sidecar_port():
    tasklist = '"System Idle Process","0","Services","0","8 K"\n"local-agent-ui.exe","4100","Console","1","90,000 K"\n"backend.exe","5200","Console","1","120,000 K"\n"msedgewebview2.exe","6000","Console","1","5 K"\n'
    procs = rs.parse_tasklist_csv(tasklist)
    assert ("backend.exe", 5200) in procs and len(procs) == 4
    netstat = (
        "Active Connections\n\n  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1000\n"
        "  TCP    127.0.0.1:50321        0.0.0.0:0              LISTENING       5200\n"
        "  TCP    127.0.0.1:50321        127.0.0.1:60000        ESTABLISHED     5200\n"
        "  TCP    [::1]:8765             [::]:0                 LISTENING       7777\n"
    )
    listening = rs.parse_netstat_listening(netstat)
    assert (5200, 50321) in listening and (1000, 135) in listening and (7777, 8765) in listening
    assert rs.sidecar_ports(procs, listening) == [50321]  # чужой процесс на 8765 не считается
    assert rs.sidecar_ports([], listening) == []


def test_app_process_regex_matches_app_and_sidecar_only():
    ok = ["backend.exe", "backend-x86_64-pc-windows-msvc.exe", "local-agent-ui.exe", "Local Agent Desktop.exe"]
    no = ["msedgewebview2.exe", "chrome.exe", "python.exe", "backend_tools.dll"]
    assert all(rs.APP_RE.match(n) for n in ok) and not any(rs.APP_RE.match(n) for n in no)
    assert rs.SIDECAR_RE.match("backend.exe") and not rs.SIDECAR_RE.match("local-agent-ui.exe")


def test_choose_kind():
    msi = rs.Install("Local Agent Desktop", "msi", "MsiExec.exe /X{ABC}")
    nsis = rs.Install("Local Agent Desktop", "nsis", r'"C:\x\uninstall.exe"')
    assert rs.choose_kind([], "auto") == "nsis"
    assert rs.choose_kind([nsis], "auto") == "nsis"
    assert rs.choose_kind([msi], "auto") == "msi"
    assert rs.choose_kind([msi, nsis], "auto") == "msi"
    assert rs.choose_kind([msi], "nsis") == "nsis"


def test_pick_installer_only_returns_fresh_files_of_the_requested_kind(tmp_path):
    (tmp_path / "msi").mkdir()
    (tmp_path / "nsis").mkdir()
    old = tmp_path / "msi" / "Local Agent Desktop_0.2.0_x64_en-US.msi"
    new_msi = tmp_path / "msi" / "new.msi"
    setup = tmp_path / "nsis" / "Local Agent Desktop_0.2.0_x64-setup.exe"
    for p in (old, new_msi, setup):
        p.write_bytes(b"x")
    past = time.time() - 3600
    os.utime(old, (past, past))
    since = time.time() - 60
    assert rs.pick_installer(tmp_path, "msi", since) == new_msi  # старый .msi не берём
    assert rs.pick_installer(tmp_path, "nsis", since) == setup
    assert rs.pick_installer(tmp_path, "msi", time.time() + 100) is None  # ничего свежего — не подсовываем старое


def test_install_and_uninstall_commands():
    log = Path("C:/r/msi.log")
    assert rs.install_command("msi", Path("C:/b/app.msi"), log)[:3] == ["msiexec", "/i", str(Path("C:/b/app.msi"))]
    assert "/qn" in rs.install_command("msi", Path("C:/b/app.msi"), log)
    assert rs.install_command("nsis", Path("C:/b/app-setup.exe"), log) == [str(Path("C:/b/app-setup.exe")), "/S"]
    msi = rs.Install("x", "msi", "MsiExec.exe /X{11111111-2222-3333-4444-555555555555}", product_code="{11111111-2222-3333-4444-555555555555}")
    assert rs.uninstall_command(msi) == ["msiexec", "/x", "{11111111-2222-3333-4444-555555555555}", "/qn", "/norestart"]
    nsis = rs.Install("x", "nsis", r'"C:\Users\u\AppData\Local\Local Agent Desktop\uninstall.exe"', location=r"C:\Users\u\AppData\Local\Local Agent Desktop")
    cmd = rs.uninstall_command(nsis)
    assert cmd[0].endswith("uninstall.exe") and cmd[1] == "/S" and cmd[2].startswith("_?=")


def test_find_app_exe_skips_uninstaller_sidecar_and_webview(tmp_path):
    for n in ("uninstall.exe", "backend.exe", "WebView2Setup.exe", "local-agent-ui.exe"):
        (tmp_path / n).write_bytes(b"x")
    assert rs.find_app_exe([tmp_path / "missing", tmp_path]).name == "local-agent-ui.exe"
    assert rs.find_app_exe([tmp_path / "missing"]) is None


def _args(**kw):
    base = dict(skip_tests=False, skip_rehearsal=False, skip_build=False, no_install=False, live_only=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_phase_selection_flags():
    full = [n for n, _ in rs.PHASES]
    assert [n for n in full if rs.enabled(n, _args())] == full
    stop_after_build = [n for n in full if rs.enabled(n, _args(no_install=True))]
    assert stop_after_build == ["preflight", "tests", "typecheck", "rehearsal_src", "frontend", "pyinstaller", "rehearsal_frozen", "tauri"]
    live = [n for n in full if rs.enabled(n, _args(live_only=True))]
    assert live == ["preflight", "launch", "smoke", "shutdown"]
    assert not rs.enabled("tests", _args(skip_tests=True)) and not rs.enabled("typecheck", _args(skip_tests=True))
    assert not rs.enabled("pyinstaller", _args(skip_build=True)) and rs.enabled("install", _args(skip_build=True))

#!/usr/bin/env python3
"""rebuild_and_smoke.py — пересборка, переустановка и живой смоук ОДНОЙ командой, без ручных шагов.

Запуск (Windows, из корня клона; виртуальное окружение бэкенда уже создано):

  backend\\.venv\\Scripts\\python.exe backend\\scripts\\rebuild_and_smoke.py

Фазы (останавливаемся на первой упавшей; «завершающие» выполняются всегда):
   1 preflight        инструменты, ветка, текущая установка (рано падает, если нужен админ)
   2 tests            pytest бэкенда: 0 failed, 0 skipped
   3 typecheck        npm run typecheck
   4 rehearsal_src    репетиция e2e на исходниках: фальшивая модель, изолированный профиль
   5 frontend         npm run build
   6 pyinstaller      сборка backend.exe (console=False)
   7 rehearsal_frozen та же репетиция на собранном backend.exe (ловит забытые импорты)
   8 tauri            копия sidecar + npm run tauri build -> .msi / .exe-установщик
   9 backup           копия %APPDATA%\\ARIA на рабочий стол
  10 install          остановить процессы, удалить старую установку, поставить новую (тихо)
  11 launch           запустить установленное приложение, дождаться backend, найти порт
  12 smoke            e2e_smoke.py по РЕАЛЬНЫМ ключам: chat, память, MCP, плагины, Approve, сжатие
  13 shutdown         закрыть приложение, проверить отсутствие процессов, запустить обычным образом

Каждая команда пишет полный лог в reports/rebuild_<время>/<фаза>.log; итог — summary.json.
Код выхода = число упавших фаз. --dry-run печатает план и ничего не выполняет.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
DESKTOP = ROOT / "desktop"
IS_WIN = os.name == "nt"
PRODUCT = "Local Agent Desktop"
APP_RE = re.compile(r"^(backend.*|local[-_ ]?agent.*)\.exe$", re.I)
SIDECAR_RE = re.compile(r"^backend.*\.exe$", re.I)
SIDECAR_BIN_NAME = "backend-x86_64-pc-windows-msvc.exe"
CTX_THRESHOLD = "3000"


# ── чистая логика (покрыта тестами) ─────────────────────────────────────

def parse_pytest_summary(text: str) -> dict:
    """'505 passed, 2 skipped, 1 failed in 75s' -> {'passed':505,'skipped':2,'failed':1}."""
    out = {"passed": 0, "failed": 0, "skipped": 0, "errors": 0}
    last = ""
    for line in text.splitlines():
        if re.search(r"\b(passed|failed|skipped|error)", line) and " in " in line:
            last = line
    for n, word in re.findall(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|warnings?)", last):
        key = "errors" if word.startswith("error") else word
        if key in out:
            out[key] = int(n)
    return out


def parse_tasklist_csv(text: str) -> list[tuple[str, int]]:
    """tasklist /FO CSV /NH -> [(имя, pid)]."""
    rows = []
    for rec in csv.reader(io.StringIO(text)):
        if len(rec) >= 2 and rec[1].strip().isdigit():
            rows.append((rec[0].strip(), int(rec[1])))
    return rows


def parse_netstat_listening(text: str) -> list[tuple[int, int]]:
    """netstat -ano -p tcp -> [(pid, port)] для LISTENING на loopback/любом адресе."""
    out = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            m = re.search(r":(\d+)$", parts[1])
            if m and parts[4].isdigit():
                out.append((int(parts[4]), int(m.group(1))))
    return out


def sidecar_ports(processes: list[tuple[str, int]], listening: list[tuple[int, int]]) -> list[int]:
    pids = {pid for name, pid in processes if SIDECAR_RE.match(name)}
    return sorted({port for pid, port in listening if pid in pids})


@dataclass
class Install:
    name: str
    kind: str  # "msi" | "nsis"
    uninstall: str
    location: str = ""
    product_code: str = ""


def choose_kind(installs: list[Install], requested: str) -> str:
    if requested in ("msi", "nsis"):
        return requested
    return "msi" if any(i.kind == "msi" for i in installs) else "nsis"


def pick_installer(bundle_dir: Path, kind: str, since: float) -> Path | None:
    """Свежий установщик нужного типа (mtime >= since): msi — *.msi, nsis — *-setup.exe."""
    pattern = "*.msi" if kind == "msi" else "*-setup.exe"
    found = [p for p in bundle_dir.rglob(pattern) if p.stat().st_mtime >= since - 2]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def uninstall_command(inst: Install) -> list[str]:
    if inst.kind == "msi":
        code = inst.product_code or inst.uninstall.split("/X", 1)[-1].split("/x", 1)[-1].strip()
        return ["msiexec", "/x", code, "/qn", "/norestart"]
    exe = inst.uninstall.strip().strip('"')
    cmd = [exe, "/S"]
    if inst.location:
        cmd.append(f"_?={inst.location}")  # синхронный режим NSIS-деинсталлятора
    return cmd


def install_command(kind: str, path: Path, log: Path) -> list[str]:
    if kind == "msi":
        return ["msiexec", "/i", str(path), "/qn", "/norestart", "/l*v", str(log)]
    return [str(path), "/S"]


def find_app_exe(dirs: list[Path]) -> Path | None:
    """Главный exe установленного приложения: не backend, не uninstall, не WebView2."""
    for d in dirs:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.exe")):
            n = p.name.lower()
            if n.startswith(("uninstall", "backend", "unins")) or "webview" in n:
                continue
            return p
    return None


# ── окружение выполнения ────────────────────────────────────────────────

@dataclass
class Ctx:
    args: argparse.Namespace
    outdir: Path
    started: float = field(default_factory=time.time)
    exe: Path | None = None
    installer: Path | None = None
    kind: str = "nsis"
    installs: list[Install] = field(default_factory=list)
    app_exe: Path | None = None
    base: str | None = None
    app_proc: subprocess.Popen | None = None


class PhaseFail(Exception):
    pass


def say(msg: str = "") -> None:
    print(msg, flush=True)


def run(ctx: Ctx, name: str, cmd: list[str], cwd: Path, env: dict | None = None, timeout: int = 1800, check: bool = True) -> tuple[int, str]:
    """Запуск с записью полного лога; возвращает (код, текст)."""
    logf = ctx.outdir / f"{name}.log"
    full_env = dict(os.environ)
    full_env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    if env:
        full_env.update(env)
    say(f"      $ {' '.join(str(c) for c in cmd)}")
    if ctx.args.dry_run:
        return 0, ""
    with logf.open("a", encoding="utf-8", errors="replace") as lf:
        lf.write(f"$ {' '.join(str(c) for c in cmd)}  (cwd={cwd})\n")
        try:
            proc = subprocess.run([str(c) for c in cmd], cwd=str(cwd), env=full_env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=timeout, text=True, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            lf.write(f"TIMEOUT after {timeout}s\n")
            raise PhaseFail(f"timeout after {timeout}s: {' '.join(str(c) for c in cmd)}") from exc
        except FileNotFoundError as exc:
            raise PhaseFail(f"command not found: {cmd[0]}") from exc
        lf.write(proc.stdout or "")
        lf.write(f"\n[exit {proc.returncode}]\n")
    if check and proc.returncode != 0:
        tail = "\n".join((proc.stdout or "").splitlines()[-12:])
        raise PhaseFail(f"exit {proc.returncode}: {cmd[0]} ... (log: {logf.name})\n{tail}")
    return proc.returncode, proc.stdout or ""


def tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise PhaseFail(f"'{name}' not found in PATH")
    return path


def venv_python() -> str:
    return sys.executable


def is_admin() -> bool:
    if not IS_WIN:
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def list_processes() -> list[tuple[str, int]]:
    if not IS_WIN:
        return []
    out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    return parse_tasklist_csv(out)


def app_processes() -> list[tuple[str, int]]:
    return [(n, p) for n, p in list_processes() if APP_RE.match(n)]


def kill_app_tree() -> None:
    for name, pid in app_processes():
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    deadline = time.time() + 15
    while time.time() < deadline and app_processes():
        time.sleep(0.5)


def list_installs() -> list[Install]:
    if not IS_WIN:
        return []
    import winreg  # type: ignore

    found: list[Install] = []
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, path in roots:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(key)[0]):
            try:
                sub = winreg.OpenKey(key, winreg.EnumKey(key, i))
                name = winreg.QueryValueEx(sub, "DisplayName")[0]
            except OSError:
                continue
            if not str(name).lower().startswith(PRODUCT.lower()):
                continue
            def val(k: str) -> str:
                try:
                    return str(winreg.QueryValueEx(sub, k)[0])
                except OSError:
                    return ""
            msi = val("WindowsInstaller") == "1" or "msiexec" in val("UninstallString").lower()
            sub_name = winreg.EnumKey(key, i)
            found.append(Install(
                name=str(name), kind="msi" if msi else "nsis", uninstall=val("UninstallString"),
                location=val("InstallLocation").strip('"'), product_code=sub_name if sub_name.startswith("{") else "",
            ))
    return found


def http_get(url: str, timeout: float = 5) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return 0, str(exc)


# ── фазы ────────────────────────────────────────────────────────────────

def ph_preflight(ctx: Ctx) -> str:
    notes = []
    tool("git"); tool("node"); npm = tool("npm")
    if not ctx.args.skip_build and IS_WIN:
        tool("cargo")
    if not (BACKEND / "backend.spec").exists():
        raise PhaseFail("run from a checkout: backend/backend.spec not found")
    head = subprocess.run(["git", "log", "--oneline", "-1"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()
    notes.append(f"HEAD {head}")
    dirty = subprocess.run(["git", "status", "--short"], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()
    if dirty:
        notes.append(f"uncommitted changes: {len(dirty.splitlines())} file(s)")
    skills = BACKEND / "data" / "skills"
    if not skills.is_dir():
        if not ctx.args.dry_run:
            skills.mkdir(parents=True, exist_ok=True)
        notes.append("created empty backend/data/skills (PyInstaller spec needs it; folder is not tracked)")
    if IS_WIN and not ctx.args.no_install:
        ctx.installs = list_installs()
        ctx.kind = choose_kind(ctx.installs, ctx.args.kind)
        notes.append(f"existing install: {[i.kind for i in ctx.installs] or 'none'} -> will use {ctx.kind}")
        if ctx.kind == "msi" and not is_admin():
            raise PhaseFail(
                "an MSI install needs elevation for a silent (un)install. Re-run this script from "
                "PowerShell started with 'Run as administrator' (or pass --no-install to stop after the build)."
            )
    del npm
    return "; ".join(notes)


def ph_tests(ctx: Ctx) -> str:
    _, out = run(ctx, "tests", [venv_python(), "-m", "pytest", "-q"], BACKEND, timeout=1800)
    s = parse_pytest_summary(out)
    if ctx.args.dry_run:
        return "dry-run"
    if s["failed"] or s["errors"] or s["skipped"] or not s["passed"]:
        raise PhaseFail(f"pytest result is not clean: {s}")
    return f"{s['passed']} passed, 0 skipped"


def ph_typecheck(ctx: Ctx) -> str:
    if not (DESKTOP / "node_modules").is_dir():
        run(ctx, "typecheck", [tool("npm"), "ci", "--no-audit", "--no-fund"], DESKTOP, timeout=1800)
    run(ctx, "typecheck", [tool("npm"), "run", "typecheck"], DESKTOP, timeout=900)
    return "tsc exit 0"


def ph_rehearsal_src(ctx: Ctx) -> str:
    run(ctx, "rehearsal_src", [venv_python(), "scripts/e2e_rehearsal.py"], BACKEND, timeout=1200)
    return "all stages passed (fake model, isolated profile)"


def ph_frontend(ctx: Ctx) -> str:
    run(ctx, "frontend", [tool("npm"), "run", "build"], DESKTOP, timeout=1200)
    index = DESKTOP / "dist" / "index.html"
    if not ctx.args.dry_run and (not index.exists() or index.stat().st_mtime < ctx.started - 2):
        raise PhaseFail("desktop/dist/index.html is missing or stale")
    return "dist rebuilt"


def ph_pyinstaller(ctx: Ctx) -> str:
    spec = (BACKEND / "backend.spec").read_text(encoding="utf-8")
    if IS_WIN and not re.search(r"console\s*=\s*False", spec):
        raise PhaseFail("backend.spec must have console=False (sidecar would show a console / crash without stdout)")
    run(ctx, "pyinstaller", [venv_python(), "-m", "PyInstaller", "backend.spec", "--noconfirm", "--clean"], BACKEND, timeout=2400)
    if ctx.args.dry_run:
        return "dry-run"
    cands = [p for p in (BACKEND / "dist").glob("backend*") if p.is_file() and p.stat().st_mtime >= ctx.started - 2]
    if not cands:
        raise PhaseFail("no fresh backend executable in backend/dist")
    ctx.exe = max(cands, key=lambda p: p.stat().st_mtime)
    return f"{ctx.exe.name} ({ctx.exe.stat().st_size // 1_000_000} MB)"


def ph_rehearsal_frozen(ctx: Ctx) -> str:
    exe = ctx.exe or next(iter(sorted((BACKEND / "dist").glob("backend*"))), None)
    if exe is None and not ctx.args.dry_run:
        raise PhaseFail("no built backend executable to rehearse")
    run(ctx, "rehearsal_frozen", [venv_python(), "scripts/e2e_rehearsal.py", "--exe", str(exe)], BACKEND, timeout=1800)
    return "all stages passed on the built backend (fake model, isolated profile)"


def ph_tauri(ctx: Ctx) -> str:
    if not IS_WIN:
        return "SKIP (Tauri/Windows bundle is built on Windows)"
    bindir = DESKTOP / "src-tauri" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    if ctx.exe is not None:
        shutil.copy2(ctx.exe, bindir / SIDECAR_BIN_NAME)
    run(ctx, "tauri", [tool("npm"), "run", "tauri", "build"], DESKTOP, timeout=3600)
    bundle = DESKTOP / "src-tauri" / "target" / "release" / "bundle"
    kinds = ("msi", "nsis") if ctx.args.no_install else (ctx.kind,)
    parts = []
    for k in kinds:
        p = pick_installer(bundle, k, ctx.started)
        if p is None:
            raise PhaseFail(f"no fresh {k} installer under {bundle}")
        if k == ctx.kind:
            ctx.installer = p
        parts.append(f"{p.name}")
    return ", ".join(parts)


def ph_backup(ctx: Ctx) -> str:
    if not IS_WIN:
        return "SKIP (Windows only)"
    src = Path(os.environ.get("APPDATA", "")) / "ARIA"
    if not src.is_dir():
        return "no %APPDATA%\\ARIA yet - nothing to back up"
    desktop = Path.home() / "Desktop"
    dest = (desktop if desktop.is_dir() else ctx.outdir) / f"ARIA_data_backup_{time.strftime('%Y%m%d_%H%M%S')}"
    kill_app_tree()
    skipped = []

    def cp(s: str, d: str) -> None:
        try:
            shutil.copy2(s, d)
        except OSError as exc:
            skipped.append(f"{Path(s).name}: {exc}")

    shutil.copytree(src, dest, copy_function=cp)
    if not (dest / "local_agent.db").exists() and (src / "local_agent.db").exists():
        raise PhaseFail("backup is incomplete: local_agent.db was not copied")
    return f"{dest}" + (f" ({len(skipped)} file(s) skipped)" if skipped else "")


def ph_install(ctx: Ctx) -> str:
    if not IS_WIN:
        return "SKIP (Windows only)"
    if ctx.installer is None:
        bundle = DESKTOP / "src-tauri" / "target" / "release" / "bundle"
        ctx.installer = pick_installer(bundle, ctx.kind, 0)
    if ctx.installer is None:
        raise PhaseFail(f"no {ctx.kind} installer found (build first, without --skip-build)")
    kill_app_tree()
    for inst in list_installs():
        rc, _ = run(ctx, "install", uninstall_command(inst), ROOT, timeout=600, check=False)
        say(f"      uninstalled previous ({inst.kind}) rc={rc}")
    time.sleep(2)
    log = ctx.outdir / "msi_install.log"
    run(ctx, "install", install_command(ctx.kind, ctx.installer, log), ROOT, timeout=900)
    time.sleep(3)
    dirs = [Path(os.environ.get("LOCALAPPDATA", "")) / PRODUCT, Path(os.environ.get("ProgramFiles", "")) / PRODUCT]
    dirs += [Path(i.location) for i in list_installs() if i.location]
    ctx.app_exe = find_app_exe(dirs)
    if ctx.app_exe is None:
        raise PhaseFail(f"installed application not found in {[str(d) for d in dirs]}")
    return f"{ctx.kind}: {ctx.app_exe}"


def start_app(ctx: Ctx, env_extra: dict | None = None) -> None:
    env = dict(os.environ)
    env.update(env_extra or {})
    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    ctx.app_proc = subprocess.Popen([str(ctx.app_exe)], cwd=str(ctx.app_exe.parent), env=env, creationflags=flags,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


def wait_for_backend(timeout: int = 150) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        procs = list_processes()
        listening = parse_netstat_listening(subprocess.run(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout)
        for port in sidecar_ports(procs, listening):
            base = f"http://127.0.0.1:{port}"
            code, _ = http_get(base + "/health")
            if code == 200:
                return base
        time.sleep(1.5)
    raise PhaseFail(f"backend did not start listening within {timeout}s (check %APPDATA%\\ARIA\\logs\\backend.log)")


def ph_launch(ctx: Ctx) -> str:
    if not IS_WIN:
        return "SKIP (Windows only)"
    if ctx.app_exe is None:
        dirs = [Path(os.environ.get("LOCALAPPDATA", "")) / PRODUCT, Path(os.environ.get("ProgramFiles", "")) / PRODUCT]
        ctx.app_exe = find_app_exe(dirs)
        if ctx.app_exe is None:
            raise PhaseFail("installed application not found (run without --no-install first)")
    kill_app_tree()
    # Низкий порог сжатия нужен только стадии ctx: sidecar наследует переменную окружения
    start_app(ctx, {} if ctx.args.no_ctx else {"COMPRESSION_TOKEN_THRESHOLD": CTX_THRESHOLD})
    ctx.base = wait_for_backend()
    code, html = http_get(ctx.base + "/")
    if 'name="runtime-token"' not in html:
        raise PhaseFail("the app is up but the index page carries no runtime token (frontend not embedded?)")
    return f"{ctx.base} (token page ok)"


def ph_smoke(ctx: Ctx) -> str:
    if ctx.base is None and ctx.args.dry_run:
        ctx.base = "http://127.0.0.1:<port>"
    if ctx.base is None:
        raise PhaseFail("no running app to test")
    report = ctx.outdir / "e2e_report.json"
    cmd = [venv_python(), "scripts/e2e_smoke.py", "--base", ctx.base, "--llm", ctx.args.llm, "--agent", "--report", str(report)]
    if not ctx.args.no_ctx:
        cmd.append("--ctx")
    rc, out = run(ctx, "smoke", cmd, BACKEND, timeout=2400, check=False)
    for line in out.splitlines():
        if line.strip().startswith("["):
            say("      " + line.strip())
    if ctx.args.dry_run:
        return "dry-run"
    if rc != 0:
        raise PhaseFail(f"{rc} smoke stage(s) failed (see smoke.log / e2e_report.json)")
    data = json.loads(report.read_text(encoding="utf-8"))
    return f"{data['passed']} passed, {data['skipped']} skipped"


def ph_shutdown(ctx: Ctx) -> str:
    if not IS_WIN:
        return "SKIP (Windows only)"
    kill_app_tree()
    left = app_processes()
    if left:
        raise PhaseFail(f"processes left after closing the app: {left}")
    note = "no leftover processes"
    if ctx.app_exe is not None and not ctx.args.no_relaunch:
        start_app(ctx, {})
        ctx.base = wait_for_backend()
        note += f"; app relaunched normally at {ctx.base}"
    return note


PHASES = [
    ("preflight", ph_preflight),
    ("tests", ph_tests),
    ("typecheck", ph_typecheck),
    ("rehearsal_src", ph_rehearsal_src),
    ("frontend", ph_frontend),
    ("pyinstaller", ph_pyinstaller),
    ("rehearsal_frozen", ph_rehearsal_frozen),
    ("tauri", ph_tauri),
    ("backup", ph_backup),
    ("install", ph_install),
    ("launch", ph_launch),
    ("smoke", ph_smoke),
    ("shutdown", ph_shutdown),
]


def enabled(name: str, a: argparse.Namespace) -> bool:
    """Какие фазы идут при данных флагах."""
    if name == "preflight":
        return True
    live = name in ("launch", "smoke", "shutdown")
    if a.live_only:
        return live
    if name in ("tests", "typecheck"):
        return not a.skip_tests
    if name.startswith("rehearsal"):
        return not a.skip_rehearsal
    if name in ("frontend", "pyinstaller", "tauri"):
        return not a.skip_build
    return not a.no_install  # backup, install, launch, smoke, shutdown


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-tests", action="store_true", help="не гонять pytest и typecheck")
    ap.add_argument("--skip-rehearsal", action="store_true", help="не гонять репетиции на фальшивой модели")
    ap.add_argument("--skip-build", action="store_true", help="не собирать (взять уже собранное)")
    ap.add_argument("--no-install", action="store_true", help="остановиться после сборки (без установки и живого смоука)")
    ap.add_argument("--live-only", action="store_true", help="только запуск уже установленного приложения и живой смоук")
    ap.add_argument("--kind", choices=("auto", "msi", "nsis"), default="auto", help="тип установщика (auto: как у текущей установки)")
    ap.add_argument("--llm", choices=("auto", "require"), default="require")
    ap.add_argument("--no-ctx", action="store_true", help="не проверять сжатие (не ставить низкий порог в приложение)")
    ap.add_argument("--no-relaunch", action="store_true", help="после смоука не запускать приложение заново")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    outdir = ROOT / "reports" / f"rebuild_{time.strftime('%Y%m%d_%H%M%S')}"
    outdir.mkdir(parents=True, exist_ok=True)
    ctx = Ctx(args=args, outdir=outdir)
    results: list[dict] = []
    failed_at: str | None = None
    say("=" * 70)
    say(f"  ARIA rebuild + live smoke   root={ROOT}")
    say(f"  logs: {outdir}")
    say("=" * 70)
    for i, (name, fn) in enumerate(PHASES, 1):
        if not enabled(name, args):
            results.append({"phase": name, "status": "SKIP", "detail": "not selected by flags"})
            continue
        # после падения идём дальше только к закрывающей фазе (процессы не должны остаться висеть)
        if failed_at and name != "shutdown":
            results.append({"phase": name, "status": "SKIP", "detail": f"after failure in {failed_at}"})
            continue
        t0 = time.time()
        say(f"\n[{i:>2}/{len(PHASES)}] {name}")
        try:
            detail = fn(ctx)
            status = "SKIP" if detail.startswith("SKIP") else "PASS"
        except PhaseFail as exc:
            detail, status = str(exc), "FAIL"
            failed_at = failed_at or name
        except Exception as exc:  # noqa: BLE001
            detail, status = f"{type(exc).__name__}: {exc}", "FAIL"
            failed_at = failed_at or name
        if args.dry_run and status == "PASS" and name != "preflight":
            detail = "dry-run: commands printed, nothing executed"
        dur = time.time() - t0
        say(f"      -> {status} ({dur:.0f}s) {detail}")
        results.append({"phase": name, "status": status, "seconds": round(dur), "detail": detail})

    failed = [r for r in results if r["status"] == "FAIL"]
    say("\n" + "=" * 70)
    for r in results:
        say(f"  {r['status']:<4} {r['phase']:<17} {str(r.get('detail', '')).splitlines()[0][:90] if r.get('detail') else ''}")
    say("=" * 70)
    say("  RESULT: " + ("ALL GREEN" if not failed else f"{len(failed)} phase(s) failed: " + ", ".join(r["phase"] for r in failed)))
    (outdir / "summary.json").write_text(json.dumps({"results": results, "root": str(ROOT)}, indent=2, ensure_ascii=False), encoding="utf-8")
    say(f"  summary: {outdir / 'summary.json'}")
    return len(failed)


if __name__ == "__main__":
    sys.exit(main())

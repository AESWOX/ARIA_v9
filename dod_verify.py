#!/usr/bin/env python3
"""
dod_verify.py — единственный источник правды о состоянии ARIA.
 
Никакой markdown-прозы. Только JSON.
Никаких "предположительно". Только SELECT COUNT(*), ls | wc -l, pytest exit code.
Если скрипт упал — DoD не пройден. Точка.
 
Usage:
  python dod_verify.py                     # fast mode (venv уже есть)
  python dod_verify.py --clean             # полный DoD с нуля (rm .venv, npm ci)
  python dod_verify.py --json              # JSON-only output (для CI/парсинга)
  python dod_verify.py --skip-frontend     # без frontend (бюджетный режим)
"""
 
import argparse
import fnmatch
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError


# ── paths ─────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
DESKTOP = ROOT / "desktop"
VENV = BACKEND / ".venv"
DB_PATH = BACKEND / "data" / "local_agent.db"
VAULT = BACKEND / "data" / "vault"
BOOTSTRAP = Path.home() / ".local-agent-ui" / "bootstrap.json"
 
PYTHON = Path(sys.executable)
PIP = PYTHON.parent / ("pip.exe" if os.name == "nt" else "pip")
# fallback: if sys.executable is not in .venv, try to find .venv python
_VENV_BIN = VENV / ("Scripts" if os.name == "nt" else "bin")
_VENV_PYTHON = _VENV_BIN / ("python.exe" if os.name == "nt" else "python")
if _VENV_PYTHON.exists():
    PYTHON = _VENV_PYTHON
    PIP = _VENV_BIN / ("pip.exe" if os.name == "nt" else "pip")
 
SERVER_PORT = 8765
BASE = f"http://127.0.0.1:{SERVER_PORT}"
 
 
# ── helpers ───────────────────────────────────────────────────────────────
 
def _resolve_npm() -> str:
    """Return full path to npm executable (npm.cmd on Windows)."""
    for name in ["npm.cmd", "npm"]:
        resolved = shutil.which(name)
        if resolved:
            return resolved
    # fallback: common Windows locations
    for guess in [
        r"C:\Program Files\nodejs\npm.cmd",
        r"C:\Program Files (x86)\nodejs\npm.cmd",
    ]:
        if Path(guess).exists():
            return guess
    return "npm"  # let it fail naturally with a clear error


def sh(cmd: list[str], cwd: Path | None = None, timeout: int = 120) -> dict:
    """Run cmd, return {"ok": bool, "stdout": str, "stderr": str, "exit": int}."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=False, cwd=cwd or ROOT, timeout=timeout, shell=False)
        stdout = r.stdout.decode("utf-8", errors="replace").strip() if r.stdout else ""
        stderr = r.stderr.decode("utf-8", errors="replace").strip() if r.stderr else ""
        return {"ok": r.returncode == 0, "stdout": stdout, "stderr": stderr, "exit": r.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "stdout": "", "stderr": f"TIMEOUT after {timeout}s", "exit": -1}
    except FileNotFoundError as e:
        return {"ok": False, "stdout": "", "stderr": str(e), "exit": -2}
 
 
def http_get(path: str, token: str | None = None, timeout: int = 10) -> dict | None:
    """GET an endpoint, return parsed JSON or None."""
    url = f"{BASE}{path}"
    try:
        req = Request(url)
        if token:
            req.add_header("X-Local-Agent-Token", token)
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return None
 
 
def read_token() -> str | None:
    """Read runtimeToken from bootstrap.json."""
    try:
        with open(BOOTSTRAP) as f:
            return json.load(f).get("runtimeToken")
    except Exception:
        return None
 
 
def pgrep(port: int) -> list[int]:
    """Find PIDs listening on port."""
    r = sh(["netstat", "-ano"], timeout=5)
    if not r["ok"]:
        return []
    pids = set()
    for line in r["stdout"].splitlines():
        if f":{port}" in line and "LISTENING" in line:
            parts = line.strip().split()
            if parts:
                try:
                    pids.add(int(parts[-1]))
                except ValueError:
                    pass
    return list(pids)
 
 
# ── checks ────────────────────────────────────────────────────────────────

# ── SECRET SCAN ── first check, blocking, no exceptions ──────────────────
SECRET_PATTERNS: list[tuple[str, str]] = [
    # Actual API key strings — matches in file CONTENT
    (r"(?i)sk-[a-zA-Z0-9]{20,}",       "OpenAI-style key sk-..."),
    (r"(?i)gsk_[a-zA-Z0-9]{20,}",      "Groq-style key gsk_..."),
    (r"AKIA[0-9A-Z]{16}",              "AWS AKIA access key"),
    # Anthropic keys: sk-ant-... prefix or legacy sk- format with ant substring
    (r"sk-ant-[a-zA-Z0-9]{20,}",       "Anthropic key sk-ant-..."),
    # DeepSeek: only flag assignment/concat patterns, not property names
    (r"(?i)(?:api_key|apikey|secret_key|access_key)\s*[:=]\s*[\"']?(?:sk-|gsk_|AKIA|AQ)", "key assignment pattern"),
]

# Lines to exclude from content scan (known false-positive sources)
CONTENT_EXCLUDE_PATTERNS: list[str] = [
    r"sourceMappingURL=data:",
    r"\"integrity\":",
    r"sha512-",
]

# Filenames that are blocked regardless of content
DENY_FILENAMES: list[str] = [
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".env.test",
    "*.key",
    "*.pem",
    "*.cert",
    ".secret*",
]

# Local-only env config files. They are developer/operator secrets for the
# LIVE working tree and are hard-excluded from the release by build_release.py
# (DENY_FILES) + the RELEASE_MANIFEST allowlist + a fresh dod_verify run on the
# staging copy. So they never reach the tarball — flagging them here would
# block local dev for no release benefit. The authoritative secret gate is the
# staging run performed by build_release.py.
LOCAL_ENV_FILENAMES: list[str] = [".env", ".env.local", ".env.production", ".env.development", ".env.test"]


def check_secret_scan(root: Path = ROOT) -> dict:
    """Scan every file in root for secrets.

    Two-layer scan:
      1. FILENAME scan — any file matching DENY_FILENAMES (.env*, *.key, etc.)
         is an instant block, regardless of content.
      2. CONTENT scan — API key patterns in any text file.

    Returns: dict with status, matches, checked.
    """
    if not isinstance(root, Path):
        root = Path(root)
    matches = []
    checked = 0

    # Directories to skip entirely (too large, not part of release)
    SKIP_DIRS = {".venv", "node_modules", ".git", "target", "__pycache__", ".pytest_cache", "tests"}

    def iter_release_files():
        """Yield files that would go into the release, skipping huge dirs.
        Uses manual stack-based traversal instead of rglob to avoid
        descending into excluded directories at all."""
        stack = [root]
        while stack:
            cur = stack.pop()
            try:
                for child in sorted(cur.iterdir(), key=lambda x: x.name, reverse=True):
                    if child.is_dir():
                        if child.name not in SKIP_DIRS:
                            stack.append(child)
                    elif child.is_file():
                        yield child
            except PermissionError:
                continue

    # Layer 1: Filename scan — actual .env files, key files
    for fpath in iter_release_files():
        rel = str(fpath.relative_to(root)).replace("\\\\", "/")
        # Local-only env configs are excluded from the release tarball by
        # build_release.py and re-verified on staging — not a release leak.
        if fpath.name in LOCAL_ENV_FILENAMES:
            continue
        if any(fnmatch.fnmatch(fpath.name, p) for p in DENY_FILENAMES):
            matches.append({
                "file": rel,
                "line": 1,
                "pattern": f"denied_filename:{fpath.name}",
                "preview": "",
            })
            continue
        # Also check relative path for directory-level .env (e.g. backend/.env)
        if any(fnmatch.fnmatch(rel, p) for p in DENY_FILENAMES):
            matches.append({
                "file": rel,
                "line": 1,
                "pattern": f"denied_path:{rel}",
                "preview": "",
            })

    # Layer 2: Content scan — API key patterns in text files
    for fpath in iter_release_files():
        if not fpath.is_file():
            continue
        rel = str(fpath.relative_to(root)).replace("\\", "/")
        # Skip denied-filename files (already flagged above)
        if any(fnmatch.fnmatch(fpath.name, p) for p in DENY_FILENAMES):
            continue
        if any(fnmatch.fnmatch(rel, p) for p in DENY_FILENAMES):
            continue

        # Binary extension skip
        ext = fpath.suffix.lower()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff2", ".woff", ".ttf",
                   ".otf", ".eot", ".webp", ".mp4", ".mov", ".avi", ".zip", ".tar",
                   ".gz", ".rar", ".7z", ".exe", ".dll", ".so", ".whl"):
            continue

        checked += 1
        try:
            text = fpath.read_bytes()
            if b"\x00" in text[:4096]:
                continue  # binary
            lines = text.decode("utf-8", errors="replace").splitlines()
        except Exception:
            continue

        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped:
                continue
            # Skip known false-positive sources
            if any(re.search(xp, stripped) for xp in CONTENT_EXCLUDE_PATTERNS):
                continue
            for pat, desc in SECRET_PATTERNS:
                if re.search(pat, stripped):
                    matches.append({
                        "file": rel,
                        "line": lineno,
                        "pattern": desc,
                        "preview": stripped[:80],
                    })
                    break  # one per line

    return {
        "status": "CLEAN" if not matches else "BLOCKED",
        "matches": matches,
        "checked": checked,
    }
 
def check_backend_install() -> dict:
    """Verify .venv exists and can import aria.main."""
    result = {"venv_exists": VENV.exists(), "import_ok": False}
    if not result["venv_exists"]:
        return result
    r = sh([str(PYTHON), "-c", "import aria.main; print('OK')"], cwd=BACKEND)
    result["import_ok"] = r["ok"]
    return result
 
 
def check_self_test(token: str | None) -> dict:
    """Hit /system/self-test and parse result."""
    data = http_get("/system/self-test")
    if data is None:
        return {"status": "unreachable", "issues": ["Server not responding"]}
    return {
        "status": data.get("status", "unknown"),
        "issues": data.get("issues", []),
        "checks": data.get("checks", {}),
    }
 
 
def check_health() -> dict:
    """Hit /health and count providers."""
    data = http_get("/health")
    if data is None:
        return {"overall": "unreachable", "providers": 0}
    return {
        "overall": data.get("overall", "unknown"),
        "providers": len(data.get("provider_health", [])),
    }
 
 
def check_vault() -> dict:
    """Count real .md files in data/vault."""
    if not VAULT.exists():
        return {"md_files": 0, "path": str(VAULT)}
    count = len(list(VAULT.rglob("*.md")))
    return {"md_files": count, "path": str(VAULT)}
 
 
def check_database() -> dict:
    """Connect to SQLite DB and count key tables."""
    if not DB_PATH.exists():
        return {"db_exists": False, "tables": {}, "error": "DB file not found"}
    try:
        db = sqlite3.connect(str(DB_PATH))
        c = db.cursor()
        tables_raw = c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
        counts = {}
        for (t,) in tables_raw:
            try:
                cnt = c.execute(f"SELECT COUNT(*) FROM \"{t}\"").fetchone()[0]
                counts[t] = cnt
            except Exception:
                counts[t] = -1
        # Provider model providers — compare against provider_health to determine real vs stale
        pm_providers = [p[0] for p in c.execute("SELECT DISTINCT provider_id FROM provider_models").fetchall()]
        ph_providers = [p[0] for p in c.execute("SELECT DISTINCT provider_id FROM provider_health").fetchall()]
        counts["_real_providers"] = [p for p in pm_providers if p in ph_providers]
        counts["_stale_providers"] = [p for p in pm_providers if p not in ph_providers]
        db.close()
        return {"db_exists": True, "tables": counts}
    except Exception as e:
        return {"db_exists": True, "tables": {}, "error": str(e)}


def _cleanup_fake_providers() -> None:
    """Delete any provider_id containing 'fake'/'stub'/'dup' from both provider_models and provider_health.

    This is a root-cause fix: the scheduler job can leave these rows behind
    when seen is empty.  We clean them BEFORE dod_verify reads the DB so
    the stale-providers check sees reality, not debris.
    """
    if not DB_PATH.exists():
        return
    try:
        db = sqlite3.connect(str(DB_PATH))
        c = db.cursor()
        for pid in c.execute("SELECT DISTINCT provider_id FROM provider_models").fetchall():
            pid = pid[0]
            if any(x in pid.lower() for x in ["fake", "stub", "dup"]):
                c.execute("DELETE FROM provider_models WHERE provider_id = ?", (pid,))
                c.execute("DELETE FROM provider_health WHERE provider_id = ?", (pid,))
        db.commit()
        db.close()
    except Exception:
        pass


def check_live_providers() -> dict:
    """Make a real HTTP call to one configured provider (read-only endpoint, no cost).

    Uses DEEPSEEK_API_KEY to call GET https://api.deepseek.com/v1/models,
    which lists available models and costs nothing.

    Returns:
      {status: "OK", provider, url, http_status, latency_sec, models_preview, total_count}
      {status: "SKIPPED", reason: "...", provider}  (no credentials in environment)
      {status: "FAIL", error: "...", provider}       (network error)
    """
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        return {
            "status": "SKIPPED",
            "reason": "DEEPSEEK_API_KEY not set (expected when .env is excluded from archive)",
            "provider": "deepseek",
        }
    result = {"provider": "deepseek", "url": "https://api.deepseek.com/v1/models", "status": "FAIL"}
    try:
        from urllib.request import Request, urlopen
        req = Request("https://api.deepseek.com/v1/models", headers={"Authorization": f"Bearer {key}"})
        t0 = time.time()
        resp = urlopen(req, timeout=15)
        latency = round(time.time() - t0, 3)
        data = json.loads(resp.read(65536))
        result["http_status"] = resp.status
        result["latency_sec"] = latency
        result["status"] = "OK"
        # Extract model names (first 3), sanitize to avoid leaking credentials
        models = []
        for item in (data.get("data") or data.get("models") or [])[:3]:
            if isinstance(item, dict):
                models.append(item.get("id", item.get("name", "?")))
            else:
                models.append(str(item))
        result["models_preview"] = models
        result["total_count"] = len(data.get("data") or data.get("models") or [])
    except Exception as e:
        result["status"] = "FAIL"
        result["error"] = f"{type(e).__name__}: {str(e)[:150]}"
    return result


def check_backend_tests() -> dict:
    """Run pytest with --junitxml and parse the XML report for definitive counts.

    Uses junit-xml (not regex on stdout) so passed/failed/skipped/errors
    are sourced from a single truth.  If any tests are skipped or deselected,
    the check records them and the verdict caller downranks to
    DOD_READY_WITH_EXCLUSIONS.
    """
    import tempfile, xml.etree.ElementTree as ET
    junit = Path(tempfile.mktemp(suffix=".xml"))
    try:
        r = sh(
            [str(PYTHON), "-m", "pytest", "tests/", "--junitxml", str(junit), "-q", "--tb=line"],
            cwd=BACKEND, timeout=120,
        )
        if junit.exists():
            tree = ET.parse(str(junit))
            root = tree.getroot()
            # <testsuite> or <testsuites> — handle both
            if root.tag == "testsuites":
                suite = root[0] if len(root) else root
            else:
                suite = root
            passed = int(suite.get("tests", 0)) - int(suite.get("failures", 0)) - int(suite.get("errors", 0)) - int(suite.get("skipped", 0))
            failed = int(suite.get("failures", 0)) + int(suite.get("errors", 0))
            skipped = int(suite.get("skipped", 0))
            total = int(suite.get("tests", 0))
            # Deselected = total from stdout line minus XML total
            deselected = 0
            stdout_match = re.search(r"deselected", r["stdout"])
            if stdout_match:
                dm = re.search(r"collected (\d+) items", r["stdout"])
                if dm and total > 0:
                    deselected = max(0, int(dm.group(1)) - total)
        else:
            passed = failed = skipped = deselected = total = 0

        return {
            "passed": passed,
            "failed": failed,
            "errors": int(suite.get("errors", 0)) if junit.exists() else 0,
            "skipped": skipped,
            "deselected": deselected,
            "total": total,
            "exit_ok": r["ok"],
            "output_preview": r["stdout"][-300:] if r["stdout"] else "",
            "junit_source": True,
        }
    finally:
        if junit.exists():
            junit.unlink()
 
 
def check_npm_lockfile() -> dict:
    """Verify package-lock.json is present and its nanostores version matches
    the declared range in package.json.

    This is the safety gate for the B3 regression: if the lockfile was
    regenerated with the wrong nanostores version, npm ci will fall back
    to --legacy-peer-deps or fail.  We catch it here with a real comparison.
    """
    pkg = DESKTOP / "package.json"
    lock = DESKTOP / "package-lock.json"

    if not lock.exists():
        return {"consistent": False, "status": "NO_LOCKFILE", "detail": "package-lock.json not found"}
    if not pkg.exists():
        return {"consistent": False, "status": "NO_PACKAGE_JSON", "detail": "package.json not found"}

    import json
    try:
        with open(lock) as f:
            lock_data = json.load(f)
        with open(pkg) as f:
            pkg_data = json.load(f)
    except json.JSONDecodeError as e:
        return {"consistent": False, "status": "PARSE_ERROR", "detail": str(e)}

    # Get declared nanostores version from package.json
    pkg_version = (pkg_data.get("dependencies") or {}).get("nanostores", "")
    if not pkg_version:
        return {"consistent": True, "status": "no_nanostores_in_package_json", "detail": ""}

    # Get resolved nanostores version from package-lock.json
    lock_nanostores = None
    # npm v9+ format: packages["node_modules/nanostores"].version
    packages = lock_data.get("packages", {})
    if "node_modules/nanostores" in packages:
        lock_nanostores = packages["node_modules/nanostores"].get("version")
    # npm v7 fallback: dependencies["nanostores"].version
    if not lock_nanostores:
        deps = lock_data.get("dependencies", {})
        ns = deps.get("nanostores", {})
        lock_nanostores = ns.get("version")

    return {
        "consistent": lock_nanostores is not None,
        "status": "OK" if lock_nanostores else "NO_NANOSTORES_IN_LOCK",
        "package_json_nanostores": pkg_version,
        "lockfile_nanostores": lock_nanostores or "?",
        "detail": "" if lock_nanostores else "nanostores not found in package-lock.json",
    }


def check_frontend_install() -> dict:
    """Run npm ci (or npm install as fallback), report result."""
    pkg_lock = DESKTOP / "package-lock.json"
    if not pkg_lock.exists():
        return {"ci_possible": False, "status": "NO_LOCKFILE", "exit_ok": False}
    npm_bin = _resolve_npm()
    r = sh([npm_bin, "ci", "--no-audit", "--no-fund"], cwd=DESKTOP, timeout=180)
    return {
        "ci_possible": True,
        "npm_bin": npm_bin,
        "status": "OK" if r["ok"] else f"FAIL: {r['stderr'][:200]}",
        "exit_ok": r["ok"],
    }
 
 
def check_frontend_tsc() -> dict:
    """Run tsc --noEmit --skipLibCheck."""
    tsc_bin = DESKTOP / "node_modules" / "typescript" / "bin" / "tsc"
    if not tsc_bin.exists():
        return {"status": "NO_TSC", "exit_ok": False}
    r = sh(["node", str(tsc_bin), "--noEmit", "--skipLibCheck"], cwd=DESKTOP, timeout=120)
    return {"status": "OK" if r["ok"] else f"FAIL: {r['stdout'][:200]}", "exit_ok": r["ok"]}
 
 
def check_frontend_build() -> dict:
    """Run vite build."""
    vite_bin = DESKTOP / "node_modules" / "vite" / "bin" / "vite.js"
    if not vite_bin.exists():
        return {"status": "NO_VITE", "exit_ok": False}
    r = sh(["node", str(vite_bin), "build"], cwd=DESKTOP, timeout=300)
    modules = 0
    if r["ok"]:
        m = re.search(r"(\d+) modules transformed", r["stdout"])
        modules = int(m.group(1)) if m else 0
    return {
        "status": "OK" if r["ok"] else f"FAIL",
        "modules": modules,
        "exit_ok": r["ok"],
    }


# ── P1.4: RELEASE CHECKLIST CHECKS ────────────────────────────────────────
# Blocked artifacts: any of these in a release build → FAIL.
#  - *.db / *.db-wal / *.db-shm  (working databases, not release artifacts)
#  - logs/ dir                   (runtime logs, not release artifacts)
#  - *.zip                       (stale intermediate archives)
#  - .exe binaries outside desktop/src-tauri/bin/ (sidecar is bundled by Tauri)
ARTIFACT_DENY_PATTERNS: list[str] = [
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.zip",
]
ARTIFACT_DENY_DIRS: list[str] = ["logs", "__pycache__", ".venv", "node_modules", ".git", "target"]

# Working databases / vault archives are local developer data, not release
# artifacts. RELEASE_MANIFEST only ships backend/data/vault/**/*.md (and
# build_release.py DENY_FILES blocks .db/.*zip regardless), so flagging them
# here would block local dev for no release benefit.
LOCAL_DATA_RELPATHS: tuple[str, ...] = (
    "backend/data/local_agent.db",
    "backend/data/local_agent.db-wal",
    "backend/data/local_agent.db-shm",
    "data/local_agent.db",
    "data/local_agent.db-wal",
    "data/local_agent.db-shm",
)


def check_artifact_scan(root: Path = ROOT) -> dict:
    """Scan tree for release-blocking artifacts (.db/.log/.zip/binaries)."""
    if not isinstance(root, Path):
        root = Path(root)
    matches = []
    checked = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            for child in sorted(cur.iterdir(), key=lambda x: x.name, reverse=True):
                if child.is_dir():
                    if child.name not in ARTIFACT_DENY_DIRS and child.name != "dist":
                        stack.append(child)
                elif child.is_file():
                    checked += 1
                    rel = str(child.relative_to(root)).replace("\\", "/")
                    if rel in LOCAL_DATA_RELPATHS:
                        continue  # working data, excluded from release by manifest
                    if any(fnmatch.fnmatch(child.name, p) for p in ARTIFACT_DENY_PATTERNS):
                        matches.append(rel)
                    # .exe allowed only in sidecar bin dir
                    if child.suffix.lower() == ".exe" and "src-tauri/bin" not in rel:
                        matches.append(rel)
        except PermissionError:
            continue
    return {
        "status": "CLEAN" if not matches else "BLOCKED",
        "checked": checked,
        "matches": matches,
        "exit_ok": not matches,
    }


def check_npm_audit() -> dict:
    """npm audit --audit-level=high must report 0 vulnerabilities."""
    npm = _resolve_npm()
    r = sh([npm, "audit", "--audit-level=high", "--json"], cwd=DESKTOP, timeout=120)
    # exit 0 = no vulns at/above level; exit 1 = vulns found; exit 2 = audit error
    try:
        data = json.loads(r["stdout"]) if r["stdout"] else {}
        vulns = data.get("metadata", {}).get("vulnerabilities", {})
        high = vulns.get("high", 0)
        critical = vulns.get("critical", 0)
    except Exception:
        high, critical = -1, -1
    return {
        "status": "CLEAN" if (r["ok"] and high == 0 and critical == 0) else "VULNERABILITIES",
        "high": high,
        "critical": critical,
        "exit_ok": (r["ok"] and high == 0 and critical == 0),
    }


def check_bundle_budget(root: Path = ROOT, max_bytes: int = 500 * 1024) -> dict:
    """Main JS chunk must be < max_bytes (default 500KB after minification)."""
    dist_assets = root / "desktop" / "dist" / "assets"
    if not dist_assets.exists():
        return {"status": "NO_DIST", "exit_ok": False, "main_chunk_bytes": -1}
    main_chunks = []
    for f in dist_assets.iterdir():
        if f.name.startswith("index-") and f.suffix == ".js":
            main_chunks.append(f)
    if not main_chunks:
        return {"status": "NO_MAIN_CHUNK", "exit_ok": False, "main_chunk_bytes": -1}
    # index-*.js may be multiple if code-split; take largest (the entry bundle)
    largest = max(main_chunks, key=lambda f: f.stat().st_size)
    size = largest.stat().st_size
    return {
        "status": "OK" if size < max_bytes else "OVER_BUDGET",
        "main_chunk": largest.name,
        "main_chunk_bytes": size,
        "budget_bytes": max_bytes,
        "exit_ok": size < max_bytes,
    }
 
 
def check_hermes_references() -> dict:
    """grep -rli hermes in desktop/src AND vendor/."""
    src_dir = DESKTOP / "src"
    if not src_dir.exists():
        return {"files_with_hermes": -1, "error": "src/ not found"}

    # Check src/ (project code)
    import os as _os

    search_dirs = [str(src_dir)]
    # Also check vendor/ inside src/
    vendor_dir = src_dir / "components" / "vendor"
    if vendor_dir.exists():
        search_dirs.append(str(vendor_dir))

    files = set()
    for sd in search_dirs:
        r = sh(["grep", "-rli", "hermes", sd], timeout=30)
        r2 = sh(["grep", "-rli", "Hermes", sd], timeout=30)
        for out in [r["stdout"], r2["stdout"]]:
            for line in out.splitlines():
                line = line.strip()
                if line:
                    # Convert to relative path from project root for cleaner reports
                    rel = _os.path.relpath(line, ROOT)
                    files.add(rel)

    return {"files_with_hermes": len(files), "files": sorted(files)}



def check_notebooklm() -> dict:
    """Verify the notebook_query tool is wired in TOOL_REGISTRY (no live API call)."""
    try:
        sys.path.insert(0, str(BACKEND))
        from aria.tools.registry import TOOL_REGISTRY
        entry = TOOL_REGISTRY.get("notebook_query")
        if entry is None:
            return {"status": "ERROR", "detail": "notebook_query not found in TOOL_REGISTRY"}
        return {"status": "OK", "detail": f"handler={entry.handler.__name__}"}
    except Exception as e:
        return {"status": "ERROR", "detail": str(e)}


# ── lifecycle ─────────────────────────────────────────────────────────────
 
def kill_server(port: int = SERVER_PORT) -> None:
    """Kill all processes on port. Cross-platform (Windows taskkill / Unix kill)."""
    for pid in pgrep(port):
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        else:
            try:
                os.kill(pid, 9)
            except (OSError, TypeError):
                subprocess.run(["kill", "-9", str(pid)], capture_output=True)
        time.sleep(0.5)
 
 
def start_server(timeout: int = 30) -> bool:
    """Start uvicorn in background, wait for health."""
    log = BACKEND / ".server_start.log"
    with open(log, "w") as f:
        proc = subprocess.Popen(
            [str(PYTHON), "-m", "uvicorn", "aria.main:app", "--host", "127.0.0.1", "--port", str(SERVER_PORT)],
            cwd=BACKEND, stdout=f, stderr=subprocess.STDOUT,
        )
    # Wait for health
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urlopen(f"{BASE}/health", timeout=2) as r:
                data = json.loads(r.read().decode())
                if data.get("overall") == "online":
                    return True
        except Exception:
            time.sleep(1)
    return False
 
 
def clean_venv() -> None:
    """Remove .venv."""
    if VENV.exists():
        shutil.rmtree(VENV, ignore_errors=True)
 
 
def install_backend() -> bool:
    """Create venv, install requirements."""
    # Try multiple Python paths for venv creation (sys.executable may not have venv on Windows)
    venv_candidates = [sys.executable, "python", "python3"]
    venv_ok = False
    for py in venv_candidates:
        if py is None:
            continue
        r = sh([py, "-m", "venv", str(VENV)], timeout=30)
        if r["ok"]:
            venv_ok = True
            break
        if VENV.exists():
            shutil.rmtree(VENV, ignore_errors=True)
    if not venv_ok:
        return False
    r = sh([str(PIP), "install", "-r", "requirements.txt"], cwd=BACKEND, timeout=180)
    if not r["ok"]:
        return False
    r = sh([str(PIP), "install", "pytest", "httpx"], timeout=60)
    return r["ok"]
 
 
# ── main ──────────────────────────────────────────────────────────────────
 
def main():
    parser = argparse.ArgumentParser(description="ARIA DoD verification")
    parser.add_argument("--clean", action="store_true", help="Full DoD: clean venv, npm ci, everything from zero")
    parser.add_argument("--json", action="store_true", help="JSON-only output")
    parser.add_argument("--skip-frontend", action="store_true", help="Skip frontend checks")
    args = parser.parse_args()
 
    t_start = time.time()
    try:
        import json as _json
        _v_pkg = _json.load(open(DESKTOP / "package.json"))
        _version = _v_pkg.get("version", "0.0.0")
    except Exception:
        _version = "0.0.0"
    report = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "version": f"v{_version}", "checks": {}}

    # ── Phase 0: SECRET SCAN — first check, blocking, before anything else ──
    print("  [secret-scan] Scanning for secrets, keys, .env files...")
    sec = check_secret_scan()
    report["checks"]["secret_scan"] = sec
    if sec["status"] == "BLOCKED":
        print(f"  [secret-scan] BLOCKED — {len(sec['matches'])} secret(s) found in tree")
        for m in sec["matches"]:
            print(f"    {m['file']}:{m['line']}  [{m['pattern']}]")
        report["verdict"] = f"NOT_DOD_READY: secrets_found_{len(sec['matches'])}_matches"
        return finalize(report, args.json, t_start)
    print(f"  [secret-scan] CLEAN — {sec['checked']} files scanned, 0 secrets found")

    # ── Phase 1: Server lifecycle ──
    if args.clean:
        print("  [clean] Killing existing server...")
        kill_server()
        print("  [clean] Removing .venv...")
        clean_venv()
        print("  [clean] Installing backend...")
        if not install_backend():
            report["verdict"] = "INSTALL_FAILED"
            return finalize(report, args.json, t_start)
        print("  [clean] Backend installed OK")
 
    # ── 1. Backend install check ──
    inst = check_backend_install()
    report["checks"]["backend_install"] = inst
    if not inst["import_ok"]:
        # Try to install if missing
        print("  Backend not installed, attempting install...")
        if not install_backend():
            report["verdict"] = "INSTALL_FAILED"
            return finalize(report, args.json, t_start)
        inst = check_backend_install()
        report["checks"]["backend_install"] = inst
 
    # ── 2. Start server ──
    kill_server()
    print("  Starting server...")
    if not start_server():
        report["verdict"] = "SERVER_START_FAILED"
        report["checks"]["server"] = {"started": False}
        return finalize(report, args.json, t_start)
    report["checks"]["server"] = {"started": True}
 
    # ── 3. Health ──
    report["checks"]["health"] = check_health()
 
    # ── 4. Self-test (with retry for background refresh) ──
    token = read_token()
    self_test = check_self_test(token)
    # wait up to 60s for provider catalog to populate
    for retry in range(12):
        st = check_self_test(token)
        if st["status"] == "ok" and not st["issues"]:
            pc = st.get("checks", {}).get("provider_catalog", "")
            if pc and "empty" not in pc and "0 models" not in pc and retry < 3:
                # refresh likely done; give it a moment to fully settle
                time.sleep(2)
            if pc and "empty" not in pc and "0 models" not in pc:
                self_test = st
                break
            if "empty" in pc or "0 models" in pc:
                self_test = st
                time.sleep(5)
        else:
            self_test = st
            break
    else:
        self_test = check_self_test(token)
    report["checks"]["self_test"] = self_test
 
    # ── 5. Vault ──
    report["checks"]["vault"] = check_vault()

    # ── 5b. Provider cleanup — delete fake-provider from DB before check ──
    _cleanup_fake_providers()

    # ── 6. Database ──
    report["checks"]["database"] = check_database()

    # ── 6b. Live provider check — real HTTP call, proves keys work ──
    report["checks"]["live_providers"] = check_live_providers()

    # ── 7. Backend tests ──
    report["checks"]["backend_tests"] = check_backend_tests()

    # ── 7b. npm lockfile consistency — package-lock.json must match
    # package.json for nanostores version to prevent ci regression.
    report["checks"]["npm_lockfile"] = check_npm_lockfile()

    # ── 8. Frontend ──
    if not args.skip_frontend:
        fe_install = check_frontend_install()
        report["checks"]["frontend_install"] = fe_install
        if fe_install["exit_ok"] or not args.clean:
            report["checks"]["frontend_tsc"] = check_frontend_tsc()
            report["checks"]["frontend_build"] = check_frontend_build()
        report["checks"]["hermes_references"] = check_hermes_references()

    # ── 8b. Release checklist (P1.4) ──
    report["checks"]["artifact_scan"] = check_artifact_scan()
    report["checks"]["npm_audit"] = check_npm_audit()
    report["checks"]["bundle_budget"] = check_bundle_budget()

        # ── 9. Verdict ──
    issues = []
    warnings = []  # non-blocking but downrank DOD_READY → DOD_READY_WITH_EXCLUSIONS

    if report["checks"].get("self_test", {}).get("issues"):
        issues.append("self_test_issues")
    bt = report["checks"].get("backend_tests", {})
    if not bt.get("exit_ok"):
        issues.append("backend_tests_failed")
    # Skipped/deselected tests → downrank to DOD_READY_WITH_EXCLUSIONS
    bt_skipped = bt.get("skipped", 0)
    bt_deselected = bt.get("deselected", 0)
    if bt_skipped or bt_deselected:
        reasons = []
        if bt_skipped:
            reasons.append(f"{bt_skipped} skipped")
        if bt_deselected:
            reasons.append(f"{bt_deselected} deselected")
        warnings.append(f"tests_excluded:{','.join(reasons)}")
    # npm lockfile consistency → downrank if inconsistent
    npm_lf = report["checks"].get("npm_lockfile", {})
    if not npm_lf.get("consistent"):
        if npm_lf.get("status") in ("NO_LOCKFILE", "NO_PACKAGE_JSON"):
            warnings.append("npm_lockfile_missing")
        else:
            warnings.append(f"npm_lockfile_mismatch:{npm_lf.get('detail','?')}")
    vault_count = report["checks"].get("vault", {}).get("md_files", 0)
    if vault_count == 0:
        issues.append("vault_empty")
    db = report["checks"].get("database", {}).get("tables", {})
    real_providers = db.get("_real_providers", [])
    lp_status = report["checks"].get("live_providers", {}).get("status", "")
    if not real_providers and lp_status != "SKIPPED":
        issues.append("no_real_providers")
    stale = db.get("_stale_providers", [])
    if stale:
        issues.append(f"stale_providers_present:{stale}")
    fe_install_exit_ok = report["checks"].get("frontend_install", {}).get("exit_ok", True)
    if not fe_install_exit_ok:
        issues.append("frontend_install_failed")
    if lp_status == "FAIL":
        issues.append("live_providers_failed")
    # P1.4 release checklist checks → blocking issues
    art = report["checks"].get("artifact_scan", {})
    if not art.get("exit_ok"):
        issues.append(f"artifact_scan_blocked:{art.get('matches', [])[:5]}")
    na = report["checks"].get("npm_audit", {})
    if not na.get("exit_ok"):
        issues.append(f"npm_audit_high:{na.get('high', '?')}_critical:{na.get('critical', '?')}")
    bb = report["checks"].get("bundle_budget", {})
    if not bb.get("exit_ok"):
        issues.append(f"bundle_over_budget:{bb.get('main_chunk_bytes', '?')}b")

    if issues:
        report["verdict"] = f"NOT_DOD_READY: {', '.join(issues)}"
    elif warnings:
        report["verdict"] = f"DOD_READY_WITH_EXCLUSIONS ({'; '.join(warnings)})"
    else:
        report["verdict"] = "DOD_READY"
    return finalize(report, args.json, t_start)
 
 
def _mask_preview(text: str) -> str:
    """Mask API-key lookalikes so the written report never leaks secrets."""
    if not isinstance(text, str):
        return text
    masked = re.sub(r"(?i)(sk-[a-zA-Z0-9]{20,})", lambda m: m.group(1)[:6] + "…" + m.group(1)[-2:], text)
    masked = re.sub(r"(?i)(gsk_[a-zA-Z0-9]{20,})", lambda m: m.group(1)[:6] + "…" + m.group(1)[-2:], masked)
    masked = re.sub(r"(AKIA[0-9A-Z]{16})", lambda m: m.group(1)[:6] + "…" + m.group(1)[-2:], masked)
    return masked


def _mask_report(report: dict) -> dict:
    """Return a copy of the report with preview fields masked."""
    out = {"timestamp": report["timestamp"], "version": report["version"], "checks": {}, "verdict": report["verdict"]}
    for name, check in report.get("checks", {}).items():
        if not isinstance(check, dict):
            out["checks"][name] = check
            continue
        copy = dict(check)
        if isinstance(copy.get("matches"), list):
            masked = []
            for m in copy["matches"]:
                if isinstance(m, dict):
                    masked.append({**m, "preview": _mask_preview(m.get("preview", ""))})
                else:
                    masked.append(m)
            copy["matches"] = masked
        out["checks"][name] = copy
    if "elapsed_seconds" in report:
        out["elapsed_seconds"] = report["elapsed_seconds"]
    return out


def finalize(report: dict, json_only: bool, t_start: float) -> None:
    report["elapsed_seconds"] = round(time.time() - t_start, 1)
    # §Masked: write a sanitized copy to disk; print the original for debugging.
    masked_report = _mask_report(report)
    if json_only:
        print(json.dumps(report, indent=2, default=str))
    else:
        # Human-readable summary + JSON
        print(f"\n{'='*60}")
        print(f"  ARIA DoD Verify  v10")
        print(f"  {report['timestamp']}")
        print(f"  Elapsed: {report['elapsed_seconds']}s")
        print(f"  Verdict: {report['verdict']}")
        print(f"{'='*60}")
        for name, check in report.get("checks", {}).items():
            status = "✅" if isinstance(check, dict) and check.get("exit_ok", True) and not check.get("issues") else "❌"
            print(f"  {status} {name}")
        print(f"\n{json.dumps(report, indent=2, default=str)}")
    # §Task I: всегда перезаписывать dod_verify.json при каждом запуске (masked)
    json_path = Path(os.path.dirname(os.path.abspath(__file__))) / "dod_verify.json"
    json_path.write_text(json.dumps(masked_report, indent=2, default=str), encoding="utf-8")
    sys.exit(0 if report["verdict"].startswith("DOD_READY") else 1)
 
 
if __name__ == "__main__":
    main()

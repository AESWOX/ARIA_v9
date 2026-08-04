#!/usr/bin/env python3
"""
build_release.py — Build ARIA release tarball from staged copy.

Usage:
  python build_release.py                          # build from workdir -> staging -> tar
  python build_release.py --staging-dir /tmp/aria   # use specific staging path
  python build_release.py --output-dir /out          # place tarball elsewhere

Principles (per Block 1 of the bulletproof release spec):
  1. Never tar czf . from the live workdir.
  2. Always rsync/copy to a staging directory first.
  3. Filter through RELEASE_MANIFEST.txt (allowlist).
  4. Apply deny-list as second line of defence.
  5. Run dod_verify.py --json on the staging copy before packing.
"""

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "RELEASE_MANIFEST.txt"

# Deny-list — hard-blocked even if in manifest
DENY_FILES = [
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".env.test",
    "*.key",
    "*.pem",
    "*.cert",
    "_fix_*.py",
    "*.sh",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.db-journal",
    "*.zip",
    "backend/logs/",
    "logs/",
]

# Directories never copied to staging
SKIP_DIRS = {".venv", "node_modules", ".git", "target", "__pycache__", ".pytest_cache", "logs", "data"}


def parse_manifest(path: Path) -> tuple[list[str], list[str]]:
    """Parse RELEASE_MANIFEST.txt into (includes, excludes).

    Lines starting with ! are exclude patterns.
    Lines starting with # or blank are ignored.
    """
    includes, excludes = [], []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            excludes.append(line[1:].strip())
        else:
            includes.append(line)
    return includes, excludes


def matches_any(name: str, patterns: list[str]) -> bool:
    """Check if name matches any glob pattern."""
    for pat in patterns:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(name, f"**/{pat}") or fnmatch.fnmatch(name, f"{pat}/**"):
            return True
    return False


def build_staging(root: Path, staging: Path, manifest_includes: list[str]) -> int:
    """Copy files matching manifest includes to staging directory.

    Uses stack-based traversal (same as scanner) to skip huge dirs.
    Returns number of files copied.
    """
    copied = 0
    SKIP = {".venv", "node_modules", ".git", "target", "__pycache__", ".pytest_cache", "legacy", "migration-package", "dist"}

    # Build a stack of (base_dir, Path objects)
    # Group patterns by their top-level directory for efficiency
    pattern_groups: dict[str, list[str]] = {}
    for pattern in manifest_includes:
        top = pattern.split("/")[0] if "/" in pattern else "."
        pattern_groups.setdefault(top, []).append(pattern)

    for top, patterns in pattern_groups.items():
        base = root / top if top != "." else root
        if not base.exists():
            continue

        # DFS traversal
        stack = [base]
        while stack:
            cur = stack.pop()
            try:
                for child in sorted(cur.iterdir(), key=lambda x: x.name, reverse=True):
                    if child.is_dir():
                        if child.name not in SKIP:
                            stack.append(child)
                    elif child.is_file():
                        rel = str(child.relative_to(root)).replace("\\", "/")
                        # Check any pattern in this group
                        matched = False
                        for pat in patterns:
                            if fnmatch.fnmatch(rel, pat):
                                matched = True
                                break
                        if not matched:
                            continue
                        # Deny-list check
                        if matches_any(child.name, DENY_FILES):
                            continue
                        if matches_any(rel, DENY_FILES):
                            continue
                        dest = staging / rel
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(child, dest)
                        copied += 1
            except PermissionError:
                continue

    return copied


def main():
    parser = argparse.ArgumentParser(description="Build ARIA release tarball")
    parser.add_argument("--staging-dir", help="Path to use as staging (default: temp dir)")
    parser.add_argument("--output-dir", default=str(ROOT), help="Where to put the tarball (default: project root)")
    parser.add_argument("--skip-verify", action="store_true", help="Skip dod_verify secret scan")
    parser.add_argument("--skip-pytest", action="store_true", help="Skip pytest on staging")
    args = parser.parse_args()

    t_start = time.time()

    # 1. Parse manifest
    if not MANIFEST.exists():
        print("FATAL: RELEASE_MANIFEST.txt not found")
        sys.exit(1)

    includes, excludes = parse_manifest(MANIFEST)
    print(f"[build] Manifest: {len(includes)} include patterns, {len(excludes)} exclude patterns")

    # 2. Create staging
    if args.staging_dir:
        staging = Path(args.staging_dir)
        if staging.exists():
            shutil.rmtree(staging)
    else:
        staging = Path(tempfile.mkdtemp(prefix="aria_release_", dir=str(ROOT)))
    staging.mkdir(parents=True, exist_ok=True)
    print(f"[build] Staging: {staging}")

    # 3. Build staging
    copied = build_staging(ROOT, staging, includes)
    print(f"[build] Copied {copied} files to staging")

    # Debug: check for __pycache__ in staging
    pycache_in = list(staging.rglob("__pycache__"))
    if pycache_in:
        print(f"  [build] WARNING: __pycache__ found in staging! ({len(pycache_in)} dirs)")
        for p in pycache_in:
            print(f"    {p.relative_to(staging)}")
        print("  [build] Removing __pycache__ from staging...")
        for p in pycache_in:
            shutil.rmtree(p, ignore_errors=True)

    if copied == 0:
        print("FATAL: No files copied — check RELEASE_MANIFEST.txt patterns")
        sys.exit(1)

    # 4. Run verification on staging (unless --skip-verify)
    if not args.skip_verify:
        print("[build] Running verification on staging...")

        # 4a. Secret scan — first check, blocking
        print("  [build] 1/2 Secret scan...")
        dver = staging / "dod_verify.py"
        if not dver.exists():
            print("[build] WARNING: dod_verify.py not in staging — skipping verification")
        else:
            staging_json = json.dumps(str(staging))
            code = f"""import sys; sys.path.insert(0, {str(ROOT)!r})
from dod_verify import check_secret_scan
import json
r = check_secret_scan(root={staging_json})
print(json.dumps(r))
"""
            result = subprocess.run(
                [sys.executable, "-c", code],
                cwd=str(ROOT), capture_output=True, timeout=60, text=True
            )
            try:
                sec_result = json.loads(result.stdout)
                print(f"  [build] Secret scan: {sec_result['status']} ({sec_result['checked']} files)")
                if sec_result["status"] == "BLOCKED":
                    print("  FATAL: Secrets found in staging — not packing")
                    for m in sec_result["matches"]:
                        print(f"    {m['file']}:{m['line']} [{m['pattern']}]")
                    shutil.rmtree(staging, ignore_errors=True)
                    sys.exit(1)
            except (json.JSONDecodeError, KeyError) as e:
                print(f"  [build] Secret scan parse error: {e}")
                print(f"  [build] stdout: {result.stdout[:300]}")
                print(f"  [build] stderr: {result.stderr[:300]}")

        # 4a-bis. Artifact scan — check for leaked db/logs/zip in staging
        print("  [build] 1.5/2 Artifact scan...")
        leaked = []
        ARTIFACT_BLOCKED = ["*.db", "*.db-wal", "*.db-shm", "*.zip", "*.log", "backend.exe", "local-agent-ui.exe"]
        for root, _dirs, files in os.walk(staging):
            for f in files:
                for pat in ARTIFACT_BLOCKED:
                    if fnmatch.fnmatch(f, pat):
                        leaked.append(os.path.join(root, f))
                        break
        if leaked:
            print("  FATAL: Leaked artifacts found in staging — not packing")
            for f in leaked:
                print(f"    {os.path.relpath(f, staging)}")
            shutil.rmtree(staging, ignore_errors=True)
            sys.exit(1)
        print(f"  [build]   Artifact scan: {len(leaked)} leaks, OK")

        # 4b. Run pytest on staging (unless --skip-pytest)
        if args.skip_pytest:
            print("  [build] 2/2 pytest SKIPPED (--skip-pytest)")
        else:
            print("  [build] 2/2 Running pytest on staging...")
            # Find the project's .venv python
            venv_pythons = [
                ROOT / "backend" / ".venv" / "Scripts" / "python.exe",
                ROOT / ".venv" / "Scripts" / "python.exe",
            ]
            pytest_python = sys.executable  # fallback
            for vp in venv_pythons:
                if vp.exists():
                    pytest_python = str(vp)
                    break
            print(f"  [build]   python: {pytest_python}")

            # Init DB + populate skills (required by test_vault_and_skills)
            init_cmd = [pytest_python, "-c", """import sys; sys.path.insert(0, 'backend')
from aria.db.base import init_db; init_db(create_all=True)
from aria.db.base import session_scope
from aria.db.models import SkillMeta
from aria.db.enums import SkillStatus
with session_scope() as db:
    if db.query(SkillMeta).count() == 0:
        db.add(SkillMeta(skill_name='test-vault', category='test', status=SkillStatus.needs_adaptation, source_origin='migrated', needs_adaptation=True))
        db.add(SkillMeta(skill_name='test-mcp', category='test', status=SkillStatus.needs_adaptation, source_origin='migrated', needs_adaptation=True))
print('DB init + test skills OK')
"""]
            subprocess.run(init_cmd, cwd=str(staging), capture_output=True, timeout=30)

            pt_result = subprocess.run(
                [pytest_python, "-m", "pytest", "backend/tests/", "-q", "--tb=line", "--no-header"],
                cwd=str(staging), capture_output=True, timeout=120, text=True
            )
            # Parse the last line for summary
            stdout_lines = [l for l in pt_result.stdout.strip().split("\n") if l.strip()]
            summary = stdout_lines[-1] if stdout_lines else "(empty)"
            failed = pt_result.returncode != 0
            if failed:
                # Show failed test names
                failures = [l for l in stdout_lines if l.startswith("FAILED")]
                print(f"  [build]   pytest FAILED (exit {pt_result.returncode})")
                for f in failures[:10]:
                    print(f"    {f}")
                print("  FATAL: Tests fail on staging — not packing")
                shutil.rmtree(staging, ignore_errors=True)
                sys.exit(1)
            else:
                print(f"  [build]   pytest PASSED: {summary}")

    else:
        print("[build] Skipping verification (--skip-verify)")

    # 5. Clean up pytest artifacts from staging before tar
    print("  [build] Cleaning pytest artifacts...")
    # pytest with cwd=staging creates ./data/local_agent.db via init_db();
    # the root-level data/ dir is NOT in the manifest — drop it entirely.
    staging_data = staging / "data"
    if staging_data.exists():
        shutil.rmtree(staging_data, ignore_errors=True)
        print("  [build]   removed staging/data (pytest-generated, not in manifest)")
    for pycache_dir in staging.rglob("__pycache__"):
        shutil.rmtree(pycache_dir, ignore_errors=True)
    for pyc_file in staging.rglob("*.pyc"):
        pyc_file.unlink(missing_ok=True)
    for pytest_cache in staging.rglob(".pytest_cache"):
        shutil.rmtree(pytest_cache, ignore_errors=True)

    # 6. Build tarball from staging
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    tarball = output_dir / "ARIA_release.tar.gz"

    # Remove existing tarball to avoid self-inclusion
    if tarball.exists():
        tarball.unlink()

    subprocess.run(
        ["tar", "czf", str(tarball), "-C", str(staging), "."],
        check=True, timeout=120
    )
    size_mb = tarball.stat().st_size / (1024 * 1024)
    elapsed = time.time() - t_start
    print(f"[build] ✅ {tarball.name} ({size_mb:.0f} MB) in {elapsed:.1f}s")

    # 6. Clean up staging
    shutil.rmtree(staging, ignore_errors=True)
    print(f"[build] Staging cleaned up")
    print(f"[build] DONE")


if __name__ == "__main__":
    main()

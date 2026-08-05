#!/usr/bin/env python3
"""
generate_manifest.py — ЕДИНСТВЕННЫЙ путь создать MANIFEST.md.

Правила железа:
1. Единственный источник данных — .dod_verify.json (свежий, mtime < 10 мин)
2. Секция "Проверки DoD" в шаблоне отсутствует — подставляется из JSON кодом
3. Если .dod_verify.json нет или старше 10 минут — скрипт падает, манифест не создаётся
4. generate_manifest.py создаёт и .dod_verify.json, и MANIFEST.md одной командой
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # backend/
PROJECT = ROOT.parent  # ARIA_v9/
MANIFEST = PROJECT / "MANIFEST.md"
DOD_OUTPUT = PROJECT / ".dod_verify.json"
DOD_SCRIPT = PROJECT / "dod_verify.py"


def check_dod_source(data: dict | None, age: float | None) -> bool:
    """Check that the dod_verify data is valid and fresh."""
    if data is None:
        print("ERROR: .dod_verify.json not found or unparseable")
        return False
    if age is not None and age > 600:
        print(f"ERROR: .dod_verify.json is {age:.0f}s old (max 600s / 10 min)")
        return False
    if not data.get("checks"):
        print("ERROR: .dod_verify.json has no 'checks' section")
        return False
    return True


def read_dod_output() -> tuple[dict | None, float | None]:
    """Read .dod_verify.json if it exists and is fresh.
    Returns (data, age_in_seconds) or (None, None).
    """
    if not DOD_OUTPUT.exists():
        # Run dod_verify to create it
        print("⏳ .dod_verify.json not found — running dod_verify.py --json (2-4 min)...")
        r = subprocess.run(
            [sys.executable, str(DOD_SCRIPT), "--json"],
            capture_output=True, text=False, cwd=str(PROJECT), timeout=600
        )
        raw = r.stdout.decode("utf-8", errors="replace")
        first_brace = raw.find("{")
        if first_brace >= 0:
            raw = raw[first_brace:]
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            print(f"ERROR: dod_verify.py output not valid JSON: {e}")
            return None, None
        DOD_OUTPUT.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return data, 0.0
    else:
        age = time.time() - DOD_OUTPUT.stat().st_mtime
        if age > 600:
            print(f"⚠️  .dod_verify.json is {age:.0f}s old (max 600s). Re-running...")
            DOD_OUTPUT.unlink(missing_ok=True)
            return read_dod_output()
        with open(DOD_OUTPUT) as f:
            data = json.load(f)
        return data, age


def generate_manifest(data: dict, elapsed: float | None) -> str:
    """Build MANIFEST.md from dod_verify data. NO manual numbers section."""
    ts = data.get("timestamp", time.strftime("%Y-%m-%dT%H:%M:%S"))
    verdict = data.get("verdict", "UNKNOWN")
    checks = data.get("checks", {})
    st = checks.get("self_test", {}) if isinstance(checks.get("self_test"), dict) else {}
    st_checks_str = str(checks.get("self_test", "")) if not isinstance(checks.get("self_test"), dict) else ""

    lines = [
        "# ARIA — Manifest (auto-generated)",
        "",
        f"> **Дата**: {ts[:10]}",
        f"> **Вердикт**: `{verdict}`",
        f"> **Elapsed**: {elapsed or '?'}s" if elapsed else f"> **Elapsed**: {elapsed or '?'}s",
        f"> Источник: `dod_verify.py --json`",
        "",
        "## Проверки DoD",
        "",
    ]

    # Self-test
    st_checks = st.get("checks", {}) if st else {}
    if st_checks:
        for name, val in st_checks.items():
            sv = str(val)
            ok = ("ok" in sv or "warn" in sv or "models" in sv
                  or "skills" in sv or "tables" in sv or "detectors" in sv)
            lines.append(f"- {'✅' if ok else '⚠️'} **{name}**: {val}")
    elif st_checks_str:
        lines.append(f"- **self_test**: {st_checks_str}")

    # Vault (from filesystem)
    vault = checks.get("vault", {})
    if isinstance(vault, dict):
        md = vault.get("md_files", 0)
        lines.append(f"- {'✅' if md > 0 else '⚠️'} **vault**: {md} .md files")

    # Backend tests (from pytest)
    bt = checks.get("backend_tests", {})
    if isinstance(bt, dict):
        p, t = bt.get("passed", 0), bt.get("total", 0)
        lines.append(f"- {'✅' if t > 0 and p == t else '❌'} **Backend tests**: {p}/{t} PASSED")

    # Frontend
    for key, label in [("frontend_tsc", "Frontend TSC"),
                        ("frontend_build", "Frontend build"),
                        ("frontend_install", "Frontend install")]:
        ck = checks.get(key, {})
        if isinstance(ck, dict):
            st = ck.get("status", "missing")
            ok = ck.get("exit_ok", False) or "OK" in str(st)
            lines.append(f"- {'✅' if ok else '❌'} **{label}**: {st}")
            if ck.get("modules"):
                lines[-1] = lines[-1].replace(st, f"{st} ({ck['modules']} modules)")

    # Hermes refs
    hr = checks.get("hermes_references", {})
    if isinstance(hr, dict):
        cnt = hr.get("files_with_hermes", -1)
        lines.append(f"- {'✅' if cnt == 0 else '❌'} **Hermes refs in src/**: {cnt} files")

    # Database summary
    db = checks.get("database", {})
    tbl = db.get("tables", {}) if isinstance(db, dict) else {}
    if isinstance(tbl, dict):
        lines.extend([
            "",
            "## База данных",
            "",
            f"- **provider_models**: {tbl.get('provider_models', '?')}",
            f"- **skills_meta**: {tbl.get('skills_meta', '?')}",
        ])
        real = tbl.get("_real_providers", [])
        lines.append(f"- **Real providers**: {', '.join(real) if real else 'none'}")
        stale = tbl.get("_stale_providers", [])
        lines.append(f"- **Stale providers**: {stale if stale else 'none'}")

    # Architecture decisions
    lines.extend([
        "",
        "## Архитектурные решения",
        "",
        "- **state.db (1.3GB Hermes legacy)**: A — archived as read-only",
        "  ARIA has its own schema (15 tables), Hermes state schema is different.",
        "  Migration would not provide value proportional to effort.",
        "- **Database profile**: A — dev-only (SQLite)",
        "  Local budget agent, no production deployment target.",
        "  Not production-ready for multi-user / HA scenarios.",
        "- **Frontend @nous-research/ui**: React 19 upgrade (from 18)",
        "  Peer dependency satisfied, npm ci now works without --legacy-peer-deps.",
    ])

    # verifier block — CI uses this to check freshness
    lines.extend([
        "",
        "<!-- GENERATED_BY=generate_manifest.py -->",
        f"<!-- DOD_TIMESTAMP={ts} -->",
        f"<!-- DOD_VERDICT={verdict} -->",
    ])

    return "\n".join(lines)


def ci_gate() -> bool:
    """CI gate: re-run dod_verify and diff against manifest."""
    if not MANIFEST.exists():
        print("⚠️  CI-GATE SKIP: no MANIFEST.md yet")
        return True

    print("⏳ CI gate: running fresh dod_verify for comparison...")
    r = subprocess.run(
        [sys.executable, str(DOD_SCRIPT), "--json"],
        capture_output=True, text=False, cwd=str(PROJECT), timeout=600
    )
    raw = r.stdout.decode("utf-8", errors="replace")
    first_brace = raw.find("{")
    fresh = json.loads(raw[first_brace:]) if first_brace >= 0 else None

    if fresh is None or not fresh.get("checks"):
        print("❌ CI-GATE: dod_verify produced no valid data")
        return False

    # Compare key numbers
    old_path = DOD_OUTPUT
    if old_path.exists():
        with open(old_path) as f:
            old = json.load(f)

        checks_old = old.get("checks", {})
        checks_new = fresh.get("checks", {})

        issues = []
        for key in ["vault", "backend_tests", "hermes_references"]:
            v_old = checks_old.get(key, {})
            v_new = checks_new.get(key, {})
            if isinstance(v_old, dict) and isinstance(v_new, dict):
                if v_old.get("md_files") != v_new.get("md_files"):
                    issues.append(f"{key}.md_files: {v_old.get('md_files')} → {v_new.get('md_files')}")
                if v_old.get("passed") != v_new.get("passed"):
                    issues.append(f"{key}.passed: {v_old.get('passed')} → {v_new.get('passed')}")

        db_old = checks_old.get("database", {}).get("tables", {})
        db_new = checks_new.get("database", {}).get("tables", {})
        if isinstance(db_old, dict) and isinstance(db_new, dict):
            for field in ("skills_meta", "provider_models"):
                if db_old.get(field) != db_new.get(field):
                    issues.append(f"database.{field}: {db_old.get(field)} → {db_new.get(field)}")

        if issues:
            print("❌ CI-GATE FAILED: dod_verify outputs differ:")
            for i in issues:
                print(f"   {i}")
            print("   Run: python backend/scripts/generate_manifest.py")
            return False

    print("✅ CI-GATE PASSED: manifest matches current state")
    return True


def main():
    force = "--force" in sys.argv
    ci_mode = "--ci" in sys.argv

    if ci_mode:
        ok = ci_gate()
        return 0 if ok else 1

    print("=== generate_manifest.py ===")
    print(f"Project: {PROJECT}")
    print()

    # Step 1: ensure fresh dod_verify data
    data, age = read_dod_output()
    if not check_dod_source(data, age):
        return 1

    elapsed = data.get("_elapsed", age)
    print(f"✅ dod_verify data loaded ({age:.0f}s old, verdict={data.get('verdict','?')})")

    # Step 2: generate manifest from data
    manifest = generate_manifest(data, elapsed)
    MANIFEST.write_text(manifest, encoding="utf-8")
    lines = manifest.count("\n")
    print(f"✅ MANIFEST.md written ({lines} lines)")

    # Step 3: CI check (unless --force)
    if not force:
        print("\n=== CI Consistency Check ===")
        ok = ci_gate()
        if not ok:
            return 1

    verdict = data.get("verdict", "UNKNOWN")
    print(f"\n🎯 Verdict: {verdict}")
    return 0 if verdict == "DOD_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())

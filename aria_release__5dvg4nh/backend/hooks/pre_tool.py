#!/usr/bin/env python3
"""
PreToolUse hook — enforcement gate requiring HUMAN approval for each edit.

HOW IT WORKS:
  1. Agent calls pre_tool.py <op> <path>
  2. pre_tool.py generates a random APPROVAL CODE and writes
     `.hermes/approval/<basename>.pending` with the diff + code
  3. pre_tool.py exits BLOCKED with the code in stdout
  4. Agent REPORTS to user: "BLOCKED. Run: python backend/hooks/approve.py <basename> <CODE>"
  5. USER runs approve.py → consumes `.pending`, writes `.approved`
  6. Agent retries pre_tool.py → finds `.approved` → PASS → exit 0

ENFORCEMENT:
  - Agent CANNOT call approve.py itself (system prompt enforces: "You MUST NOT")
  - .pending file consumed on use — one-shot approval
  - All approvals logged in backend/.hermes/approval/ with timestamp
"""

import sys
import os
import hashlib
import secrets
import time
import json
import difflib

# ARIA root — relative to this script location (backend/hooks/ -> backend/)
ARIA_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
HERMES_DIR = os.path.join(ARIA_ROOT, ".hermes")
APPROVAL_DIR = os.path.join(HERMES_DIR, "approval")
BAK_DIR = os.path.join(HERMES_DIR, "bak")

PROTECTED = [
    "backend/aria/core/loop.py",
    "backend/aria/tools/registry.py",
    "backend/aria/config.py",
    "desktop/src/lib/api.ts",
]


def _ensure_dirs():
    os.makedirs(APPROVAL_DIR, exist_ok=True)
    os.makedirs(BAK_DIR, exist_ok=True)


def _diff_summary(basename):
    """Generate a diff summary comparing .bak to current file."""
    baks = sorted(
        [f for f in os.listdir(BAK_DIR) if f.startswith(basename + ".") and f.endswith(".bak")],
        key=lambda x: os.path.getmtime(os.path.join(BAK_DIR, x)),
    )
    if not baks:
        return "(no .bak found)"

    bak_path = os.path.join(BAK_DIR, baks[-1])
    current_paths = [
        os.path.join(ARIA_ROOT, p) for p in PROTECTED if os.path.basename(p) == basename
    ]
    if not current_paths:
        return "(no matching protected file)"

    cur_path = current_paths[0]
    if not os.path.isfile(bak_path) or not os.path.isfile(cur_path):
        return "(file missing)"

    try:
        with open(bak_path) as f:
            bak_lines = f.readlines()
        with open(cur_path) as f:
            cur_lines = f.readlines()

        diff = list(difflib.unified_diff(bak_lines, cur_lines, fromfile=".bak", tofile="current", n=3))
        if not diff:
            return "(no changes)"
        lines = "".join(diff[:12])
        if len(diff) > 12:
            lines += f"\n... ({len(diff)} total diff lines)"
        return lines.strip()
    except Exception as e:
        return f"(diff error: {e})"


def main():
    if len(sys.argv) < 3:
        print("Usage: python backend/hooks/pre_tool.py <op> <path>")
        sys.exit(2)

    op = sys.argv[1]
    path = sys.argv[2]

    if op not in ("patch", "write_file"):
        sys.exit(0)

    basename = os.path.basename(path)
    if basename not in [os.path.basename(p) for p in PROTECTED]:
        sys.exit(0)

    _ensure_dirs()

    pending_path = os.path.join(APPROVAL_DIR, f"{basename}.pending")
    approved_path = os.path.join(APPROVAL_DIR, f"{basename}.approved")

    # Check if already approved
    if os.path.isfile(approved_path):
        try:
            with open(approved_path) as f:
                payload = json.load(f)
            if payload.get("consumed", False):
                os.remove(approved_path)
                sys.exit(0)
            payload["consumed"] = True
            with open(approved_path, "w") as f:
                json.dump(payload, f)
            sys.exit(0)
        except Exception:
            os.remove(approved_path)
            sys.exit(0)

    # Not approved — generate approval code
    code = secrets.token_hex(8)
    diff_text = _diff_summary(basename)

    payload = {
        "basename": basename,
        "op": op,
        "path": path,
        "code": code,
        "created_at": time.time(),
        "diff": diff_text,
    }

    with open(pending_path, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"BLOCKED: edit to protected file {basename}")
    print(f"  Operation: {op}")
    print(f"  Path: {path}")
    print(f"  Diff preview:\n{diff_text}")
    print(f"")
    print(f"  To approve, run:")
    print(f"    python backend/hooks/approve.py {basename} {code}")
    sys.exit(1)


if __name__ == "__main__":
    main()

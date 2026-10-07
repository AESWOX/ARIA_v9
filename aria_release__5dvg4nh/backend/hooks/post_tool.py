#!/usr/bin/env python3
"""
PostToolUse hook — auto-commit after write_file/patch to tracked file types.

Also runs regression detection: compares the current file against the
newest .bak backup and warns if previously-added instrumentation lines
went missing.

Does NOT block the commit — silent regression is worse than a false
alarm, so it only warns.  The caller (agent) sees the warning and can
decide manually.
"""

import subprocess
import sys
import os
import hashlib

ARIA_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
BAK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".hermes", "bak")

_AUTOCOMMIT_EXTENSIONS = frozenset({".py", ".yaml", ".yml", ".md"})

# Instrumentation patterns that should NEVER be silently removed by an edit
_INSTRUMENTATION_MARKERS = [
    "init_db(",
    "session_scope()",
    "event_bus.emit",
    "refresh_provider_models_job",
    "seed_database()",
    "token_store.issue()",
]


def _check_regression(path: str):
    """Compare post-edit file against ALL .baks for instrumentation removal."""
    basename = os.path.basename(path)
    if not os.path.isdir(BAK_DIR):
        return

    baks = sorted(
        [f for f in os.listdir(BAK_DIR) if f.startswith(basename + ".") and f.endswith(".bak")],
        key=lambda f: os.path.getmtime(os.path.join(BAK_DIR, f)),
    )
    if not baks:
        return
    if not os.path.isfile(path):
        return

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        cur_content = f.read()

    for marker in _INSTRUMENTATION_MARKERS:
        any_bak_has = False
        for bak_name in baks:
            bak_path = os.path.join(BAK_DIR, bak_name)
            with open(bak_path, "r", encoding="utf-8", errors="replace") as f:
                if marker in f.read():
                    any_bak_has = True
                    break
        marker_in_cur = marker in cur_content

        if any_bak_has and not marker_in_cur:
            print(f"⚠️  REGRESSION WARNING: marker '{marker}' was in .bak but is MISSING in current file")
            print(f"    Checked {len(baks)} .bak files, last: {baks[-1]}")
            print(f"    Path: {path}")
    return


def _git_auto_commit(path: str, reason: str):
    if not any(path.endswith(ext) for ext in _AUTOCOMMIT_EXTENSIONS):
        return
    git_dir = os.path.join(ARIA_ROOT, ".git")
    if not os.path.isdir(git_dir):
        return

    # Check staged
    r = subprocess.run(["git", "diff", "--cached", "--name-only"], capture_output=True, text=True, cwd=ARIA_ROOT, timeout=10)
    if r.stdout.strip():
        return

    # Stage and commit
    rel_path = os.path.relpath(path, ARIA_ROOT)
    r = subprocess.run(["git", "add", rel_path], capture_output=True, text=True, cwd=ARIA_ROOT, timeout=10)
    if r.returncode != 0:
        return

    msg = f"auto({reason}): {os.path.basename(path)}"
    r = subprocess.run(["git", "commit", "-m", msg, "--no-verify"], capture_output=True, text=True, cwd=ARIA_ROOT, timeout=10)
    if r.returncode == 0:
        print(f"Auto-commit: {msg}")
    elif "nothing to commit" not in r.stderr and "nothing to commit" not in r.stdout:
        print(f"Auto-commit skipped: {r.stderr[:200]}")


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    reason = sys.argv[2] if len(sys.argv) > 2 else "post_tool"

    if not path or not os.path.isfile(path):
        sys.exit(0)

    _check_regression(path)
    _git_auto_commit(path, reason)
    sys.exit(0)


if __name__ == "__main__":
    main()

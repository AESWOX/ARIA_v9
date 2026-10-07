#!/usr/bin/env python3
"""
Protect hook — critical file guard.

Creates a .bak copy before any patch/write_file to protected files.
Prints a diff summary for human review.

PROTECTED FILES:
  - backend/aria/core/loop.py       — event loop
  - backend/aria/tools/registry.py  — tool registry
  - backend/aria/config.py          — configuration
  - desktop/src/lib/api.ts          — API client
"""

import sys
import os
import shutil
import difflib
import datetime

ARIA_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
HERMES_DIR = os.path.join(ARIA_ROOT, ".hermes")
BAK_DIR = os.path.join(HERMES_DIR, "bak")

PROTECTED = [
    "backend/aria/core/loop.py",
    "backend/aria/tools/registry.py",
    "backend/aria/config.py",
    "desktop/src/lib/api.ts",
]


def _ensure_bak_dir():
    bak_dir = os.path.normpath(BAK_DIR)
    os.makedirs(bak_dir, exist_ok=True)
    return bak_dir


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    if not path:
        sys.exit(0)

    basename = os.path.basename(path)
    protected_basenames = [os.path.basename(p) for p in PROTECTED]
    if basename not in protected_basenames:
        sys.exit(0)

    if not os.path.isfile(path):
        sys.exit(0)

    bak_dir = _ensure_bak_dir()
    _ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    bak_path = os.path.join(bak_dir, f"{basename}.{_ts}.bak")

    shutil.copy2(path, bak_path)
    print(f"Protect hook: backed up {basename} -> {bak_path}")

    orig_size = os.path.getsize(path)
    print(f"Protect hook: {basename} size={orig_size:,} bytes")
    print(f"Protect hook: ALWAYS review the diff before applying!")

    sys.exit(0)


if __name__ == "__main__":
    main()

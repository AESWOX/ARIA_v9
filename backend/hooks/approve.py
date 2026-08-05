#!/usr/bin/env python3
"""
approve.py — approve a pending protected-file edit.

Called by the USER (not the agent) in response to pre_tool.py's BLOCKED message.

Usage:
    python backend/hooks/approve.py <basename> <code>

The code must match the code in `.hermes/approval/<basename>.pending`.
On success, writes `.hermes/approval/<basename>.approved` which pre_tool.py
checks on retry.

The .pending file is CONSUMED (deleted) after reading, so each approval
code is one-shot.
"""

import sys
import os
import json
import time

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
HERMES_DIR = os.path.normpath(os.path.join(HOOKS_DIR, "..", ".hermes"))
APPROVAL_DIR = os.path.join(HERMES_DIR, "approval")


def main():
    if len(sys.argv) < 3:
        print("Usage: python backend/hooks/approve.py <basename> <code>")
        sys.exit(1)

    basename = sys.argv[1]
    user_code = sys.argv[2].strip()

    pending_path = os.path.join(APPROVAL_DIR, f"{basename}.pending")
    approved_path = os.path.join(APPROVAL_DIR, f"{basename}.approved")

    if not os.path.isfile(pending_path):
        print(f"ERROR: no pending approval for {basename}")
        print(f"  Expected: {pending_path}")
        print(f"  Run pre_tool.py first to generate a pending request.")
        sys.exit(1)

    with open(pending_path) as f:
        payload = json.load(f)

    expected_code = payload.get("code", "")
    if user_code != expected_code:
        print(f"ERROR: code mismatch for {basename}")
        print(f"  Expected: {expected_code}")
        print(f"  Got:      {user_code}")
        sys.exit(1)

    # Code matches — write approval
    payload["approved_at"] = time.time()
    os.makedirs(os.path.dirname(approved_path), exist_ok=True)
    with open(approved_path, "w") as f:
        json.dump(payload, f)

    # Consume pending
    os.remove(pending_path)

    print(f"✅ Approved: {basename}")
    print(f"  Code: {user_code}")
    print(f"  Run pre_tool.py again to proceed.")


if __name__ == "__main__":
    main()

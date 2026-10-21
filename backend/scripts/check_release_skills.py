"""Fail if an allowlisted skill contains something that looks like a secret or an internal address.

Run before building the public installer:  python scripts/check_release_skills.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ALLOW = [ln.strip() for ln in (ROOT / "release_skills.txt").read_text(encoding="utf-8").splitlines()
         if ln.strip() and not ln.lstrip().startswith("#")]
PATTERNS = {
    "api key assignment": re.compile(r"(?i)(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{16,}"),
    "known key prefix": re.compile(r"\b(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_\-]{20,}|gsk_[A-Za-z0-9]{20,})"),
    "ip address": re.compile(r"\b(?!127\.|0\.0\.0\.0|10\.0\.0\.)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    "windows user path": re.compile(r"(?i)[A-Z]:\\Users\\[^\\\s]+"),
}


def main() -> int:
    skills = ROOT / "data" / "skills"
    bad = 0
    for name in ALLOW:
        folder = skills / name
        if not folder.is_dir():
            print(f"[skip] {name}: not present on this machine")
            continue
        for f in folder.rglob("*"):
            if not f.is_file() or f.stat().st_size > 2_000_000:
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for label, rx in PATTERNS.items():
                m = rx.search(text)
                if m:
                    bad += 1
                    print(f"[FAIL] {f.relative_to(ROOT)}: {label}")
    print("OK: allowlisted skills are clean" if not bad else f"{bad} finding(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

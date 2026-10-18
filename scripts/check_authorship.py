#!/usr/bin/env python3
"""Проверка авторства: в диапазоне коммитов только разрешённые автор/коммиттер и нет трейлера Co-authored-by.

Использование: python scripts/check_authorship.py [<диапазон git>]
По умолчанию проверяется origin/main..HEAD. Разрешённые адреса: .github/allowed_authors.txt.
Код возврата 1, если найдено нарушение.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRAILER = re.compile(r"^\s*co-authored-by\s*:", re.I | re.M)


def allowed(root: Path = ROOT) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {"author": set(), "committer": set()}
    for line in (root / ".github" / "allowed_authors.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        kind, email = line.split("|", 1)
        out.setdefault(kind.strip(), set()).add(email.strip().lower())
    return out


def check(root: Path, rng: str, ok: dict[str, set[str]] | None = None) -> list[str]:
    """Список нарушений в диапазоне коммитов `rng` репозитория `root` (пустой — всё в порядке)."""
    ok = ok or allowed(root)
    fmt = "%H%x1f%ae%x1f%ce%x1f%B%x1e"
    raw = subprocess.run(
        ["git", "log", rng, f"--format={fmt}"], cwd=root, capture_output=True, text=True, encoding="utf-8", check=True
    ).stdout
    bad: list[str] = []
    for rec in raw.split("\x1e"):
        if not rec.strip():
            continue
        sha, ae, ce, body = rec.strip("\n").split("\x1f", 3)
        if ae.lower() not in ok["author"]:
            bad.append(f"{sha[:10]}: автор {ae} не в списке")
        if ce.lower() not in ok["committer"]:
            bad.append(f"{sha[:10]}: коммиттер {ce} не в списке")
        if TRAILER.search(body):
            bad.append(f"{sha[:10]}: в сообщении есть Co-authored-by")
    return bad


def main() -> int:
    rng = sys.argv[1] if len(sys.argv) > 1 else "origin/main..HEAD"
    bad = check(ROOT, rng)
    if bad:
        print("Нарушения авторства:\n  " + "\n  ".join(bad))
        return 1
    print(f"Авторство в порядке ({rng}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

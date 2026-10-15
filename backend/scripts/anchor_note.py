"""Дописывает в ANCHOR.md строку про последний rebuild_and_smoke: коммит, итог, число тестов, упавшие фазы.

Нужен, чтобы «подтверждено на Windows» в якоре писала сама машина, а не память. Запуск из корня репозитория:
    backend\\.venv\\Scripts\\python.exe backend\\scripts\\anchor_note.py
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MARK = "## Журнал прогонов"


def main() -> int:
    runs = sorted((ROOT / "reports").glob("rebuild_*/summary.json"), key=lambda p: p.stat().st_mtime)
    if not runs:
        print("нет reports/rebuild_*/summary.json: сначала запустите rebuild_and_smoke.py")
        return 1
    summary = json.loads(runs[-1].read_text(encoding="utf-8"))
    results = summary.get("results", [])
    failed = [r["phase"] for r in results if r.get("status") != "PASS"]
    tests = next((r.get("detail", "") for r in results if r.get("phase") == "tests"), "")
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip())
    verdict = "ALL GREEN" if not failed else "FAIL: " + ", ".join(failed)
    line = (
        f"- {dt.datetime.now():%Y-%m-%d %H:%M} · `{head}`{' (дерево грязное)' if dirty else ''} · {verdict} · "
        f"pytest: {tests or 'нет данных'} · отчёт `{runs[-1].parent.name}`"
    )
    anchor = ROOT / "ANCHOR.md"
    text = anchor.read_text(encoding="utf-8")
    if MARK not in text:
        text = text.rstrip() + f"\n\n{MARK}\n"
    anchor.write_text(text.rstrip() + "\n" + line + "\n", encoding="utf-8")
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

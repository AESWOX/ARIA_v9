"""Live check of your Gemini keys against the models ARIA is configured to use.

    python scripts/check_gemini_keys.py

For every key in GEMINI_API_KEYS / GEMINI_API_KEY it (1) lists the models the
key can see, (2) sends a 1-token chat to the configured flash and pro models.
Exit code 0 only if at least one key answers on the flash model.

Free-tier quota is per Google Cloud PROJECT, not per key: several keys created
in the same project share one quota. Create each key in a different project.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aria import paths  # noqa: E402


def _load_env() -> None:
    env = paths.env_file()
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def check(keys: list[str], base_url: str, models: dict[str, str], client: httpx.Client) -> dict:
    report: dict = {}
    for key in keys:
        tag = f"...{key[-4:]}"
        entry: dict = {"visible_models": None, "chat": {}}
        try:
            r = client.get(f"{base_url}/models", headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 200:
                data = r.json()
                items = data.get("data") or data.get("models") or []
                entry["visible_models"] = sorted(
                    str(i.get("id") or i.get("name", "")).removeprefix("models/") for i in items if isinstance(i, dict)
                )
            else:
                entry["list_error"] = f"HTTP {r.status_code}: {r.text[:160]}"
        except httpx.HTTPError as exc:
            entry["list_error"] = repr(exc)
        for label, model in models.items():
            try:
                r = client.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {key}"},
                    json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5},
                )
                entry["chat"][label] = "OK" if r.status_code == 200 else f"HTTP {r.status_code}: {r.text[:160]}"
            except httpx.HTTPError as exc:
                entry["chat"][label] = repr(exc)
        report[tag] = entry
    return report


def main() -> int:
    _load_env()
    raw = os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY") or ""
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        print(f"No GEMINI_API_KEYS / GEMINI_API_KEY found (looked in env and {paths.env_file()})")
        return 2
    base = os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")
    models = {
        "flash": os.environ.get("GEMINI_FLASH_MODEL", "gemini-2.5-flash"),
        "pro": os.environ.get("GEMINI_PRO_MODEL", "gemini-2.5-flash"),
    }
    with httpx.Client(timeout=30) as client:
        report = check(keys, base, models, client)
    ok = False
    for tag, entry in report.items():
        print(f"key {tag}")
        print("  models visible:", ", ".join(entry["visible_models"] or []) or entry.get("list_error", "n/a"))
        for label, res in entry["chat"].items():
            print(f"  chat[{label}={models[label]}]: {res}")
        ok = ok or entry["chat"].get("flash") == "OK"
    print("\nNOTE: keys from the same Google project share ONE free quota.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

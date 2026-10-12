"""Автоподбор цепочек моделей Gemini: список моделей ключа -> проверка каждой -> цепочки pro / flash / compression.

Запуск (PowerShell, из корня репозитория):
  .\\backend\\.venv\\Scripts\\python.exe backend\\scripts\\gemini_autoconfig.py              # проверить и сохранить в кэш
  .\\backend\\.venv\\Scripts\\python.exe backend\\scripts\\gemini_autoconfig.py --dry-run    # только показать
  .\\backend\\.venv\\Scripts\\python.exe backend\\scripts\\gemini_autoconfig.py --write-env  # + записать в .env явно

Ключи читаются из .env и НЕ печатаются. Проверка тратит по одному крошечному запросу на модель (до 12 штук).
Кэш (gemini_models.json) приложение подхватывает при запуске и при смене .env; явные GEMINI_*_MODELS главнее кэша.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aria import paths  # noqa: E402
from aria.llm import gemini_discovery as gd  # noqa: E402

ENV_VARS = {"pro": "GEMINI_PRO_MODELS", "flash": "GEMINI_FLASH_MODELS", "compression": "COMPRESSION_MODELS"}


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if match:
                out[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return out


def write_env(path: Path, updates: dict[str, str]) -> None:
    """Меняет/добавляет переменные, остальное не трогает; бэкап .bak; UTF-8 без BOM."""
    lines = path.read_text(encoding="utf-8-sig").splitlines() if path.exists() else []
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    for name, value in updates.items():
        pattern = re.compile(rf"^\s*{re.escape(name)}\s*=")
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = f"{name}={value}"
                break
        else:
            lines.append(f"{name}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", help=".env (по умолчанию %%APPDATA%%\\ARIA\\.env, если есть, иначе backend\\.env)")
    ap.add_argument("--no-probe", action="store_true", help="только список моделей, без проверочных запросов")
    ap.add_argument("--max-per-tier", type=int, default=gd.MAX_PER_TIER)
    ap.add_argument("--dry-run", action="store_true", help="ничего не сохранять")
    ap.add_argument("--write-env", action="store_true", help="записать цепочки в .env (иначе только кэш)")
    args = ap.parse_args(argv)

    user_env = paths.user_data_dir() / ".env"
    env_path = Path(args.env) if args.env else (user_env if user_env.exists() else paths.env_file())
    data_dir = env_path.parent if env_path.parent == paths.user_data_dir() else paths.data_dir()
    env = read_env(env_path)
    keys = [k.strip() for k in (env.get("GEMINI_API_KEYS") or env.get("GEMINI_API_KEY") or "").split(",") if k.strip()]
    base_url = env.get("GEMINI_BASE_URL") or gd.DEFAULT_BASE_URL
    print(f"env: {env_path}   ключей Gemini: {len(keys)}   (сами ключи не печатаются)")
    if not keys:
        print("FAIL: в .env нет GEMINI_API_KEYS")
        return 2

    def progress(tier: str, model: str, status: str, detail: str) -> None:
        print(f"  [{tier:5}] {model:36} {status:12} {detail}")

    if not args.no_probe:
        print("проверка моделей (по одному запросу на модель):")
    try:
        result = gd.discover(keys, base_url, probe=not args.no_probe, max_per_tier=args.max_per_tier, progress=progress)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: {exc}")
        return 3

    print("\nцепочки (порядок = порядок попыток, дальше вниз по качеству):")
    for kind in ("pro", "flash", "compression"):
        print(f"  {ENV_VARS[kind]:20} {','.join(result['chains'].get(kind, [])) or '(пусто: останется одна модель из .env)'}")
    if not result["chains"]:
        print("FAIL: не нашлось ни одной подходящей модели")
        return 4
    if args.dry_run:
        print("\ndry-run: ничего не сохранено")
        return 0
    path = gd.save_cache(result, data_dir)
    print(f"\nкэш сохранён: {path}")
    if args.write_env:
        write_env(env_path, {ENV_VARS[k]: ",".join(v) for k, v in result["chains"].items()})
        print(f".env обновлён (бэкап: {env_path.name}.bak)")
    print("Перезапустите приложение (закрыть полностью и открыть), чтобы цепочки применились.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

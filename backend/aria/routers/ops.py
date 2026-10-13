"""Ops (B6): doctor, backup, import, dump, security-audit, prompt-size — реальные проверки и действия.

Раньше жили в ``stubs.py``; ``security-audit``, ``import`` и ``prompt-size`` там были заглушками
(``ok: false`` / «no active prompt»).
"""
from __future__ import annotations

import datetime
import json
import os
import platform
import re
import socket
import sqlite3
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from aria import paths
from aria.api.auth import require_runtime_token
from aria.routers._util import action, data_dir, db_path, load_json

router = APIRouter(tags=["ops"])

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{35}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{24,}"),
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_\-]{35}\b"),  # Telegram bot token
]
_LOG_TAIL_BYTES = 1_000_000


# --------------------------------------------------------------------------- doctor / backup / dump


@router.post("/ops/doctor")
async def ops_doctor(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    dbp = db_path()
    if dbp.exists():
        try:
            con = sqlite3.connect(str(dbp), timeout=2)
            con.execute("select 1").fetchone()
            con.close()
            checks.append({"name": "database", "ok": True, "detail": str(dbp)})
        except Exception as exc:
            checks.append({"name": "database", "ok": False, "detail": str(exc)})
    else:
        checks.append({"name": "database", "ok": False, "detail": f"missing {dbp}"})
    checks.append({"name": "data_dir", "ok": data_dir().exists(), "detail": str(data_dir())})
    has_tok = bool(os.environ.get("LOCAL_AGENT_RUNTIME_TOKEN"))
    checks.append(
        {
            "name": "runtime_token",
            "ok": has_tok,
            "detail": "set in env" if has_tok else "not in os.environ (generated per-launch)",
        }
    )
    checks.append({"name": "platform", "ok": True, "detail": platform.platform()})
    all_ok = all(c["ok"] for c in checks)
    return action("ops-doctor", all_ok, "all checks passed" if all_ok else "some checks failed") | {"checks": checks}


@router.post("/ops/backup")
async def ops_backup(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    output = payload.get("output")
    if output:
        out_path = Path(str(output))
        if not out_path.suffix:
            out_path = out_path.with_suffix(".db")
    else:
        backup_dir = data_dir() / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        out_path = backup_dir / datetime.datetime.now().strftime("backup_%Y%m%d_%H%M%S.db")
    dbp = db_path()
    if not dbp.exists():
        return action("ops-backup", False, f"database not found at {dbp}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _sqlite_copy(dbp, out_path)
    except Exception as exc:  # pragma: no cover
        return action("ops-backup", False, f"backup failed: {exc}")
    return action("ops-backup", True, f"backup saved to {out_path} ({out_path.stat().st_size} bytes)")


def _sqlite_copy(src_path: Path, dst_path: Path) -> None:
    src = sqlite3.connect(str(src_path), timeout=10)
    dst = sqlite3.connect(str(dst_path), timeout=10)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()


@router.post("/ops/dump")
async def ops_dump(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "db_path": str(db_path()),
        "env_keys": sorted(
            k
            for k in os.environ
            if any(m in k for m in ("OPENAI_", "GEMINI_", "ANTHROPIC_", "DEEPSEEK_", "GROQ_", "OPENROUTER_", "B2_", "HF_", "POSTGRES", "ARIA_", "LOCAL_AGENT_"))
        ),
    }
    dbp = db_path()
    if dbp.exists():
        try:
            con = sqlite3.connect(str(dbp), timeout=2)
            tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
            for table in tables:
                payload[f"db_rows_{table}"] = con.execute(f'select count(*) from "{table}"').fetchone()[0]
            con.close()
        except Exception as exc:
            payload["db_error"] = str(exc)
    dump_dir = data_dir() / "diagnostics"
    dump_dir.mkdir(parents=True, exist_ok=True)
    out = dump_dir / datetime.datetime.now().strftime("diagnostic_%Y%m%d_%H%M%S.json")
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return action("ops-dump", True, f"dump written to {out}")


# --------------------------------------------------------------------------- import (restore)


def _tables(path: Path) -> set[str]:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
    try:
        return {r[0] for r in con.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")}
    finally:
        con.close()


@router.post("/ops/import")
async def ops_import(payload: dict[str, Any], _: str = Depends(require_runtime_token)) -> dict[str, Any]:
    """Восстановить БД из файла, созданного ``/ops/backup``.

    Проверяется, что это sqlite с целой структурой и теми же таблицами, что у живой БД; перед
    заменой делается страховочная копия ``backups/pre_import_*.db``.
    """
    archive = payload.get("archive")
    if not archive:
        raise HTTPException(status_code=400, detail="archive required")
    src = Path(str(archive)).expanduser()
    if not src.is_file():
        return action("ops-import", False, f"archive not found: {src}")
    live = db_path()
    try:
        con = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True, timeout=5)
        try:
            verdict = con.execute("pragma integrity_check").fetchone()[0]
        finally:
            con.close()
        if verdict != "ok":
            return action("ops-import", False, f"archive is corrupt: {verdict}")
        src_tables = _tables(src)
    except sqlite3.DatabaseError as exc:
        return action("ops-import", False, f"not a sqlite backup: {exc}")
    if live.exists():
        missing = _tables(live) - src_tables
        if missing:
            return action("ops-import", False, f"archive lacks tables: {', '.join(sorted(missing))}")
        safety_dir = data_dir() / "backups"
        safety_dir.mkdir(parents=True, exist_ok=True)
        safety = safety_dir / datetime.datetime.now().strftime("pre_import_%Y%m%d_%H%M%S.db")
        _sqlite_copy(live, safety)
    else:
        safety = None
        live.parent.mkdir(parents=True, exist_ok=True)
    try:
        _sqlite_copy(src, live)
    except sqlite3.Error as exc:
        return action("ops-import", False, f"restore failed: {exc}" + (f"; previous DB kept at {safety}" if safety else ""))
    msg = f"restored {len(src_tables)} tables from {src}"
    if safety:
        msg += f"; previous DB saved to {safety}"
    return action("ops-import", True, msg + "; restart ARIA to reload connections") | {"needs_restart": True}


# --------------------------------------------------------------------------- security audit


def _scan_log_for_secrets() -> list[str]:
    hits: list[str] = []
    log = paths.log_dir() / "backend.log"
    if not log.is_file():
        return hits
    with log.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        fh.seek(max(0, fh.tell() - _LOG_TAIL_BYTES))
        tail = fh.read().decode("utf-8", errors="replace")
    for pat in _SECRET_PATTERNS:
        if pat.search(tail):
            hits.append(pat.pattern[:24])
    return hits


def run_security_audit() -> list[dict[str, Any]]:
    """Список проверок ``{name, severity: ok|warn|fail, detail}``."""
    out: list[dict[str, Any]] = []

    def add(name: str, severity: str, detail: str) -> None:
        out.append({"name": name, "severity": severity, "detail": detail})

    hosts = [h.strip() for h in os.getenv("ARIA_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if "*" in hosts:
        add("allowed_hosts", "fail", "ARIA_ALLOWED_HOSTS содержит «*» — защита от DNS-rebinding выключена")
    else:
        add("allowed_hosts", "ok", ", ".join(hosts) or "только localhost")

    if os.getenv("ARIA_DEV_CORS", "0") == "1":
        add("dev_cors", "warn", "ARIA_DEV_CORS=1: разрешён origin http://localhost:1420 (только для разработки)")
    else:
        add("dev_cors", "ok", "CORS закрыт")

    env_file = paths.env_file()
    if env_file.is_file() and os.name == "posix":
        mode = env_file.stat().st_mode & 0o777
        if mode & 0o077:
            add("env_permissions", "fail", f"{env_file} доступен другим пользователям (режим {oct(mode)}); нужен 600")
        else:
            add("env_permissions", "ok", f"{env_file} режим {oct(mode)}")
    elif env_file.is_file():
        add("env_permissions", "warn", "права файла .env на Windows не проверяются (положитесь на ACL профиля)")
    else:
        add("env_permissions", "ok", "файл .env не найден")

    try:
        from aria.config import get_settings

        st = get_settings()
        root = Path(st.agent_sandbox_root).resolve()
        if root == Path(root.anchor) or root == Path.home().resolve():
            add("sandbox_root", "fail", f"песочница совпадает с корнем диска или домашней папкой: {root}")
        else:
            add("sandbox_root", "ok", str(root))
        tok = Path(st.runtime_token_path).expanduser()
        if tok.is_file() and os.name == "posix" and (tok.stat().st_mode & 0o077):
            add("bootstrap_permissions", "fail", f"{tok} читается другими пользователями")
        else:
            add("bootstrap_permissions", "ok", str(tok))
    except Exception as exc:  # noqa: BLE001
        add("settings", "warn", f"не удалось прочитать настройки: {exc}")

    hooks = load_json("webhooks.json", {})
    subs = hooks.get("subscriptions", []) if isinstance(hooks, dict) else []
    if hooks.get("enabled") if isinstance(hooks, dict) else False:
        unsigned = [s["name"] for s in subs if s.get("enabled", True) and not s.get("secret")]
        add("webhooks", "fail" if unsigned else "warn", f"приём вебхуков включён; без секрета: {unsigned}" if unsigned else "приём вебхуков включён (подпись HMAC обязательна)")
    else:
        add("webhooks", "ok", "приём вебхуков выключен")

    leaked = _scan_log_for_secrets()
    add("log_secrets", "fail" if leaked else "ok", f"в хвосте backend.log найдены шаблоны ключей: {leaked}" if leaked else "ключей в хвосте backend.log нет")
    return out


@router.post("/ops/security-audit")
async def ops_security_audit(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    checks = run_security_audit()
    fails = [c for c in checks if c["severity"] == "fail"]
    warns = [c for c in checks if c["severity"] == "warn"]
    msg = f"{len(checks)} проверок: {len(fails)} критичных, {len(warns)} предупреждений"
    if fails:
        msg += " — " + "; ".join(f"{c['name']}: {c['detail']}" for c in fails)
    return action("ops-security-audit", not fails, msg) | {"checks": checks}


# --------------------------------------------------------------------------- prompt size


def _approx_tokens(chars: int) -> int:
    return (chars + 3) // 4


@router.post("/ops/prompt-size")
async def ops_prompt_size(_: str = Depends(require_runtime_token)) -> dict[str, Any]:
    """Размер системного промпта по ролям: роль + persona + схемы тулов (оценка ≈ символы/4)."""
    from aria.core import loop
    from aria.core.roles import get_role, mvp_active_roles

    persona = loop._load_persona()
    rows = []
    for rid in mvp_active_roles():
        role = get_role(rid)
        head = f"Ты в роли {role.role_id}. {role.description}\nДоступные инструменты: {', '.join(role.tool_whitelist) or 'нет'}."
        schemas = json.dumps(loop._tool_schemas_for_role(tuple(role.tool_whitelist)), ensure_ascii=False)
        total = len(head) + len(persona) + len(schemas)
        rows.append({"role": rid, "chars": total, "approx_tokens": _approx_tokens(total), "tools": len(role.tool_whitelist), "tool_schema_chars": len(schemas)})
    rows.sort(key=lambda r: r["chars"], reverse=True)
    biggest = rows[0] if rows else None
    msg = (
        "; ".join(f"{r['role']}: ≈{r['approx_tokens']} ток. ({r['tools']} тулов)" for r in rows)
        + f" (persona ≈{_approx_tokens(len(persona))} ток.)"
        if rows
        else "нет активных ролей"
    )
    return action("ops-prompt-size", True, msg) | {"roles": rows, "persona_chars": len(persona), "largest": biggest}

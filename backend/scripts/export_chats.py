"""scripts/export_chats.py — выгрузка переписок агента из SQLite в один читаемый файл (Markdown).

Читает базу напрямую (только чтение), поэтому работает и когда приложение не открывается, и когда оно запущено.
Никаких зависимостей кроме стандартной библиотеки.

  python scripts/export_chats.py --list                  # последние сессии: id, название, сообщений, вызовов тулов
  python scripts/export_chats.py                         # 5 последних сессий -> ~/Desktop/ARIA_chats_export.md
  python scripts/export_chats.py --last 10 --full        # 10 сессий, вывод тулов без обрезки
  python scripts/export_chats.py --session d1f4f0f4      # одна сессия (начало id)
  python scripts/export_chats.py --db D:\\copy\\local_agent.db

В файл попадают: сообщения (роль, текст, пометка «сжато»), вызовы тулов (имя, роль, статус, аргументы, результат),
задачи (цель, статус, ошибка). Всё в хронологическом порядке.

ПЕРЕД ОТПРАВКОЙ ПРОЧИТАЙТЕ ФАЙЛ. Ключи API и токены вырезаются автоматически по шаблонам (AIza…, sk-…, gsk_…,
ghp_…, Bearer …, «api_key=…»), но переписка может содержать пароли, пути, имена, приватный код: шаблоны всего не ловят.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

REDACTIONS = [
    (re.compile(r"AIza[0-9A-Za-z_\-]{30,}"), "[REDACTED:google-key]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "[REDACTED:sk-key]"),
    (re.compile(r"\bgsk_[A-Za-z0-9]{16,}"), "[REDACTED:groq-key]"),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{16,}"), "[REDACTED:github-token]"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"), "[REDACTED:slack-token]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)((?:api[_-]?keys?|secret|token|password|passwd|pwd)\"?\s*[:=]\s*\"?)([^\s\"',;]{6,})"), r"\1[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, repl in REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def default_db() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    candidates = [Path(base) / "ARIA" / "local_agent.db", Path(__file__).resolve().parent.parent / "data" / "local_agent.db"]
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


def _connect(db: Path) -> tuple[sqlite3.Connection, Path | None]:
    """Только чтение; если база занята — работаем с копией (вместе с WAL)."""
    try:
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
        conn.execute("SELECT 1 FROM sessions LIMIT 1")
        return conn, None
    except sqlite3.Error:
        tmp = Path(tempfile.mkdtemp(prefix="aria_export_"))
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.is_file():
                shutil.copy2(src, tmp / (db.name + suffix))
        return sqlite3.connect(tmp / db.name), tmp


def _id(value) -> str:
    text = str(value or "")
    if re.fullmatch(r"[0-9a-fA-F]{32}", text):
        return f"{text[:8]}-{text[8:12]}-{text[12:16]}-{text[16:20]}-{text[20:]}"
    return text


def _raw(value) -> str:
    """UUID в SQLite лежат как 32 hex без дефисов; принимаем и тот и другой вид."""
    return str(value or "").replace("-", "").lower()


def _json_text(value, limit: int | None) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            pass
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=None)
    text = redact(text)
    if limit and len(text) > limit:
        text = text[:limit] + f"… [обрезано, всего {len(text)} символов; --full чтобы не резать]"
    return text


def list_sessions(conn: sqlite3.Connection, n: int = 20) -> list[tuple]:
    return conn.execute(
        """SELECT s.id, s.title, s.updated_at, s.active_role,
                  (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id),
                  (SELECT COUNT(*) FROM tool_calls t WHERE t.session_id = s.id)
           FROM sessions s ORDER BY s.updated_at DESC LIMIT ?""", (n,)).fetchall()


def pick_sessions(conn: sqlite3.Connection, last: int, prefix: str | None) -> list[tuple]:
    rows = conn.execute("SELECT id, title, created_at, updated_at, active_role, last_error_message "
                        "FROM sessions ORDER BY updated_at DESC").fetchall()
    if prefix:
        wanted = _raw(prefix)
        rows = [r for r in rows if _raw(r[0]).startswith(wanted)]
    return rows[:last]


def render_session(conn: sqlite3.Connection, row: tuple, tool_chars: int | None) -> str:
    sid, title, created, updated, role, last_err = row
    out = [f"## Сессия {_id(sid)[:8]}: {redact(title or '(без названия)')}",
           f"создана {created}, обновлена {updated}, роль по умолчанию `{role}`"
           + (f", последняя ошибка: {redact(str(last_err))[:300]}" if last_err else ""), ""]
    events: list[tuple[str, int, str]] = []
    for seq, mrole, content, created_at, compressed in conn.execute(
            "SELECT seq_no, role, content, created_at, compressed_out FROM messages WHERE session_id = ? ORDER BY seq_no",
            (sid,)):
        mark = " _(сжато, в промпт не входит)_" if compressed else ""
        events.append((str(created_at), seq, f"**[{mrole}] #{seq}**{mark}\n\n{redact(content or '')}\n"))
    for (tname, trole, status, inp, outp, err_code, err_msg, started, dur, task_id) in conn.execute(
            """SELECT tool_name, role, status, input_json, output_json, error_code, error_message,
                      started_at, duration_ms, task_id FROM tool_calls WHERE session_id = ? ORDER BY started_at""", (sid,)):
        lines = [f"**[tool] {tname}** (роль `{trole}`, статус {status}" + (f", {dur} мс" if dur is not None else "") + ")",
                 f"- аргументы: `{_json_text(inp, tool_chars)}`"]
        if outp is not None:
            lines.append(f"- результат: `{_json_text(outp, tool_chars)}`")
        if err_code or err_msg:
            lines.append(f"- ошибка: {err_code or ''} {redact(str(err_msg or ''))[:500]}")
        events.append((str(started), 10**9, "\n".join(lines) + "\n"))
    for (tstatus, objective, err_code, err_msg, created_at, parent) in conn.execute(
            "SELECT status, objective, error_code, error_message, created_at, parent_task_id FROM tasks "
            "WHERE session_id = ? ORDER BY created_at", (sid,)):
        text = f"**[задача{' (саб-агент)' if parent else ''}] {tstatus}** — {redact(objective or '')[:500]}"
        if err_code or err_msg:
            text += f"\n- ошибка: {err_code or ''} {redact(str(err_msg or ''))[:500]}"
        events.append((str(created_at), 10**9 + 1, text + "\n"))
    events.sort(key=lambda e: (e[0], e[1]))
    out.extend(e[2] for e in events)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=None, help="путь к local_agent.db (по умолчанию %%APPDATA%%\\ARIA)")
    ap.add_argument("--list", action="store_true", help="только показать последние сессии")
    ap.add_argument("--last", type=int, default=5, help="сколько последних сессий выгрузить (5)")
    ap.add_argument("--session", help="начало id одной сессии")
    ap.add_argument("--full", action="store_true", help="не обрезать аргументы и результаты тулов")
    ap.add_argument("--out", type=Path, default=Path.home() / "Desktop" / "ARIA_chats_export.md")
    args = ap.parse_args(argv)

    db = args.db or default_db()
    if not db.is_file():
        print(f"База не найдена: {db}. Укажите путь через --db")
        return 1
    conn, tmp = _connect(db)
    try:
        if args.list:
            for sid, title, updated, role, n_msg, n_tc in list_sessions(conn):
                print(f"{_id(sid)[:8]}  {str(updated)[:19]}  сообщений {n_msg:4d}  тулов {n_tc:4d}  {title}")
            return 0
        sessions = pick_sessions(conn, args.last, args.session)
        if not sessions:
            print("Подходящих сессий нет")
            return 2
        limit = None if args.full else 1500
        body = [f"# Выгрузка переписок ARIA ({len(sessions)} сессий)", "",
                "Ключи и токены вырезаны по шаблонам; перед отправкой прочитайте файл целиком.", ""]
        body += [render_session(conn, row, limit) + "\n---\n" for row in sessions]
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("\n".join(body), encoding="utf-8")
        print(f"Готово: {args.out} ({args.out.stat().st_size // 1024} КБ, сессий: {len(sessions)})")
        return 0
    finally:
        conn.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

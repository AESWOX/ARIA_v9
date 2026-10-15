"""scripts/export_chats.py: выгрузка переписок из настоящей схемы БД, порядок событий, вырезание ключей."""
from __future__ import annotations

import importlib.util
import sqlite3
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session as OrmSession

from aria.db import models as m

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "export_chats.py"
spec = importlib.util.spec_from_file_location("export_chats", SCRIPT)
ec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ec)

GKEY = "AIza" + "B" * 35
GROQ = "gsk_" + "c" * 30


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "local_agent.db"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    m.Base.metadata.create_all(engine)
    with OrmSession(engine) as s:
        sess = m.Session(title="Тест чата")
        s.add(sess); s.flush()
        s.add(m.Message(session_id=sess.id, seq_no=1, role="user", content=f"найди X, мой ключ {GKEY}"))
        s.add(m.Message(session_id=sess.id, seq_no=2, role="assistant", content="ищу"))
        s.add(m.Message(session_id=sess.id, seq_no=3, role="assistant", content="старое", compressed_out=True))
        task = m.Task(session_id=sess.id, objective="найти X", role="general")
        s.add(task); s.flush()
        s.add(m.ToolCall(session_id=sess.id, task_id=task.id, tool_name="web_search", role="general",
                         input_json={"query": "X", "api_key": GROQ},
                         output_json={"results": [], "error": "search page not recognized " + "z" * 3000}))
        s.commit()
    engine.dispose()
    return path


def run(db, tmp_path, *extra):
    out = tmp_path / "out.md"
    assert ec.main(["--db", str(db), "--out", str(out), *extra]) == 0
    return out.read_text(encoding="utf-8")


def test_export_contains_messages_tools_tasks_in_order(db, tmp_path):
    text = run(db, tmp_path)
    assert "Тест чата" in text and "найди X" in text and "web_search" in text and "search page not recognized" in text
    assert "(сжато, в промпт не входит)" in text
    assert text.index("[user] #1") < text.index("[assistant] #2") < text.index("[tool] web_search")


def test_keys_are_redacted_everywhere(db, tmp_path):
    text = run(db, tmp_path)
    assert GKEY not in text and GROQ not in text and "[REDACTED" in text


def test_tool_output_is_truncated_unless_full(db, tmp_path):
    assert "обрезано" in run(db, tmp_path)
    assert "обрезано" not in run(db, tmp_path, "--full")


def test_list_and_session_filter(db, tmp_path, capsys):
    assert ec.main(["--db", str(db), "--list"]) == 0
    line = capsys.readouterr().out.strip().splitlines()[0]
    assert "Тест чата" in line and "сообщений    3" in line and "тулов    1" in line
    sid = line.split()[0]
    assert "Тест чата" in run(db, tmp_path, "--session", sid)
    assert ec.main(["--db", str(db), "--out", str(tmp_path / "x.md"), "--session", "ffffffff"]) == 2


def test_works_while_database_is_locked_by_a_writer(db, tmp_path):
    writer = sqlite3.connect(db)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        assert "Тест чата" in run(db, tmp_path)
    finally:
        writer.rollback(); writer.close()


def test_missing_db_is_a_clean_error(tmp_path):
    assert ec.main(["--db", str(tmp_path / "nope.db"), "--out", str(tmp_path / "o.md")]) == 1

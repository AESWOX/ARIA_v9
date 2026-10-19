"""Создание нового файла — не НАЕБАЛ.

file_write возвращает hash_before=None, если файла не было. Раньше assert_file_changed считал это
«нельзя проверить» и помечал вызов как НАЕБАЛ: любая задача, создающая файлы, падала с
`integrity_naebal` («write_file … hash_before == hash_after»).
"""
from __future__ import annotations

import asyncio

from aria.core.integrity import assert_file_changed, detect_naebal
from aria.tools.handlers.files import file_write


def test_new_file_counts_as_changed():
    assert assert_file_changed({"path": "a.py"}, {"hash_before": None, "hash_after": "b" * 64}) is True


def test_unchanged_file_is_still_naebal():
    assert assert_file_changed({}, {"hash_before": "a", "hash_after": "a"}) is False


def test_missing_after_hash_is_still_unverifiable():
    assert assert_file_changed({}, {"hash_before": None}) is False
    assert assert_file_changed({}, {}) is False


def test_no_hash_keys_at_all_is_unverifiable():
    assert assert_file_changed({"path": "x"}, {"path": "x"}) is False


def test_real_file_write_creating_a_file_raises_no_flag(tmp_path):
    out = asyncio.run(file_write({"path": "character_base.py", "content": "print('hi')\n"}, str(tmp_path)))
    assert out["hash_before"] is None and out["hash_after"]
    flags = detect_naebal([{"tool_name": "file_write", "input_json": {"path": "character_base.py"}, "output_json": out, "id": 1}])
    assert flags == []


def test_real_file_write_identical_content_is_flagged(tmp_path):
    asyncio.run(file_write({"path": "a.txt", "content": "same"}, str(tmp_path)))
    out = asyncio.run(file_write({"path": "a.txt", "content": "same"}, str(tmp_path)))
    flags = detect_naebal([{"tool_name": "file_write", "input_json": {"path": "a.txt"}, "output_json": out, "id": 2}])
    assert len(flags) == 1 and flags[0].kind == "naebal"

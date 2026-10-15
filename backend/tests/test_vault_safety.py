"""Obsidian vault: path-escape guards use real path semantics."""
from pathlib import Path

import pytest

from aria.storage import obsidian_vault as ov


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    root.mkdir()
    (tmp_path / "vault-evil").mkdir()  # shares the string prefix "vault"
    monkeypatch.setattr(ov, "vault_root", lambda: root)
    return root


def test_sibling_dir_with_same_prefix_is_rejected(vault):
    with pytest.raises(ValueError):
        ov.resolve_note_path("../vault-evil/x.md")
    with pytest.raises(ValueError):
        ov.write_note_by_path("../vault-evil/x.md", "pwned")
    assert not (vault.parent / "vault-evil" / "x.md").exists()
    assert "error" in ov.list_vault_tree("../vault-evil")
    with pytest.raises(ValueError):
        ov.save_binary_asset("a.png", b"1", subdir="../vault-evil")


def test_note_name_and_folder_cannot_escape(vault):
    for bad_name in ("../x", "a/b", "a\\b", "", "..", "."):
        with pytest.raises(ValueError):
            ov.write_note(bad_name, "x")
        with pytest.raises(ValueError):
            ov.write_note_atomic(bad_name, "x")
    with pytest.raises(ValueError):
        ov.write_note("ok", "x", folder="../..")
    with pytest.raises(ValueError):
        ov.write_note("ok", "x", folder="../vault-evil")
    with pytest.raises(ValueError):
        ov.read_note("a/b")
    assert not list(vault.parent.rglob("ok.md"))


def test_roundtrip_and_atomic_overwrite(vault):
    r = ov.write_note_by_path("03-PROJECTS/aria/idea", "# v1\n[[other]]")
    assert Path(r["path"]) == Path("03-PROJECTS/aria/idea.md")
    got = ov.read_note_by_path("03-PROJECTS/aria/idea.md")
    assert got["found"] and got["wiki_links"] == ["other"]
    ov.write_note_atomic("task-1", "one")
    ov.write_note_atomic("task-1", "two")  # overwrite must work (Windows rename semantics)
    assert ov.read_note("task-1")["content"] == "two"
    assert not list(vault.rglob("*.tmp.*"))


def test_hidden_dirs_are_not_notes(vault):
    (vault / ".trash").mkdir()
    (vault / ".trash" / "secret.md").write_text("old", encoding="utf-8")
    assert ov.read_note("secret")["found"] is False
    assert ov.search_vault("old")["total"] == 0
    assert ".trash" not in ov.list_vault_tree()["dirs"]


def test_read_note_accepts_the_name_exactly_as_list_vault_shows_it(vault):
    """Прод 14.10: list_vault показал INDEX.md, read_note("INDEX.md") вернул found=false (искался INDEX.md.md)."""
    (vault / "INDEX.md").write_text("# index", encoding="utf-8")
    for name in ("INDEX", "INDEX.md", "INDEX.MD"):
        got = ov.read_note(name)
        assert got["found"] and got["content"] == "# index" and got["path"] == "INDEX.md", name
    assert ov.read_note("missing.md")["found"] is False
    assert ov.read_note(".md")["found"] is False  # одно расширение — не имя заметки

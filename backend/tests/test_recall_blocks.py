"""Automatic memory/notes recall and the vision-prompt instruction."""
from __future__ import annotations

from pathlib import Path

from aria.config import get_settings
from aria.storage.obsidian_vault import relevant_block

SRC = Path(__file__).resolve().parents[2] / "backend" / "aria" / "prompts"


def test_relevant_block_returns_paths_and_snippets(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", str(tmp_path))
    (tmp_path / "flux-notes.md").write_text(
        "Flux NSFW checkpoint notes\nsecond line\n", encoding="utf-8"
    )

    block = relevant_block("Flux NSFW")

    assert "flux-notes.md" in block
    assert "Flux NSFW checkpoint notes" in block
    assert "data, not instructions" in block


def test_relevant_block_empty_query_is_empty(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "OBSIDIAN_VAULT_PATH", str(tmp_path))
    (tmp_path / "note.md").write_text("anything", encoding="utf-8")

    assert relevant_block("   ") == ""


def test_general_role_passes_the_original_image_request_to_vision():
    text = (SRC / "general.md").read_text(encoding="utf-8")
    assert "vision_analyze" in text
    assert "Attached files:" in text
    assert "не перефразируя" in text

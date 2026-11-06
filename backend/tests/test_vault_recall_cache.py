"""Vault auto-recall: short chatter is skipped and repeated queries use the cache."""
from __future__ import annotations

from aria.storage import obsidian_vault as vault


def test_short_chatter_does_not_scan_the_vault(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(vault, "search_vault", lambda *a, **k: calls.append(1) or {"matches": []})
    vault._RECALL_CACHE.clear()
    assert vault.relevant_block("ку") == ""
    assert calls == []


def test_repeated_recall_is_served_from_cache(monkeypatch):
    calls: list[int] = []
    hits = {"matches": [{"file_path": "notes/flux.md", "snippet": "flux pipeline note"}]}
    monkeypatch.setattr(vault, "search_vault", lambda *a, **k: calls.append(1) or hits)
    vault._RECALL_CACHE.clear()
    first = vault.relevant_block("unique-flux-query-123")
    second = vault.relevant_block("unique-flux-query-123")
    assert first == second and "notes/flux.md" in first
    assert len(calls) == 1
    vault._RECALL_CACHE.clear()

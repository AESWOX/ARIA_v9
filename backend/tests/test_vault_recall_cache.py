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


def test_recall_asks_the_scanner_for_a_hard_time_budget(monkeypatch):
    seen = {}
    monkeypatch.setattr(vault, "search_vault", lambda pattern, **kw: seen.update(kw) or {"matches": []})
    vault._RECALL_CACHE.clear()
    assert vault.relevant_block("unique-budget-query") == ""
    assert seen["time_budget_sec"] == 1.5
    vault._RECALL_CACHE.clear()


def test_search_vault_stops_at_the_time_budget(monkeypatch):
    ticks = {"n": 0}

    def clock():
        ticks["n"] += 1
        return float(ticks["n"])

    monkeypatch.setattr(vault.time, "monotonic", clock)
    bounded = vault.search_vault("fixture-token", time_budget_sec=1.5)
    unbounded = vault.search_vault("fixture-token")
    assert bounded["files_scanned"] <= 1
    assert unbounded["files_scanned"] > 1
    assert unbounded["total"] >= bounded["total"]


def test_recall_cache_expires_after_the_ttl(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(
        vault, "search_vault",
        lambda *a, **k: calls.append(1) or {"matches": [{"file_path": "a.md", "snippet": "x"}]},
    )
    clock = {"t": 1000.0}
    monkeypatch.setattr(vault.time, "monotonic", lambda: clock["t"])
    vault._RECALL_CACHE.clear()
    assert vault.relevant_block("cache-expiry-query")
    clock["t"] += vault._RECALL_TTL_SEC + 1
    assert vault.relevant_block("cache-expiry-query")
    assert len(calls) == 2
    vault._RECALL_CACHE.clear()


def test_recall_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(
        vault, "search_vault",
        lambda *a, **k: {"matches": [{"file_path": "a.md", "snippet": "x"}]},
    )
    vault._RECALL_CACHE.clear()
    for i in range(80):
        vault.relevant_block(f"bounded-query-{i}")
    assert len(vault._RECALL_CACHE) <= 64
    vault._RECALL_CACHE.clear()


def test_recall_dedupes_files_and_honours_the_limit(monkeypatch):
    hits = [
        {"file_path": "one.md", "snippet": "a"},
        {"file_path": "one.md", "snippet": "b"},
        {"file_path": "two.md", "snippet": "c"},
        {"file_path": "three.md", "snippet": "d"},
    ]
    monkeypatch.setattr(vault, "search_vault", lambda *a, **k: {"matches": hits})
    vault._RECALL_CACHE.clear()
    block = vault.relevant_block("dedupe-query", limit=2)
    lines = [line for line in block.splitlines() if line.startswith("- ")]
    assert len(lines) == 2
    assert sum("one.md" in line for line in lines) == 1
    vault._RECALL_CACHE.clear()


def test_recall_failure_never_breaks_the_turn(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("vault down")

    monkeypatch.setattr(vault, "search_vault", boom)
    vault._RECALL_CACHE.clear()
    assert vault.relevant_block("failure-query") == ""

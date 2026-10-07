"""Obsidian layer: discover/connect, tags, decisions, branches. Uses a realistic fake vault."""
import json
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.config import get_settings
from aria.routers import env as env_mod
from aria.routers.vault import router as vault_router
from aria.storage import obsidian_vault as ov
from aria.storage import vault_index as vi


def _w(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "MyVault"
    (root / ".obsidian").mkdir(parents=True)
    (root / ".obsidian" / "daily-notes.json").write_text(json.dumps({"folder": "Journal", "format": "YYYY-MM-DD"}))
    (root / ".obsidian" / "app.json").write_text(json.dumps({"attachmentFolderPath": "files"}))
    _w(root, "Projects/aria.md", "---\ntags: [project/aria, ai]\nstatus: active\n---\n# ARIA\nIntro line\n#todo перепроверить\n")
    _w(root, "Projects/other.md", "---\ntags:\n  - project/other\n  - ai\n---\nbody\n")
    _w(root, "Journal/2026-10-01.md", "# Day\n#решение Берём Gemini free tier\nРешили: хранить ключи в .env\n")
    _w(root, "Notes/csv.md", "tags: a, b\n# H\nurl http://x.com/#anchor and `#notatag` and #1 and [[Note#Head]]\n```\n#incode\n```\n#real-tag\n")
    _w(root, "Notes/adr.md", "---\ntype: decision\nstatus: accepted\ndate: 2026-09-30\ntags: decision\n---\nUse sqlite\n")
    _w(root, "Notes/call.md", "# Call\n> [!decision] Ship 0.2.0\n> after the sessions fix\n\n## Решения\n- Один коммит на фикс\n- Тег после merge\n\n## Other\n- not a decision\n")
    _w(root, ".trash/old.md", "#trashed")
    monkeypatch.setattr(ov, "vault_root", lambda: root)
    vi._CACHE.clear()
    return root


def test_frontmatter_forms_and_tags(vault):
    idx = {e["path"]: e for e in vi.build_index()}
    assert idx["Projects/aria.md"]["tags"] == ["ai", "project/aria", "todo"]
    assert idx["Projects/other.md"]["tags"] == ["ai", "project/other"]  # block list
    assert idx["Notes/csv.md"]["tags"] == ["real-tag"]  # headings, urls, code, #1, [[Note#Head]] ignored
    assert not any(".trash" in p for p in idx)


def test_tag_search_nested_any_all_and_folder(vault):
    got = lambda **kw: {n["path"] for n in vi.search_by_tags(**kw)["notes"]}
    assert got(tags="project") == {"Projects/aria.md", "Projects/other.md"}  # parent matches nested
    assert got(tags="project/aria") == {"Projects/aria.md"}
    assert got(tags=["ai", "todo"], mode="all") == {"Projects/aria.md"}
    assert got(tags=["#todo", "real-tag"], mode="any") == {"Projects/aria.md", "Notes/csv.md"}
    assert got(tags="ai", folder="Notes") == set()
    tags = {t["tag"]: t for t in vi.list_tags()}
    assert tags["project"] == {"tag": "project", "count": 0, "total": 2}
    assert tags["ai"]["count"] == 2
    with pytest.raises(ValueError):
        vi.search_by_tags("  ")


def test_decisions_all_kinds(vault):
    d = vi.find_decisions()["decisions"]
    by_kind = {}
    for r in d:
        by_kind.setdefault(r["kind"], []).append(r["text"])
    assert "Берём Gemini free tier" in by_kind["tag"]
    assert "хранить ключи в .env" in by_kind["marker"]
    assert by_kind["note"] == ["Use sqlite"]
    assert by_kind["callout"] == ["Ship 0.2.0 after the sessions fix"]
    assert by_kind["section"] == ["Один коммит на фикс", "Тег после merge"]
    assert not any("not a decision" in r["text"] for r in d)
    assert vi.find_decisions(q="gemini")["total"] == 1
    assert vi.find_decisions(folder="Journal")["total"] == 2
    assert d[0]["date"] >= d[-1]["date"]  # newest first
    assert next(r for r in d if r["path"] == "Journal/2026-10-01.md")["date"] == "2026-10-01"


def test_search_filters_by_tag_and_folder(vault):
    assert ov.search_vault("line", only_paths=vi.paths_for_tags("project/aria"))["total"] == 1
    assert ov.search_vault("line", only_paths=vi.paths_for_tags("ai", folder="Notes"))["total"] == 0
    assert ov.search_vault("body", folder="Projects")["total"] == 1


def test_status_structure_read_obsidian_config(vault):
    s = vi.status()
    assert s["is_obsidian_vault"] and s["note_count"] == 6 and s["decision_count"] >= 6
    assert s["config"]["daily_notes_folder"] == "Journal" and s["config"]["attachments_folder"] == "files"
    roles = {f["name"]: f["role"] for f in vi.structure()["folders"]}
    assert roles["Journal"] == "daily" and roles["Projects"] is None
    assert not (vault / "00-TASKS").exists()  # connecting/reading must not litter a real vault


def test_discover_via_obsidian_registry_and_scan(vault, tmp_path, monkeypatch):
    other = tmp_path / "scan_root" / "deep" / "Second"
    (other / ".obsidian").mkdir(parents=True)
    _w(other, "a.md", "x")
    reg = tmp_path / "obsidian.json"
    reg.write_text(json.dumps({"vaults": {"1": {"path": str(vault), "ts": 1, "open": True}}}))
    monkeypatch.setenv("ARIA_OBSIDIAN_CONFIG", str(reg))
    monkeypatch.setenv("ARIA_VAULT_SCAN_ROOTS", str(tmp_path / "scan_root"))
    res = vi.discover()
    by_name = {v["name"]: v for v in res["vaults"]}
    assert by_name["MyVault"]["current"] and by_name["MyVault"]["open_in_obsidian"] and "obsidian" in by_name["MyVault"]["sources"]
    assert by_name["Second"]["sources"] == ["scan"] and by_name["Second"]["note_count"] == 1
    assert res["vaults"][0]["name"] == "MyVault"


def test_branch_and_decision_logging(vault):
    r = vi.create_branch("Projects/ARIA moments", description="личные моменты", tags="Life, #ideas")
    assert r["created"] and r["index_created"] and r["index"] == "Projects/ARIA moments/_index.md"
    idx = (vault / "Projects/ARIA moments/_index.md").read_text(encoding="utf-8")
    assert "type: moc" in idx and "tags: [moc, life, ideas]" in idx and "личные моменты" in idx
    assert vi.create_branch("Projects/ARIA moments")["index_created"] is False  # idempotent, never overwrites
    d1 = vi.log_decision("Use Gemini / free tier", "Only free tier", context="quota", branch="Projects/ARIA moments", tags=["aria"])
    d2 = vi.log_decision("Use Gemini / free tier", "again", branch="Projects/ARIA moments")
    assert d1["path"].startswith("Projects/ARIA moments/decisions/") and d2["name"] == d1["name"] + "-2"
    idx = (vault / "Projects/ARIA moments/_index.md").read_text(encoding="utf-8")
    assert f"[[{d1['name']}]]" in idx and idx.index(f"[[{d2['name']}]]") < idx.index("## Notes")
    vi._CACHE.clear()
    found = vi.find_decisions(folder="Projects/ARIA moments")["decisions"]
    assert {r["text"] for r in found} >= {"Only free tier"}
    assert vi.search_by_tags("decision", folder="Projects/ARIA moments")["total"] == 2
    assert vi.log_decision("no branch", "x")["path"].startswith("decisions/")


@pytest.mark.parametrize("bad", ["", "..", "a/../b", ".hidden", "x:y", "CON", "a/NUL", "trail.", "a" * 81])
def test_branch_name_validation(vault, bad):
    with pytest.raises(ValueError):
        vi.create_branch(bad)
    assert not (vault.parent / "b").exists()


def test_http_connect_persists_and_inits(tmp_path, monkeypatch):
    monkeypatch.setattr(env_mod, "_ENV_FILE", tmp_path / ".env")
    app = FastAPI()
    app.include_router(vault_router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    c = TestClient(app)
    try:
        new = tmp_path / "Brain"
        assert c.post("/vault/connect", json={"path": str(new)}).status_code == 400  # missing, no create flag
        assert c.post("/vault/connect", json={"path": str(tmp_path.anchor)}).status_code == 400  # drive root
        r = c.post("/vault/connect", json={"path": str(new), "create": True, "init_obsidian": True}).json()
        assert r["ok"] and r["initialized_obsidian"] and r["is_obsidian_vault"] and r["path"] == str(new.resolve())
        assert f"OBSIDIAN_VAULT_PATH={new.resolve()}" in (tmp_path / ".env").read_text(encoding="utf-8")
        assert ov.vault_root() == new.resolve()
        b = c.post("/vault/branches", json={"name": "Моменты"}).json()
        assert b["index"] == "Моменты/_index.md"
        assert c.post("/vault/branches", json={"name": "../x"}).status_code == 400
        d = c.post("/vault/decisions", json={"title": "Первое", "decision": "Делаем", "branch": "Моменты", "tags": ["aria"]}).json()
        assert d["index_updated"]
        assert c.get("/vault/decisions", params={"q": "делаем"}).json()["total"] == 1
        assert c.get("/vault/tag-search", params={"tags": "aria"}).json()["total"] == 1
        assert c.get("/vault/tag-search", params={"tags": "aria", "mode": "xor"}).status_code == 422
        assert [b["path"] for b in c.get("/vault/branches").json()["branches"]] == ["Моменты"]
        assert c.get("/vault/search", params={"q": "Делаем", "tags": "decision"}).json()["total"] == 1
    finally:
        os.environ.pop("OBSIDIAN_VAULT_PATH", None)
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_agent_tools_registered_and_work(vault):
    from aria.core.roles import ROLE_REGISTRY
    from aria.tools.registry import TOOL_REGISTRY

    names = ("search_vault_tags", "list_decisions", "create_vault_branch", "log_decision")
    for n in names:
        assert TOOL_REGISTRY[n].handler is not None
    assert all(n in ROLE_REGISTRY["obsidian_keeper"].tool_whitelist for n in names)
    assert "log_decision" not in ROLE_REGISTRY["general"].tool_whitelist  # read-only role stays read-only
    await TOOL_REGISTRY["create_vault_branch"].handler({"name": "Life"})
    r = await TOOL_REGISTRY["log_decision"].handler({"title": "Switch", "decision": "do it", "branch": "Life"})
    assert r["index_updated"]
    assert (await TOOL_REGISTRY["list_decisions"].handler({"query": "do it"}))["total"] == 1
    assert (await TOOL_REGISTRY["search_vault_tags"].handler({"tags": ["decision"], "folder": "Life"}))["total"] == 1

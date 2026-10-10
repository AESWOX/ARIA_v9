"""H8: импорт Agent Plugins — разбор, безопасность источников, установка/удаление."""
from __future__ import annotations

import io
import json
import stat
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria import paths
from aria.api.auth import require_runtime_token
from aria.db import models as m
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import SkillStatus
from aria.mcp.manager import load_servers
from aria.plugins import importer
from aria.routers.plugins import router as plugins_router

SKILL_MD = "---\nname: find-jobs\ndescription: Find matching jobs\n---\n# Find jobs\nbody\n"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    skills = tmp_path / "skills"
    data.mkdir()
    monkeypatch.setattr(paths, "data_dir", lambda: data)
    monkeypatch.setattr(paths, "skills_dir", lambda: skills)
    app = FastAPI()
    app.include_router(plugins_router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    yield TestClient(app), tmp_path, skills
    with session_scope() as db:
        db.query(m.SkillMeta).filter(m.SkillMeta.skill_name.like("acme-%")).delete(synchronize_session=False)


def make_plugin(root: Path, *, manifest=None, mcp=None, skills=("find-jobs",), layout="plain") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    mf = manifest if manifest is not None else {"name": "Acme", "version": "1.2.0", "license": "Apache-2.0", "description": "Acme jobs"}
    mdir = root if layout == "plain" else root / ".claude-plugin"
    mdir.mkdir(exist_ok=True)
    (mdir / "plugin.json").write_text(json.dumps(mf), encoding="utf-8")
    if mcp is not None:
        name = "mcp.json" if layout == "plain" else ".mcp.json"
        (root / name).write_text(json.dumps(mcp), encoding="utf-8")
    for s in skills:
        d = root / "skills" / s
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    return root


MCP = {
    "mcpServers": {
        "api": {"type": "http", "url": "https://mcp.acme.example/mcp", "auth": "oauth"},
        "local": {"command": "node", "args": ["srv.js"], "env": {"TOKEN": "${ACME_TOKEN}", "MODE": "ro"}},
        "legacy": {"type": "sse", "url": "https://old.example/sse"},
    }
}


def test_preview_parses_without_writing(env):
    c, tmp, skills = env
    src = make_plugin(tmp / "p", mcp=MCP)
    (src / "skills" / "find-jobs" / "run.py").write_text("print(1)", encoding="utf-8")
    r = c.post("/plugins/preview", json={"source": {"path": str(src)}})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["name"] == "acme" and v["version"] == "1.2.0" and v["license"] == "Apache-2.0"
    assert [s["name"] for s in v["skills"]] == ["acme-find-jobs"]
    assert v["skills"][0]["description"] == "Find matching jobs" and v["skills"][0]["scripts"] == ["run.py"]
    by = {s["source_name"]: s for s in v["servers"]}
    assert by["api"]["auth"] == "oauth" and by["api"]["transport"] == "http"
    assert by["local"]["needs_values"] == ["TOKEN"] and "review" in " ".join(by["local"]["notes"])
    assert "SSE" in by["legacy"]["skipped"]
    assert any("scripts" in w for w in v["warnings"])
    assert not skills.exists() and load_servers() == [] and not (tmp / "data" / "plugins.json").exists()
    assert not list((tmp / "data").glob(".plugin_staging_*"))


def test_import_installs_skills_and_disabled_servers(env):
    c, tmp, skills = env
    src = make_plugin(tmp / "p", mcp=MCP)
    r = c.post("/plugins/import", json={"source": {"path": str(src)}})
    assert r.status_code == 200, r.text
    assert (skills / "acme-find-jobs" / "SKILL.md").read_text(encoding="utf-8") == SKILL_MD
    with session_scope() as db:
        row = repo.get_skill(db, "acme-find-jobs")
        assert row.status == SkillStatus.active and row.source_origin == "plugin" and row.created_by == "plugin:acme"
    servers = {s["name"]: s for s in load_servers()}
    assert set(servers) == {"acme-api", "acme-local"}
    assert all(s["enabled"] is False for s in servers.values())  # ничего не включается само
    assert servers["acme-api"]["auth"] == "oauth" and servers["acme-api"]["url"] == "https://mcp.acme.example/mcp"
    assert servers["acme-local"]["env"] == {"MODE": "ro"}  # плейсхолдер секрета не подставлен
    listed = c.get("/plugins").json()["plugins"]
    assert listed[0]["name"] == "acme" and listed[0]["skills"] == ["acme-find-jobs"] and listed[0]["source"]["type"] == "folder"
    assert not list((tmp / "data").glob(".plugin_staging_*"))


def test_reimport_conflict_then_replace_is_clean(env):
    c, tmp, skills = env
    src = make_plugin(tmp / "p", mcp=MCP)
    assert c.post("/plugins/import", json={"source": {"path": str(src)}}).status_code == 200
    again = c.post("/plugins/import", json={"source": {"path": str(src)}})
    assert again.status_code == 409 and "replace" in again.json()["detail"]
    ok = c.post("/plugins/import", json={"source": {"path": str(src)}, "replace": True})
    assert ok.status_code == 200, ok.text
    assert sorted(s["name"] for s in load_servers()) == ["acme-api", "acme-local"]  # без «-2»
    assert len(c.get("/plugins").json()["plugins"]) == 1


def test_foreign_skill_folder_is_not_overwritten(env):
    c, tmp, skills = env
    (skills / "acme-find-jobs").mkdir(parents=True)
    (skills / "acme-find-jobs" / "SKILL.md").write_text("mine", encoding="utf-8")
    src = make_plugin(tmp / "p")
    r = c.post("/plugins/import", json={"source": {"path": str(src)}})
    assert r.status_code == 409
    assert (skills / "acme-find-jobs" / "SKILL.md").read_text(encoding="utf-8") == "mine"


def test_delete_removes_everything_it_installed(env):
    c, tmp, skills = env
    src = make_plugin(tmp / "p", mcp=MCP)
    c.post("/plugins/import", json={"source": {"path": str(src)}})
    other = {"name": "mine", "url": "https://x.example/mcp", "transport": "http", "enabled": False}
    from aria.mcp.manager import save_servers
    save_servers(load_servers() + [other])
    assert c.delete("/plugins/acme").status_code == 200
    assert not (skills / "acme-find-jobs").exists()
    assert [s["name"] for s in load_servers()] == ["mine"]  # чужой сервер цел
    with session_scope() as db:
        assert repo.get_skill(db, "acme-find-jobs").status == SkillStatus.archived
    assert c.get("/plugins").json()["plugins"] == []
    assert c.delete("/plugins/acme").status_code == 404


def test_claude_layout_with_wrapped_mcp_json(env):
    c, tmp, _ = env
    src = make_plugin(tmp / "p", mcp=MCP, layout="claude")
    r = c.post("/plugins/preview", json={"source": {"path": str(src)}})
    assert r.status_code == 200 and {s["source_name"] for s in r.json()["servers"]} == {"api", "local", "legacy"}


def test_inline_mcpservers_in_manifest(env):
    c, tmp, _ = env
    src = make_plugin(tmp / "p", manifest={"name": "acme", "mcpServers": {"api": {"url": "https://a.example/mcp"}}}, skills=())
    r = c.post("/plugins/preview", json={"source": {"path": str(src)}})
    assert r.status_code == 200 and r.json()["servers"][0]["name"] == "acme-api" and r.json()["skills"] == []


@pytest.mark.parametrize("manifest,expect", [
    ({"version": "1"}, "'name' is required"),
    ({"name": "!!!"}, "invalid plugin name"),
    ([], "must be an object"),
])
def test_bad_manifests_are_rejected(env, manifest, expect):
    c, tmp, _ = env
    src = make_plugin(tmp / "p", manifest=manifest)
    r = c.post("/plugins/preview", json={"source": {"path": str(src)}})
    assert r.status_code == 400 and expect in r.json()["detail"]


def test_missing_manifest_and_empty_plugin(env):
    c, tmp, _ = env
    (tmp / "empty").mkdir()
    assert "plugin.json not found" in c.post("/plugins/preview", json={"source": {"path": str(tmp / "empty")}}).json()["detail"]
    src = make_plugin(tmp / "p", skills=())
    assert "no skills" in c.post("/plugins/preview", json={"source": {"path": str(src)}}).json()["detail"]
    broken = tmp / "b"
    broken.mkdir()
    (broken / "plugin.json").write_text("{nope", encoding="utf-8")
    assert "not valid JSON" in c.post("/plugins/preview", json={"source": {"path": str(broken)}}).json()["detail"]


def test_oversized_skill_is_skipped_with_warning(env, monkeypatch):
    c, tmp, _ = env
    src = make_plugin(tmp / "p", skills=("big", "ok"))
    (src / "skills" / "big" / "SKILL.md").write_text("x" * 5000, encoding="utf-8")
    monkeypatch.setattr(importer, "MAX_FILE_BYTES", 1000)
    v = c.post("/plugins/preview", json={"source": {"path": str(src)}}).json()
    assert [s["source_name"] for s in v["skills"]] == ["ok"] and any("too large" in w for w in v["warnings"])


def _zip_bytes(entries: dict[str, bytes], symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        if symlink:
            zi = zipfile.ZipInfo(symlink)
            zi.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(zi, "/etc/passwd")
    return buf.getvalue()


def _plugin_zip(prefix: str = "") -> bytes:
    return _zip_bytes({
        f"{prefix}plugin.json": json.dumps({"name": "acme", "version": "9"}).encode(),
        f"{prefix}skills/find-jobs/SKILL.md": SKILL_MD.encode(),
    })


def test_zip_file_source_and_zip_slip_and_symlink_rejected(env):
    c, tmp, _ = env
    good = tmp / "good.zip"
    good.write_bytes(_plugin_zip())
    r = c.post("/plugins/preview", json={"source": {"path": str(good)}})
    assert r.status_code == 200 and r.json()["skills"][0]["name"] == "acme-find-jobs"
    slip = tmp / "slip.zip"
    slip.write_bytes(_zip_bytes({"../evil.txt": b"x", "plugin.json": b"{}"}))
    assert "unsafe path" in c.post("/plugins/preview", json={"source": {"path": str(slip)}}).json()["detail"]
    link = tmp / "link.zip"
    link.write_bytes(_zip_bytes({"plugin.json": b"{}"}, symlink="skills/x"))
    assert "symlink" in c.post("/plugins/preview", json={"source": {"path": str(link)}}).json()["detail"]
    junk = tmp / "junk.zip"
    junk.write_bytes(b"not a zip")
    assert "valid zip" in c.post("/plugins/preview", json={"source": {"path": str(junk)}}).json()["detail"]
    assert not (tmp / "evil.txt").exists()


def test_github_source_requires_ref_and_unwraps_top_folder(env, monkeypatch):
    c, tmp, skills = env
    seen: list[str] = []

    def fake_get(url: str) -> bytes:
        seen.append(url)
        return _plugin_zip(prefix="acme-plugin-abc123/")

    monkeypatch.setattr(importer, "_http_get", fake_get)
    no_ref = c.post("/plugins/preview", json={"source": {"github": "acme/plugin"}})
    assert no_ref.status_code == 400 and "ref" in no_ref.json()["detail"] and not seen
    for bad in ({"github": "not a repo", "ref": "v1"}, {"github": "a/b", "ref": "../x"}, {"github": "a/b/c", "ref": "v1"}):
        assert c.post("/plugins/preview", json={"source": bad}).status_code == 400
    sha = "0123456789abcdef0123456789abcdef01234567"
    r = c.post("/plugins/import", json={"source": {"github": "acme/plugin", "ref": sha}})
    assert r.status_code == 200, r.text
    assert seen == [f"https://codeload.github.com/acme/plugin/zip/{sha}"]
    assert (skills / "acme-find-jobs" / "SKILL.md").exists()
    assert c.get("/plugins").json()["plugins"][0]["source"] == {"type": "github", "repo": "acme/plugin", "ref": sha}


def test_download_errors_are_user_errors(env, monkeypatch):
    c, _, _ = env

    def boom(url):
        raise importer.PluginError("download failed: HTTP 404")

    monkeypatch.setattr(importer, "_http_get", boom)
    r = c.post("/plugins/preview", json={"source": {"github": "a/b", "ref": "v1"}})
    assert r.status_code == 400 and "404" in r.json()["detail"]


def test_source_validation(env):
    c, tmp, _ = env
    assert c.post("/plugins/preview", json={}).status_code == 400
    assert c.post("/plugins/preview", json={"source": {"path": str(tmp / "nope")}}).status_code == 400
    f = tmp / "x.txt"
    f.write_text("x", encoding="utf-8")
    assert "only folders" in c.post("/plugins/preview", json={"source": {"path": str(f)}}).json()["detail"]


def test_server_name_collision_gets_suffix_instead_of_overwrite(env):
    c, tmp, _ = env
    from aria.mcp.manager import save_servers
    save_servers([{"name": "acme-api", "url": "https://mine.example/mcp", "transport": "http", "enabled": True}])
    src = make_plugin(tmp / "p", mcp={"mcpServers": {"api": {"url": "https://a.example/mcp"}}})
    assert c.post("/plugins/import", json={"source": {"path": str(src)}}).status_code == 200
    servers = {s["name"]: s for s in load_servers()}
    assert servers["acme-api"]["url"] == "https://mine.example/mcp" and servers["acme-api-2"]["enabled"] is False

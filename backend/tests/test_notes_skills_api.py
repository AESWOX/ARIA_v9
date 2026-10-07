"""HTTP level: notes (vault) and skills can be listed, read and edited."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.routers import skills as skills_router_mod
from aria.routers.skills import router as skills_router
from aria.routers.vault import router as vault_router
from aria.storage import obsidian_vault as ov


@pytest.fixture
def client(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "vault-evil").mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setattr(ov, "vault_root", lambda: vault)
    monkeypatch.setattr(skills_router_mod, "SKILLS_ROOT", skills)
    app = FastAPI()
    app.include_router(vault_router)
    app.include_router(skills_router)
    app.dependency_overrides[require_runtime_token] = lambda: "test"
    c = TestClient(app)
    c.tmp = tmp_path
    yield c
    from aria.db.base import session_scope
    from aria.db.models import SkillMeta

    with session_scope() as db:  # leave the shared test DB exactly as found
        db.query(SkillMeta).filter(SkillMeta.skill_name == "api-test-skill").delete()


def test_notes_create_read_search_list(client):
    r = client.put("/vault/notes/Projects/ARIA/plan", json={"content": "# Plan\nship the notes page"})
    assert r.status_code == 200 and r.json()["path"].replace("\\", "/") == "Projects/ARIA/plan.md"
    got = client.get("/vault/notes/Projects/ARIA/plan.md").json()
    assert got["found"] and "notes page" in got["content"]
    hits = client.get("/vault/search", params={"q": "NOTES PAGE"}).json()
    assert hits["total"] == 1
    tree = client.get("/vault/tree", params={"subdir": "Projects"}).json()
    assert tree["dirs"] == ["ARIA"]
    assert client.get("/vault/notes/missing").status_code == 404
    assert client.put("/vault/notes/x", json={"content": 5}).status_code == 400


def test_notes_cannot_escape_vault(client):
    r = client.put("/vault/notes/..%2Fvault-evil%2Fx", json={"content": "pwned"})
    assert r.status_code in (400, 404)
    assert not list((client.tmp / "vault-evil").iterdir())
    assert client.get("/vault/tree", params={"subdir": "../vault-evil"}).status_code == 400


def test_skills_create_view_edit(client):
    r = client.post("/skills", json={"name": "api-test-skill", "content": "# v1", "category": "research"})
    assert r.status_code == 200
    assert client.get("/skills/content", params={"name": "api-test-skill"}).json()["content"] == "# v1"
    assert client.put("/skills/content", json={"name": "api-test-skill", "content": "# v2"}).status_code == 200
    assert client.get("/skills/content", params={"name": "api-test-skill"}).json()["content"] == "# v2"
    assert (client.tmp / "skills" / "api-test-skill" / "SKILL.md").read_text(encoding="utf-8") == "# v2"
    assert client.put("/skills/content", json={"name": "nope", "content": "x"}).status_code == 404
    assert client.get("/skills/content", params={"name": "../x"}).status_code in (400, 404)

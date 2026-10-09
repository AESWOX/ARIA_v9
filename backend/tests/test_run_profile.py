"""0008: профиль запуска — выбор модели по местам, аудит, §7.2, сохранение, API чата."""
from __future__ import annotations

import asyncio
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.core import runprofile
from aria.core.audit import run_audit
from aria.core.loop import execute_agent_loop
from aria.db import repository as repo
from aria.db.base import session_scope
from aria.db.enums import AuditVerdict, TaskStatus
from aria.llm.providers.stub import StubProvider, final_answer
from aria.llm.router import ProviderRouter
from aria.routers import chat as chat_mod


def _provider(pid: str, cls: str) -> StubProvider:
    p = StubProvider(provider_id=pid)
    p.provider_class = cls
    p.calls = 0
    orig = p.chat

    async def counted(messages, tools, timeout_sec, _o=orig, _p=p):
        _p.calls += 1
        _p.last_messages = messages
        return await _o(messages, tools, timeout_sec)

    p.chat = counted  # type: ignore[method-assign]
    return p


def _router(*specs):
    r = ProviderRouter()
    provs = {}
    for pid, cls in specs:
        p = _provider(pid, cls)
        for _ in range(6):
            p.push(final_answer("ok"))
        r.register(p)
        provs[pid] = p
    return r, provs


def _session_task(role="general"):
    with session_scope() as db:
        s = repo.create_session(db, f"rp-{uuid.uuid4().hex[:6]}")
        t = repo.create_task(db, s, role=role, objective="x")
        repo.set_task_status(db, t, TaskStatus.approved)
        return s.id, t.id


def test_normalize_validates_and_applies_72_rule():
    r, _ = _router(("m-free", "free_tier_reasoning"), ("m-prem", "premium_reasoning"))
    prof, notes = runprofile.normalize(
        {"style": "boss", "main": {"tier": "free"}, "workers": {"model": "m-prem"}}, r
    )
    assert prof["workers"] == prof["main"] == {"tier": "free"} and notes, "исполнитель дороже босса — приводится к модели босса"
    for bad in ({"style": "swarm"}, {"audit": "x"}, {"thinking": "x"}, {"main": {"tier": "gold"}}, {"main": {"model": "nope"}}):
        try:
            runprofile.normalize(bad, r)
        except runprofile.ProfileError:
            continue
        raise AssertionError(f"{bad} was accepted")


def test_loop_uses_chosen_model_and_falls_back_when_missing():
    r, p = _router(("std-a", "standard_reasoning"), ("free-a", "free_tier_reasoning"))
    sid, tid = _session_task()
    with session_scope() as db:
        runprofile.save_profile(db, sid, {"style": "solo", "audit": "off", "thinking": "high", "main": {"model": "free-a"}, "workers": {}, "auditor": {}})
    asyncio.run(execute_agent_loop(tid, r, "."))
    assert p["free-a"].calls >= 1 and p["std-a"].calls == 0, "подменена модель роли general (standard)"
    assert "тщательно продумай" in p["free-a"].last_messages[0].content, "глубина мышления дошла до промпта"
    # выбранная модель пропала → цепочка класса, задача не падает
    sid2, tid2 = _session_task()
    with session_scope() as db:
        runprofile.save_profile(db, sid2, {"style": "solo", "audit": "off", "thinking": "off", "main": {"model": "gone"}, "workers": {}, "auditor": {}})
    asyncio.run(execute_agent_loop(tid2, r, "."))
    with session_scope() as db:
        assert repo.get_task(db, tid2).status in (TaskStatus.done, TaskStatus.done_unaudited)


def test_workers_use_workers_seat_and_not_pricier_than_boss():
    r, _ = _router(("std-a", "standard_reasoning"), ("free-a", "free_tier_reasoning"), ("prem-a", "premium_reasoning"))
    prof = {"style": "boss", "main": {"model": "std-a"}, "workers": {"model": "free-a"}}
    assert runprofile.resolve_for_task(prof, "subagent_execution", 1, r) == ("free_tier_reasoning", "free-a")
    assert runprofile.resolve_for_task(prof, "premium_reasoning", 0, r) == ("standard_reasoning", "std-a")
    # исполнение: даже если в сохранённом профиле исполнитель дороже (старые данные) — берётся модель босса
    bad = {"style": "boss", "main": {"model": "std-a"}, "workers": {"model": "prem-a"}}
    assert runprofile.resolve_for_task(bad, "subagent_execution", 1, r) == ("standard_reasoning", "std-a")
    assert runprofile.root_role(prof) == "orchestrator" and runprofile.root_role({"style": "solo"}) == "general"


def test_audit_levels():
    r, p = _router(("std-a", "standard_reasoning"))
    with session_scope() as db:
        s = repo.create_session(db, "au")
        t = repo.create_task(db, s, role="general", objective="x")
        off = asyncio.run(run_audit(db, s, t, r, level="off"))
        assert off.verdict == AuditVerdict.unaudited and p["std-a"].calls == 0
        repo.start_tool_call(db, s, t, "file_read", "general", "low", {})
        for c in repo.list_tool_calls(db, t.id):
            repo.finish_tool_call(db, c, __import__("aria.db.enums", fromlist=["ToolStatus"]).ToolStatus.ok, output_json={"a": 1})
        light = asyncio.run(run_audit(db, s, t, r, level="light"))
        assert light.verdict == AuditVerdict.pass_ and p["std-a"].calls == 0, "лёгкий аудит не зовёт модель"


def _client(r):
    app = FastAPI()
    app.include_router(chat_mod.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    app.state.router = r
    return TestClient(app)


def test_models_endpoint_and_profile_persistence_and_default():
    r, _ = _router(("std-a", "standard_reasoning"), ("free-a", "free_tier_reasoning"))
    c = _client(r)
    data = c.get("/chat/models").json()
    assert {m["id"] for m in data["models"]} == {"std-a", "free-a"}
    assert data["tiers"]["standard"] and data["tiers"]["free"] and not data["tiers"]["premium"]
    sid = c.post("/chat/sessions", json={}).json()["session_id"]
    body = {"style": "boss", "audit": "light", "thinking": "medium", "main": {"tier": "standard"}, "workers": {"model": "free-a"}}
    assert c.put(f"/chat/sessions/{sid}/profile", json=body).status_code == 200
    got = c.get(f"/chat/sessions/{sid}/profile").json()
    assert got["saved"] and got["profile"]["workers"] == {"model": "free-a"} and got["profile"]["audit"] == "light"
    # «после перезапуска»: профиль берётся из БД, новая сессия получает последний выбор
    sid2 = c.post("/chat/sessions", json={}).json()["session_id"]
    assert c.get(f"/chat/sessions/{sid2}/profile").json()["profile"]["style"] == "boss"
    assert c.put(f"/chat/sessions/{sid}/profile", json={"style": "swarm"}).status_code == 400
    assert c.put(f"/chat/sessions/{sid}/profile", json={"main": {"model": "nope"}}).status_code == 400


def test_chat_send_uses_selected_model():
    r, p = _router(("std-a", "standard_reasoning"), ("free-a", "free_tier_reasoning"))
    # реальные id не начинаются с "stub", иначе чат считает ответ заглушкой
    c = _client(r)
    sid = c.post("/chat/sessions", json={}).json()["session_id"]
    assert c.put(f"/chat/sessions/{sid}/profile", json={"main": {"model": "std-a"}}).status_code == 200
    res = c.post(f"/chat/sessions/{sid}/send", json={"content": "hi"})
    assert res.status_code == 200, res.text
    assert res.json()["provider_id"] == "std-a" and p["free-a"].calls == 0

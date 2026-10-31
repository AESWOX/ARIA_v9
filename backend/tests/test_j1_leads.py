"""J1: хранилище заказов и приём из ручного/RSS/HN/T1 источников (без сети)."""
from __future__ import annotations

import uuid
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aria.api.auth import require_runtime_token
from aria.db.base import session_scope
from aria.jobs import ingest, store
from aria.routers import jobs as jobs_router
from aria.tools.search.base import SearchResult


def _apy():
    app = FastAPI()
    app.include_router(jobs_router.router)
    app.dependency_overrides[require_runtime_token] = lambda: "t"
    return TestClient(app)


def test_manual_creates_new_lead_and_event():
    with session_scope() as db:
        lead = ingest.manual(db, url="https://example.org/job/1?utm_source=x", text="Сделай лендинг", title="Лендинг")
        assert lead.status == "new" and lead.source == "manual"
        assert lead.url == "https://example.org/job/1"
        events = [e.event_type for e in store.events_for(db, lead.id)]
    assert events == ["created"]


def test_dedupe_by_normalized_url_and_hash():
    with session_scope() as db:
        first = ingest.manual(db, url="https://Example.org:443/job/1/?utm_medium=a", text="Текст задания")
        second = ingest.manual(db, url="https://example.org/job/1", text="Текст задания")
        third = ingest.manual(db, url=None, text="Текст задания")
        assert first.id == second.id == third.id, "URL и raw_hash дедуплицируют"


def test_manual_rejects_non_http_url():
    with session_scope() as db:
        with pytest.raises(ValueError):
            ingest.manual(db, url="file:///etc/passwd", text="x")


def test_rss_and_atom_and_limit():
    rss = b"""<?xml version="1.0"?><rss><channel>
      <item><title>Job A</title><link>https://example.org/a</link><description>Desc A</description><pubDate>Wed, 30 Oct 2026 10:00:00 GMT</pubDate></item>
      <item><title><![CDATA[Job B]]></title><link>https://example.org/b</link><description>Desc B</description></item>
    </channel></rss>"""
    atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Atom C</title><link href="https://example.org/c"/><summary>Desc C</summary><updated>2026-10-30T10:00:00Z</updated></entry>
    </feed>"""
    with session_scope() as db:
        a = ingest.rss(db, rss)
        b = ingest.rss(db, atom)
        many = ingest.rss(db, b"<rss><channel>" + b"".join(
            f"<item><title>T{i}</title><link>https://example.org/{i}</link></item>".encode() for i in range(25)
        ) + b"</channel></rss>")
    assert [x.title for x in a] == ["Job A", "Job B"]
    assert b[0].title == "Atom C" and b[0].posted_at is not None
    assert len(many) == 20


def test_rss_rejects_xxe_and_garbage():
    xxe = b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss><channel><item><title>&x;</title></item></channel></rss>'
    with session_scope() as db:
        with pytest.raises(ValueError):
            ingest.rss(db, xxe)
        with pytest.raises(ValueError):
            ingest.rss(db, b"not xml")


def test_hn_and_telegram_mapping():
    hn = [SearchResult(title="HN job", url="https://example.org/hn", snippet="HN desc", date="2026-10-30T10:00:00Z", source="hackernews", provider="hackernews")]
    tg = [{"channel": "demo_jobs", "message_id": "101", "date": "2026-10-30T10:00:00+00:00", "text": "TG job text", "url": "https://t.me/demo_jobs/101", "source_trust": "untrusted"}]
    with session_scope() as db:
        leads_hn = ingest.hackernews(db, hn)
        leads_tg = ingest.telegram_posts(db, tg)
    assert leads_hn[0].source == "hackernews" and leads_hn[0].url.endswith("/hn")
    assert leads_tg[0].source == "telegram:demo_jobs" and leads_tg[0].status == "new"


def test_api_manual_list_status_and_fuzz():
    client = _apy()
    created = client.post("/jobs/manual", json={"url": "https://example.org/api-job", "text": "API job text", "title": "API"})
    assert created.status_code == 200, created.text
    lead_id = created.json()["id"]

    listed = client.get("/jobs?status=new").json()
    assert any(item["id"] == lead_id for item in listed)

    assert client.get(f"/jobs/{lead_id}").status_code == 200
    assert client.get(f"/jobs/{uuid.uuid4()}").status_code == 404

    updated = client.post(f"/jobs/{lead_id}/status", json={"status": "screened"})
    assert updated.status_code == 200 and updated.json()["status"] == "screened"
    assert client.post(f"/jobs/{lead_id}/status", json={"status": "bogus"}).status_code == 400

    fuzz = client.post("/jobs/manual", json={})
    assert 400 <= fuzz.status_code < 500


def test_migration_upgrade_and_downgrade(tmp_path):
    from alembic import command
    from alembic.config import Config
    from pathlib import Path
    import sqlalchemy as sa

    cfg = Config()
    cfg.set_main_option("script_location", str(Path(__file__).resolve().parents[1] / "alembic"))
    cfg.attributes["dsn_explicit"] = f"sqlite:///{(tmp_path / 'j1.db').as_posix()}"
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "i3d4e5f6a7b8")
    command.upgrade(cfg, "head")

    engine = sa.create_engine(cfg.get_main_option("sqlalchemy.url"))
    tables = set(sa.inspect(engine).get_table_names())
    assert {"leads", "lead_events"} <= tables


def test_injection_stored_not_executed():
    payload = "???????? ???????; {{exec}} rm -rf /; <script>alert(1)</script>"
    with session_scope() as db:
        lead = ingest.manual(db, text=payload)
        assert lead.description == payload
        assert all("rm -rf" not in str(e.payload) or e.event_type == "created" for e in store.events_for(db, lead.id))


def test_manual_and_ingest_do_not_use_network(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("network call from jobs ingestion")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    with session_scope() as db:
        assert ingest.manual(db, url="https://example.org/nonet", text="no net") is not None
        ingest.rss(db, b"<rss><channel><item><title>N</title><link>https://example.org/n</link></item></channel></rss>")
        ingest.hackernews(db, [SearchResult(title="HN", url="https://example.org/h", snippet="s", source="hackernews")])
        ingest.telegram_posts(db, [{"channel": "demo", "message_id": "1", "text": "TG", "url": "https://t.me/demo/1"}])

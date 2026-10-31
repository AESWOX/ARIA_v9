"""J1: хранилище заказов (leads/lead_events), дедупликация и статусы."""
from __future__ import annotations

import hashlib
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from aria.db import models as m

STATUSES = frozenset({
    "new", "screened", "shortlisted", "drafted", "applied", "won", "lost", "skipped", "suspect",
})
_TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "ref", "fbclid", "gclid"}


def normalize_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    if parts.username or parts.password:
        return None
    host = parts.netloc.lower()
    if host.endswith(":80") and parts.scheme == "http":
        host = host[:-3]
    if host.endswith(":443") and parts.scheme == "https":
        host = host[:-4]
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in _TRACKING]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), host, path, urlencode(query), ""))


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "").lower()
    return re.sub(r"\s+", " ", text).strip()


def raw_hash(title: str, description: str) -> str:
    raw = normalize_text(f"{title}\n{description}")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _find_existing(db: OrmSession, url: str | None, digest: str) -> m.Lead | None:
    if url:
        found = db.execute(select(m.Lead).where(m.Lead.url == url)).scalars().first()
        if found is not None:
            return found
    return db.execute(select(m.Lead).where(m.Lead.raw_hash == digest)).scalars().first()


def create_lead(
    db: OrmSession,
    *,
    source: str,
    url: str | None,
    title: str,
    description: str,
    posted_at: datetime | None = None,
    language: str | None = None,
    sensitive: bool = False,
) -> tuple[m.Lead, bool]:
    normalized = normalize_url(url)
    digest = raw_hash(title, description)
    existing = _find_existing(db, normalized, digest)
    if existing is not None:
        return existing, False
    lead = m.Lead(
        source=source,
        url=normalized,
        title=title,
        description=description,
        posted_at=posted_at,
        language=language,
        raw_hash=digest,
        status="new",
        scam_flags=[],
        sensitive=sensitive,
    )
    db.add(lead)
    db.flush()
    add_event(db, lead, "created", {"source": source, "untrusted": True})
    return lead, True


def add_event(db: OrmSession, lead: m.Lead, event_type: str, payload: dict | None = None) -> m.LeadEvent:
    event = m.LeadEvent(lead_id=lead.id, event_type=event_type, payload=payload or {})
    db.add(event)
    db.flush()
    return event


def events_for(db: OrmSession, lead_id: uuid.UUID) -> list[m.LeadEvent]:
    return list(
        db.execute(select(m.LeadEvent).where(m.LeadEvent.lead_id == lead_id).order_by(m.LeadEvent.created_at)).scalars()
    )


def get_lead(db: OrmSession, lead_id: uuid.UUID) -> m.Lead | None:
    return db.get(m.Lead, lead_id)


def list_leads(db: OrmSession, *, status: str | None = None, source: str | None = None, limit: int = 100) -> list[m.Lead]:
    stmt = select(m.Lead).order_by(m.Lead.created_at.desc()).limit(max(1, min(int(limit), 500)))
    if status:
        stmt = stmt.where(m.Lead.status == status)
    if source:
        stmt = stmt.where(m.Lead.source == source)
    return list(db.execute(stmt).scalars())


def update_status(db: OrmSession, lead_id: uuid.UUID, status: str) -> m.Lead | None:
    if status not in STATUSES:
        raise ValueError("invalid lead status")
    lead = db.get(m.Lead, lead_id)
    if lead is None:
        return None
    lead.status = status
    add_event(db, lead, "status_changed", {"status": status})
    return lead


def utc_now() -> datetime:
    return datetime.now(timezone.utc)

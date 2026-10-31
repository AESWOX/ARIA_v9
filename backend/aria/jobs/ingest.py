"""J1: приём заказов из разрешённых источников (manual/RSS/HN/T1), без сети."""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from sqlalchemy.orm import Session as OrmSession

from aria.db import models as m
from aria.jobs import store

MAX_ITEMS = 20
MAX_TITLE = 300
MAX_DESCRIPTION = 20_000
MAX_RSS_BYTES = 2_097_152
_TAGS = re.compile(r"<[^>]+>")


def _clip(text: str, limit: int) -> str:
    value = html.unescape(_TAGS.sub("", str(text or "")))
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    try:
        if raw.endswith("Z"):
            return datetime.fromisoformat(raw[:-1] + "+00:00")
        parsed = datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(raw)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def manual(db: OrmSession, *, url: str | None = None, text: str | None = None, title: str | None = None, sensitive: bool = False) -> m.Lead:
    body = re.sub(r"\s+", " ", str(text or "")).strip()[:MAX_DESCRIPTION]
    normalized = store.normalize_url(url) if url else None
    if url and normalized is None:
        raise ValueError("manual url must be http(s)")
    clean_title = _clip(title or (body.split("\n")[0] if body else normalized or ""), MAX_TITLE)
    if not normalized and not body:
        raise ValueError("manual lead needs url or text")
    lead, _created = store.create_lead(
        db, source="manual", url=normalized, title=clean_title, description=body, sensitive=sensitive,
    )
    return lead


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(item: ET.Element, names: tuple[str, ...]) -> str:
    for child in list(item):
        if _local(child.tag) in names:
            return (child.text or "").strip()
    return ""


def _entry_link(item: ET.Element) -> str:
    for child in list(item):
        if _local(child.tag) != "link":
            continue
        href = child.attrib.get("href")
        if href:
            return href.strip()
        if (child.text or "").strip():
            return (child.text or "").strip()
    return ""


def rss(db: OrmSession, xml_bytes: bytes) -> list[m.Lead]:
    if not isinstance(xml_bytes, (bytes, bytearray)):
        raise ValueError("rss bytes required")
    if len(xml_bytes) > MAX_RSS_BYTES:
        raise ValueError("rss payload too large")
    lowered = bytes(xml_bytes).lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise ValueError("rss DTD/ENTITY is not allowed")
    try:
        root = ET.fromstring(bytes(xml_bytes))
    except ET.ParseError as exc:
        raise ValueError("invalid RSS/Atom") from exc
    leads: list[m.Lead] = []
    for item in root.iter():
        if _local(item.tag) not in ("item", "entry"):
            continue
        title = _clip(_child_text(item, ("title",)), MAX_TITLE)
        link = store.normalize_url(_entry_link(item))
        description = _clip(
            _child_text(item, ("description", "summary", "content", "encoded")), MAX_DESCRIPTION,
        )
        date = _parse_date(_child_text(item, ("pubdate", "published", "updated")))
        if not title and not description:
            continue
        lead, _created = store.create_lead(
            db, source="rss", url=link, title=title or link or "RSS item",
            description=description, posted_at=date,
        )
        leads.append(lead)
        if len(leads) >= MAX_ITEMS:
            break
    return leads


def hackernews(db: OrmSession, results: list) -> list[m.Lead]:
    leads: list[m.Lead] = []
    for item in list(results or [])[:MAX_ITEMS]:
        title = _clip(getattr(item, "title", "") if not isinstance(item, dict) else item.get("title"), MAX_TITLE)
        url = getattr(item, "url", None) if not isinstance(item, dict) else item.get("url")
        description = _clip(getattr(item, "snippet", "") if not isinstance(item, dict) else item.get("snippet"), MAX_DESCRIPTION)
        date = getattr(item, "date", None) if not isinstance(item, dict) else item.get("date")
        if not title and not url:
            continue
        lead, _created = store.create_lead(
            db, source="hackernews", url=url, title=title or str(url or "HN item"),
            description=description, posted_at=_parse_date(date if isinstance(date, str) else None),
        )
        leads.append(lead)
    return leads


def telegram_posts(db: OrmSession, posts: list[dict]) -> list[m.Lead]:
    leads: list[m.Lead] = []
    for post in list(posts or [])[:MAX_ITEMS]:
        if not isinstance(post, dict):
            continue
        text = _clip(post.get("text"), MAX_DESCRIPTION)
        if not text:
            continue
        channel = str(post.get("channel") or "unknown")
        lead, _created = store.create_lead(
            db, source=f"telegram:{channel}", url=post.get("url"), title=text.split("\n")[0][:MAX_TITLE],
            description=text, posted_at=_parse_date(post.get("date")),
        )
        leads.append(lead)
    return leads

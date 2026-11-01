"""J3: черновик отклика из фактов профиля. Ничего не отправляет."""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy.orm import Session as OrmSession

from aria.config import get_settings
from aria.jobs import store
from aria.llm.providers.base import ChatMessage

logger = logging.getLogger("local_agent.jobs.drafts")

_AI_TEXT = "Использован ИИ-ассистент."
_STRUCTURE = (
    "Структура: 1) понял задачу; 2) что сделаю; 3) срок и цена; 4) один вопрос уточнения. "
    "Факты (опыт, клиенты, годы, ссылки) — ТОЛЬКО из профиля исполнителя и текста заказа. "
    "Не выдумывай опыт и клиентов. Не отправляй ничего, это черновик."
)
_STOP_NAMES = {
    "python", "react", "javascript", "typescript", "киев", "москва", "лондон",
    "здравствуйте", "привет", "спасибо", "уточните", "срок", "цена", "работа", "задача",
}
_CAP = re.compile(r"[A-ZА-ЯЁ][a-zа-яё-]{2,}")
_URL = re.compile(r"https?://[^\s<>\"')]+")
_BARE = re.compile(r"\b[\w-]+\.(?:com|org|net|io|dev|ai|ru|co|uk|es|me)\b", re.IGNORECASE)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
_EXP = re.compile(r"(\d{1,2})\s*(?:лет|года|years?)", re.IGNORECASE)

_LANG_NAMES = {"ru": "русский", "uk": "украинский", "en": "English", "es": "Español"}


def _values(obj) -> list[str]:
    if isinstance(obj, dict):
        out: list[str] = []
        for value in obj.values():
            out.extend(_values(value))
        return out
    if isinstance(obj, (list, tuple)):
        out = []
        for value in obj:
            out.extend(_values(value))
        return out
    return [str(obj)] if obj is not None else []


def _allowed(profile: dict, lead) -> str:
    parts = _values(profile)
    if lead is not None:
        for field in ("title", "description", "url", "language"):
            value = getattr(lead, field, None) if not isinstance(lead, dict) else lead.get(field)
            if value:
                parts.append(str(value))
        budget = getattr(lead, "budget_usd_est", None) if not isinstance(lead, dict) else lead.get("budget_usd_est")
        if budget is not None:
            parts.append(str(budget))
    if profile.get("ai_disclosure", True):
        parts.append(_AI_TEXT)
        parts.append(profile.get("ai_disclosure_text") or _AI_TEXT)
    return store.normalize_text(" ".join(parts))


def verify_draft(text: str, profile: dict, lead=None) -> list[dict]:
    allowed_blob = _allowed(profile or {}, lead)
    allowed_urls = set()
    for match in _URL.finditer(allowed_blob):
        normalized = store.normalize_url(match.group(0))
        if normalized:
            allowed_urls.add(normalized)
    findings: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str) -> None:
        key = (kind, value.lower())
        if key not in seen:
            seen.add(key)
            findings.append({"kind": kind, "value": value})

    raw = text or ""
    for match in _URL.finditer(raw):
        candidate = match.group(0).rstrip(".,;:!?)]}")
        url = store.normalize_url(candidate)
        if url and url not in allowed_urls:
            add("url", candidate)
    for match in _BARE.finditer(raw):
        token = match.group(0)
        if not any(token.lower() in url.lower() for url in allowed_urls):
            add("url", token)
    allowed_years = set(_YEAR.findall(allowed_blob))
    for year in _YEAR.findall(raw):
        if year not in allowed_years:
            add("year", year)
    allowed_numbers = set(re.findall(r"\d+", allowed_blob))
    for match in _EXP.finditer(raw):
        if match.group(1) not in allowed_numbers:
            add("experience", match.group(0))
    for match in _CAP.finditer(raw):
        token = match.group(0)
        before = raw[: match.start()].rstrip()
        if not before or before[-1] in ".!?\n":
            continue
        if store.normalize_text(token) in allowed_blob:
            continue
        if before.endswith(("-", "*", ":", "?", "?", "?")) or re.search(r"\d\)$", before):
            continue
        lowered = token.lower()
        if any(lowered.startswith(stop[:5]) for stop in _STOP_NAMES if len(stop) >= 4):
            continue
        if any(part in allowed_blob for part in lowered.split("-")):
            continue
        add("company", token)
    return findings


def _pick_language(lead, profile: dict, explicit: str | None) -> str:
    if explicit:
        return explicit
    language = getattr(lead, "language", None) if not isinstance(lead, dict) else lead.get("language")
    if language:
        return str(language)
    languages = (profile or {}).get("languages") or []
    if isinstance(languages, str):
        languages = [languages]
    if languages:
        return str(languages[0])
    text = getattr(lead, "description", "") if not isinstance(lead, dict) else lead.get("description", "")
    lowered = (text or "").lower()
    if any(ch in lowered for ch in "іїєґ"):
        return "uk"
    if any(ch in lowered for ch in "áéíóúñ¿"):
        return "es"
    if re.search(r"[а-яё]", lowered):
        return "ru"
    return "en"


def _with_disclosure(text: str, profile: dict) -> str:
    if not profile.get("ai_disclosure", True):
        return text
    marker = profile.get("ai_disclosure_text") or _AI_TEXT
    if marker.lower() in (text or "").lower():
        return text
    return f"{text.rstrip()}\n\n{marker}"


async def draft_reply(
    db: OrmSession, lead, router, profile: dict | None = None, language: str | None = None,
) -> dict:
    profile = profile if profile is not None else get_settings().jobs_profile_dict
    lang = _pick_language(lead, profile, language)
    task = {
        "title": str(getattr(lead, "title", "") or "")[:200],
        "description": str(getattr(lead, "description", "") or "")[:1500],
        "budget": getattr(lead, "budget_usd_est", None),
    }
    messages = [
        ChatMessage(role="system", content=f"{_STRUCTURE}\nЯзык ответа: {_LANG_NAMES.get(lang, lang)} ({lang})."),
        ChatMessage(role="user", content=json.dumps({"order": task, "profile": profile}, ensure_ascii=False)),
    ]
    findings: list[dict] = []
    draft = ""
    attempts = 0
    for attempt in range(2):
        attempts = attempt + 1
        result = await router.route_chat(
            "standard_reasoning", messages, tools=[],
            allow_degrade=True, resilient=True,
            fallback_classes=() if bool(getattr(lead, "sensitive", False)) else ("free_tier_reasoning",),
            purpose="other", sensitive=bool(getattr(lead, "sensitive", False)),
        )
        draft = str(result.response.text or "").strip()
        findings = verify_draft(draft, profile, lead)
        if not findings:
            break
        messages = messages + [
            ChatMessage(role="assistant", content=draft),
            ChatMessage(role="user", content="Исправь выдуманные факты: " + json.dumps(findings, ensure_ascii=False)),
        ]
    if not draft or findings:
        store.add_event(db, lead, "draft_failed", {"findings": findings})
        logger.warning("draft verification failed: %s", findings)
        return {"draft": None, "findings": findings, "attempts": attempts}
    final = _with_disclosure(draft, profile)
    store.add_event(db, lead, "draft_ready", {"draft": final, "findings": []})
    if lead.status not in ("applied", "won", "lost"):
        lead.status = "drafted"
    return {"draft": final, "findings": [], "attempts": attempts}

"""J3: черновик отклика из фактов профиля; без отправки и без сети."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from aria.db import models as m
from aria.db.base import session_scope
from aria.jobs import drafts, ingest, store
from aria.llm.providers.base import LlmProvider, LlmResponse
from aria.llm.router import ProviderRouter

PROFILE = {
    "skills": ["Python", "React"],
    "rates": "25 USD/hour",
    "languages": ["ru", "en"],
    "portfolio": ["https://portfolio.example/me"],
    "tone": "friendly",
    "ai_disclosure": True,
    "founded": "2021",
}


class _Router:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0
        self.kwargs = {}
        self.messages = []
        self.provider_class = None

    async def route_chat(self, *args, **kwargs):
        self.calls += 1
        self.provider_class = args[0] if args else kwargs.get("provider_class")
        self.kwargs = kwargs
        self.messages = args[1] if len(args) > 1 else kwargs.get("messages")
        return SimpleNamespace(response=LlmResponse(text=self.answers.pop(0)), provider_id="fake")


class _UsageProvider(LlmProvider):
    def __init__(self, text: str):
        self.provider_id = "deepseek-chat"
        self.provider_class = "standard_reasoning"
        self.model = "deepseek-flash"
        self.text = text

    async def check_connectivity(self, timeout_sec):
        return True

    async def chat(self, messages, tools, timeout_sec):
        return LlmResponse(text=self.text, usage={"prompt_tokens": 50, "completion_tokens": 10})


def _lead(text="Нужен лендинг на Python, бюджет 300 USD", language="ru"):
    with session_scope() as db:
        lead = ingest.manual(db, text=text, title="Landing")
        lead.budget_usd_est = 300
        lead.language = language
        return lead.id


GOOD = "Понял задачу. Сделаю лендинг на Python. Срок 3 дня, 25 USD/hour. Уточните сроки? (Использован ИИ-ассистент.)"


def test_verifier_true_negatives_and_false_positives():
    ok = drafts.verify_draft(
        "Здравствуйте! Работаю с Python и React. Портфолио: https://portfolio.example/me. 2021 год, 25 USD/hour.",
        PROFILE,
    )
    assert ok == []

    bad = drafts.verify_draft("Работал с Google в 2019, 10 лет опыта. https://fake.example/x", PROFILE)
    kinds = {f["kind"] for f in bad}
    assert {"company", "year", "experience", "url"} <= kinds


def test_verifier_handles_bare_domains_and_empty_profile():
    assert drafts.verify_draft("Смотрите example.com", {})[0]["kind"] == "url"
    assert drafts.verify_draft("", {}) == []


@pytest.mark.anyio
async def test_draft_success_one_call_language_and_status(monkeypatch):
    lead_id = _lead()
    router = _Router([GOOD])
    with session_scope() as db:
        lead = db.get(m.Lead, lead_id)
        result = await drafts.draft_reply(db, lead, router, profile=PROFILE, language="ru")
        events = store.events_for(db, lead.id)
        status = lead.status

    assert router.calls == 1 and result["draft"] and status == "drafted"
    assert any(e.event_type == "draft_ready" and e.payload.get("draft") for e in events)
    prompt = json.dumps([getattr(m, "content", "") for m in router.messages], ensure_ascii=False)
    assert "русский" in prompt
    assert router.kwargs.get("tools") == [] and router.kwargs.get("purpose") == "other"
    assert "ИИ" in result["draft"]  # ai_disclosure по умолчанию


@pytest.mark.anyio
async def test_draft_retries_on_hallucination_and_fails_clean(tmp_path, monkeypatch, caplog):
    lead_id = _lead()
    bad = "Работал с Google в 2019, 10 лет опыта."
    router = _Router([bad, GOOD])
    with session_scope() as db:
        lead = db.get(m.Lead, lead_id)
        result = await drafts.draft_reply(db, lead, router, profile=PROFILE, language="ru")
    assert router.calls == 2 and result["draft"] == GOOD

    lead_id2 = _lead(text="Second order: React, budget 400 USD")
    router2 = _Router([bad, bad])
    with session_scope() as db:
        lead = db.get(m.Lead, lead_id2)
        failed = await drafts.draft_reply(db, lead, router2, profile=PROFILE, language="ru")
        events = store.events_for(db, lead.id)
        status = lead.status
    assert router2.calls == 2 and failed["draft"] is None and status == "new"
    assert any(e.event_type == "draft_failed" for e in events)
    assert "draft verification failed" in caplog.text


@pytest.mark.anyio
async def test_sensitive_lead_uses_sensitive_route():
    lead_id = _lead()
    with session_scope() as db:
        lead = db.get(m.Lead, lead_id)
        lead.sensitive = True
    router = _Router([GOOD])
    with session_scope() as db:
        lead = db.get(m.Lead, lead_id)
        await drafts.draft_reply(db, lead, router, profile=PROFILE)
    assert router.kwargs.get("sensitive") is True


@pytest.mark.anyio
async def test_usage_row_written_by_router():
    lead_id = _lead()
    router = ProviderRouter()
    router.register(_UsageProvider(GOOD))
    with session_scope() as db:
        lead = db.get(m.Lead, lead_id)
        await drafts.draft_reply(db, lead, router, profile=PROFILE)
    with session_scope() as db:
        rows = list(db.execute(select(m.LlmUsage).where(m.LlmUsage.purpose == "other")).scalars())
    assert len(rows) == 1


@pytest.mark.anyio
async def test_draft_does_no_network(monkeypatch):
    import socket

    def forbidden(*args, **kwargs):
        raise AssertionError("network from drafts")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    lead_id = _lead()
    router = _Router([GOOD])
    with session_scope() as db:
        lead = db.get(m.Lead, lead_id)
        result = await drafts.draft_reply(db, lead, router, profile=PROFILE)
    assert result["draft"]
    assert not hasattr(drafts, "send_reply") and not hasattr(drafts, "submit_reply")


def test_ai_disclosure_can_be_disabled():
    text = drafts._with_disclosure("Черновик", {"ai_disclosure": False})
    assert text == "Черновик"
    assert "ИИ" in drafts._with_disclosure("Черновик", {"ai_disclosure": True})


def test_verifier_hyphen_bullet_city_false_positives():
    assert drafts.verify_draft("Сделаю Python-скрипт. - Портфолио: https://portfolio.example/me. Работал в Лондоне.", PROFILE) == []
    assert drafts.verify_draft("Смотрите [ссылка](https://portfolio.example/me).", PROFILE) == []


@pytest.mark.anyio
async def test_status_guard_and_custom_disclosure():
    lead_id = _lead(text="Третий заказ: Python, бюджет 500 USD")
    with session_scope() as db:
        lead = db.get(m.Lead, lead_id)
        lead.status = "applied"
    router = _Router([GOOD])
    profile = {**PROFILE, "ai_disclosure_text": "AI assistance was used."}
    with session_scope() as db:
        lead = db.get(m.Lead, lead_id)
        result = await drafts.draft_reply(db, lead, router, profile=profile)
    assert result["draft"].endswith("AI assistance was used.") and lead.status == "applied"


@pytest.mark.anyio
async def test_provider_class_sensitive_and_truncation():
    lead_id = _lead(text="Четвёртый заказ: Python, бюджет 700 USD")
    with session_scope() as db:
        lead = db.get(m.Lead, lead_id)
        lead.title = "x" * 400
        lead.description = "y" * 3000
        lead.sensitive = True
    router = _Router([GOOD])
    with session_scope() as db:
        lead = db.get(m.Lead, lead_id)
        await drafts.draft_reply(db, lead, router, profile=PROFILE)
    assert router.provider_class == "standard_reasoning"
    assert router.kwargs.get("sensitive") is True and router.kwargs.get("fallback_classes") == ()
    user = json.loads(router.messages[1].content)
    assert len(user["order"]["title"]) <= 200 and len(user["order"]["description"]) <= 1500


@pytest.mark.anyio
async def test_language_explicit_and_auto():
    lead_id = _lead(text="Пятый заказ: нужен лендинг")
    router = _Router([GOOD])
    with session_scope() as db:
        lead = db.get(m.Lead, lead_id)
        lead.language = None
        await drafts.draft_reply(db, lead, router, profile={k: v for k, v in PROFILE.items() if k != "languages"}, language="en")
    assert "English" in router.messages[0].content

    router2 = _Router([GOOD])
    with session_scope() as db:
        lead = db.get(m.Lead, lead_id)
        lead.language = None
        await drafts.draft_reply(db, lead, router2, profile={k: v for k, v in PROFILE.items() if k != "languages"})
    assert "русский" in router2.messages[0].content

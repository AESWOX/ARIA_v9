"""J2: признаки обмана, возраст домена (RDAP) и раскрытие коротких ссылок — без LLM."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import httpx
from urllib.parse import quote, urljoin

from aria.tools.handlers.web import validate_public_url

MAX_RDAP_BYTES = 200_000
RDAP_TIMEOUT_SEC = 8.0
MAX_REDIRECTS = 5

MARKERS: tuple[tuple[str, str, str], ...] = (
    ("prepayment", r"предоплат|оплат\w* впер[её]д|депозит|предоплата с вашей", "просят деньги вперёд"),
    ("messenger", r"мессенджер|whatsapp|telegram|телеграм|скайп|skype", "увод в мессенджер"),
    ("download_run", r"скача[йт]\w* и запуст|установи\w* программ|запусти\w* файл|\.exe\b", "скачать и запустить файл"),
    ("crypto", r"крипт|биткоин|bitcoin|usdt|кошел[её]к|wallet", "оплата криптой"),
    ("gift_cards", r"подарочн\w* карт|gift card|itunes card", "подарочные карты"),
    ("test_unpaid", r"тестов\w* задани\w*|пробн\w* задани\w*", "тестовое задание без оплаты"),
    ("pay_after", r"оплата после|оплата потом|без договора|без гарантий", "оплата после работы без гарантий"),
    ("documents", r"паспорт|паспортн|документ\w* личности|селфи с", "просят личные документы"),
    ("deposit", r"переведи\w* депозит|залог|взнос", "требуют залог"),
    ("materials_money", r"деньги за материал|оплати\w* материал|возмести\w* расход", "просят оплатить материалы"),
    ("offsite", r"личн\w* кабинет|вне площадки|напрямую мне", "работа вне площадки"),
    ("urgency", r"срочно[,!\s]|только сегодня|дедлайн сегодня", "искусственная срочность"),
    ("bank_details", r"реквизит|карт[ау] для перевода|номер карты", "просят банковские реквизиты"),
    ("contact_offsite", r"напиши\w* на почту|email me|в личку", "контакт вне площадки"),
    ("too_good", r"легк\w* деньг|быстрый доход|без опыта.*много", "слишком выгодные условия"),
)


def scan_text(text: str) -> list[dict]:
    lowered = (text or "").lower()
    found = []
    for code, pattern, reason in MARKERS:
        if re.search(pattern, lowered):
            found.append({"code": code, "reason": reason})
    return found


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _default_rdap(host: str) -> dict:
    validate_public_url(f"http://{host}/")
    async with httpx.AsyncClient(timeout=RDAP_TIMEOUT_SEC, follow_redirects=False) as client:
        async with client.stream("GET", f"https://rdap.org/domain/{quote(host, safe='')}") as response:
            if response.status_code != 200:
                return {}
            buf = bytearray()
            async for chunk in response.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_RDAP_BYTES:
                    return {}
            try:
                data = json.loads(bytes(buf).decode("utf-8", "replace"))
            except ValueError:
                return {}
            return data if isinstance(data, dict) else {}


async def domain_age_days(host: str, fetch=None) -> int | None:
    fetcher = fetch or _default_rdap
    try:
        data = await fetcher(host)
    except Exception:  # noqa: BLE001 — RDAP не обязателен
        return None
    events = data.get("events") if isinstance(data, dict) else None
    for event in events or []:
        if not isinstance(event, dict):
            continue
        action = str(event.get("eventAction") or "").lower()
        if action == "registration":
            registered = _parse_time(event.get("eventDate"))
            if registered is not None:
                return max(0, (datetime.now(timezone.utc) - registered).days)
    return None


async def _default_expand(url: str) -> str:
    current = url
    async with httpx.AsyncClient(timeout=RDAP_TIMEOUT_SEC, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            validate_public_url(current)
            response = await client.head(current)
            location = response.headers.get("Location")
            if response.status_code in (301, 302, 303, 307, 308) and location:
                current = urljoin(current, location)
                continue
            return str(response.url)
    raise ValueError("too many redirects")


async def expand_short_url(url: str, expand=None) -> str:
    validate_public_url(url)
    final = await (expand or _default_expand)(url)
    return validate_public_url(final)


def check_lead(lead, *, age_days: int | None = None) -> dict:
    url = lead.get("url") if isinstance(lead, dict) else getattr(lead, "url", None)
    description = lead.get("description") if isinstance(lead, dict) else getattr(lead, "description", "")
    title = lead.get("title") if isinstance(lead, dict) else getattr(lead, "title", "")
    flags = scan_text(f"{title or ''}\n{description or ''}")
    if url:
        try:
            validate_public_url(str(url))
        except Exception:  # noqa: BLE001 — внутренние/небезопасные URL
            flags.append({"code": "unsafe_url", "reason": "URL не проходит проверку безопасности"})
    if age_days is not None and age_days < 30:
        flags.append({"code": "young_domain", "reason": f"домен моложе 30 дней ({age_days})"})
    return {"flags": flags, "suspect": bool(flags)}

"""app/llm/compression.py

Компрессия истории сообщений — ОТДЕЛЬНАЯ подсистема от основной ротации
ответов агента (app/llm/router.py). Собственный KeyPool, собственный
курсор ротации 1-3-5-...-9 по Gemini-ключам, не пересекается с тем,
каким ключом отвечает агент прямо сейчас.

Правила (из старого config.yaml, reference-config/compression-settings.yaml):
  - protect_first_n / protect_last_n сообщений не трогаем никогда
  - если сообщений в сессии больше compression_hard_message_limit —
    средний диапазон схлопывается в одну summary-запись
  - оригиналы НЕ удаляются из БД (compressed_out=True), только исключаются
    из промпта (list_messages_for_prompt) — аудируемость сохраняется
  - вызывается через Gemini 2.5 Flash (дешёвая быстрая модель)

Точка входа: maybe_compress(db, session) — вызывается в начале каждой
итерации loop.py, до сборки messages для LLM.
"""
from __future__ import annotations

import json
import logging

import httpx
from sqlalchemy import func, select

from aria.config import get_settings
from aria.db import models as m
from aria.db import repository as repo
from aria.db.enums import TaskStatus
from aria.llm.key_pool import KeyPool, NoAvailableKeys

logger = logging.getLogger("local_agent.compression")

SUMMARY_PREFIX = "[compression summary"
_CHARS_PER_TOKEN = 3  # консервативно для кириллицы/кода: лучше сжать раньше, чем упереться в лимит
_TOOL_TEXT_CAP = 2000  # символов результата тула, которые видит суммаризатор
_MSG_TEXT_CAP = 8000

_compression_pool: KeyPool | None = None


def _get_compression_pool() -> KeyPool | None:
    """Ленивая инициализация — свой собственный, полностью независимый
    от router.py курсор ротации по тому же (или отдельно заданному)
    списку Gemini-ключей."""
    global _compression_pool
    settings = get_settings()
    if not settings.compression_enabled:
        return None
    keys = settings.compression_gemini_api_keys_list
    if not keys:
        return None
    if _compression_pool is None:
        _compression_pool = KeyPool(keys, name="compression")
    return _compression_pool


def estimate_tokens(text: str | None) -> int:
    return (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN if text else 0


def _message_text(msg: m.Message, cap: int) -> str:
    """Текст сообщения для оценки/суммаризации; у тулов — их результат, а не только «tool -> status»."""
    text = msg.content or ""
    cj = msg.content_json
    if msg.role == "tool" and isinstance(cj, dict) and cj.get("output") is not None:
        try:
            text = f"{text}: {json.dumps(cj['output'], ensure_ascii=False, default=str)}"
        except (TypeError, ValueError):
            pass
    return text[:cap]


def history_tokens(history: list[m.Message]) -> int:
    """Оценка токенов промпта сессии (результаты тулов — в том виде, как их режет loop: до 6000 символов)."""
    return sum(estimate_tokens(_message_text(msg, 6000)) for msg in history)


def _needs_compression(history: list[m.Message], settings) -> bool:
    if settings.compression_token_threshold > 0 and history_tokens(history) > settings.compression_token_threshold:
        return True
    limit = settings.compression_hard_message_limit
    return limit > 0 and len(history) > limit


def _protected_tail_start(history: list[m.Message], last_n: int) -> int:
    """Индекс, с которого история остаётся дословной: последние last_n сообщений И всё от
    последнего сообщения пользователя (текущий ход: вызовы и результаты тулов не теряются)."""
    start = max(0, len(history) - last_n)
    for i in range(len(history) - 1, -1, -1):
        if history[i].role == "user":
            return min(start, i)
    return start


async def _summarize(text_block: str) -> str:
    settings = get_settings()
    pool = _get_compression_pool()
    if pool is None:
        raise RuntimeError("compression pool not configured (no compression keys)")

    prompt = (
        "Сожми диалог ниже в краткое фактологическое summary для памяти агента. "
        "Сохрани: принятые решения, важные факты, незавершённые задачи. "
        "Не сохраняй: технический шум, повторы, приветствия.\n\n"
        f"{text_block}"
    )

    last_error: Exception | None = None
    for _ in range(len(pool)):
        try:
            key = pool.next_key()
        except NoAvailableKeys as exc:
            last_error = exc
            break
        try:
            async with httpx.AsyncClient(timeout=settings.compression_timeout_sec) as client:
                resp = await client.post(
                    f"{settings.compression_base_url}/chat/completions",
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                    json={
                        "model": settings.compression_model,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data["choices"][0]["message"]["content"] or ""
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 429:
                pool.mark_rate_limited(key)
                last_error = exc
                continue
            if status in (401, 403):
                pool.mark_dead(key)
                last_error = exc
                continue
            raise
    raise last_error or NoAvailableKeys("compression pool exhausted")


async def maybe_compress(session_id) -> bool:
    """Возвращает True если сжатие реально произошло в этом вызове.

    ВАЖНО: не держит db-транзакцию открытой во время сетевого запроса —
    иначе долгий/ретраящийся HTTP к провайдеру компрессии держит writer-lock
    SQLite и валит параллельные записи (event_bus.emit и т.д.) с
    'database is locked' по истечении busy_timeout."""
    settings = get_settings()
    if not settings.compression_enabled:
        return False

    from aria.db.base import session_scope  # локальный импорт, чтобы не плодить циклы

    with session_scope() as db:
        session = repo.get_session(db, session_id)
        # Задача ждёт Approve — контекст не трогаем: возобновление должно увидеть ровно то, что видел агент.
        if db.execute(
            select(func.count()).select_from(m.Task).where(
                m.Task.session_id == session.id, m.Task.status == TaskStatus.awaiting_attention
            )
        ).scalar_one():
            return False
        history = repo.list_messages_for_prompt(db, session.id, limit=10_000)
        if not _needs_compression(history, settings):
            return False

        first_n = settings.compression_protect_first_n
        tail_start = _protected_tail_start(history, settings.compression_protect_last_n)
        if tail_start <= first_n:
            return False

        middle = history[first_n:tail_start]
        if not middle:
            return False

        text_block = "\n".join(
            f"[{msg.role}] {_message_text(msg, _TOOL_TEXT_CAP if msg.role == 'tool' else _MSG_TEXT_CAP)}"
            for msg in middle
            if msg.content or msg.content_json
        )
        middle_ids = [msg.id for msg in middle]
        # диапазон по реальным seq_no сжимаемых строк (включая вложенные прежние сводки)
        seq_range = [min(msg.seq_no for msg in middle), max(msg.seq_no for msg in middle)]
        middle_count = len(middle)
    # ---- db-сессия закрыта, транзакции нет — теперь идём в сеть ----

    try:
        summary_text = await _summarize(text_block)
    except Exception as exc:  # noqa: BLE001
        logger.warning("compression failed for session %s, leaving history uncompressed: %s", session_id, exc)
        return False

    # ---- отдельная короткая сессия только на запись результата ----
    with session_scope() as db:
        session = repo.get_session(db, session_id)
        repo.append_message(
            db,
            session,
            role="system",
            content=f"{SUMMARY_PREFIX} of {middle_count} messages]\n{summary_text}",
            content_json={
                "type": "compression_summary",
                "compressed_message_count": middle_count,
                "compressed_seq_range": seq_range,
            },
        )
        repo.mark_messages_compressed(db, middle_ids)

    logger.info("session %s: compressed %d messages into 1 summary", session_id, middle_count)
    return True
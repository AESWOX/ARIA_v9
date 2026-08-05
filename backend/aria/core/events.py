"""core/events.py — §10.2 WS events + §10.3 backfill.

Событие сначала персистится в таблицу `events` (иначе reconnect/backfill
невозможен и стейт живёт только в памяти — запрещено принципом §4), затем
рассылается всем живым WS-подписчикам через asyncio.Queue.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from aria.db import repository as repo
from aria.db.base import session_scope


class EventBus:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def emit(
        self,
        event_type: str,
        payload: dict[str, Any],
        session_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        db=None,
    ) -> dict:
        """Если передан db (уже открытая сессия вызывающего кода) — переиспользуем
        её вместо открытия новой. Открывать вторую сессию поверх уже открытой
        в том же потоке/процессе — гарантированный self-deadlock на SQLite
        (writer-lock ждёт сам себя до истечения busy_timeout)."""
        if db is not None:
            event = repo.persist_event(db, event_type, payload, session_id=session_id, task_id=task_id)
            envelope = {
                "event_id": str(event.event_id),
                "event_type": event.event_type,
                "session_id": str(session_id) if session_id else None,
                "task_id": str(task_id) if task_id else None,
                "ts": event.ts.astimezone(timezone.utc).isoformat(),
                "payload": payload,
            }
        else:
            with session_scope() as db_local:
                event = repo.persist_event(db_local, event_type, payload, session_id=session_id, task_id=task_id)
                envelope = {
                    "event_id": str(event.event_id),
                    "event_type": event.event_type,
                    "session_id": str(session_id) if session_id else None,
                    "task_id": str(task_id) if task_id else None,
                    "ts": event.ts.astimezone(timezone.utc).isoformat(),
                    "payload": payload,
                }
        for q in list(self._subscribers):
            try:
                q.put_nowait(envelope)
            except asyncio.QueueFull:
                pass
        return envelope


event_bus = EventBus()

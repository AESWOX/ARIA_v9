"""HTTP API facade.

Реальные FastAPI routes живут в тематических роутерах `aria.routers.*`.
Этот модуль — совместимая точка входа (structural anchor под ТЗ v7.1),
пере-экспортирующая ключевые объекты из их новых домов.
"""

from aria.main import app
from aria.routers.config import get_public_config, verify_pin
from aria.routers.sessions import (
    approve_attention,
    create_session,
    delete_session,
    export_session_markdown,
    get_session,
    list_attention_items,
    list_sessions,
    post_message,
    reject_attention,
)
from aria.routers.storage import get_b2_buckets, get_b2_object_meta, list_b2_objects
from aria.routers.system import get_health
from aria.routers.tasks import cancel_task, get_audit_reports, start_task
from aria.routers.vault import get_vault_note, get_vault_tree, put_vault_note, search_vault

__all__ = [
    "app",
    "get_health",
    "get_public_config",
    "verify_pin",
    "list_sessions",
    "create_session",
    "get_session",
    "delete_session",
    "export_session_markdown",
    "post_message",
    "list_attention_items",
    "get_vault_tree",
    "get_vault_note",
    "put_vault_note",
    "search_vault",
    "get_b2_buckets",
    "list_b2_objects",
    "get_b2_object_meta",
    "start_task",
    "cancel_task",
    "get_audit_reports",
    "approve_attention",
    "reject_attention",
]

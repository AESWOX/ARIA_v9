"""FastAPI entrypoint — app assembly only.

All HTTP routes live in thematic routers under ``aria.routers``:
providers, storage, sessions, tasks, system, config, vault.
Shared serializers/payload builders live in ``aria.http_utils``.
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sys
from typing import Any

from dotenv import load_dotenv

# Load .env BEFORE anything else — settings depend on it
from aria import paths as _paths

_paths.ensure_user_data()
load_dotenv(_paths.env_file())

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from aria.api.auth import token_store
from aria.config import get_settings
from aria.db import repository as repo
from aria.db.base import init_db, run_migrations, session_scope
from aria.db.enums import ProviderStatus
from aria.http_utils import seed_database
from aria.llm.router import build_default_router
from aria.routers.actions import router as actions_router
from aria.routers.analytics import router as analytics_router
from aria.routers.auth import router as auth_router
from aria.routers.config import router as config_router
from aria.routers.cron import router as cron_router
from aria.routers.env import router as env_router
from aria.routers.files import router as files_router
from aria.routers.logs import router as logs_router
from aria.routers.model import router as model_router
from aria.routers.profiles import router as profiles_router
from aria.routers.providers import router as providers_router
from aria.routers.sessions import router as sessions_router
from aria.routers.skills import router as skills_router
from aria.routers.storage import router as storage_router
from aria.routers.stubs import router as stubs_router
from aria.routers.system import router as system_router
from aria.routers.tasks import router as tasks_router
from aria.routers.tools import router as tools_router
from aria.routers.vault import router as vault_router

# --- Файловое логирование (иначе история живёт только в scrollback консоли
# и теряется при закрытии/переполнении терминала) ---
_LOG_DIR = str(_paths.log_dir())
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = os.path.join(_LOG_DIR, "backend.log")

_file_handler = logging.handlers.RotatingFileHandler(
    _LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
)
_file_handler.setLevel(logging.INFO)

_root_logger = logging.getLogger()
_root_logger.addHandler(_file_handler)
_root_logger.setLevel(logging.INFO)

# uvicorn пишет через свои собственные логгеры — цепляем хендлер и туда,
# иначе access/error логи uvicorn не попадут в файл.
for _uvicorn_logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    logging.getLogger(_uvicorn_logger_name).addHandler(_file_handler)

app = FastAPI(title="Local Agent v7.1", version="0.9.0")
settings = get_settings()
router = build_default_router()
app.state.router = router
_background_tasks: dict[str, asyncio.Task[Any]] = {}

# --- P1.5: warn when SQLite is used outside single-user local desktop mode ---
if settings.POSTGRES_DSN.startswith("sqlite") and (
    os.getenv("ARIA_SERVER_MODE", "0") == "1" or settings.http_host not in ("127.0.0.1", "localhost")
):
    logging.getLogger("local_agent.main").warning(
        "SQLite backend detected in server/multi-user mode — SQLite is only recommended "
        "for single-user local desktop use. For production/multi-user set POSTGRES_DSN "
        "to a real Postgres DSN."
    )

# ── CORS ─────────────────────────────────────────────────────────────
# Same-origin SPA mode (this backend serves desktop/dist itself), so the
# production allowlist is EMPTY: any cross-origin request is rejected by
# default and no preflight OPTIONS is ever accepted for prod traffic.
#
# DEV ONLY exception (vite HMR on http://localhost:1420): the Tauri dev
# window loads the vite dev server, which is a *different origin* from
# this backend. That is the single, deliberate, dev-only cross-origin
# hop in the whole system — do NOT add origins here for production.
_DEV_CORS_ORIGINS = [
    "http://localhost:1420",  # DEV ONLY — vite dev server (HMR)
]
_cors_origins = _DEV_CORS_ORIGINS if os.getenv("ARIA_DEV_CORS", "0") == "1" else []

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Strip /api prefix (Vite proxy equivalent in production) ──
# Raw ASGI middleware, not @app.middleware("http"): BaseHTTPMiddleware
# bypasses WebSocket scopes, which left the frontend's /api/ws upgrade
# unmatched against the /ws route (live-updates dead in Tauri mode).
class _StripApiPrefixMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path.startswith("/api/"):
                scope["orig_path"] = path  # preserved for the SPA fallback check
                scope["path"] = path[4:]
            elif path == "/api":
                scope["orig_path"] = path
                scope["path"] = "/"
        return await self.app(scope, receive, send)


app.add_middleware(_StripApiPrefixMiddleware)


_app_logger = logging.getLogger("local_agent.main")


@app.middleware("http")
async def allow_private_network(request, call_next):
    try:
        response = await call_next(request)
    except Exception:
        # Гарантированный лог необработанного исключения в наш файл,
        # независимо от того, что uvicorn делает со своими логгерами
        # (dictConfig может перетереть handlers на uvicorn/uvicorn.error
        # в зависимости от порядка старта — сюда это не влияет).
        _app_logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
        raise
    response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response


for _r in (
    actions_router,
    analytics_router,
    auth_router,
    config_router,
    cron_router,
    env_router,
    files_router,
    logs_router,
    model_router,
    profiles_router,
    providers_router,
    sessions_router,
    skills_router,
    storage_router,
    stubs_router,
    system_router,
    tasks_router,
    tools_router,
    vault_router,
):
    app.include_router(_r)


# ── Same-origin SPA serving ──────────────────────────────────────────
# The backend serves the built frontend (desktop/dist) itself, so the webview
# and the API share one origin. The runtime token is embedded into the HTML
# at serve time (<meta name="runtime-token">), which replaces the old
# Rust→invoke→bootstrap handshake entirely.
#
# dist is resolved in this order:
#   1. PyInstaller bundle: dist/ is embedded next to the frozen app
#      (backend.spec datas) and lives under sys._MEIPASS.
#   2. Dev repo layout: backend/../desktop/dist (run_backend.py / pytest).
_DIST_DIR: Path
if getattr(sys, "_MEIPASS", None):  # frozen PyInstaller onefile
    _DIST_DIR = Path(sys._MEIPASS) / "dist"
else:
    _DIST_DIR = Path(__file__).resolve().parents[2] / "desktop" / "dist"
_ASSETS_DIR = _DIST_DIR / "assets"

_index_html: str | None = None


def _render_index() -> str:
    """index.html with the active runtime token injected into <head>."""
    global _index_html
    if _index_html is None:
        index_path = _DIST_DIR / "index.html"
        if not index_path.exists():
            return (
                "<html><body><h1>Frontend not built</h1>"
                "<p>Run <code>npm run build</code> in desktop/ first.</p></body></html>"
            )
        _index_html = index_path.read_text(encoding="utf-8")
    token = token_store.current_token()
    metas = [f'<meta name="runtime-token" content="{token}">']
    if token_store.first_run():
        metas.append('<meta name="first-run" content="true">')
    meta = "\n  ".join(metas)
    if "</head>" in _index_html:
        return _index_html.replace("</head>", meta + "</head>")
    return meta + _index_html


# Assets are served statically from dist/assets (hashed filenames).
if _ASSETS_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=_ASSETS_DIR), name="assets")


@app.get("/{full_path:path}", include_in_schema=False, response_model=None)
async def spa_fallback(request: Request, full_path: str) -> FileResponse | HTMLResponse:
    # Never swallow /api/* — a missing API route is a 404, not the SPA shell.
    # strip_api_prefix rewrote scope["path"], so check the ORIGINAL path here.
    orig = request.scope.get("orig_path", request.url.path)
    if orig.startswith("/api"):
        raise HTTPException(status_code=404, detail="Not Found")

    candidate = _DIST_DIR / full_path
    if full_path and candidate.is_file():
        return FileResponse(candidate)

    return HTMLResponse(_render_index())


@app.on_event("startup")
async def startup() -> None:
    init_db()
    run_migrations()
    token_store.issue()
    seed_database()
    from aria.db.skills_seed import seed_skills
    seed_skills()
    # L2 prod-release: дефолтные scheduler_jobs при первом старте (идемпотентно)
    from aria.scheduler.jobs import seed_default_scheduler_jobs
    seed_default_scheduler_jobs()
    # Register real providers from router into DB health table
    with session_scope() as db:
        for pclass, providers in router.providers_by_class.items():
            for p in providers:
                if p.provider_id != "stub-local":
                    repo.upsert_provider_health(
                        db,
                        p.provider_id,
                        label=p.provider_id,
                        provider_class=pclass,
                        status=ProviderStatus.active,
                    )
    # Attach router to app.state so self-test can find it
    app.state.router = router

    # Provider catalog refresh (background — не блокирует boot)
    from aria.scheduler.jobs import refresh_provider_models_job

    async def _bg_refresh_catalog() -> None:
        try:
            count = await refresh_provider_models_job(router)
            _app_logger.info("provider catalog refreshed: %s models", count)
        except Exception:
            _app_logger.exception("provider catalog refresh failed")

    _background_tasks["catalog_refresh"] = asyncio.create_task(_bg_refresh_catalog())

    # Periodic scheduler: TTL-expiry of stale attention items + provider catalog
    # refresh. Fix 0.3 — раньше expire_stale_attention_items_job нигде не
    # запускался (watchdog отсутствовал): stuck awaiting_attention задачи
    # висели бесконечно.
    from aria.scheduler.jobs import expire_stale_attention_items_job, refresh_provider_models_job

    _SCHEDULER_INTERVAL_SEC = 120

    async def _scheduler_loop() -> None:
        _app_logger.info("background scheduler started (interval=%ss)", _SCHEDULER_INTERVAL_SEC)
        while True:
            try:
                expired = expire_stale_attention_items_job()
                if expired:
                    _app_logger.info("expired %d stale attention item(s)", expired)
                count = await refresh_provider_models_job(router)
                _app_logger.debug("provider catalog refresh: %s models", count)
            except Exception:
                _app_logger.exception("scheduler tick failed")
            await asyncio.sleep(_SCHEDULER_INTERVAL_SEC)

    _background_tasks["scheduler_loop"] = asyncio.create_task(_scheduler_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    _app_logger.info("shutting down %d background task(s)", len(_background_tasks))
    for name, task in list(_background_tasks.items()):
        task.cancel()
    for name, task in list(_background_tasks.items()):
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            _app_logger.exception("background task %s failed during shutdown", name)
    _background_tasks.clear()

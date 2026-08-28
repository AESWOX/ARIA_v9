# ARIA v9 — WORK_NOTES: достройка бэкенд-роутеров

Дата: 2026-08-20
Цель: реализовать недостающие бэкенд-роутеры ARIA v9 по контрактам `desktop/src/lib/api.ts` до полного покрытия приложения.
Правило: никаких "готово" без прогона проверки (verify-or-die); каждый write → read того же пути.

---

## 1. Gap-анализ: методы apiClient vs существующие роутеры

### 1.1 Существующие роутеры (7 шт., импортированы в `main.py`)
- `routers/vault.py`: GET `/vault/tree`, `/vault/notes/{note_path:path}`, `/vault/search`; PUT `/vault/notes/{note_path:path}`.
- `routers/tasks.py`: POST `/tasks/{task_id}/start`, `/tasks/{task_id}/run-executor`, `/tasks/{task_id}/cancel`; GET `/tasks/{task_id}/children`, `/tasks/{task_id}/audit-reports`.
- `routers/system.py`: GET `/status`, `/health`, `/model/info`, `/system/self-test`; POST `/system/shutdown`, `/system/feedback`; WS `/ws` (loopback `?token=`, backfill по `last_event_id`).
- `routers/storage.py`: POST `/storage/b2/upload`, `/storage/vault/upload`; GET `/storage/b2/buckets`, `/storage/b2/objects`, `/storage/b2/objects/{key:path}/meta`.
- `routers/sessions.py`: GET `/sessions`, `/sessions/stats`, `/sessions/empty/count`, `/sessions/{session_id}`, `/sessions/{session_id}/export.md`, `/attention-items`; POST `/sessions`, `/sessions/{session_id}/messages`, `/attention-items/{item_id}/approve|reject`; DELETE `/sessions/{session_id}`.
- `routers/providers.py`: GET `/providers/models`.
- `routers/config.py`: GET `/config`, `/config/public`, `/auth/me`, `/profiles`, `/profiles/active`, `/dashboard/themes`, `/dashboard/font`, `/dashboard/plugins`; POST `/auth/verify-pin`, `/llm/inline-complete`, `/llm/transform`.

### 1.2 Контракты apiClient (источник истины — `desktop/src/lib/api.ts`)
Паттерн: `export const api = { ... }`, методы через `fetchJSON<T>(url, opts)`, URL с `${encodeURIComponent(...)}`, хелперы `profileQuery(profile)`/`appendProfileParam`. `PROFILE_SCOPED_PREFIXES` (строки 112–125).

**WS auth**: gated mode — POST `/api/auth/ws-ticket` → `{ticket, ttl_seconds}` (одноразовый, TTL=30), WS-upgrade `?ticket=`; loopback — `?token=` (текущий backend уже так работает). `logout` — POST `/auth/logout` вне `/api`.

### 1.3 Недостающие группы (нужно реализовать)

| Группа | Маршруты | Тип |
|---|---|---|
| files | GET/POST `/api/files`, POST `/api/files/mkdir`, POST `/api/files/upload-stream`, DELETE `/api/files` | real |
| logs | GET `/api/logs` | real |
| analytics | GET `/api/analytics/usage`, GET `/api/analytics/models` | real |
| env | GET `/api/env`, GET `/api/env/reveal` | real |
| cron | jobs CRUD+pause/resume/trigger, delivery-targets, blueprints+instantiate | real (scheduler/jobs.py существует) |
| skills | GET `/api/skills`, POST `/api/skills/toggle`, GET `/api/skills/content`, hub install/uninstall/update | real (DB SkillMeta) |
| toolsets | GET `/api/tools/toolsets`, PUT `/{name}`, config, provider, env, post-setup | real |
| model | GET `/api/model/options`, GET `/api/model/auxiliary`, POST `/api/model/set` | real |
| system stats | GET `/api/system/stats` | real |
| auth ws-ticket | POST `/api/auth/ws-ticket` → `{ticket, ttl_seconds}` (одноразовый, TTL=30) | real |
| sessions ext | GET `/api/sessions/empty`, POST `/api/sessions/bulk-delete`, POST `/api/sessions/prune`, GET `/api/sessions/search` | real |
| config ext | GET `/config/defaults`, `/config/schema`, GET/PUT `/config/raw`, PUT `/dashboard/theme` | real |
| profiles ext | POST/DELETE `/profiles`, PUT description/describe-auto/model, GET/PUT setup-command/soul | real |
| oauth | GET/DELETE `/api/providers/oauth`, POST `/{providerId}/start|submit|poll` | stub |
| gateway | POST `/api/gateway/start|stop|restart` | stub |
| mcp | GET/POST `/api/mcp/servers`, DELETE `/{name}`, POST `/{name}/test`, PUT `/{name}/enabled`, catalog | stub |
| ops | doctor, security-audit, backup, import, hooks, prompt-size, dump, config-migrate, debug-share, checkpoints+prune | stub |
| dashboard plugins | rescan, hub, agent-plugins install/update/remove, plugin-providers | stub |
| messaging | GET `/api/messaging/platforms`, telegram onboarding | stub |
| pairing | `/api/pairing*` | stub |
| webhooks | `/api/webhooks*` | stub |
| credentials | `/api/credentials/pool*` | stub |
| memory | `/api/memory*` | stub |
| curator | `/api/curator*` | stub |
| portal | `/api/portal*` | stub |
| update | POST `/api/aria/update`, GET `/api/aria/update/check`, GET `/api/actions/{name}/status` | stub |

### 1.4 Контракты типов (опорные секции api.ts)
- files: `FileEntry {name, path, type: 'file'|'dir', size, modified}`, upload-stream принимает FormData.
- env: `EnvVarInfo {name, value, source, is_secret}`, `reveal` возвращает секреты (только по паролю).
- cron: `SchedulerJob {id, name, schedule, enabled, ...}` (см. `scheduler/jobs.py:list_scheduler_jobs_payload`), blueprints.
- skills: SkillMeta из БД; hub install по `identifier`, параметр `profile`.
- model/set: `{model_id, auxiliary?, profile?}`; options/auxiliary — из `ProviderModel` + `TIER_HINTS`.
- system/stats: агрегаты по БД (sessions/tasks/events/tool_calls) + uptime.
- sessions: empty/bulk-delete `{ids}`, prune, search `{q}`.
- config/raw: YAML; defaults/schema — из pydantic `Settings`.
- profiles: CRUD + описание/модель/soul (markdown).
- OAuth/gateway/mcp/ops/dashboard/прочие: `ActionResponse {ok, error?, data?}`.
- ActionResponse (общий контракт стабов): `{ok: boolean, error?: string, data?: unknown}`.

## 2. Поддерживающие модули (существуют, прочитаны)
- `db/repository.py`, `db/base.py` (`session_scope`), `db/models.py` (390), `db/enums.py` (153, `TaskStatus` 12 значений + `TASK_TRANSITIONS`).
- `config.py` (219): `Settings(BaseSettings)`, `POSTGRES_DSN="sqlite:///./data/local_agent.db"`, `B2_*`, `LOCK_TTL_SECONDS=30`.
- `tools/registry.py` (595): `ToolSpec`, встроенные хендлеры.
- `api/auth.py` (`require_runtime_token`), `http_utils.py`, `scheduler/jobs.py`, `core/events.py` (`event_bus`), `core/rate_limit.py`.
- `core/` ещё: approvals, audit, delegate, executor, guardrails, integrity, locking, loop, plan_validator, roles, secretscanner, state_machine, notifiers/telegram.
- `integrations/mcp_server/server.py`, `integrations/notebooklm/`.
- `api/ws.py`, `api/http.py` — не читаны (Фаза 1 закрыта на объём; при необходимости дочитать).

## 3. Известные пред-существующие баги (НЕ задача роутеров, но зафиксировать)
- `tools/registry.py` `_task_status`: обращается к `row.summary/row.error/row.completed_at`, которых нет в Task (реальные: `objective`, `error_message`, `closed_at`).

## 4. План реализации
- Фаза 0: WORK_NOTES.md (этот файл).
- Фаза 2: реализовать недостающие роутеры по группам из §1.3 (real → stub), каждый роутер — отдельный файл в `backend/aria/routers/`.
- Фаза 3: подключить все новые роутеры в `main.py` (включая новые: files, logs, analytics, env, cron, skills, toolsets, model, ops, mcp, gateway, oauth, messaging, pairing, webhooks, credentials, memory, curator, portal, update, dashboard-ext).
- Фаза 4: запуск `uvicorn aria.main:app` + smoke-тест ключевых эндпоинтов (curl GET/POST) → только потом считать готово.
- Правила: после каждого write — read того же пути; финальный claim только после реального запуска и HTTP-ответов.

## 5. Чек-лист verify (прогон 2026-08-23: TestClient против `aria.main`, runtime-token через `token_store.issue()`)
- [x] `python -m compileall backend/aria` — без ошибок.
- [x] Запуск сервера без исключений — app import + TestClient, маршруты отвечают.
- [x] GET `/api/status`, `/api/files`, `/api/env`, `/api/logs`, `/api/skills`, `/api/model/options`, `/api/system/stats` — все HTTP 200.
- [x] POST `/api/auth/ws-ticket` → **200** `{ticket, ttl_seconds}`; без runtime-token → 401. Одноразовый ticket: первый `?ticket=` consume успешен, повторный → WS close 1008. Валидный `?token=` тоже принимается; в server-mode (ARIA_SERVER_MODE=1) без ticket/token → 1008, в desktop-mode (loopback) → accept без авторизации.
- [x] WS `/api/ws` (пре-существующий баг): strip-мидлварь была `@app.middleware("http")` (BaseHTTPMiddleware) → WebSocket-скоупы не обрабатывались, `/api/ws` не матчился на `/ws` (live-updates мертвы). Заменена на ASGI `_StripApiPrefixMiddleware` (`main.py`), стрипает `/api` для http И websocket. Проверено: `/api/ws` → first event `backend.health_changed`.
- [x] Стабы без 404 (выборка): GET `/api/providers/oauth`, GET `/api/mcp/servers`, GET `/api/messaging/platforms`, POST `/api/ops/prompt-size`, `/api/ops/config-migrate` — все 200.
- Примечание: `/api/aria/update/check` — GET (в моём POST-прогоне 405 ожидаемо, это не баг).

### Аудит Фазы 2 + фиксы (2026-08-23)
- **sessions.py**: все 6 вызовов `repo.list_sessions(db)` → `limit=None` — сняты LIMIT 50 + `ORDER BY updated_at` (ломали stats/empty/prune/bulk-delete; prune удалял максимум 50 и не по created_at).
- **env.py**: реестр перестал замораживать env при импорте — новый `_live()` читает `os.environ` на каждый GET `/api/env` (is_set/redacted_value актуальны после PUT/DELETE).
- **env.py reveal**: убран декоративный параметр `session_token` (Header) — удалён неиспользуемый импорт `Header`.
- **stubs.py ops**: `/ops/backup` — реальный sqlite `.backup()` (создан `data/backups/backup_*.db`, 430080 B, 18 таблиц); `/ops/dump` — реальный JSON-дамп с `db_rows_*` счётчиками в `data/diagnostics/`; `/ops/doctor` — реальные проверки (db/data_dir/runtime_token/platform); `/ops/security-audit` — честный `ok: false` (не реализован, вместо ложного `true`).
- **system.py stats**: `psutil.boot_time/virtual_memory/disk_usage/Process` обёрнуты в try/except (на платформах без psutil stats больше не падают).
- ⚠️ Найден в ходе verify, НЕ из аудита: **ws-ticket отсутствовал** (см. чеклист). Реализован по решению владельца (2026-08-23): `WsTicketStore` в `api/auth.py`, роутер `routers/auth.py`, приём `?ticket=`/`?token=` в `routers/system.py` WS-endpoint, плюс фикс ASGI-мидлвари для WS-скоупов.

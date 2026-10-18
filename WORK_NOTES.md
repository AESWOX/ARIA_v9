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
- ~~`tools/registry.py` `_task_status`: обращается к `row.summary/row.error/row.completed_at`~~ — **исправлено**, тул читает `objective`/`error_message` (проверено `test_task_status_reads_a_real_task_and_survives_garbage_id`).

### Дефекты инвентаризации B5 (`backend/tests/test_b5_tool_inventory.py`, обновлено 2026-10-13)
Префикс `B5-` — чтобы не путать с пунктами D1–D6 в `DOD.md` (фронтенд).

| ID | Суть | Статус |
|---|---|---|
| B5-D1 | `core/delegate.py:run_parallel` нигде не вызывался — loop исполнял `delegate_task` по очереди | исправлено 13.10: `loop._run_delegate_batch` запускает все `delegate_task` одного ответа через `delegate.run_bounded` (потолок `delegate_max_parallel`, лишние ждут очереди). Родитель встаёт на паузу, если ребёнок ждёт Approve, и продолжается, когда решено по ВСЕМ ждущим детям (`deliver_child_result`); результат ребёнка доставляется один раз; мёртвый `run_parallel` удалён |
| B5-D2 | `delegate_max_parallel`/`delegate_max_depth` из `.env` не читались | исправлено: `delegate.max_depth()`/`max_parallel()` читают Settings |
| B5-D3 | `_delegate_task` не применял `timeout_sec` | исправлено: `asyncio.wait_for`, статус `timeout`, задача переводится в `failed` |
| B5-D4 | неизвестная роль в `delegate.delegate_task` бросала исключение | исправлено: `DelegateResult(status="failed")` |
| B5-D5 | в guardrails имена тулов из Hermes (`read_file`, `terminal`, …), которых нет в реестре | исправлено: только имена из `TOOL_REGISTRY` |
| B5-D6 | read-only тулы не входили в `IDEMPOTENT_TOOL_NAMES` — no-progress детектор на них молчал | исправлено: все `safe_read` тулы включены |
| B5-D7 | `GuardrailConfig.mutating_tools` не читался | исправлено: успешная мутация сбрасывает счётчики no-progress |
| B5-D8 | побег из песочницы через соседний каталог с общим префиксом | исправлено 2026-10-12 (`Path.is_relative_to`) |

Открытое наблюдение того же прогона: тулы `file_search`, `friend_memory_read`, `list_decisions`, `list_vault`, `memory_search`, `skill_lookup`, `task_status` молча принимают пустые/мусорные аргументы (ошибки нет, возвращается пустой результат) — это осознанное поведение для необязательных аргументов, но для `file_search` стоит требовать `pattern`.

### Область тулов по ролям (важно для агента и для оператора)
Доступность тула = пересечение `RoleDefinition.tool_whitelist` и `ToolSpec.allowed_roles`. В реестре 22 тула; роль видит только свой срез. Роль `general` раньше имела ровно 7 тулов (vault read-only, `file_read`, `file_search`), из-за чего агент в чате называл «7 инструментов» как общее число и не мог проверить число навыков live. Теперь `general` дополнительно получила `memory_search`, `notebook_query`, `skill_lookup` и (P0) `web_search`, `web_fetch` (все read-only). Запись (`write_note`, `log_decision`, `create_vault_branch`), `shell_execute`, `file_write` и `delegate_task` остаются у `obsidian_keeper`/`coder`/`orchestrator`. Промпт роли теперь явно говорит модели, что список — это срез её роли.

### Хуки (H4) — как устроены на самом деле
События `pre_tool`, `post_tool`, `task_start`, `task_close`, `on_approval`, `on_error`; хук — shell-команда из `data/hooks.json` (создаётся через `/ops/hooks`), выполняется только при `allowed=true`; JSON-контекст на stdin и в `ARIA_HOOK_PAYLOAD`; таймаут 10 с (макс. 60 с); секреты вычищаются из окружения; блокировать можно только в `pre_tool` (код 2 или `{"block": true}`). Порядок вокруг тула: `pre_tool` → чекпоинт (`file_write`) → тул → `post_tool`. `dod_verify.py` хуком не является — это отдельный скрипт проверки релиза.

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

---

## P0 — веб: `web_search` и `web_fetch` (2026-10-13)

**Что было сломано.** `web_search` возвращал `results: []` при HTTP 200 и 25 КБ страницы. Старый регекс требовал `<a href="https://…" … class="result-link">` — `href` строго до `class`, двойные кавычки, абсолютная ссылка. Разметка DuckDuckGo Lite (по реконструкции, см. ниже) другая: `rel` первым, `class='result-link'` в одинарных кавычках, `href` — протокол-относительный редирект `//duckduckgo.com/l/?uddg=<url>`. Живой смоук «ALL GREEN» этого не видел: он проверял только размер скачанной страницы, парсер не вызывал. Теперь смоук вызывает боевой обработчик и требует ≥ 3 результатов.

**Что сделано.** Парсер на `html.parser` (любой порядок атрибутов и тип кавычек, раскрытие `uddg=`, сниппеты, отсев рекламы `/y.js`, дубли по url); запасной эндпоинт `html.duckduckgo.com/html` при пустой выдаче или HTTP-отказе; понятная ошибка при капче/блокировке; предупреждение в лог, если страница > 5 КБ, а ссылок ноль (признак смены разметки). `web_fetch(url, max_chars)`: только http/https, лимиты 2 МБ / 10 с на операцию / 15 с на весь запрос, типы только текстовые (html, plain, json, xml, csv, markdown), без сжатия, извлечение читаемого текста (скрипты/стили вырезаны, невидимые символы убраны). Оба тула помечают ответ `source_trust: "untrusted"` и `notice`; промпт роли `general` велит не исполнять инструкции со страниц. Тулы добавлены ролям `general`, `orchestrator`, `research`.

**SSRF.** Запрещены: схемы кроме http/https, логин:пароль в URL, `localhost` и `.local/.internal/.lan`, любой адрес, не являющийся публичным (`ipaddress.is_global`, мультикаст, IPv4-mapped, 6to4, NAT64). Резолвятся и проверяются ВСЕ адреса имени. Проверка повторяется на каждом шаге редиректа (максимум 5) и на итоговом URL.

**Остаточные риски (осознанно не закрыты в P0).**
- DNS rebinding (TOCTOU): адрес проверяется до запроса, а urllib резолвит имя ещё раз при соединении. Закрывается подключением по проверенному IP (свой `HTTPConnection`), но это ломает работу через системный прокси, поэтому вынесено из малого патча.
- Скрытый на странице текст (CSS `display:none`, белое на белом) попадает в извлечённый текст как обычный: защита — пометка `untrusted` и правило в промпте, не фильтр.
- Режим `image` у `web_fetch` (§7.1 п.1) не делался: относится к P4.

**Проверено на живой странице (14.10).** Настоящая выдача с машины владельца сохранена как `backend/tests/fixtures/ddg/ddg_lite_real.html` (24 087 байт, `result-link` в одинарных кавычках, `rel` первым, `//duckduckgo.com/l/?uddg=` — как и предполагалось); `test_every_lite_sample_gives_at_least_three_results[ddg_lite_real.html]` проходит, `live_smoke` — 5 результатов, `web_fetch` читает example.com и отклоняет `http://127.0.0.1`. Остальные образцы (`ddg_lite_sample.html`, `ddg_html_sample.html`, `ddg_captcha.html`) по-прежнему реконструкции. **Не проверено:** вызов веб-тулов из установленного приложения (в `e2e_smoke` нет веб-стадии), долгая работа без блокировки IP (на RunPod IP дата-центра блокировки вероятнее). Известный недочёт: `web_fetch` просит `Accept-Encoding: identity` и отклоняет ответ с `Content-Encoding`, поэтому сайты, которые сжимают ответ принудительно, не откроются.

---

## P1 — Groq: автоподбор цепочек (2026-10-14)

**Что было сломано.** Модель `qwen/qwen3.6-27b` была зашита в `aria/llm/router.py` в четырёх местах (классы `free_tier_reasoning`, `standard_reasoning`, запасное звено `premium_reasoning`, `subagent_execution`) и в `live_smoke.py`. У Groq такой модели больше нет, поэтому живые запросы к Groq давали 404, а `rebuild_and_smoke` этого не замечал: в `e2e_smoke` Groq не проверяется.

**Что сделано.** Модуль `aria/llm/groq_discovery.py` по схеме `gemini_discovery`: `GET <base>/models` через пул ключей (до 3 ключей, 401/сеть — следующий), фильтр чат-моделей, цепочки `main` (сильные первыми) и `fast` (слабые первыми), проба `--probe`, кэш `groq_models.json` (12 ч, сброс при смене ключей), приоритет `GROQ_MODELS` → кэш → `GROQ_MODEL`. Роутер регистрирует звенья через `_register_groq_chain`: одна модель = один пул ключей на все классы, первые звенья сохраняют прежние `provider_id`.

**Решения, которые стоит знать.** Ранжирование по размеру в имени (`70b`, `120b`) — эвристика, не оценка качества; модель без размера считается средней. `allam-*` исключена: арабская узкоспециализированная модель, по размеру (7b) вставала первой в `fast`. Запасная `GROQ_MODEL` по умолчанию `openai/gpt-oss-20b` — единственная, про которую проверено, что она жива на пуле владельца (`llama-3.3-70b-versatile` в его списке нет).

**Наблюдения прогона 14.10.** Пул из 10 ключей отдал 4 чат-кандидата, все три оставшиеся — модели с рассуждением. Смоуку пришлось поднять `max_tokens` с 20 до 512: при малом лимите все токены уходят на рассуждение и `content` приходит пустым. Приложение `max_tokens` вообще не задаёт (`OpenAICompatibleProvider.chat`), поэтому в чате этой проблемы не ожидается; вживую в установленном приложении не проверено.

**Не сделано.** Проверка поддержки вызова тулов моделями Groq; единый список моделей и бейдж здоровья провайдера (это остаток P1 из плана); Groq в `e2e_smoke`.

## Gemini: якорь цепочки (2026-10-14)

**Что было сломано.** `select_candidates` брал только `MAX_PER_TIER=4` новейших версий яруса. На реальном списке ключа (12.10) ярус flash получал 3.8/3.7/3.6/3.5, а `gemini-2.5-flash` (которая «почти не даёт ошибок») отрезалась до проверки. Бесплатная квота новых версий быстро кончалась, цепочка заканчивалась до стабильной модели, и стадия `ctx` падала с 429 (14.10: FAIL на 114 с; до этого проходила за 320 с).

**Что сделано.** `MIN_VERSION=2.5` (старее в цепочки не попадает), «якорь» яруса (самая старая стабильная модель) добавляется сверх 4 новейших и переживает обрезку по `MAX_CHAIN` (теперь 12), метка `ALGO=2` в кэше заставляет пересобрать цепочки при ближайшем запуске.

**Проверка ротации ключей.** По логу установленного приложения (14.10) на моделях 3.5 и 3.6 в паузу уходили все 10 разных ключей по 3–4 раза: ротация работает. Текст 429 от Google общий («Resource has been exhausted»), какая квота исчерпана — из него не видно; гипотеза, что 10 ключей из одного проекта делят квоту, не проверена (по DOD A29 квота Gemini считается на проект и модель).

**Остаточное.** Статусы из ручной пробы сохраняются, пока ключи те же: если 3.8/3.7 были перегружены в момент пробы, они остаются в конце цепочки, пока скрипт не перезапустят. DeepSeek отвечает 402 (нет баланса), это видно в итоговой ошибке роутера: пополнить баланс или убрать ключ в Keys.

## Разбор `backend.log` 12–14.10: почему агент «тупит» (2026-10-14)

Лог 7,9 МБ, в нём нет текстов сообщений, поэтому судить об «уме» модели по нему нельзя; видны причины, из-за которых агент выглядит глупо независимо от модели.

1. **Поиск выдавал блокировку за «ничего не найдено».** 14.10 15:02 страницы выдачи 8137 и 9494 байт дали 0 результатов, маркеры блокировки не сработали, и тул вернул модели `no results found`. Исправлено (см. A39), но что именно прислал DuckDuckGo, неизвестно: страница тогда не сохранялась. Теперь такая страница сохраняется в `<data>\debug\`; если повторится, её нужно прислать.
2. **Планировщик не работал вообще.** 13.10 10:11 Oracle три раза подряд: `step_id` вида `step-1-clone-repo` не проходил валидацию UUID. Причина в промпте (требовал UUID, пример с опечаткой `uuid-сткрока`) и в строгой схеме. Исправлено, не проверено на живой модели.
3. **Основная модель — Flash, а не Pro.** Провайдер с именем `gemini-pro` на самом деле ходит в `gemini-3.6-flash`; настоящие Pro на этом ключе недоступны (`3.1-pro-preview` — квота 0, `2.5-pro` — 404). Это потолок «ума» на бесплатном ключе, кодом он не лечится.
4. **Перегрузка новых моделей.** За три дня 503 «high demand»: `gemini-3.6-flash` 154, `gemini-3.5-flash` 135, `gemini-3.8-flash` 19; 429: 3.6 — 38, 3.5 — 37, 3.8 — 20. Каждый новый вызов цикла агента снова пробует перегруженную модель первой (retry, затем switch), это секунды на каждом шаге. Паузы ключей при дневной квоте доходят до ~12,5 часов (`cooldown 44976s`). `gemini-2.5-flash` в списке ошибок не встречается совсем. Кандидат на отдельный патч: запоминать перегрузку модели на несколько минут (circuit breaker), а не пробовать заново на каждом вызове.
5. **DeepSeek 402** (28 + 17 событий; 14.10 12:07 чат упал целиком с этой ошибкой).
6. **Память.** 8 ГБ: `TaskRunner` 20 раз предупреждал о нехватке (минимум 408 МБ свободно 14.10 13:43, во время сборки), работает в 1 воркер.
7. **SQLite `database is locked`**: `scheduler tick failed` три раза 13.10 10:58–11:00. Не разбирался.
8. **Сжатие контекста**: одно предупреждение (`gemini-3.5-flash-lite` 503, следующая модель взята). Проблем со сжатием в логе нет.

## Шторм `GET /models` (2026-10-15)

В `backend.log` за 12–14.10: 32 639 запросов `GET /models`, 1 358 пачек (медиана 27 запросов), по ~2 400 в час при открытом приложении. Причина: `_scheduler_loop` в `aria/main.py` просыпается каждые 30 с и безусловно вызывал `refresh_provider_models_job`, хотя расписание задачи — раз в 30 минут. Задача обходит всех зарегистрированных «провайдеров» (каждое звено цепочки Gemini считается отдельным, около 30) и для каждого делала свой `GET /models` и upsert всех моделей в SQLite. Проблема существовала до патчей 14.10 (пачки были и 12.10), мои цепочки её не создавали, но и не уменьшали число провайдеров. Исправлено: гейт на 30 минут и один запрос на хост. Связь с `database is locked` (3 раза 13.10) и с «приложение перестало открываться» (15.10) предположительная: она не доказана, это следует проверять после сборки по `backend.log` (должно быть ~2 пачки в час).

## Разбор реальной переписки 14.10 (2026-10-15)

Режим сессии в футере: `one model · result: no check · reasoning: fast` (один провайдер, аудит выключен, быстрое рассуждение) — это настройки владельца, они сами снижают аккуратность.

Подтверждено по тексту переписки:
1. **Выдуманные «логи тестов».** На просьбу проверить каждый тул и вывести логи в ответе вызваны только `list_vault` и `read_note`; про `search_vault`, `search_vault_tags`, `list_decisions`, `file_search`, `file_read` написано «Работает» с «логом выполнения», вызовов этих тулов в том ходе не было. Тот же ответ описал механику «хука» без источника.
2. **Не знает собственные тулы и среду.** На «что помнишь?» ответил «каждый диалог с чистого листа» без вызова `memory_search`; на «присоединить БД second brain» дал лекцию про Open WebUI/AnythingLLM/LM Studio, хотя vault уже подключён тулами; назвал себя работающим «локально», хотя ответы идут из облачной модели. Позже заявил, что тулов ровно 7, и что `web_search` нет, а на повторе `web_search` был вызван.
3. **Ссылки.** Признал, что ID на Civitai выдуманы по шаблону, и в следующем же ответе снова дал ссылки вида `civitai.com/models/<число>`; часть позднейших ссылок со slug похожа на взятые из результатов поиска, но не проверена.
4. **Противоречие самому себе.** Один ответ утверждал выпуск FLUX 3 в июле 2026 «по свежему поиску», другой назвал FLUX 2/3 «маркетинговыми названиями» и посоветовал их не искать, причём без нового поиска.
5. **Оценки без основания** (формулировка «около 15–30 эндпоинтов, 1–2 дня») при том, что в репозитории больше тысячи тестов.
6. **Чип `web_search -> ok`** показывается и когда тул вернул ошибку блокировки: «ok» значит «вызов выполнен», а не «результат получен»; пользователь это читает как успех.
7. **Баг тула** (исправлен, A41): `read_note("INDEX.md")` → `found: false` для существующего файла.

Не подтверждено: что именно было в системном промпте чата (в переписке этого не видно), и откуда ответ взял детали из ТЗ (L1–L8, `HEAD 5132c25`): тулов чтения в том ходе не показано.
## Ручной сценарий UI (P1, п.3)

1. Вкладка **Chat**, режим `plan`, расплывчатая задача: «сделай отчёт по логам сервиса».
2. Бэкенд возвращает `questions`, в чате появляется **карточка уточнений**: варианты ответа (если есть значение по умолчанию) + поле для своего ответа. Кнопки: «Отправить ответы» и «Принять допущения».
3. Ответы уходят в `POST /tasks/{id}/clarify`; гейт повторно валидирует TaskSpec. При принятых допущениях задача переходит в `approved`, допущения попадают в отчёт аудита.
4. Перед EXECUTE в чате появляется **предпросмотр плана**: список шагов, роль, инструмент, зависимости (`не указаны (идёт по порядку)` / `нет (независим)` / список id), `write_scope`, критерии приёмки, а также фаза и число волн. Кнопка «Запросить подтверждение» работает через существующий `AttentionType.task_tz_approval`, кнопки Approve/Reject — через `POST /api/attention-items/{id}/approve|reject`.
5. Во время выполнения видны **волны и статусы узлов**, а также подзадачи (`GET /tasks/{id}/children`) с ролью и статусом.
6. После аудита появляется **отчёт**: вердикт, аудитор, статусы критериев приёмки (`ac-1 pass/fail` с пометкой), список недостающих требований.

Ограничения записи: пути в `desktop/` проверены сборкой (`npm run build`), живой сценарий в GUI на этом стенде не воспроизводился — требуется прогон на машине с запущенным бэкендом.

## Раунд 2 (fix/clarify-round2), 2026-10-16

### Ручной сценарий UI (п.5.3) — статус
Раунд 2 писался на реконструкции ветки с файлами только из патчей; desktop-сборка (`npm run build`)
на этом стенде не воспроизводилась (нет полного дерева desktop/). Сценарий из ТЗ:
расплывчатая задача → карточка с двумя вопросами → два разных ответа → план → подтверждение →
выполнение → отчёт, а также отдельная проверка кнопки «Принять допущения» — требует
прогона на машине с запущенным бэкендом. Логика сценария покрыта автотестами
`test_pr2_1_task_endpoints.py` (12 passed): два вопроса с разными id, два разных ответа
не перетирают друг друга, «Принять допущения» -> допущения записаны и задача идёт дальше.

### Живой прогон eval (п.4.3)
Живой режим `--live` требует ключей провайдеров; на стенде не выполнялся — строка
«живой прогон, дата, провайдер» будет добавлена при первом прогоне с ключами.

### Проверка раунда 2 на Windows (16–18.10.2026)

- 16.10: `git am` серии без конфликтов; найден и исправлен тест, зависевший от Linux-пути (`/proc`); 10 полных прогонов подряд
  по 1228 passed; `alembic heads` — одна голова `e3f5a6b7c8d9`; `clarify_eval.py --detailed`: recall 0.9231, лишние 0.0, пропуски 0.0 (fake-провайдер);
  `rebuild_and_smoke.py` ALL GREEN.
- 17–18.10: в установленном приложении при входе в чат — чёрный экран. Причина: `labels={t.taskSpec}` в `NativeChatPage` при необъявленной `t`
  (в серверном логе — запрос чанка страницы без последующих `/chat/*`). `npm run typecheck` ничего не проверял (solution-tsconfig) — исправлено,
  добавлен `ErrorBoundary`.
- 18.10: в режиме Agent карточка уточнений не появлялась (вопросы обрабатывались только в пути обычного чата) и показывалась ложная ошибка
  «already queued or running» — исправлено. Сборка с исправлением: 1228 passed, `rebuild_and_smoke.py` ALL GREEN (HEAD `5083e2c`).
- Осталось: пройти ручной сценарий целиком в установленном приложении (новый чат, режим Agent, расплывчатая задача → карточка → ответ
  / «Принять допущения» → план → подтверждение → отчёт), `clarify_eval.py --live`, затем мерж. Известное ограничение: после перезагрузки
  страницы карточка пропадает (вопросы остаются текстом в чате).
- Параллельно выявлено (не относится к коду ARIA): процесс `cua-driver.exe` (в репозитории не упоминается) завис, окно «не отвечает»;
  происхождение не установлено (нужны права администратора, чтобы увидеть путь).
- 18.10: добавлены правила авторства (`docs/REPO_POLICY.md`, хук, CI-проверка, тесты). Перепись истории проверена на копии (162 коммита → один автор, деревья файлов идентичны); на реальном репозитории не выполнялась — ждёт мержа `fix/clarify-round2` и решения по одному личному адресу в истории.

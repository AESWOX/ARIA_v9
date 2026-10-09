# Ревью волн 0–1 (09.10.2026)

База: `main` @ `09034ec`. Применены `0001` (wave0) и `0002` (wave1) из `aria-wave0-1.bundle`; тесты — Linux, Python 3.12. Windows, живые провайдеры, сборка exe и фронтенд **не запускались**.

## Найденные дефекты (все воспроизведены; тесты — `backend/tests/test_wave1_review_fixes.py`)
| # | Дефект | Эффект |
|---|---|---|
| 1 | `console=False` без защиты `sys.stdout=None` | uvicorn: «Unable to configure formatter» — sidecar не стартует |
| 2 | `resume_after_approval` не передаёт подтверждение | после Approve команда не запускается; результат `status=ok` |
| 3 | `approved` читался из аргументов модели | модель обходит политику, подставив `approved:true` |
| 4 | allowlist: `python -c`, `node -e`, `find -exec/-delete`, `npx`, `pip install`, `git -c`, `> ~/…`, `&`, `sudo`, порядок флагов `rm` | исполнение без подтверждения |
| 5 | остановка приложения → `cancelled` | задача не возобновляется (A10) |
| 6 | повторный `submit` | одна задача исполняется дважды |
| 7 | сбой в `plan` до `in_progress` | задача вечно `approved`, перезапускается при каждом старте |
| 8 | `notifier=None` в раннере | Telegram-эскалации потеряны |
| 9 | `POST /sessions/{id}/messages` | агент всё ещё внутри HTTP-запроса |
| 10 | cron: якорь «сутки назад» | новое расписание срабатывает сразу |
| 11 | нет `asyncio_mode` | 19 тестов тихо пропущены |
| 12 | 3 exe в git после wave0 | см. команды ниже |

## Не сделано / не проверено
- UI не менялся (A11): режим `plan` из интерфейса недоступен.
- Поведение Sessions-страницы на ответ `queued`.
- Нагрузка: 3 воркера на SQLite (в `plan` сессия БД держится всю задачу).
- `resume_pending` всегда ставит режим `agent`.
- Сборка PyInstaller/Tauri, `.msi`, блок G.

## Установка (PowerShell, корень клона)
```powershell
git checkout -b wave0-1 main
git am --3way <путь>\0001-fix-wave0-*.patch     # 35 МБ; при проблемах — взять aria-wave0-1.bundle
git am --3way <путь>\0002-feat-wave1-*.patch
git am --3way <путь>\0003-fix-wave1-review.patch
git rm --cached "aria_release__5dvg4nh/desktop/src-tauri/bin/backend-x86_64-pc-windows-msvc.exe" "sandbox_share/Local Agent Desktop_0.1.0_x64-setup.exe" sandbox_share/WebView2RuntimeStandalone.exe
git commit -m "chore: untrack remaining binaries"
git ls-files | Select-String '\.(exe|msi)$'     # пусто
cd backend
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q          # ожидаем: 340 passed, без skipped
```
Затем порядок сборки: фронтенд → PyInstaller → копия sidecar → Tauri; ручной smoke G1/G2 (проверить именно запуск при `console=False`).

## Итог 09.10 (после мержа)
- `main` @ `080da9d`: 0001 → 0002 → 0003, untrack exe, доки. Windows-прогон владельца: **340 passed, 0 skipped, покрытие 57 %**.
- Sidecar (PyInstaller) и установленная сборка стартуют; закрытие окна не оставляет процессов (один цикл).
- Chat отвечает; 404 на `gemini-2.5-flash` устранён сменой модели в `.env` (A19), 503 пережит ретраем (A20).
- `console=False` подтверждён (`backend.spec:52`), запуск без консоли работает (A17/E1 закрыты).
- Не проверено: Sessions → `queued` (A21), cron в свою минуту, 20/20 запусков (G2).
- Новые находки: дефолт модели в коде протух (A19); маска вывода `.env` не покрывала `GEMINI_API_KEYS` — ключи утекли в чат (F1, срочно); после `git rm --cached` исчезает `desktop/src-tauri/bin/` (E2).

## Дополнение (09.10, патч 0008)

- 0008 = Approve для пишущих тулов MCP + профиль запуска («Команда» в Chat) + правка перезапуска из 0006. Собран заново после второго обрыва сессии (песочница сбросилась, незакоммиченная часть первой попытки потеряна) от `main` @ `02e55da`. Песочница (Linux, Py 3.12): бэкенд 474 passed (464 + 4 в `test_h7_mcp_approval.py` + 6 в `test_run_profile.py`; тест `agent_cannot_run…` в `test_h7_mcp_client.py` заменён проверкой паузы на Approve), `npm run typecheck` и `vite build` без ошибок. Windows, живые провайдеры и живой MCP-сервер не проверялись.
- Найдено при разборе: если приложение перезапускалось между Approve и запуском, действие терялось (в 0006 подтверждённое исполнялось только из HTTP-обработчика). Теперь `resume_pending` исполняет подтверждённое, но не исполненное действие; повтор исключён привязкой tool_call к подтверждению.
- Не сделано: потолок бюджета на задачу; `reasoning_effort` провайдеров (глубина мышления пока — подсказка в промпте). Миграция Alembic для нового значения enum `mcp_tool_approval` на SQLite не нужна (enum хранится как VARCHAR); для другой СУБД понадобится.

## Дополнение (09.10, после обрыва сессии)

- 0006 смержен владельцем: `main` @ `c482cae`, Windows: 440 passed.
- 0007 (H7, ядро MCP-клиента) пересобран с нуля из `main` @ `c482cae` (прошлая сессия оборвалась на лимите, песочница сбросилась): `aria/mcp/{client,manager}.py`, `routers/mcp.py`, правки `loop.py`/`main.py`/`stubs.py`, фикстура `tests/fixtures/fake_mcp_server.py`, `tests/test_h7_mcp_client.py`. Песочница (Linux, Py 3.12): 464 passed. Windows, живые MCP-серверы и фронтенд не проверялись.

## Дополнение (09.10, патч 0009)

- 0008 смержен владельцем: `main` @ `9d2def1`, Windows: **474 passed**, `typecheck` exit 0. Панель «Team», карточка Approve и живой MCP не проверялись.
- 0009 = OAuth 2.1 для MCP **и** встроенный каталог (патчи 0009 и 0010 из плана объединены: они делят роутер, менеджер и тесты). Поверх `main` @ `9d2def1`. Песочница (Linux, Py 3.12): бэкенд **497 passed** (474 + 23 в `test_h7_mcp_oauth.py`), `npm run typecheck` и `vite build` без ошибок.
- Что сделано: поиск сервера авторизации (PRM RFC 9728, метаданные RFC 8414), динамическая регистрация клиента, Authorization Code + PKCE S256, `resource` (RFC 8707), обновление токена с ротацией, отзыв (RFC 7009); токены в `mcp_oauth.json` (0600), в `mcp_servers.json`, логи и ответы API не попадают; callback `/mcp/oauth/callback` без токена, защита — одноразовый `state` (10 минут) и PKCE; redirect только на loopback; удаление сервера стирает токены; вызов тула при 401 один раз обновляет токен и повторяет, тулы в реестре сохраняются при переподключении.
- Каталог: Upwork (`https://mcp.upwork.com/mcp`, OAuth), `fetch` (uvx), `memory` (npx). В `read_tools` Upwork внесены только читающие тулы из плана §5; имена из навыков плагина, живьём не сверялись (неверное имя безопасно: тул остаётся под Approve). Команды `uvx`/`npx` у `fetch`/`memory` живьём не запускались.
- UI: на карточке HTTP-сервера «Authorize» / «Re-authorize» и «Revoke access», бейдж `authorized`; после Authorize страница опрашивает статус до 2 минут.
- Не сделано: SSE-GET поток; потолок бюджета на задачу.
- Риск, снимается только на живом: принимает ли Upwork redirect на `http://127.0.0.1:<порт>/mcp/oauth/callback` (порт установленной сборки случайный). Запасной вариант — фиксированный порт или deep-link Tauri.
- Чтобы снять шумную нумерацию: пакет каталога/вкладки больше не отдельный 0010; следующий номер — 0010 для CTX.
- 0010 (CTX): сжатие по порогу 100к токенов на сессию, подключено в чат; найдены и исправлены: сводка уходила в конец промпта, `list_messages_for_prompt(limit)` отбрасывал новые сообщения, суммаризатор не видел результатов тулов. 505 passed в песочнице; Windows и живой Gemini не проверены.

## Установка 0009 и 0010 (PowerShell, корень клона, `main` @ `9d2def1`)
```powershell
git checkout -b feat/0009-0010 main
git am --3way <путь>\0009-feat-0009-mcp-oauth-catalog.patch
git am --3way <путь>\0010-feat-0010-ctx-compression.patch
cd backend; .\.venv\Scripts\python.exe -m pytest -q     # ожидаем: 505 passed, 0 skipped
cd ..\desktop; npm run typecheck                          # ожидаем: exit 0
```
Живая проверка после сборки: MCP → Install у Upwork → Authorize (принимает ли Upwork redirect на локальный порт); длинный диалог в Chat (сообщение после сжатия отвечает, в логе есть строка о сжатии).


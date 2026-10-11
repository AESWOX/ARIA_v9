# ARIA — регламент работы и карта окружения

Версия: 2026-10-09 (ночь, после 0010). Читать первым, когда начинаете новую сессию с ассистентом: скопируйте разделы 1–2 в начало чата.

## 1. Как со мной работать

Пункты 1–2 — ваше прямое требование. Пункты 3–9 я вывел из сегодняшней сессии: поправьте, что не так.

1. **Ответ = готовые команды PowerShell одним блоком + что должно получиться на выходе.** Без вступлений и повторов уже сделанного.
2. **Каждый шаг называет, где он выполняется.** Либо `cd C:\Users\User\Desktop\ARIA_v9` в начале блока, либо абсолютные пути.
3. **Секреты не печатаются.** `.env` показывать только так: `Get-Content "$env:APPDATA\ARIA\.env" | ForEach-Object { $_ -replace '=.+','=***' }`. Маска по `KEY=` не годится: она пропустила `GEMINI_API_KEYS=` (09.10, ключи ушли в чат). Ключи в чат не вставлять; менять через страницу Env.
4. **Статус только по вашему выводу.** Ассистент работает в песочнице на Linux и вашу Windows не видит. Формулировки: «подтверждено вашим выводом» и «не проверено». Слова «установлено на 100 %» без вывода проверки недопустимы.
5. **Присланный вывод разбирается построчно до следующего шага.** Если вывода не хватает (например, нет `Get-Process`), ассистент говорит об этом и просит именно недостающее.
6. **Разрушающие команды** (`reset --hard`, `stash drop`, `Remove-Item`, `taskkill`, удаление веток) идут с пояснением и после бэкапа.
7. **Правки кода — патчем или файлом**, с командой применения и тестом, который падает без правки.
8. **Доки выдаются файлами**, имена по шаблонам в `Downloads`; скрипт сам находит свежие и копирует в репо.
9. **Ошибка ассистента называется сразу и с причиной**, без оправданий.

## 2. Карта: где что лежит

| Что | Где |
|---|---|
| Репозиторий | `C:\Users\User\Desktop\ARIA_v9` (GitHub `AESWOX/ARIA_v9`, ветка `main`, прямой push в `main` работает) |
| Backend (Python) | `...\ARIA_v9\backend`, venv: `backend\.venv\Scripts\python.exe` |
| Фронтенд + Tauri | `...\ARIA_v9\desktop`, Tauri: `desktop\src-tauri` |
| Результат PyInstaller | `backend\dist\` (onefile `backend*.exe`) |
| Копия sidecar для Tauri | `desktop\src-tauri\bin\backend-x86_64-pc-windows-msvc.exe` (вне git; папку создавать самому) |
| Установщики | `desktop\src-tauri\target\release\bundle\msi\*.msi` и `...\nsis\*setup.exe` (смотреть `LastWriteTime`) |
| Данные установленного приложения | `%APPDATA%\ARIA`: `.env` (ключи, модели), `logs\backend.log`, `skills\`, база |
| Данные в режиме разработки | `backend\data` |
| Бэкап перед установкой патчей | `Desktop\aria_backup_20261009` (sidecar, `test_zz_*.py`, `local_changes.diff`); stash `before wave0-1 install` |
| Загрузки с патчами и доками | `%USERPROFILE%\Downloads` |
| Порт | при ручном запуске `backend*.exe` — `127.0.0.1:8765`; в установленной сборке порт выбирает Tauri (по гайду; не проверялось) |
| Процессы | `local-agent-ui.exe` → `backend.exe` → `backend.exe` (загрузчик + дочерний). Убивать только деревом: `taskkill /F /T /PID <id>` |

## 3. Команды

**Одной командой: пересборка → установка → живой смоук (рекомендуется, патч 0014)**
```powershell
cd C:\Users\User\Desktop\ARIA_v9
git checkout main; git pull --ff-only; git status --short              # дерево чистое
.\backend\.venv\Scripts\python.exe backend\scripts\rebuild_and_smoke.py
```
Скрипт сам проходит 13 фаз и останавливается на первой упавшей: preflight → tests (pytest: 0 failed, 0 skipped) → typecheck → репетиция на исходниках → фронтенд → PyInstaller → репетиция на собранном `backend.exe` → Tauri → копия `%APPDATA%\ARIA` на рабочий стол → тихое удаление старой и установка новой → запуск приложения → живой смоук по реальным ключам → закрытие и проверка, что процессов не осталось → обычный запуск приложения. Итог — строка `RESULT: ALL GREEN` или список упавших фаз; полные логи каждой команды лежат в `reports\rebuild_<время>\` (`summary.json`, `<фаза>.log`, `e2e_report.json`).

- **Если текущая установка — MSI**, тихая установка требует прав администратора: открыть PowerShell через «Запуск от имени администратора» и запустить ту же команду (скрипт скажет об этом сразу, до долгой сборки). NSIS-установщик (`-setup.exe`) прав не требует: `--kind nsis` (старый MSI при этом сначала нужно удалить).
- **Без установки** (только тесты, репетиции и сборка): `... rebuild_and_smoke.py --no-install`. **План без выполнения:** `--dry-run`. **Только живой смоук уже установленного приложения:** `--live-only`. **Пропуски:** `--skip-tests`, `--skip-rehearsal`, `--skip-build`, `--no-ctx`, `--no-relaunch`.
- **Живой смоук** трогает ваш реальный профиль: создаёт и удаляет тестовые сессии, заметку памяти, MCP-сервер `e2e-smoke`, временный плагин; тратит несколько запросов к модели. Для проверки сжатия приложение на время запуска получает `COMPRESSION_TOKEN_THRESHOLD=3000`; после смоука оно закрывается и запускается заново обычным образом. Резервная копия профиля делается до установки.
- **Репетиция отдельно** (без ключей и без вашего профиля, фальшивая модель, изолированный каталог данных): `.\backend\.venv\Scripts\python.exe backend\scripts\e2e_rehearsal.py`; на собранном sidecar: `... e2e_rehearsal.py --exe backend\dist\backend.exe`. Любая стадия ниже — то же, что гоняется вживую (`backend\scripts\e2e_smoke.py`).
- Что проверяет смоук: infra (токен обязателен, self-test), surface (все основные маршруты, каталог MCP с Upwork), mcp (подключение, тулы, default-deny), plugin (preview/import/409/delete), memory, chat (ответ модели и вспоминание факта из памяти), agent_shell (boss → coder → карточка Approve → команда исполнена, A22), agent_mcp (пишущий тул MCP не вызывается до Approve и вызывается после), ctx (длинный диалог → сводка сжатия → чат отвечает дальше).
- **Не автоматизировано:** Authorize у Upwork (нужен ваш вход в браузере), окно и вид интерфейса (белый экран, панель Team), cron в свою минуту, 20/20 запусков.

**Состояние репо**
```powershell
cd C:\Users\User\Desktop\ARIA_v9
git status --short
git log --oneline -5
git ls-files | Select-String '\.(exe|msi)$'      # должно быть пусто
```

**Тесты** (ожидаю: 505 passed, без skipped — получено на Windows для `main` @ `8917af4`)
```powershell
cd C:\Users\User\Desktop\ARIA_v9\backend
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

**Полная сборка** (порядок важен)
```powershell
cd C:\Users\User\Desktop\ARIA_v9\backend
Select-String backend.spec -Pattern "console"          # должно быть console=False
.\.venv\Scripts\python.exe -m PyInstaller backend.spec --noconfirm --clean
$exe = (Get-ChildItem .\dist -Recurse -Filter backend*.exe | Sort LastWriteTime -Desc | Select -First 1).FullName
$exe; (Get-Item $exe).LastWriteTime                    # сегодняшняя дата

# проверка sidecar до Tauri (убить хвосты, дождаться порта до 40 с)
Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'backend|local-agent' } | ForEach-Object { taskkill /F /T /PID $_.ProcessId | Out-Null }
$p = Start-Process $exe -PassThru
$up = $false
foreach ($i in 1..40) { Start-Sleep 1; if (Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue) { $up = $true; "порт открылся через $i с"; break } }
"listening: $up"
taskkill /F /T /PID $p.Id

# Tauri
New-Item -ItemType Directory -Force ..\desktop\src-tauri\bin | Out-Null
Copy-Item $exe ..\desktop\src-tauri\bin\backend-x86_64-pc-windows-msvc.exe -Force
cd ..\desktop
npm run tauri build
Get-ChildItem src-tauri\target\release\bundle -Recurse -Include *.msi,*setup.exe | Select FullName, LastWriteTime   # дата сегодняшняя
```

**Установка**
```powershell
Copy-Item "$env:APPDATA\ARIA" "$env:USERPROFILE\Desktop\ARIA_data_backup_$(Get-Date -Format yyyyMMdd)" -Recurse
Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'backend|local-agent' } | ForEach-Object { taskkill /F /T /PID $_.ProcessId | Out-Null }
Start-Process (Get-ChildItem C:\Users\User\Desktop\ARIA_v9\desktop\src-tauri\target\release\bundle\msi -Filter *.msi | Sort LastWriteTime -Desc | Select -First 1).FullName
```

**Процессы и лог**
```powershell
Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'backend|local-agent' } | Select ProcessId, ParentProcessId, Name
Get-Content "$env:APPDATA\ARIA\logs\backend.log" -Encoding UTF8 | Select-String -NotMatch 'httpx|GET /' | Select -Last 30
```

**Сменить модель Gemini** (имена брать из списка API, не из головы)
```powershell
$f = "$env:APPDATA\ARIA\.env"
$lines = (Get-Content $f -Encoding UTF8) -replace '^GEMINI_FLASH_MODEL=.*', "GEMINI_FLASH_MODEL=gemini-3.8-flash"
[IO.File]::WriteAllLines($f, $lines, (New-Object Text.UTF8Encoding $false))
Select-String $f -Pattern '^GEMINI_FLASH_MODEL'
```
После правки полностью закрыть приложение (команда убийства дерева выше) и открыть заново.

**Коммит и мерж без `gh`**
```powershell
cd C:\Users\User\Desktop\ARIA_v9
git checkout main; git pull --ff-only origin main
git checkout -b <ветка>
# ... правки ...
git add <файлы>; git commit -m "<сообщение>"
git push -u origin <ветка>
git checkout main; git merge --no-ff <ветка> -m "Merge <ветка>"; git push origin main
git push origin --delete <ветка>
```

## 4. Грабли, на которые уже наступили

| Что | Причина | Как избежать |
|---|---|---|
| `Copy-Item ... не удалось найти часть пути` | `git rm --cached` убрал последний файл, папка `src-tauri\bin` исчезла | `New-Item -ItemType Directory -Force` перед копированием |
| `resource path bin\backend-...exe doesn't exist` | Tauri собирается без sidecar | то же; не ставить `.msi` с датой старше сегодняшней |
| `Errno 10048` на порту 8765 | живой дочерний `backend.exe` от прошлого запуска; `Stop-Process` убил только загрузчик | `taskkill /F /T /PID` по дереву |
| Лента `httpx` в логе | обновление каталога провайдеров | фильтровать `-NotMatch 'httpx'` |
| `HTTP 404 model ... no longer available` | в `.env` устаревшее `GEMINI_FLASH_MODEL` | список моделей из API, потом правка `.env` |
| `HTTP 404 model=3.8-flash` | в `.env` записано имя без префикса `gemini-` | брать имя целиком из списка |
| `HTTP 503 high demand` | перегрузка модели у провайдера | ретрай/фолбэк отработал 09.10; временная |
| `gh` не найден | GitHub CLI не установлен | мерж командами git (раздел 3) или `winget install GitHub.cli` |
| Кракозябры в логе | чтение без `-Encoding UTF8`; тексты ошибок ОС приходят в cp1251 | читать с `-Encoding UTF8`; русские тексты ошибок ОС всё равно могут быть мусором |
| Тихая установка `.msi` падает без прав | MSI ставится на всю машину и требует повышения | PowerShell «от имени администратора» или NSIS-установщик (`--kind nsis`) |
| Задача Bootstrap запускалась при каждом старте | демо-задача оставалась `needs_rework`, а он считается возобновляемым | исправлено в 0014 (сид больше не одобряет задачу; старую раннер закрывает один раз) |
| `[Errno 2]` на одобренной shell-команде | каталог песочницы не создавался на свежем профиле | исправлено в 0014 (создаётся перед запуском) |
| В чате «The model provider ... rejected the account (HTTP 402)» или стадии смоука `provider_http_402` | у провайдера моделей нет средств (`Insufficient Balance`) или у ключа нет доступа | пополнить счёт или убрать ключ в Keys; после 0017 остальные провайдеры пробуются автоматически, а этот откладывается на 10 минут |
| Грязное дерево перед `git am` | правки, сделанные вручную до патчей | `git stash push -u`, копия вне репо |

## 5. Состояние на 09.10

**Работает (подтверждено вашим выводом 11.10):** на `main` @ `a330c32` 538 тестов зелёные, `tsc` без ошибок; полная цепочка `rebuild_and_smoke.py` на Windows (два прогона) проходит 12 из 13 фаз: сборка PyInstaller, репетиция на собранном `backend.exe`, Tauri, тихая установка NSIS, запуск, закрытие без процессов-сирот, перезапуск. Живой смоук: память, MCP, плагины и инфраструктура проходят; чат на реальной модели и сжатие проходили в первом прогоне; агентские стадии и чат во втором упали из-за **HTTP 402 `Insufficient Balance`** (нет средств на счёте платного провайдера моделей, не баг приложения). Установленная сборка включает 0004–0016;

**Новое в 0008 (после установки патча и сборки):** над полем ввода в Chat — строка «Team: …». Раскройте её: стиль (Solo / Boss + subs), модель для Босса/Сабов/Аудитора (Auto или класс; «Advanced» показывает конкретные модели по вашим ключам), аудит (Off / Light / Strict), Thinking. Выбор сохраняется для сессии и запоминается для новых. Agent и Plan используют выбранные модели; Chat — модель из «Boss/Model». Пишущие тулы MCP теперь не блокируются, а показывают карточку Approve/Reject.

**Не проверено в установленном приложении:** режимы Agent/Plan из Chat (A11), память, хуки, чекпоинты, MCP (всё в `main`, но сборки с ними не было); страница Sessions и ответ `queued` (A21). **Не сделано:** голос, зрение, рой, Telegram, Upwork-радар, SSE-GET поток MCP, интерфейс импорта плагинов (0013 даёт только API). Chat в режиме Chat по-прежнему без тулов.

**Не проверено:** Sessions → `queued` (A21); cron в свою минуту; 20/20 запусков; `npm run typecheck`.

**Ключи Gemini (F1):** 10 штук лежали в чате открытым текстом 09.10; по вашим словам, заменены (вывод проверки не приложен).

## 6. Следующий смоук (порядок)

1. Страница Sessions → открыть «ПРОБА1» → отправить сообщение → хвост лога без `httpx|GET /` → прислать.
2. Закрыть окно, проверить процессы; повторить ещё 4 раза.
3. Cron на ближайшую минуту: сработал ли в свою минуту.
4. Затем: **проверить счета провайдеров.** `Insufficient Balance` (HTTP 402) значит, что у одного из платных провайдеров (похоже на DeepSeek) пусто на счёте: пополнить счёт или убрать его ключ на странице Keys. Какие провайдеры настроены, видно без показа ключей: `Select-String -Path "$env:APPDATA\ARIA\.env" -Pattern "KEY" | ForEach-Object { ($_.Line -split "=")[0] }` (печатает только имена переменных). Затем поставить 0017 (`git am`) и снова запустить `rebuild_and_smoke.py` целиком: список провайдеров печатается в стадии `surface`, при 402/429 в сообщении упавшей стадии есть подсказка, что это счёт или квота. Если правится только скрипт смоука, хватит `--live-only` (около 2 минут).

**Новое в 0009 (после установки патча и сборки):** вкладка MCP — каталог работает без сети; «Install» у Upwork добавляет сервер, затем на его карточке «Authorize»: откроется браузер, войдите на upwork.com, вернитесь в ARIA (статус обновится сам, до 2 минут). «Revoke access» отзывает доступ и стирает токены локально; на стороне Upwork доступ также снимается в Account Settings → Connected Apps. Токены лежат в `mcp_oauth.json` рядом с `mcp_servers.json`. Тулы, которые могут менять данные, по-прежнему идут через Approve.

**Новое в 0010 (после установки патча):** длинные диалоги сжимаются сами: когда история сессии превышает примерно 100 000 токенов, старая середина заменяется сводкой, первые сообщения и текущий ход остаются как есть, полная история в базе не теряется. Первый ответ после сжатия большой истории может прийти медленнее (до 2 минут на суммаризацию). Порог меняется настройкой `compression_token_threshold` (0 — выключить токенный триггер); сжатие целиком выключается `compression_enabled`.

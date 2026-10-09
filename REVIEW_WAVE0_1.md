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

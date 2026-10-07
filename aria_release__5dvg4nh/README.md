# ARIA — Local Agent Desktop

Локальный AI-агент с десктопным UI (Tauri + React) и FastAPI-бэкендом.

## Быстрый старт

### Вариант A — Dev-режим (Vite + бэкенд, браузер)

```bash
# 1. Бэкенд
cd backend
.venv/Scripts/python.exe run_backend.py        # API на http://127.0.0.1:8765

# 2. Фронтенд (в другом терминале)
cd desktop
npm run dev                                     # UI на http://127.0.0.1:1420
```

### Вариант B — Dev-режим (Tauri-окно)

```bash
# 1. Бэкенд
cd backend
.venv/Scripts/python.exe run_backend.py

# 2. Tauri dev (в другом терминале)
cd desktop
npx tauri dev
```

### Вариант C — Production (Tauri build)

```bash
cd desktop
npx tauri build        # собирает .msi / .exe (NSIS) в src-tauri/target/release/bundle/
```

Или через `start_aria.bat` (единый запуск бэкенда + Vite + Tauri dev).

## Проверка состояния (DoD)

```bash
python dod_verify.py            # быстрая проверка
python dod_verify.py --clean    # полная с нуля (чистит .venv, npm ci)
python dod_verify.py --json     # только JSON
```

Вердикт: `DOD_READY` / `DOD_READY_WITH_EXCLUSIONS` / `NOT_DOD_READY: <причины>`.
Результат пишется в `dod_verify.json`.

## Release

Перед каждым релизом — см. [RELEASE_CHECKLIST.md](RELEASE_CHECKLIST.md).
Сборка архива: `python build_release.py` (allowlist из `RELEASE_MANIFEST.txt`,
секрет-скан и артефакт-скан встроены; при нарушении сборка падает).

## Структура

```
backend/          FastAPI-бэкенд (aria/ пакет)
  aria/
    main.py       приложение, маршруты (декомпозированы в routers/)
    routers/      тематические роутеры (providers, storage, sessions, tasks, ...)
    core/         loop, events, approvals, rate_limit
    db/           модели, repository, миграции
    llm/          роутер провайдеров, инструменты
    storage/      B2, Obsidian vault
    tests/        pytest (147 тестов)
desktop/          React + Vite + Tauri
  src/            фронтенд (страницы, компоненты)
  src-tauri/      Rust-обёртка (sidecar backend.exe, CSP, single-instance)
  src-tauri/bin/  backend.exe (sidecar, собирается PyInstaller'ом)
build_release.py  сборка релизного архива
dod_verify.py     проверка DoD
RELEASE_CHECKLIST.md  чек-лист релиза
```

## Конфигурация

- `.env` (backend/) — провайдеры, `POSTGRES_DSN`, токены.
- `POSTGRES_DSN=sqlite:///./data/local_agent.db` — по умолчанию SQLite (однопользовательский режим).
  Для multi-user / production укажите реальный Postgres DSN. В серверном режиме
  при SQLite бэкенд пишет предупреждение в лог.
- `aria_dev_mode=True` — режим разработки: runtime-токен не обязателен.
  По умолчанию выключен: API требует заголовок `X-Local-Agent-Token`.

## Системные требования

- Windows 10+, Rust/Cargo 1.79+, Node 20+, Python 3.11
- Бэкенд: FastAPI, uvicorn, SQLAlchemy
- Фронтенд: React 19, Vite 6, Tailwind 4

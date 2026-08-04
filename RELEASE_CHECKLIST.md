# Release Checklist — ARIA Desktop

Автоматизированная проверка перед каждым релизом. Запуск: `python dod_verify.py` (fast mode)
или `python dod_verify.py --clean` (полная проверка с нуля).

Каждый пункт ниже — БЛОКИРУЮЩИЙ: если хотя бы один не пройден, релиз не выпускается.

## Обязательные проверки

| # | Проверка | Команда | Критерий прохождения |
|---|----------|---------|----------------------|
| 1 | **Secret scan** | `python dod_verify.py` | 0 совпадений (ключи, `.env`, `*.key`, `*.pem`) |
| 2 | **Artifact scan** | встроено в `dod_verify.py` (`check_artifact_scan`) | 0 файлов `*.db`, `*.zip`, `logs/`, `.exe` вне `src-tauri/bin/` |
| 3 | **npm audit** | встроено в `dod_verify.py` (`check_npm_audit`) | 0 high / 0 critical |
| 4 | **Bundle budget** | встроено в `dod_verify.py` (`check_bundle_budget`) | главный JS-чанк < 500 KB |
| 5 | **Backend tests** | `cd backend && .venv/Scripts/python.exe -m pytest tests/ -q` | 0 failed (skip/deselect → downrank до DOD_READY_WITH_EXCLUSIONS) |
| 6 | **Frontend build** | `cd desktop && npm run build` | exit 0, без ошибок |
| 7 | **TypeScript** | `cd desktop && npx tsc -p . --noEmit` | exit 0 |
| 8 | **Self-test** | `python dod_verify.py` (проверка `/system/self-test`) | все под-проверки ok |
| 9 | **Live providers** | встроено в `dod_verify.py` (`check_live_providers`) | ≥1 реальный провайдер отвечает |
| 10 | **Vault** | встроено в `dod_verify.py` (`check_vault`) | >0 `.md` файлов |
| 11 | **No Hermes references** | встроено в `dod_verify.py` (`check_hermes_references`) | 0 файлов с "hermes" в `desktop/src/` |
| 12 | **Smoke test (manual)** | установить `.msi` на чистую машину, запустить | окно открывается, `/status` → 200 за <6 с, нет белого экрана |

## Ручные проверки (не автоматизированы)

- **P0.1 — Sidecar port**: приложение запускается 20/20 раз на чистой машине И после
  force-kill; если backend не поднялся — пользователь видит экран ошибки, а не белый экран.
- **P0.2 — CSP**: окно не содержит `dangerousDisableAssetCspModification`; политика задана
  явно в `tauri.conf.json`.
- **P2.3 — Keyboard/focus**: навигация по табу, фокус виден, Escape закрывает модалки.

## Интеграция в pipeline

`build_release.py` (сборка архива):
1. Копирует файлы по `RELEASE_MANIFEST.txt` (allowlist).
2. Фильтрует DENY_FILES (`.env*`, `*.key`, `*.db`, `*.zip`, ...).
3. Secret-скан по изменённым файлам — при совпадении сборка ПАДАЕТ.
4. Артефакт-скан: после копирования проверяет, что в staging нет заблокированных файлов.

`dod_verify.py` (верификация):
- Фаза 0: secret-scan — блокирующая, при BLOCKED сразу выход.
- 8b: artifact_scan + npm_audit + bundle_budget — новые P1.4 проверки.
- Вердикт: `DOD_READY` / `DOD_READY_WITH_EXCLUSIONS` / `NOT_DOD_READY: <причины>`.

## Известные исключения

- `react-router` CVE (GHSA-qwww-vcr4-c8h2, RSC CSRF): устранено через `overrides` →
  `react-router@8.3.0` в `desktop/package.json`. npm audit → 0 уязвимостей.

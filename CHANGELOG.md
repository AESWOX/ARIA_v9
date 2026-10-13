# Changelog

## 2026-10-13 (2)
- test(B4): авторизованные GET — безпараметровые отвечают 2xx, с path-параметрами на несуществующий id дают 404 (исключения заданы явно)
- refactor(delegate): удалён неиспользуемый `run_parallel` (заменён `run_bounded`)
- feat(delegate): параллельное делегирование (B5-D1) — все `delegate_task` одного ответа исполняются одновременно через `delegate.run_bounded`, потолок `delegate_max_parallel`; родитель продолжается только после решения по всем ждущим Approve детям, дубль доставки результата исключён
- test(C5): 143 теста на подтверждение `shell_execute` по всем вариантам SEC-2 (handler, `_execute_tool`, полный цикл с Approve/Reject)
- test(B7): тесты не зависят от порядка — эталонный снимок БД и откат перед каждым тестом, возврат глобального engine после модуля; `pytest-randomly` в requirements-dev
- ci(B2): `.github/workflows/backend-tests.yml` — случайный порядок, `--cov-branch`, порог покрытия 55 % (сейчас 69 %)
- docs: DOD.md (B2/B5/B7/C5 — DONE, F1 — закрыт по словам владельца), WORK_NOTES.md (B5-D1 закрыт)

## 2026-10-13
- fix(delegate): лимиты глубины/параллелизма читаются из Settings; неизвестная роль даёт `failed`, а не исключение; `delegate_task` применяет `timeout_sec` (B5-D2/D3/D4)
- fix(guardrails): имена тулов приведены к реестру ARIA, read-only тулы покрыты no-progress детектором, `mutating_tools` задействован (B5-D5/D6/D7)
- feat(agent): роль `general` получила `memory_search`, `notebook_query`, `skill_lookup`; промпт роли объясняет, что список тулов — срез роли; заполнен `prompts/general.md`
- docs: WORK_NOTES.md — таблица дефектов B5, область тулов по ролям, описание хуков; открыт только B5-D1 (параллельное делегирование не подключено)

## 2026-07-21
- Проведена сверка архива `local-agent-v7-final-delivery` против ТЗ v7.1
- Добавлены отсутствовавшие release-артефакты: `README.md`, `runbook.md`, `architecture.md`, `docker-compose.yml`
- Добавлен каркас `migration-package/` по структуре ТЗ
- Добавлены модули-обёртки `backend/app/api/http.py`, `backend/app/api/ws.py`
- Добавлен базовый scheduler модуль `backend/app/scheduler/jobs.py`
- Добавлены placeholder-пакеты `backend/app/storage/postgres`, `backend/app/storage/redis`
- Добавлены минимальные unittest smoke tests в `backend/tests/`
- Удалены `__pycache__` / `*.pyc` из релизной поставки

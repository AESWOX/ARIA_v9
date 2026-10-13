# Changelog

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

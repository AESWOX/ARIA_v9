# Changelog

## 2026-10-14 (1)
- fix(llm): `allam-2-7b` (арабская модель) исключена из цепочек — по размеру она вставала первой в `fast`; запасная `GROQ_MODEL` по умолчанию = `openai/gpt-oss-20b` (проверена `--probe` на пуле владельца; `llama-3.3-70b-versatile` в списке пула нет)
- fix(llm): Groq больше не зашит на `qwen/qwen3.6-27b` (404 на живом API, 4 места в `router.py`) — цепочки моделей подбираются по `GET /models` пула ключей (`aria/llm/groq_discovery.py`, та же схема, что у Gemini): только чат-модели (без whisper/guard/tts/compound), `main` сильные→слабые, `fast` слабые→сильные, проба `python -m aria.llm.groq_discovery --probe` исключает 404/403/«снята с обслуживания»/квота 0, занятые уходят в конец; кэш `groq_models.json` на 12 ч
- feat(config): `GROQ_MODELS` (явная цепочка, главнее автоподбора), `GROQ_MODEL` (запасная одна модель без сети/кэша), `GROQ_MODELS_AUTO=0` (выключить); первые звенья сохраняют прежние `provider_id`, одна модель = один пул ключей на все классы
- test: `tests/test_groq_discovery.py` (20), `live_smoke.py` берёт модель из цепочки; 1020 → 1040 passed

## 2026-10-13 (3)
- fix(web): `web_search` возвращал `results: []` — парсер переписан на `html.parser` (любой порядок атрибутов и кавычек, раскрытие редиректов `uddg=`, сниппеты, отсев рекламы), добавлен запасной эндпоинт `html.duckduckgo.com/html`, понятная ошибка при капче, предупреждение в лог при нуле ссылок на нормальной странице
- feat(web): новый тул `web_fetch(url, max_chars)` — читаемый текст публичной страницы; SSRF-защита (только http/https, публичные адреса, проверка на каждом редиректе, лимиты размера/времени/типа)
- feat(agent): `web_search` и `web_fetch` у ролей `general`, `orchestrator`, `research`; ответы помечены `source_trust: "untrusted"`, промпт `general` запрещает исполнять инструкции со страниц
- test(web): образцы выдачи в `tests/fixtures/ddg/`, `tests/test_web_tools.py` (82 теста, SSRF включая реальный локальный сокет), B5-инвентарь: 22 тула и обязательный непустой результат `web_search` на образце (+4); всего 933 → 1019 passed
- test(smoke): `live_smoke.py` вызывает боевые `web_search`/`web_fetch` и требует ≥ 3 результатов (раньше проверял только размер страницы и пропустил сломанный парсер)

## 2026-10-13 (2)
- perf(llm): проверка связи с провайдером (GET /models) кэшируется на хост на 60 с — раньше шла перед каждым вызовом модели для каждого провайдера цепочки (~18 000 лишних запросов к Gemini за два дня); неуспех не кэшируется
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

# ARIA — Full Loop-Engineering TZ v1.1

**Дата:** 2026-07-28
**Статус:** финальный, к передаче исполнителю
**Версия документа:** 1.1 (все правки §1–10 + 6 supplement-дыр приняты)
**Базируется на:** ARIA v13 DOD_READY (backend/aria/*)
**Источник:** SOUL.md + архитектурный ревью кода `core/*.py`, `db/models.py`, `db/enums.py`

---

## §0. Структура документа

```
§0  Структура документа
§1  Текущее состояние (уже построено, не трогать)
§2  Loop-engineering v1 — Stage 1–7 (немедленная реализация)
§3  Модель данных — task_plans
§4  Integrity-аудит: НАЕБАЛ / ПРОЕБАЛ / ЗАБЫЛ
§5  Hooks: pre-delivery secret-scan, post-tool-call
§6  Bounded retry + эскалация (Notifier Protocol)
§7  Тестовый план (9 тестов)
§8  DoD этой итерации + Known Issues + integrity_events
§9  Что исключено из v1 (жёсткий список)
§10 Порядок внедрения
§11 Phase 2 — Multi-agent Swarm
§12 Phase 3 — Obsidian REST API (полноценный)
§13 Phase 4 — Автоматическая генерация скиллов
§14 Phase 5 — Cross-session Memory Consolidation
§15 Phase 6 — A/B тестирование провайдеров
§16 Phase 7 — CI/CD Pipeline Integration
§17 Phase 8 — Web Dashboard & Monitoring
```

---

## §1. Текущее состояние (уже построено, не переписывать)

Следующие компоненты **уже реализованы** в `backend/aria/` и НЕ требуют разработки в рамках этого ТЗ:

### 1.1 Sub-agent execution (Stage 3)

```python
# core/delegate.py — §7.2/§7.3
delegate_task(goal, context, toolsets)  # одиночный саб
run_parallel(tasks)                      # до 3 параллельных
```
- `MAX_DEPTH = 1` (запрет вложенной делегации)
- `Task.parent_task_id`, `Task.delegation_depth` — трекинг иерархии
- Sub-agent = рекурсивный вызов той же loop-функции с урезанным role/tool_whitelist

### 1.2 Bounded retry controller (Stage 6 — ядро)

```python
# core/audit.py
Task.audit_attempt_no              # счётчик попыток
settings.audit_max_attempts (default: 3)
AuditVerdict.fail_after_max_attempts  # вердикт при исчерпании
```
Не хватает: эскалация через Notifier Protocol при `fail_after_max_attempts` (см. §6).

### 1.3 Correctness audit (Stage 4 — ядро)

```python
# core/audit.py
run_audit(task, tool_calls) -> AuditReport
├── _structural_check()     # ToolStatus.ok/error/blocked — обязателен, без LLM
└── LLM qualitative pass    # qa_auditor role, стандартная модель
```
Не хватает: integrity-детекторы НАЕБАЛ/ЗАБЫЛ (см. §4).

### 1.4 Post-tool-call логирование (Stage 5 — половина)

```python
# ToolCall таблица — пишется синхронно на каждый вызов
# id, task_id, tool_name, status, risk_level, input_json, output_json,
# started_at, finished_at, duration_ms
```

### 1.5 State machine

```python
# core/state_machine.py
assert_transition_allowed(current, target)
TASK_TRANSITIONS — полная таблица переходов (см. enum TaskStatus)
```

### 1.6 Anti-looping guardrails

```python
# core/guardrails.py
├── IDEMPOTENT_TOOL_NAMES — read-инструменты
├── MUTATING_TOOL_NAMES — write-инструменты
├── repeated_failure_detection
├── tool_loop_detection
├── before_call() / after_call()
└── Decision: allow / warn / block
```

### 1.7 Vault storage (Stage 1 — базовый)

```python
# storage/obsidian_vault.py — файловый транспорт
├── vault_root()
├── create_note(path, content)
├── read_note(path)
├── update_note(path, content)
├── search_vault(query)
├── list_vault(subdir)
└── delete_note(path)
```
- Работает без запущенного Obsidian-приложения
- Vault: `backend/data/vault/` (174 .md файла)
- Поддиректории: `00-TASKS`, `01-WIKI`, `02-PROJECTS`, `03-DECISIONS`

### 1.8 Secret-scan (инструмент, в build_release.py)

```python
# Только в build_release.py — паттерн не вынесен в общий модуль
# scan_files(root) — обход файлов, проверка по DENY_FILES списку
# Работает: 0 секретов на 583 файлах
```

---

## §2. Loop-engineering v1 — Stage 1–7 (немедленная реализация)

### 2.1 Архитектура цикла

```
Пользователь → [Stage 1: Vault-check] → [Stage 2: Plan] → [Stage 3: Execute]
                                                                     ↓
[Stage 7: Delivery] ← [Stage 6: Retry?] ← [Stage 5: Hooks] ← [Stage 4: Audit]
     ↓
 Vault-заметка (единственный источник — БД)
```

### 2.2 Stage 1 — Vault-check

**Компонент:** `obsidian_vault.py` (файловый, без REST API)

**Вход:** `task_id`, контекст запроса
**Выход:** контекст из vault (существующие заметки/теги по теме)

**Логика:**
1. Получить `objective` из `Task`
2. `search_vault(query)` — поиск по ключевым словам задачи
3. Извлечь релевантные фрагменты существующих заметок
4. Вернуть как enriched context для Stage 2

**Acceptance criteria:**
- Работает при закрытом Obsidian-приложении
- 0 обращений к `localhost:27124`
- Если vault пуст — возвращает пустой контекст, не падает

### 2.3 Stage 2 — Plan

**Компонент:** `executor.py` → запись в `task_plans` (БД)

**Вход:** задача от пользователя + контекст из Stage 1
**Выход:** запись в `task_plans` (таблица, см. §3)

**Логика:**
1. Oracle-роль (оркестратор) разбивает задачу на подзадачи
2. Каждая подзадача: `PlanStep {role, tool_ref, skill_ref, objective}` — **строгая схема** (см. ниже)
3. Валидация схемы: ни одной подзадачи без tool_ref/skill_ref — freeform запрещён
4. Статус плана: `draft`

**PlanStep валидатор (код в схеме, не runtime-проверка):**

```python
class PlanStep(BaseModel):
    step_id: UUID
    objective: str
    role: str
    tool_ref: str | None
    skill_ref: str | None
    status: Literal["pending", "in_progress", "done", "failed"]
    tool_call_ids: list[UUID] = []

    @model_validator
    def must_have_tool_or_skill(self):
        if not self.tool_ref and not self.skill_ref:
            raise ValueError("freeform step forbidden")
```

**Политика при невалидном плане от Oracle:**
- Reject → replan максимум **2 раза**
- После 2 неудач → **НАЕБАЛ от Oracle** (см. §4.3), `task_plans.status = escalated` + Telegram
- Retry Oracle не помогает — у него сломана _способность_ генерировать план, не _исполнение_

**Acceptance criteria:**
- Ни одной подзадачи без привязки к tool/skill
- План = массив `PlanStep`, не проза (для ЗАБЫЛ-детектора)
- Oracle НАЕБАЛ при 3× invalid plan → hard fail, escalation, `Task.status = failed`

### 2.4 Stage 3 — Sub-agent execution

**Уже существует** в `core/delegate.py`.

**Адаптация:**
- На вход берёт одну подзадачу из `task_plans.plan_json`
- На выход — лог tool_calls (уже есть) + результат
- Обновляет `task_plans.status` по завершению

### 2.5 Stage 4 — Двойной аудит

**Компонент:** `core/audit.py` (расширение)

**Два независимых аудитора:**

```
1. Correctness-audit (существует)
   ├── _structural_check() — ToolStatus проверка
   └── LLM qualitative pass — qa_auditor роль

2. Integrity-audit (НОВОЕ, см. §4)
   ├── НАЕБАЛ-детектор — чистые функции, без LLM
   ├── ПРОЕБАЛ-детектор (де-факто уже есть в correctness)
   └── ЗАБЫЛ-детектор — строго по step_id / tool_call_ids, без semantic matching
```

**Acceptance criteria:**
- Integrity-аудит обязателен, не опция
- Раздельные verdict-каналы (не смешиваются с correctness)
- LLM в детекторах v1 запрещён

### 2.6 Stage 5 — Hooks

**Pre-delivery secret-scan (см. §5):**
- Вынести `scan_files()` из `build_release.py` в `core/secretscanner.py`
- Сканирует только файлы, изменённые в текущем delivery (не весь vault)
- ALLOWLIST для тестовых ключей/примеров в docs
- Severity: critical (блокирует) / warning (логирует)
- Вызывать на каждой доставке результата (Stage 5 → 6/7)

**Post-tool-call логирование:**
- Уже существует (синхронная запись в ToolCall)
- Дополнить: запись `hash_before` для модифицирующих tool_call (см. §4)

### 2.7 Stage 6 — Bounded retry + эскалация

**Уже существует (ядро):**
- `audit_attempt_no`, `audit_max_attempts = 3`, `fail_after_max_attempts`

**Новое:**
- Эскалация через **Notifier Protocol** (см. §6) при исчерпании попыток
- Обновление `task_plans.iteration_count`
- При НАЕБАЛ-вердикте: retry запрещён, escalation немедленно

### 2.8 Stage 7 — Delivery

**Компонент:** `executor.py` → `obsidian_vault.write_note()`

**Логика:**
1. Считать финальный результат из `task_plans.final_result_json`
2. Если `final_result_json IS NULL` — сгенерировать заметку «Задача не завершена: {причина}» со ссылкой на audit-verdict
3. Если `plan_json.status != 'done'` — same, не падать с 500
4. Иначе: сгенерировать vault-заметку целиком (шаблон + `obsidian-markdown` skill)
5. Записать через `write_note()` + атомарная запись (temp + rename, см. §2.9)
6. Обновить `task_plans.status = 'done'`

**Delivery-контракт:** vault-заметка обязана содержать:
- Все `step_id` + их `status`
- Список `tool_call_ids`
- `integrity-verdict` (pass / НАЕБАЛ / ЗАБЫЛ)
- Ссылку на `task_id`

Если skill сгенерировал заметку без этих полей → `delivery = fail`.

**Acceptance criteria:**
- Заметка генерируется целиком из `final_result_json + plan_json`. Ручной контент запрещён.
- БД — единственный источник правды
- Obsidian-markdown properties/callouts/wikilinks
- Пустой результат → заметка «не завершена», не 500

### 2.9 §2.9 — Конкурентность и изоляция (NEW)

**Optimistic locking на `task_plans`:**

```sql
-- Добавить в таблицу
version INTEGER NOT NULL DEFAULT 1

-- UPDATE с проверкой
UPDATE task_plans
SET plan_json = ..., version = version + 1
WHERE id = :id AND version = :expected_version;
```

**Vault-запись задачи под file-lock (атомарная):**

```python
def write_note_atomic(path: str, content: str) -> None:
    """Атомарная запись: temp → rename."""
    tmp = path + ".tmp." + str(uuid4())
    tmp.write_text(content, encoding="utf-8")
    tmp.rename(path)  # атомарно на той же файловой системе
```

**Lock TTL:**
- Lock-файл: `{vault_root()}/.locks/{task_id}.lock`
- TTL = 30s (`settings.lock_ttl_seconds`, конфигурируемо)
- После таймаута — принудительный unlock, задача переходит в `awaiting_attention`
- Parallel sub-agents одной задачи: максимум 1 writer в `task_plans` и vault одновременно

**Acceptance:** два параллельных retry одной задачи не портят `plan_json` и не оставляют half-written заметку.

---

## §3. Модель данных — task_plans

### 3.1 Новая таблица (финальная схема)

```sql
CREATE TABLE task_plans (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         UUID NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMP NOT NULL DEFAULT NOW(),

    -- Структура плана
    plan_json       JSONB NOT NULL,     -- [{PlanStep}] (см. §2.3)
    iteration_count INTEGER NOT NULL DEFAULT 0,  -- 0..3

    -- Версионирование (Дыра #2)
    version         INTEGER NOT NULL DEFAULT 1,
    plan_history    JSONB DEFAULT '[]',  -- [{version, plan_json, changed_at}]
    -- Retention: plan_history_max_entries = 20, shift oldest при превышении

    -- Статус
    status          VARCHAR(32) NOT NULL DEFAULT 'draft',
                    -- draft | in_progress | audit_pending | done | escalated

    -- Результат
    final_result_json JSONB,            -- заполняется на Stage 7
    integrity_verdict VARCHAR(32)        -- pass | naebal | zabyl | escalated
);
```

### 3.2 Правила версионирования

- Каждое изменение `plan_json` → инкремент `version` + append `{version, plan_json, changed_at}` в `plan_history`
- Retention: при превышении `plan_history_max_entries = 20` → `shift()` oldest entry
- `iteration_count` — не единственный след изменений

### 3.3 Правила работы

- План меняется → правится **только** запись в БД (через optimistic lock)
- Vault-заметка перегенерируется целиком после каждого успешного шага
- Ручной патч vault-заметки запрещён
- Изменение плана = `version++` + запись в `plan_history`

---

## §4. Integrity-аудит: НАЕБАЛ / ПРОЕБАЛ / ЗАБЫЛ

### 4.1 Три детектора

| Категория | Что значит | Как детектить | Acceptance criteria |
|---|---|---|---|
| **НАЕБАЛ** | Соврал про результат — заявил успех, которого не было | Чистые функции: `assert_file_changed`, `get_exit_code`, `has_junit_artifact`. Сверка claimed vs actual | Любое расхождение claimed vs actual артефакт → hard fail, retry запрещён |
| **ПРОЕБАЛ** | Честно накосячил — пытался, но не вышло | Correctness-audit находит расхождение план↔результат, но нет следов сокрытия | Обычный retry-кейс, до 3 итераций, не integrity-нарушение |
| **ЗАБЫЛ** | Потерял часть задачи по ходу (контекст съехал) | `extract_covered_step_ids()` — строго по `step_id`/`tool_call_ids`. Semantic matching исключён из v1 | Любой пункт плана без tool_call → fail |

### 4.2 Чистые функции детекторов (обязательные, без LLM)

```python
def file_content_hash(path: Path) -> str:
    """sha256 содержимого файла. Используется до и после write/patch."""

def assert_file_changed(tc: ToolCall) -> bool:
    """True только если hash_before != hash_after. Иначе НАЕБАЛ."""

def get_exit_code(tc: ToolCall) -> int | None:
    """Из output_json / stderr. None = артефакт отсутствует → НАЕБАЛ."""

def has_junit_artifact(tc: ToolCall) -> bool:
    """True только если junit-файл реально существует и не пустой."""

def extract_covered_step_ids(
    plan_json: list[PlanStep],
    tool_calls: list[ToolCall]
) -> set[str]:
    """Строго по tool_call_ids и step_id. Semantic match запрещён в v1."""
```

### 4.3 Red-flag паттерны (НАЕБАЛ)

Список **обязательный**, расширяется по мере находок:

1. **Деселект/скип тестов без объяснения** в отчёте
2. **Self-referential JSON** — verify-скрипт ссылается сам на себя как на источник правды
3. **«Восстановлено»/«исправлено»** без приложенного артефакта (архив, diff, лог) для проверки
4. **No-op patch** — файл открыт на запись, но `hash_before == hash_after`
5. **Circular dependency claim** — «A зависит от B, B зависит от A, всё ок»
6. **Fake test output** — утверждение о прохождении теста, но junit-файл отсутствует или пуст
7. **Oracle invalid plan 3×** — Oracle 3 раза выдал freeform-шаги → НАЕБАЛ от Oracle (см. §2.3)

### 4.4 Механизм детекции НАЕБАЛ

```python
def detect_naebal(tool_calls: list[ToolCall]) -> IntegrityFlag | None:
    """Сверяет claimed result из output_json с фактическим состоянием.
    Только чистые функции, без LLM."""

    for tc in tool_calls:
        if tc.tool_name in ("terminal",) and "pytest" in tc.input_json:
            exit_code = get_exit_code(tc)
            junit = has_junit_artifact(tc)
            claimed_pass = "passed" in tc.output_json

            if claimed_pass and (exit_code != 0 or not junit):
                return IntegrityFlag.NAEBAL(
                    tool_call_id=tc.id,
                    reason=f"заявлен passed, exit_code={exit_code}, junit={junit}"
                )

        if tc.tool_name in ("write_file", "patch"):
            if not assert_file_changed(tc):
                return IntegrityFlag.NAEBAL(
                    tool_call_id=tc.id,
                    reason="write_file выполнен, но hash_before == hash_after"
                )

    return None
```

### 4.5 Механизм детекции ЗАБЫЛ

```python
def detect_zabyl(
    plan_json: list[PlanStep],
    tool_calls: list[ToolCall]
) -> IntegrityFlag | None:
    """Сверяет план (чек-лист по step_id) с выполненными tool_calls.
    Строго по tool_call_ids. Semantic matching исключён."""

    covered = extract_covered_step_ids(plan_json, tool_calls)
    missing_steps = [s for s in plan_json if s.step_id not in covered]

    if missing_steps:
        return IntegrityFlag.ZABYL(
            missing_steps=[s.step_id for s in missing_steps],
            reason=f"{len(missing_steps)}/{len(plan_json)} шагов не покрыты tool_calls"
        )

    return None
```

### 4.6 Правила

- **НАЕБАЛ:** любое расхождение claimed vs actual артефакт → hard fail, retry запрещён
- **ЗАБЫЛ:** только по `step_id` / `tool_call_ids`. Semantic matching исключён из v1
- **Partial success** (< 100% шагов закрыто) = ЗАБЫЛ
- **100% детерминированные unit-тесты** на каждый red-flag
- **LLM в детекторах v1 запрещён** — нарушает детерминизм

---

## §5. Hooks: pre-delivery secret-scan, post-tool-call

### 5.1 Secret-scanner (вынос из build_release.py)

```python
# core/secretscanner.py
from pydantic import BaseModel
from enum import Enum

class Severity(str, Enum):
    critical = "critical"   # Блокирует доставку
    warning = "warning"     # Логирует, не блокирует

class SecretMatch(BaseModel):
    file: Path
    pattern: str
    severity: Severity
    snippet: str

class ScanResult(BaseModel):
    matches: list[SecretMatch]
    files_scanned: int
    critical_count: int
    warning_count: int
```

**DENY_PATTERNS и ALLOWLIST:**

```python
DENY_PATTERNS: list[str] = [
    ".*sk-[a-zA-Z0-9]{20,}.*",      # OpenAI/Sk
    ".*AIza[0-9A-Za-z_-]{35}.*",    # Gemini
    ".*ghp_[a-zA-Z0-9]{36}.*",      # GitHub PAT
    ".*gho_[a-zA-Z0-9]{36}.*",      # GitHub OAuth
    ".*xox[bpras]-[a-zA-Z0-9-]{10,}.*",  # Slack
    "-----BEGIN (RSA |EC )?PRIVATE KEY-----",  # Private keys
]

ALLOWLIST: list[str] = [
    "sk-your-api-key-here",         # Пример в документации
    "AIzaSyTest1234",               # Тестовый ключ
    "ghp_yourAccessToken",           # Пример в README
]
```

**Основные функции:**

```python
def scan_file(path: Path, deny: list[str], allow: list[str]) -> list[SecretMatch]:
    """Сканирует один файл, проверяет allowlist."""

def scan_changed_files(
    root: Path,
    changed_paths: list[Path],
    deny: list[str] | None = None,
    allow: list[str] | None = None
) -> ScanResult:
    """Сканирует только изменённые файлы текущего delivery. Не весь vault."""

def assert_no_secrets(result: ScanResult, min_severity: Severity = "critical") -> None:
    """Бросает исключение, если найдены critical-секреты."""
```

**Правила:**
- Scan только файлов, изменённых в текущем delivery (не весь vault)
- ALLOWLIST для тестовых ключей, примеров в docs
- `severity=critical` → блокирует доставку
- `severity=warning` → логирует, delivery продолжается

---

## §6. Bounded retry + эскалация (Notifier Protocol)

### 6.1 Notifier Protocol (интерфейс)

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class Notifier(Protocol):
    """Интерфейс уведомлений. Не привязан к Telegram API."""

    async def send_escalation(
        self,
        task_id: str,
        objective: str,
        claimed_result: str,
        audit_findings: str,
        iteration: int,
        tool_call_log_url: str,
    ) -> None:
        ...
```

### 6.2 TelegramNotifier (единственная реализация в v1)

```python
class TelegramNotifier:
    """Реализация Notifier через Telegram Bot API.

    - timeout=10s, retry=2
    - idempotency_key = f"{task_id}:{iteration}" — защита от дублирования
    - При недоступности бота: задача всё равно переходит в failed + лог ошибки.
      Notifier failure НЕ является integrity-нарушением (см. Дыра #3).
    """

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout = 10
        self.retry = 2
```

### 6.3 Правила

- **Прямой вызов Telegram API из audit/retry запрещён** — только через Notifier Protocol
- **Notifier failure ≠ НАЕБАЛ** — явно зафиксировать. Уведомления вне integrity-контура. `assert_file_changed()` и `get_exit_code()` не вызываются для уведомлений.
- При недоступности бота: задача переходит в `failed`, ошибка логируется, цикл не виснет

### 6.4 Формат эскалации

```
🚨 ARIA — эскалация задачи

Задача:      {id[:8]} — {objective[:60]}
Вердикт:     {verdict} ({iteration}/{max_attempts})

Что заявлялось:
{claimed_result[:200]}

Что показал audit:
{audit_findings[:300]}

Tool-call лог: http://localhost:8765/tasks/{id}/tool-calls
```

---

## §7. Тестовый план (9 тестов)

### 7.1 Integrity injection test (НАЕБАЛ)

```python
def test_integrity_detects_naebal():
    """
    Саб намеренно «врёт»: mock claimed result ≠ actual diff.
    Детектор НАЕБАЛ обязан:
    - поймать на 100% прогонов (не флейково)
    - выдать hard fail
    - не допустить retry
    """
```

### 7.2 Bounded retry exhaustion test

```python
def test_retry_exhaustion_escalates():
    """
    3 неудачные итерации подряд.
    Проверка:
    - iteration_count = 3
    - Notifier.send_escalation вызван (mock)
    - Task.status = 'failed'
    """
```

### 7.3 Plan/vault consistency test

```python
def test_plan_update_regenerates_vault():
    """
    План меняется в БД → vault-заметка перегенерируется целиком.
    Старая версия не остаётся как второй источник правды.
    - До: vault содержит старую заметку
    - После: vault содержит новую заметку, старая перезаписана
    """
```

### 7.4 Secret-scan hook test

```python
def test_secret_scan_blocks_delivery():
    """
    Намеренно подложенный секрет в диффе результата.
    Pre-delivery hook обязан:
    - заблокировать доставку
    - записать ToolCall.status = blocked_policy
    - не допустить запись в vault
    """
```

### 7.5 ЗАБЫЛ-detector test

```python
def test_zabyl_detector_missing_steps():
    """
    План из 3 пунктов, финальный отчёт закрывает только 2.
    Детектор ЗАБЫЛ обязан:
    - поймать несоответствие
    - указать какой шаг пропущен (по step_id)
    - не смешивать с НАЕБАЛ/ПРОЕБАЛ вердиктом
    - НЕ использовать LLM-matching
    """
```

### 7.6 НАЕБАЛ blocks retry test (NEW)

```python
def test_naebal_blocks_retry():
    """
    НАЕБАЛ-вердикт → iteration_count НЕ увеличивается.
    Немедленный escalate.
    Retry запрещён.
    """
```

### 7.7 Invalid plan replans max 2 test (NEW)

```python
def test_invalid_plan_replans_max_2():
    """
    Oracle 3 раза выдаёт freeform → escalate.
    - 1-й раз: reject → replan
    - 2-й раз: reject → replan
    - 3-й раз: НАЕБАЛ от Oracle, escalation
    - task_plans.status = escalated
    """
```

### 7.8 Plan versioning test (NEW)

```python
def test_plan_versioning():
    """
    Изменение plan_json:
    - увеличивает version
    - пишет запись в plan_history
    - при превышении max_entries (20) — shift oldest
    """
```

### 7.9 Concurrent retry isolation test (NEW)

```python
def test_concurrent_retry_isolation():
    """
    Два параллельных retry одной задачи:
    - optimistic lock защищает plan_json
    - vault-запись атомарна (temp + rename)
    - итоговое состояние консистентно
    """
```

---

## §8. DoD этой итерации + Known Issues + integrity_events

### 8.1 DoD (Definition of Done)

| Условие | Критерий |
|---------|----------|
| Существующие тесты | 80/80 зелёных |
| Новые тесты | 9/9 зелёных (см. §7) |
| Stage 1–7 работают как единый цикл | `executor.run(task_id)` → полный проход |
| `task_plans` таблица | Создана (alembic migration), схема соответствует §3 |
| Integrity-аудит | Все 3 детектора активны, LLM запрещён, 100% детерминированы |
| Secret-scan hook | Вызван на каждой доставке, сканирует только changed files |
| Notifier Protocol | TelegramNotifier реализован, отказ не integrity-нарушение |
| Vault-заметка | Генерируется целиком из БД, соответствует delivery-контракту |
| Optimistic locking | `version` + атомарная запись, lock TTL = 30s |
| Oracle НАЕБАЛ | 3× invalid plan → hard fail |
| `integrity_events` таблица | Создана, логирует каждый вердикт |

### 8.2 integrity_events таблица (NEW)

```sql
CREATE TABLE integrity_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id         UUID NOT NULL REFERENCES tasks(id),
    tool_call_id    UUID REFERENCES tool_calls(id),
    detector        VARCHAR(32) NOT NULL,  -- 'naebal' | 'zabyl'
    verdict         VARCHAR(32) NOT NULL,  -- 'pass' | 'fail' | 'false_positive'
    artifact_hash   VARCHAR(64),
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);
```

**Правила:**
- Каждый integrity-verdict логируется в `integrity_events`
- `false_positive` — **только ручная метка** (через команду или dashboard Phase 8)
- Без ручной метки `false_positive` нельзя утверждать, что детектор работает
- Это единственный источник правды для метрики точности детекторов

### 8.3 Known Issues (не блокируют DoD)

| Проблема | Описание | Статус |
|----------|----------|--------|
| `dod_verify.py INSTALL_FAILED` | `--clean` не создаёт venv автоматически на чистом чекауте | Не блокирует — проблема build/deploy, не loop-engineering |
| Obsidian REST API | Не реализован, файловый транспорт | Сознательное решение, Phase 3 |
| NotebookLM | Исключён из скоупа | Нет официального API, session-куки ненадёжны |
| Telegram — только эскалация | Не полный бот, нет команд | Сознательное решение, расширяется позже |
| Semantic matching | Исключён из v1 | Нестабильно, источник ложных срабатываний |

---

## §9. Что исключено из v1 (жёсткий список)

| Исключено | Почему |
|-----------|--------|
| Semantic matching в ЗАБЫЛ | Нестабильно, источник ложных срабатываний |
| LLM внутри integrity-детекторов | Нарушает детерминизм |
| Full-tree secret-scan на delivery | Медленно + false positives |
| Прямой Telegram API без Notifier | Нет контроля отказов |
| Freeform-шаги в плане | Ломает ЗАБЫЛ-детектор |
| Ручной патч vault-заметки | Уже запрещено, закрепить жёстко |
| NotebookLM | Нет официального API |
| Obsidian REST API | Отдельное ТЗ в Phase 3 |

---

## §10. Порядок внедрения

```
1. Миграция task_plans (+ version, plan_history, integrity_events)
2. PlanStep validator + replan policy (Oracle 2× → НАЕБАЛ)
3. Чистые функции детекторов (§4.2) + unit-тесты на каждый red-flag
4. Secret-scanner с allowlist + changed-only (§5)
5. Notifier Protocol + TelegramNotifier (§6)
6. Locking: optimistic + file-lock + TTL (§2.9)
7. Delivery-контракт + обработка пустого результата (§2.8)
8. Тесты: 9 шт из §7
9. Интеграционный прогон executor.run() — Stage 1–7
10. Прогон 80 существующих тестов — 0 регрессий
```

---

## §11–§17. Будущие фазы (Phase 2–8)

### Phase 2 — Multi-agent Swarm
**Когда:** Stage 1–7 прошли 5+ циклов без integrity-нарушений.
- Параллельные планы (веер/аукцион/консенсус)
- Координация сабов через `depends_on`
- Топологическая сортировка зависимостей

### Phase 3 — Obsidian REST API
**Когда:** плагин Local REST API установлен на ПК.
- `storage/obsidian_rest.py` — HTTP-клиент
- Живой поиск по графу связей
- Bi-directional sync

### Phase 4 — Автоматическая генерация скиллов
**Когда:** накоплено 20+ integrity-отчётов.
- Из успешных проходов → паттерны → SKILL.md
- Из проблемных мест → патчи guardrails
- Валидация: сгенерированный skill улучшает результат?

### Phase 5 — Cross-session Memory Consolidation
**Когда:** 2+ недели использования.
- Cross-session learning
- Архивация старых планов
- Deduplication

### Phase 6 — A/B тестирование провайдеров
**Когда:** Phase 2 (нужны параллельные прогоны).
- Одна задача на DeepSeek VS Gemini VS Groq
- Rating по integrity-score
- Автовывод слабых провайдеров

### Phase 7 — CI/CD Pipeline Integration
**Когда:** GitHub webhook настроен.
- git push → ARIA цикл → integrity-audit → merge/block

### Phase 8 — Web Dashboard
**Когда:** Phase 3 (данные для дашбордов).
- /plans, /integrity, /escalations
- Heatmap: кто чаще НАЕБАЛ
- Кнопка «resolve» в Telegram

**Полный цикл Phase 2–8:** 30–45 дней после v1.

---

## Приложение A: Глоссарий

| Термин | Значение |
|--------|----------|
| НАЕБАЛ | Саботировал — заявил успех, которого не было |
| ПРОЕБАЛ | Честно накосячил — пытался, не вышло |
| ЗАБЫЛ | Потерял контекст — не закрыл все пункты плана |
| Integrity-score | % проходов без НАЕБАЛ/ЗАБЫЛ |
| Correctness-score | % проходов без ПРОЕБАЛ |
| Vault | Obsidian-совместимая директория .md файлов |
| task_plans | Таблица планов задач (единственный источник правды) |
| Self-referential JSON | verify-скрипт, ссылающийся сам на себя |
| Notifier Protocol | Интерфейс уведомлений, не привязан к Telegram API |
| PlanStep | Валидируемая модель шага плана (tool_ref или skill_ref обязательны) |
| Oracle НАЕБАЛ | 3× invalid plan от оркестратора → hard fail |
| integrity_events | Таблица логов каждого integrity-вердикта |
| Lock TTL | 30s timeout на optimistic lock |

"""Config contract — §23 ТЗ v7.1.

Canonical keys обязательны; значения — v7 default profile, если ADR не утверждает иное.
Все настройки читаются из env с префиксом, сохраняя имена из §23.2.

SQLite vs Postgres (P1.5):
- Default POSTGRES_DSN points at a local sqlite file — fine for single-user
  local desktop use (backend started via `run_backend.py` on 127.0.0.1).
- If the backend runs in server/multi-user mode (ARIA_SERVER_MODE=1 or
  http_host set to a non-loopback address) while POSTGRES_DSN still starts
  with "sqlite", aria.main logs a startup warning: SQLite is not recommended
  for production/multi-user. Set POSTGRES_DSN to a real Postgres DSN then.
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from aria import paths


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=paths.env_file_setting(), extra="ignore")

    # --- §23.2 env naming convention ---
    # Default value points at a local sqlite file so the backend runs with zero
    # external infra out of the box (§18 dev profile). Set POSTGRES_DSN in .env
    # to point at real Postgres for production (canonical key name per §23.2).
    POSTGRES_DSN: str = paths.default_dsn()
    REDIS_URL: str = "redis://localhost:6379/0"
    OBSIDIAN_VAULT_PATH: str = paths.default_vault_path()
    B2_BUCKET: str = ""
    B2_KEY_ID: str = ""
    B2_APPLICATION_KEY: str = Field(default="", validation_alias=AliasChoices("B2_APPLICATION_KEY", "B2_APP_KEY"))

    # --- §23.1 canonical keys / v7 default profile ---
    # §2.9 TZ v1.1: optimistic lock TTL (file-lock + DB)
    LOCK_TTL_SECONDS: int = 30
    loop_max_iterations: int = 15
    # Волна 1 (A10): размер пула TaskRunner. На 4 ГБ RAM держим 3 по умолчанию,
    # при нехватке памяти TaskRunner сам опускается до 1 воркера.
    runner_max_workers: int = 3
    audit_max_attempts: int = 3
    # П.1.6 ТЗ: выключатель параллельного EXECUTE. False — волны DAG не собираются,
    # план исполняется строго по порядку шагов (аварийный режим для старых планов).
    plan_dag_parallel_enabled: bool = True
    # П.4.1 ТЗ: выключатель Clarifier-гейта (PR-3). False — поведение как до PR-3:
    # быстрый путь draft -> approved без уточняющих вопросов.
    clarifier_gate_enabled: bool = True
    # П.4.2 ТЗ: список глаголов-маркеров задачи (через запятую) для agent-режима;
    # пусто = встроенный список clarifier._DEFAULT_TASK_VERBS.
    clarifier_task_verbs: str = ""
    # П.5.2 ТЗ: лимиты параллелизма по классам провайдеров, например
    # "free_tier_reasoning=2,standard_reasoning=3" (иначе один общий лимит).
    provider_max_concurrency: str = ""
    # П.5.4 ТЗ: значения карты DLP хранятся зашифрованными (enc:v1:), не открытым текстом.
    dlp_encrypt_secrets: bool = True
    # PR-7: DLP включается только для чата и по умолчанию выключен; агентский
    # цикл не маскируется — плейсхолдеры сломали бы пути и команды в tool calls.
    dlp_chat_enabled: bool = False
    dlp_names: str = ""  # имена/логины через запятую: то, что не ловится шаблоном
    delegate_max_parallel: int = 5
    delegate_timeout_sec: int = 60
    tools_default_timeout_sec: int = 120
    providers_connectivity_timeout_sec: int = 2
    providers_degraded_ttl_min: int = 15
    security_auto_lock_minutes: int = 15
    watchdog_runpod_ttl_min: int = 120
    budget_warn_threshold_pct: int = 80
    aria_dev_mode: bool = False
    budget_block_threshold_pct: int = 100
    # Spend limits in USD (0 = limit disabled). Spend is estimated from message token counts
    # at DeepSeek Flash peak rates, so it is an upper bound for Flash and a lower bound for Pro.
    budget_daily_usd: float = 0.0
    budget_weekly_usd: float = 0.0

    @field_validator("aria_dev_mode", mode="before")
    @classmethod
    def _force_dev_mode_off_in_frozen(cls, value: object) -> bool:
        # §P3 ТЗ v7.1: в frozen (PyInstaller) сборке aria_dev_mode жёстко
        # выключен — нельзя случайно открыть защищённые руты через .env
        # рядом с exe. Dev-mode auth bypass живёт только в запуске из исходников.
        if getattr(sys, "frozen", False):
            return False
        return value if value is not None else False

    backup_retention_days: int = 30

    @field_validator("POSTGRES_DSN", "OBSIDIAN_VAULT_PATH", "agent_sandbox_root", mode="before")
    @classmethod
    def _blank_means_default(cls, value: object, info) -> object:
        # ``KEY=`` in .env must mean "use the default", not an empty path
        # (an empty vault path silently resolves to the current directory).
        if isinstance(value, str) and not value.strip():
            return {
                "POSTGRES_DSN": paths.default_dsn(),
                "OBSIDIAN_VAULT_PATH": paths.default_vault_path(),
                "agent_sandbox_root": paths.default_sandbox_path(),
            }[info.field_name]
        return value

    # --- §8.2 approval TTLs (default implementation profile) ---
    approval_ttl_default_hours: int = 24
    approval_ttl_budget_escalation_minutes: int = 30
    approval_ttl_shell_high_risk_hours: int = 2

    # --- §7.2 delegation ---
    delegate_max_depth: int = 1
    delegate_retry: int = 1

    # --- runtime / API (§10.4 auth handshake) ---
    http_host: str = "127.0.0.1"
    http_port: int = 8765
    # Cross-platform, backend-owned bootstrap location outside the repo tree
    # (~/.local-agent-ui/bootstrap.json). Tauri reads this file directly instead
    # of relying on independently-configured env defaults (fixes §10.4 gap).
    runtime_token_path: str = str(Path.home() / ".local-agent-ui" / "bootstrap.json")
    # First-launch marker, deliberately independent of bootstrap.json: written
    # unconditionally on every issue() even when the Tauri shell disables the
    # bootstrap handshake (LOCAL_AGENT_DISABLE_BOOTSTRAP_WRITE=1), so onboarding
    # fires exactly once. Deleting bootstrap.json (PIN re-setup) must not
    # re-trigger onboarding, and vice versa.
    onboarded_marker_path: str = str(Path.home() / ".local-agent-ui" / ".onboarded")
    ws_backfill_limit: int = 500

    @field_validator("runtime_token_path", "onboarded_marker_path", mode="before")
    @classmethod
    def _default_path_if_blank(cls, value: str | None, info) -> str:
        # RUNTIME_TOKEN_PATH= (blank) in .env is meant to mean "use the
        # default", but pydantic-settings treats a blank env value as an
        # explicit empty-string override, which resolves to "." and crashes
        # the backend on startup (IsADirectoryError). Treat blank as unset.
        if not value:
            defaults = {
                "runtime_token_path": str(Path.home() / ".local-agent-ui" / "bootstrap.json"),
                "onboarded_marker_path": str(Path.home() / ".local-agent-ui" / ".onboarded"),
            }
            return defaults.get(info.field_name, value)
        return value

    # --- §16.3 Security UX: idle-lock PIN. No hardcoded default — if unset,
    # backend generates a random one at startup and keeps it in backend memory;
    # bootstrap contains only pinRequired/idleLock metadata while verification
    # still goes through backend HTTP.
    LOCAL_AGENT_UI_PIN: str = ""

    # --- LLM providers ---
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_api_key: str = ""
    # Model ids are configurable: DeepSeek retired deepseek-chat / deepseek-reasoner
    # (GET /v1/models on 15.10.2026 lists only deepseek-flash and deepseek-v4-pro).
    deepseek_chat_model: str = "deepseek-flash"  # standard_reasoning link
    deepseek_reasoner_model: str = "deepseek-v4-pro"  # premium_reasoning link
    # DeepSeek V4 thinks by default (effort=high) and bills reasoning tokens as output.
    # off = {"thinking": {"type": "disabled"}}; low|high|max = reasoning_effort.
    deepseek_chat_thinking: str = "off"
    deepseek_reasoner_thinking: str = "high"

    # Legacy single-key fallback (used only if GEMINI_API_KEYS is empty).
    gemini_api_key: str = ""
    # Main rotation pool used for actual agent answers, e.g.:
    # GEMINI_API_KEYS=key1,key2,key3,...,key9
    gemini_api_keys: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    # Model ids are configurable because Google moves models between the free
    # and paid tiers. Defaults are Flash for BOTH classes so a free key never
    # hits a paid-only model. With billing enabled set e.g.
    # GEMINI_PRO_MODEL=gemini-2.5-pro in .env.
    gemini_flash_model: str = "gemini-3.8-flash"
    gemini_pro_model: str = "gemini-3.8-flash"
    # 0021: цепочки моделей (через запятую). Квота Gemini считается на проект И МОДЕЛЬ, поэтому каждая
    # модель в цепочке даёт свою отдельную квоту на тех же ключах: когда исчерпана одна, запрос идёт на
    # следующую. Пусто = одна модель из GEMINI_FLASH_MODEL / GEMINI_PRO_MODEL / COMPRESSION_MODEL (как раньше).
    # Пример: GEMINI_FLASH_MODELS=gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash-lite
    gemini_flash_models: str = ""
    gemini_pro_models: str = ""
    # 0027: автоподбор цепочек по доступным моделям (aria/llm/gemini_discovery.py, scripts/gemini_autoconfig.py).
    # Работает, только когда цепочка не задана явно; явные GEMINI_*_MODELS всегда главнее. 0 = выключить.
    gemini_models_auto: bool = True

    groq_api_key: str = ""
    # GROQ_API_KEYS=key1,key2,...,key10
    groq_api_keys: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    # Одна запасная модель, если нет ни GROQ_MODELS, ни кэша автоподбора (первый старт без сети).
    # Не гарантия: список моделей Groq меняется, автоподбор (aria/llm/groq_discovery.py) его заменяет.
    groq_model: str = "openai/gpt-oss-20b"  # жива на пуле владельца 14.10.2026 (--probe)
    # Явная цепочка (через запятую, порядок = порядок попыток) для всех классов Groq; главнее автоподбора.
    groq_models: str = ""
    # Автоподбор цепочек из GET /models по вашему пулу ключей. 0 = выключить.
    groq_models_auto: bool = True

    # --- Compression: separate key pool, separate rotation cursor from the
    # main answer pool above. Does NOT share state with gemini_api_keys. ---
    compression_enabled: bool = True
    compression_gemini_api_keys: str = ""  # falls back to gemini_api_keys if empty
    compression_model: str = "gemini-3.8-flash"
    compression_models: str = ""  # цепочка моделей сжатия (см. gemini_flash_models); пусто = compression_model
    compression_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    compression_timeout_sec: int = 120
    # CTX: основной триггер — оценка токенов истории сессии (порог на СЕССИЮ,
    # не на ключ Gemini: ротация ключей по квоте историю не меняет).
    # 0 = токенный триггер выключен.
    compression_token_threshold: int = 100_000
    # запасной триггер по числу сообщений; 0 = выключен (раньше было 60)
    compression_hard_message_limit: int = 0
    compression_protect_first_n: int = 3
    compression_protect_last_n: int = 15
    compression_target_ratio: float = 0.2

    agent_sandbox_root: str = paths.default_sandbox_path()

    @field_validator(
        "gemini_api_keys", "groq_api_keys", "compression_gemini_api_keys",
        "gemini_flash_models", "gemini_pro_models", "compression_models", "groq_models",
        mode="before",
    )
    @classmethod
    def _normalize_key_list(cls, value: str | None) -> str:
        return value or ""

    def _parse_keys(self, raw: str | None) -> list[str]:
        return [k.strip() for k in (raw or "").split(",") if k.strip()]

    @staticmethod
    def _model_chain(chain_raw: str | None, single: str) -> list[str]:
        """Цепочка моделей без дублей, порядок сохраняется; пустой список в .env = одна модель (прежнее поведение)."""
        out: list[str] = []
        for name in (chain_raw or "").split(","):
            name = name.strip()
            if name and name not in out:
                out.append(name)
        return out or ([single.strip()] if single and single.strip() else [])

    def _auto_chain(self, kind: str) -> str:
        """0027: цепочка из кэша автоподбора (строкой через запятую) или пусто."""
        if not self.gemini_models_auto:
            return ""
        from aria.llm import gemini_discovery

        return ",".join(gemini_discovery.cached_chain(kind))

    def _groq_chain(self, kind: str) -> list[str]:
        """GROQ_MODELS (явно) -> кэш автоподбора (main | fast) -> GROQ_MODEL."""
        auto = ""
        if not self.groq_models and self.groq_models_auto:
            from aria.llm import groq_discovery

            auto = ",".join(groq_discovery.cached_chain(kind))
        return self._model_chain(self.groq_models or auto, self.groq_model)

    @property
    def groq_main_chain(self) -> list[str]:
        return self._groq_chain("main")

    @property
    def groq_fast_chain(self) -> list[str]:
        return self._groq_chain("fast")

    @property
    def gemini_flash_chain(self) -> list[str]:
        return self._model_chain(self.gemini_flash_models or self._auto_chain("flash"), self.gemini_flash_model)

    @property
    def gemini_pro_chain(self) -> list[str]:
        return self._model_chain(self.gemini_pro_models or self._auto_chain("pro"), self.gemini_pro_model)

    @property
    def clarifier_verbs_list(self) -> list[str]:
        """П.4.2 ТЗ: настроенный список глаголов-маркеров задачи (пусто = встроенный)."""
        return [v.strip() for v in (self.clarifier_task_verbs or "").split(",") if v.strip()]

    @property
    def provider_concurrency_limits(self) -> dict[str, int]:
        """П.5.2 ТЗ: "класс=лимит" из provider_max_concurrency (мусор игнорируется)."""
        out: dict[str, int] = {}
        for chunk in (self.provider_max_concurrency or "").split(","):
            if "=" not in chunk:
                continue
            name, _, value = chunk.partition("=")
            name, value = name.strip(), value.strip()
            try:
                limit = int(value)
            except ValueError:
                continue
            if name and limit > 0:
                out[name] = limit
        return out

    @property
    def dlp_names_list(self) -> list[str]:
        """PR-7: явный список имён/сущностей для маскировки чата."""
        return [n.strip() for n in (self.dlp_names or "").split(",") if n.strip()]

    @property
    def compression_chain(self) -> list[str]:
        return self._model_chain(self.compression_models or self._auto_chain("compression"), self.compression_model)

    @property
    def gemini_api_keys_list(self) -> list[str]:
        from aria.secrets import secret_provider
        keys = secret_provider.get_key_list("GEMINI_API_KEYS")
        if keys:
            return keys
        # fallback: read from pydantic field for backward compat
        return self._parse_keys(self.gemini_api_key)

    @property
    def groq_api_keys_list(self) -> list[str]:
        from aria.secrets import secret_provider
        keys = secret_provider.get_key_list("GROQ_API_KEYS")
        if keys:
            return keys
        return self._parse_keys(self.groq_api_key)

    @property
    def compression_gemini_api_keys_list(self) -> list[str]:
        from aria.secrets import secret_provider
        keys = secret_provider.get_key_list("COMPRESSION_GEMINI_API_KEYS")
        if keys:
            return keys
        return self.gemini_api_keys_list

    @property
    def vision_gemini_api_keys_list(self) -> list[str]:
        """Vision uses its OWN env var first, falls back to main GEMINI_API_KEYS."""
        from aria.secrets import secret_provider
        keys = secret_provider.get_key_list("VISION_GEMINI_API_KEYS")
        if keys:
            return keys
        return self.gemini_api_keys_list

    @property
    def deepseek_api_key_resolved(self) -> str | None:
        """Resolved DeepSeek key — SecretProvider first, then pydantic field."""
        from aria.secrets import secret_provider
        key = secret_provider.get_key("DEEPSEEK_API_KEY")
        if key:
            return key
        return self.deepseek_api_key or None

    # --- Codex exec bridge (cheap-path integration, see app/agents/codex_exec_bridge.py) ---
    # Off by default: with this false, /tasks/{id}/start keeps using the
    # existing demo executor. Flip to true only once `codex` is installed and
    # OPENAI_API_KEY / `codex login` is set up locally.
    codex_enabled: bool = False
    codex_binary_path: str = "codex"
    # read-only | workspace-write | danger-full-access — see docs/sandbox.md
    # in the Codex repo. workspace-write is the sane default: Codex can edit
    # files under codex_workspace_dir but nothing outside it.
    codex_sandbox_mode: str = "workspace-write"
    codex_workspace_dir: str = "."
    # Only needed if you're not using `codex login` (ChatGPT account) locally.
    # Never commit a real value — set it in your local .env only.
    openai_api_key: str = ""
    # Multi-provider path: name of the [model_providers.<id>] block in
    # ~/.codex/config.toml that points at your local LiteLLM proxy (see
    # codex_config.toml.snippet + litellm-config.yaml). Leave empty to use
    # Codex's built-in OpenAI provider with openai_api_key above instead.
    codex_model_provider: str = ""
    # Default model alias (must match a model_name in litellm-config.yaml,
    # e.g. "qwen" | "deepseek" | "gemini" | "openrouter" | "groq" | "grok").
    # Can be overridden per-task via the `model` field on POST /tasks/{id}/start.
    codex_default_model: str = ""

    # --- Research notes bridge (unofficial API, cookie-based auth) ---
    # Off by default: requires a Google session cookies file.
    # This is an unofficial API that can break without warning.
    notebook_enabled: bool = False
    notebook_auth_path: str = ""
    notebook_timeout_sec: int = 30

    # --- Loop-Engineering v1.1 — Notifier (Telegram) ---
    # If both are set, executor.run_task instantiates TelegramNotifier.
    # If either is empty/None, notifier is skipped (soft degrade, no crash).
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Loop-Engineering v1.1 — Optimistic lock TTL ---
    # Max seconds a file-lock / optimistic lock is held before forced release.
    lock_ttl_seconds: int = 30




@lru_cache
def get_settings() -> Settings:
    # Installed app: make sure the persistent user dir (db, vault, skills,
    # logs) exists and bundled skills are copied before anything touches it.
    paths.ensure_user_data()
    return Settings()

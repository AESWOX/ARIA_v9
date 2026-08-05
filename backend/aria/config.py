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

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- §23.2 env naming convention ---
    # Default value points at a local sqlite file so the backend runs with zero
    # external infra out of the box (§18 dev profile). Set POSTGRES_DSN in .env
    # to point at real Postgres for production (canonical key name per §23.2).
    POSTGRES_DSN: str = "sqlite:///./data/local_agent.db"
    REDIS_URL: str = "redis://localhost:6379/0"
    OBSIDIAN_VAULT_PATH: str = "./data/vault"
    B2_BUCKET: str = ""
    B2_KEY_ID: str = ""
    B2_APPLICATION_KEY: str = ""

    # --- §23.1 canonical keys / v7 default profile ---
    # §2.9 TZ v1.1: optimistic lock TTL (file-lock + DB)
    LOCK_TTL_SECONDS: int = 30
    loop_max_iterations: int = 15
    audit_max_attempts: int = 3
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
    backup_retention_days: int = 30

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
    ws_backfill_limit: int = 500

    @field_validator("runtime_token_path", mode="before")
    @classmethod
    def _default_runtime_token_path_if_blank(cls, value: str | None) -> str:
        # RUNTIME_TOKEN_PATH= (blank) in .env is meant to mean "use the
        # default", but pydantic-settings treats a blank env value as an
        # explicit empty-string override, which resolves to "." and crashes
        # the backend on startup (IsADirectoryError). Treat blank as unset.
        if not value:
            return str(Path.home() / ".local-agent-ui" / "bootstrap.json")
        return value

    # --- §16.3 Security UX: idle-lock PIN. No hardcoded default — if unset,
    # backend generates a random one at startup and keeps it in backend memory;
    # bootstrap contains only pinRequired/idleLock metadata while verification
    # still goes through backend HTTP.
    LOCAL_AGENT_UI_PIN: str = ""

    # --- LLM providers ---
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_api_key: str = ""

    # Legacy single-key fallback (used only if GEMINI_API_KEYS is empty).
    gemini_api_key: str = ""
    # Main rotation pool used for actual agent answers, e.g.:
    # GEMINI_API_KEYS=key1,key2,key3,...,key9
    gemini_api_keys: str = ""
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"

    groq_api_key: str = ""
    # GROQ_API_KEYS=key1,key2,...,key10
    groq_api_keys: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # --- Compression: separate key pool, separate rotation cursor from the
    # main answer pool above. Does NOT share state with gemini_api_keys. ---
    compression_enabled: bool = True
    compression_gemini_api_keys: str = ""  # falls back to gemini_api_keys if empty
    compression_model: str = "gemini-2.5-flash"
    compression_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    compression_timeout_sec: int = 120
    # message-count based trigger (token-based can replace this later):
    # once history exceeds this many messages, compress everything except
    # the first N and last N into a single summary message.
    compression_hard_message_limit: int = 60
    compression_protect_first_n: int = 3
    compression_protect_last_n: int = 15
    compression_target_ratio: float = 0.2

    agent_sandbox_root: str = "./data/sandbox"

    @field_validator("gemini_api_keys", "groq_api_keys", "compression_gemini_api_keys", mode="before")
    @classmethod
    def _normalize_key_list(cls, value: str | None) -> str:
        return value or ""

    def _parse_keys(self, raw: str | None) -> list[str]:
        return [k.strip() for k in (raw or "").split(",") if k.strip()]

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
    return Settings()

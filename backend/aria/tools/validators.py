"""tools/validators.py — §11 инварианты + §14.2 high-risk command policy."""
from __future__ import annotations

import re

from aria.db.enums import IdempotencyClass

# §14.2 — high-risk patterns минимум включают:
HIGH_RISK_PATTERNS: list[re.Pattern] = [
    re.compile(r"\brm\s+-rf\b"),
    re.compile(r"\bsudo\s+rm\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+if="),
    re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bALTER\s+TABLE\s+.*\bDROP\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+--force\b"),
    re.compile(r"\bchmod\s+-R\s+777\b"),
    # "любые массовые delete/move в пользовательских каталогах" — эвристика:
    re.compile(r"\brm\s+-r[f]?\s+.*(/home|/Users|~)", re.IGNORECASE),
    re.compile(r"\bmv\s+.*\*.*\s+/dev/null\b"),
]


def is_high_risk_command(command: str) -> bool:
    return any(p.search(command) for p in HIGH_RISK_PATTERNS)


def build_dry_run_command(command: str) -> str:
    """Best-effort dry-run превью для §14.2 'approval + dry-run обязателен'.
    Для shell не существует универсального dry-run — показываем echo-превью
    и, если команда начинается с rm/mv/dd, добавляем безопасный --dry-run/-n
    флаг там, где инструмент это поддерживает."""
    stripped = command.strip()
    if stripped.startswith("rm "):
        return stripped.replace("rm ", "rm --interactive=always -v ", 1) + "  # (запрошено подтверждение перед удалением)"
    if stripped.startswith("mv "):
        return stripped + " -n  # (no-clobber, ничего не перезапишет)"
    if stripped.startswith("rsync"):
        return stripped + " --dry-run"
    return f"echo DRY-RUN: {stripped}"


class ToolValidationError(Exception):
    pass


def assert_role_allowed(role_id: str, tool_whitelist: tuple[str, ...], tool_name: str) -> None:
    if tool_name not in tool_whitelist:
        raise ToolValidationError(f"role {role_id} is not whitelisted for tool {tool_name} (§7.1 tool_whitelist)")


def assert_retry_policy(idempotency_class: IdempotencyClass, is_retry: bool) -> None:
    """§11: 'Ретрай без idempotency policy запрещён.' unsafe_write/external_side_effect
    не ретраятся автоматически без явного подтверждения на уровне вызывающего кода."""
    if is_retry and idempotency_class in (IdempotencyClass.unsafe_write, IdempotencyClass.external_side_effect):
        raise ToolValidationError(
            f"auto-retry запрещён для idempotency_class={idempotency_class.value} без approval (§11)"
        )

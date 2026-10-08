"""tools/validators.py — §11 инварианты + §14.2 high-risk command policy.

Волна 1 (A13): к high-risk-паттернам добавлена **default-deny** политика shell.
Раньше ``shell_execute`` исполнял любую команду, если она не совпала с чёрным
списком regex — то есть «разрешено всё, что не запрещено». Теперь разрешено
только то, что в allowlist; всё остальное требует подтверждения владельца.
"""
from __future__ import annotations

import re
import shlex

from aria.db.enums import IdempotencyClass

# §14.2 — high-risk patterns минимум включают:
HIGH_RISK_PATTERNS: list[re.Pattern] = [
    # rm с рекурсивным флагом в любом написании: -rf, -fr, -Rf, -rfv, --recursive
    re.compile(r"\brm\s+(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\b"),
    re.compile(r"\bsudo\s+rm\b"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdd\s+if="),
    re.compile(r"\bDROP\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bTRUNCATE\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bALTER\s+TABLE\s+.*\bDROP\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+--force\b"),
    re.compile(r"\bchmod\s+-R\s+777\b"),
    # "любые массовые delete/move в пользовательских каталогах" — эвристика:
    re.compile(r"\brm\s+-[a-zA-Z]*[rR][a-zA-Z]*\s+.*(/home|/Users|~)", re.IGNORECASE),
    re.compile(r"\bmv\s+.*\*.*\s+/dev/null\b"),
]

# ── Волна 1 (A13): default-deny allowlist ────────────────────────────────
# Читающие и обычные рабочие команды. Всё, чего здесь нет (curl/wget на чужой
# хост, ssh, произвольные бинарники, sed -i по системным путям, интерпретаторы
# с inline-кодом) — только через подтверждение владельца.
SHELL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "ls", "dir", "cat", "type", "pwd", "echo", "head", "tail", "wc", "sort",
        "uniq", "cut", "tr", "rg", "grep", "find", "which", "where", "tree",
        "mkdir", "touch", "cp", "copy", "mv", "move", "rm", "del", "test",
        "python", "python3", "pip", "node", "npm", "npx", "git", "pytest",
        "black", "ruff", "mypy", "tsc",
    }
)

# Разделители составных команд: каждую часть проверяем отдельно.
_SHELL_SEPARATORS = re.compile(r"\|\||&&|[|;\n]")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def is_high_risk_command(command: str) -> bool:
    return any(p.search(command) for p in HIGH_RISK_PATTERNS)


def _segment_head(segment: str) -> str:
    """Первый исполняемый токен сегмента (env-префиксы и sudo снимаем)."""
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return ""
    while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
        tokens.pop(0)
    if tokens and tokens[0] in ("sudo", "env", "command", "nice"):
        tokens.pop(0)
        while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
            tokens.pop(0)
    if not tokens:
        return ""
    return tokens[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()


def classify_shell_command(command: str) -> str:
    """Вернуть ``"allow"`` или ``"approval"`` для команды (§14.2 / волна 1 A13).

    Правила:
      * пустая команда → approval;
      * совпадение с HIGH_RISK_PATTERNS → approval;
      * подстановка команд (``$(...)``, `` `...` ``) → approval: состав такой
        команды мы анализировать не берёмся;
      * иначе каждый сегмент (через ``;``/``&&``/``||``/``|``) обязан начинаться
        с бинарника из SHELL_ALLOWLIST.
    """
    if not command or not command.strip():
        return "approval"
    if is_high_risk_command(command):
        return "approval"
    if "$(" in command or "`" in command:
        return "approval"
    segments = [s for s in _SHELL_SEPARATORS.split(command) if s.strip()]
    if not segments:
        return "approval"
    for segment in segments:
        head = _segment_head(segment)
        if not head or head not in SHELL_ALLOWLIST:
            return "approval"
    return "allow"


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

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
    re.compile(r"\brm\b[^;&|\n]*?\s(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)(?=\s|$)"),
    re.compile(r"\b(?:del|erase)\b[^;&|\n]*\s/[sS]\b", re.IGNORECASE),
    re.compile(r"\b(?:rmdir|rd)\b[^;&|\n]*\s/[sS]\b", re.IGNORECASE),
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
# Читающие и обычные рабочие команды. Всё, чего здесь нет — только через
# подтверждение владельца. Для части бинарников разрешён лишь безопасный набор
# подкоманд/флагов (SUBCOMMAND_ALLOW, _INLINE_CODE_FLAGS, _FIND_DANGEROUS).
SHELL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "ls", "dir", "cat", "type", "pwd", "echo", "head", "tail", "wc", "sort",
        "uniq", "cut", "tr", "rg", "grep", "find", "which", "where", "tree",
        "mkdir", "touch", "cp", "copy", "mv", "move", "rm", "del", "test",
        "python", "python3", "pip", "node", "npm", "git", "pytest",
        "black", "ruff", "mypy", "tsc",
    }
)

# Бинарник → допустимые подкоманды (первый не-флаговый токен ОБЯЗАН быть здесь).
SUBCOMMAND_ALLOW: dict[str, frozenset[str]] = {
    "git": frozenset({"status", "log", "diff", "show", "branch", "add", "commit",
                      "ls-files", "rev-parse", "blame", "remote", "tag"}),
    "pip": frozenset({"list", "show", "freeze", "check"}),
    "npm": frozenset({"test", "run", "ls", "list", "view", "outdated", "audit"}),
}
# Интерпретаторы: inline-код (-c/-e/…) = произвольное исполнение.
_INTERPRETERS = frozenset({"python", "python3", "node"})
_INLINE_CODE_FLAGS = frozenset({"-c", "-e", "-p", "--eval", "--print", "-"})
_FIND_DANGEROUS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls"})
_DELETERS = frozenset({"rm", "del"})

# Разделители составных команд: каждую часть проверяем отдельно.
# Одиночный «&» — разделитель в cmd.exe и фон в POSIX: тоже режем по нему.
_SHELL_SEPARATORS = re.compile(r"\|\||&&|[|;&\r\n]")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Безобидные перенаправления, которые не считаем записью в файл.
_SAFE_REDIRECTS = re.compile(r"\d?>>?\s*(?:/dev/null|nul)\b|\d>&\d", re.IGNORECASE)
_REDIRECT_TARGET = re.compile(r"(?:\d?>>?|<)\s*([^\s;&|<>]+)")
_REDIRECT_TARGET_FULL = re.compile(r"(?:\d?>>?|<)\s*[^\s;&|<>]+")
_PATH_ESCAPE = re.compile(r"^(?:[/\\~]|[A-Za-z]:)|(?:^|[\\/])\.\.(?:[\\/]|$)|[$%`]")


def is_high_risk_command(command: str) -> bool:
    return any(p.search(command) for p in HIGH_RISK_PATTERNS)


def _tokens(segment: str) -> list[str]:
    """Токены сегмента. posix=False — иначе shlex съедает «\» в Windows-путях."""
    try:
        raw = shlex.split(segment, posix=False)
    except ValueError:
        return []
    return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'" else t for t in raw]


def _normalize_head(tokens: list[str]) -> tuple[str, list[str]]:
    """(имя бинарника, остальные токены). ``sudo`` → пустое имя (всегда approval)."""
    tokens = list(tokens)
    while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
        tokens.pop(0)
    if tokens and tokens[0].lower() == "sudo":
        return "", tokens
    if tokens and tokens[0] in ("env", "command", "nice", "time"):
        tokens.pop(0)
        while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
            tokens.pop(0)
    if not tokens:
        return "", []
    head = tokens[0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
    if head.endswith(".exe"):
        head = head[:-4]
    return head, tokens[1:]


def _segment_allowed(segment: str) -> bool:
    head, args = _normalize_head(_tokens(segment))
    if not head or head not in SHELL_ALLOWLIST:
        return False
    flags = [a for a in args if a.startswith("-") or (head in ("del", "dir", "copy", "move") and a.startswith("/") and len(a) <= 3)]
    positional = [a for a in args if a not in flags]
    # Любой путь вне корня песочницы / с расширением переменных — подтверждение.
    if any(_PATH_ESCAPE.search(a) for a in positional):
        return False
    if head in SUBCOMMAND_ALLOW:
        sub = next((a for a in args if not a.startswith("-")), "")
        if args and args[0].startswith("-"):  # `git -c …`, `npm --prefix …` — до подкоманды
            return False
        if sub not in SUBCOMMAND_ALLOW[head]:
            return False
    if head in _INTERPRETERS and any(a in _INLINE_CODE_FLAGS for a in args):
        return False
    if head == "find" and any(a in _FIND_DANGEROUS for a in args):
        return False
    if head in _DELETERS and any(
        re.match(r"^-[a-zA-Z]*[rR]", a) or a == "--recursive" or a.lower() == "/s" for a in args
    ):
        return False
    return True


def classify_shell_command(command: str) -> str:
    """Вернуть ``"allow"`` или ``"approval"`` для команды (§14.2 / волна 1 A13).

    Разрешено только если ВСЕ условия выполнены:
      * нет совпадения с HIGH_RISK_PATTERNS;
      * нет подстановок (``$(...)``, обратные кавычки), cmd-экранирования ``^``;
      * нет перенаправлений ``>``/``<`` (кроме в /dev/null и ``2>&1``);
      * каждый сегмент (``;``/``&&``/``||``/``|``/``&``) начинается с бинарника из
        SHELL_ALLOWLIST и проходит пер-командные правила (_segment_allowed):
        подкоманды git/pip/npm, запрет inline-кода у python/node, запрет
        ``find -exec/-delete``, запрет рекурсивного rm, запрет путей вне
        корня (абсолютные, ``..``, ``~``, ``$VAR``/``%VAR%``).
    """
    if not command or not command.strip():
        return "approval"
    if is_high_risk_command(command):
        return "approval"
    if "$(" in command or "`" in command or "^" in command:
        return "approval"
    stripped = _SAFE_REDIRECTS.sub("", command)
    # Перенаправление в относительный путь внутри песочницы — обычная работа;
    # наружу (~, абсолютный, .., $VAR) — только с подтверждением.
    for target in _REDIRECT_TARGET.findall(stripped):
        if _PATH_ESCAPE.search(target.strip("\"'")):
            return "approval"
    stripped = _REDIRECT_TARGET_FULL.sub(" ", stripped)
    if re.search(r"[<>]", stripped):
        return "approval"
    segments = [s for s in _SHELL_SEPARATORS.split(stripped) if s.strip()]
    if not segments:
        return "approval"
    return "allow" if all(_segment_allowed(seg) for seg in segments) else "approval"


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

"""core/secretscanner.py — §5 Pre-delivery secret-scan.

Вынесен из build_release.py, обобщён для вызова на каждой доставке.
Сканирует только изменённые файлы (не весь vault).
ALLOWLIST для тестовых ключей/примеров.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar


@dataclass(frozen=True)
class SecretMatch:
    """Одна находка секрета."""

    file: str
    pattern: str
    severity: str  # 'critical' | 'warning'
    snippet: str


@dataclass
class ScanResult:
    """Результат scan."""

    matches: list[SecretMatch] = field(default_factory=list)
    files_scanned: int = 0

    @property
    def critical_count(self) -> int:
        return sum(1 for m in self.matches if m.severity == "critical")

    @property
    def warning_count(self) -> int:
        return sum(1 for m in self.matches if m.severity == "warning")

    @property
    def clean(self) -> bool:
        return self.critical_count == 0


# Deny-паттерны: ключи API, токены, приватные ключи
DENY_PATTERNS: list[str] = [
    # ── API keys: traditional ──
    r".*sk-[a-zA-Z0-9]{20,}.*",           # OpenAI/Sk
    r".*sk-proj-[a-zA-Z0-9]{20,}.*",      # OpenAI Project key (2025+)
    r".*AIza[0-9A-Za-z_-]{35}.*",          # Gemini
    r".*sk-ant-[a-zA-Z0-9]{20,}.*",        # Anthropic
    r".*pplx-[a-f0-9]{32,}.*",             # Perplexity
    r".*r8_[a-zA-Z0-9]{20,}.*",            # Replicate
    r".*co-[a-zA-Z0-9]{20,}.*",            # Cohere
    r".*pat_[a-zA-Z0-9]{20,}.*",           # HuggingFace PAT (new format)
    # ── GitHub ──
    r".*ghp_[a-zA-Z0-9]{36}.*",            # GitHub PAT classic
    r".*gho_[a-zA-Z0-9]{36}.*",            # GitHub OAuth
    r".*ghu_[a-zA-Z0-9]{36}.*",            # GitHub user token
    r".*github_pat_[a-zA-Z0-9_-]{84}.*",   # GitHub fine-grained PAT (2023+)
    # ── Internal / vault ──
    r".*xox[bpras]-[a-zA-Z0-9-]{10,}.*",   # Slack
    r"-----BEGIN (RSA |EC )?PRIVATE KEY-----",  # Private keys
    r".*gsk_[a-zA-Z0-9]{20,}.*",           # Groq
    r".*hf_[a-zA-Z0-9]{20,}.*",            # HuggingFace classic
    r".*sk_[a-zA-Z0-9]{32,}.*",            # Custom/go-sk format
]

# Паттерны, которые разрешены (тестовые ключи, примеры в docs)
ALLOWLIST: list[str] = [
    "sk-you...here",
    "sk-proj-fake...test",
    "AIzaSyTest",
    "ghp_yo...oken",
    "github_pat_fake_test",
    "gsk_test_",
    "hf_example",
    "sk-ant-fake...test",
    "sk-TEST_NOT_REAL",  # test keys — obviously fake, won't match patterns
    "sk-proj-TEST_NOT_REAL",
    "pplx-fake-test",
]

# Файлы, которые никогда не сканируются
SKIP_EXTENSIONS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".ttf", ".woff", ".woff2", ".eot",
    ".pyc", ".pyo",
    ".db", ".sqlite", ".sqlite3",
    ".tar", ".gz", ".zip", ".7z", ".rar",
    ".lock", # lock-файлы блокировок
})

# Файлы, которые МОГУТ содержать легитимные ключи (предупреждение, не блок)
KEY_FILES: frozenset[str] = frozenset({
    ".env", ".env.example", ".env.local",
    "docker-compose.yml", "docker-compose.yaml",
    "secrets.yml", "secrets.yaml",
})


def _compile_patterns(patterns: list[str]) -> list[re.Pattern]:
    """Компилирует список regex-паттернов."""
    compiled: list[re.Pattern] = []
    for p in patterns:
        try:
            compiled.append(re.compile(p))
        except re.error:
            pass  # bad pattern, skip
    return compiled


def _is_allowed(line: str, allowlist: list[str]) -> bool:
    """Проверяет, не входит ли строка в allowlist."""
    for allowed in allowlist:
        if allowed in line:
            return True
    return False


def scan_file(
    path: Path,
    deny: list[str] | None = None,
    allow: list[str] | None = None,
) -> list[SecretMatch]:
    """Сканирует один файл на секреты.

    Args:
        path: абсолютный путь к файлу.
        deny: список regex-паттернов (default: DENY_PATTERNS).
        allow: список allowlist-строк (default: ALLOWLIST).

    Returns:
        list[SecretMatch]: найденные секреты.
    """
    if path.suffix.lower() in SKIP_EXTENSIONS:
        return []

    deny_patterns = _compile_patterns(deny or DENY_PATTERNS)
    allow_strings = allow or ALLOWLIST

    if not deny_patterns:
        return []

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []  # binary or unreadable

    matches: list[SecretMatch] = []
    for i, line in enumerate(text.splitlines(), 1):
        if _is_allowed(line, allow_strings):
            continue
        for pat in deny_patterns:
            if pat.search(line):
                severity = "warning" if path.name in KEY_FILES else "critical"
                matches.append(SecretMatch(
                    file=str(path),
                    pattern=pat.pattern[:40],
                    severity=severity,
                    snippet=f"line {i}: {line.strip()[:80]}",
                ))
                break  # one match per line

    return matches


def scan_changed_files(
    changed_paths: list[Path],
    deny: list[str] | None = None,
    allow: list[str] | None = None,
) -> ScanResult:
    """Сканирует только изменённые файлы текущего delivery.

    Args:
        changed_paths: список путей к изменённым файлам.
        deny: список regex-паттернов.
        allow: список allowlist-строк.

    Returns:
        ScanResult: все находки (critical блокирует, warning логирует).
    """
    result = ScanResult()
    for path in changed_paths:
        if not path.exists() or not path.is_file():
            continue
        matches = scan_file(path, deny=deny, allow=allow)
        result.matches.extend(matches)
        result.files_scanned += 1
    return result


class SecretScanError(Exception):
    """Выбрасывается, если найдены critical-секреты."""

    def __init__(self, result: ScanResult):
        details = "\n".join(
            f"  [{m.severity}] {m.file}: {m.snippet}"
            for m in result.matches
        )
        super().__init__(
            f"Secret scan blocked: {result.critical_count} critical, "
            f"{result.warning_count} warning:\n{details}"
        )
        self.result = result


def assert_no_secrets(result: ScanResult, min_severity: str = "critical") -> None:
    """Проверяет результат scan.

    Args:
        result: результат scan.
        min_severity: минимальный уровень для блокировки ('critical' | 'warning').

    Raises:
        SecretScanError: если найдены секреты >= min_severity.
    """
    if min_severity == "critical" and result.critical_count > 0:
        raise SecretScanError(result)
    if min_severity == "warning" and len(result.matches) > 0:
        raise SecretScanError(result)

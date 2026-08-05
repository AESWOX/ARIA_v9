"""
core/guardrails.py — §11 Tool-call loop guardrails.

Port of Hermes' agent/tool_guardrails.py for ARIA.

Per-turn controller for detecting repeated failures, same-tool loops,
and no-progress idempotent calls. Returns decisions (allow/warn/block)
that the loop can act on.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

# Tools that never mutate state — repeating them is wasteful
IDEMPOTENT_TOOL_NAMES = frozenset({
    "read_file", "search_files", "read_note", "search_vault",
    "list_vault", "web_search", "web_extract",
})

# Tools that DO mutate state — repeating them is dangerous
MUTATING_TOOL_NAMES = frozenset({
    "terminal", "write_file", "patch", "file_write", "shell_execute",
    "write_note", "memory", "skill_manage", "cronjob", "delegate_task",
})


@dataclass(frozen=True)
class ToolCallSignature:
    """Unique signature: (tool_name, hashed_args) for exact-failure detection."""
    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Mapping[str, Any] | None) -> ToolCallSignature:
        raw = json.dumps(args or {}, sort_keys=True, default=str)
        h = hashlib.sha256(raw.encode()).hexdigest()[:16]
        return cls(tool_name=tool_name, args_hash=h)


@dataclass(frozen=True)
class ToolGuardrailDecision:
    """Result of a guardrail check.

    action: "allow" | "warn" | "block"
    code: machine-readable reason code
    message: human-readable message
    """
    tool_name: str
    signature: ToolCallSignature
    action: str = "allow"
    code: str = ""
    message: str = ""
    count: int = 0


@dataclass(frozen=True)
class GuardrailConfig:
    """Thresholds for per-turn tool-call loop detection."""
    warnings_enabled: bool = True
    hard_stop_enabled: bool = False
    exact_failure_warn_after: int = 2
    exact_failure_block_after: int = 5
    same_tool_failure_warn_after: int = 3
    same_tool_failure_halt_after: int = 8
    no_progress_warn_after: int = 2
    no_progress_block_after: int = 5
    idempotent_tools: frozenset[str] = field(default_factory=lambda: IDEMPOTENT_TOOL_NAMES)
    mutating_tools: frozenset[str] = field(default_factory=lambda: MUTATING_TOOL_NAMES)


def _detect_tool_failure(tool_name: str, result: dict | str | None) -> tuple[bool, str]:
    """Detect if a tool call failed.

    Returns (is_failure, reason_suffix).
    """
    if result is None:
        return True, ""

    if isinstance(result, dict):
        if result.get("error"):
            return True, f" [error]"
        exit_code = result.get("exit_code")
        if exit_code is not None and exit_code != 0:
            return True, f" [exit {exit_code}]"
        if result.get("success") is False:
            return True, " [failed]"

    result_str = str(result)[:500].lower()
    if '"error"' in result_str or '"failed"' in result_str:
        return True, " [error]"

    return False, ""


@dataclass
class ToolCallGuardrailController:
    """Per-turn controller for repeated failed/non-progressing tool calls."""

    config: GuardrailConfig = field(default_factory=GuardrailConfig)
    _exact_failure_counts: dict = field(default_factory=dict)
    _same_tool_failure_counts: dict = field(default_factory=dict)
    _no_progress: dict = field(default_factory=dict)
    _halt_decision: ToolGuardrailDecision | None = None

    def reset_for_turn(self) -> None:
        self._exact_failure_counts.clear()
        self._same_tool_failure_counts.clear()
        self._no_progress.clear()
        self._halt_decision = None

    @property
    def halt_decision(self) -> ToolGuardrailDecision | None:
        return self._halt_decision

    def before_call(self, tool_name: str, args: Mapping[str, Any] | None) -> ToolGuardrailDecision:
        """Check BEFORE a tool call — warn always, block only if hard_stop enabled."""
        signature = ToolCallSignature.from_call(tool_name, args or {})
        exact_count = self._exact_failure_counts.get(signature, 0)
        same_count = self._same_tool_failure_counts.get(tool_name, 0)

        # Block mode — only when hard_stop enabled
        if self.config.hard_stop_enabled:
            # Exact failure check (same tool + same args)
            if exact_count >= self.config.exact_failure_block_after:
                decision = ToolGuardrailDecision(
                    action="block",
                    code="repeated_exact_failure_block",
                    message=(
                        f"Blocked {tool_name}: the same tool call failed {exact_count} "
                        "times with identical arguments. Stop retrying it unchanged."
                    ),
                    tool_name=tool_name,
                    signature=signature,
                    count=exact_count,
                )
                self._halt_decision = decision
                return decision

            # No-progress check (idempotent tools returning same result hash)
            if tool_name in self.config.idempotent_tools:
                record = self._no_progress.get(signature)
                if record is not None:
                    _result_hash, repeat_count = record
                    if repeat_count >= self.config.no_progress_block_after:
                        decision = ToolGuardrailDecision(
                            action="block",
                            code="idempotent_no_progress_block",
                            message=(
                                f"Blocked {tool_name}: this read-only call returned the same "
                                f"result {repeat_count} times. Stop repeating it unchanged."
                            ),
                            tool_name=tool_name,
                            signature=signature,
                            count=repeat_count,
                        )
                        self._halt_decision = decision
                        return decision

        # Warning mode — warn when past warn threshold but not blocked
        warnings = []
        if exact_count >= self.config.exact_failure_warn_after:
            warnings.append(f"same call failed {exact_count}x")
        if same_count >= self.config.same_tool_failure_warn_after:
            warnings.append(f"{tool_name} failed {same_count}x")

        if warnings and self.config.warnings_enabled:
            return ToolGuardrailDecision(
                action="warn",
                code="repeated_failure_warning",
                message=f"{tool_name}: {'; '.join(warnings)}. Change strategy.",
                tool_name=tool_name,
                signature=signature,
                count=max(exact_count, same_count),
            )

        return ToolGuardrailDecision(tool_name=tool_name, signature=signature, action="allow")

    def after_call(
        self, tool_name: str, args: Mapping[str, Any] | None, result: dict | str | None
    ) -> ToolGuardrailDecision:
        """Record a tool result AFTER execution — updates failure/no-progress counters."""
        is_failure, reason = _detect_tool_failure(tool_name, result)
        signature = ToolCallSignature.from_call(tool_name, args or {})

        if is_failure:
            self._exact_failure_counts[signature] = self._exact_failure_counts.get(signature, 0) + 1
            self._same_tool_failure_counts[tool_name] = self._same_tool_failure_counts.get(tool_name, 0) + 1
        else:
            # Reset same-tool counter on success
            self._same_tool_failure_counts.pop(tool_name, None)

            # Track no-progress for idempotent tools
            if tool_name in self.config.idempotent_tools:
                result_hash = hashlib.sha256(str(result).encode()).hexdigest()[:16]
                record = self._no_progress.get(signature)
                if record is not None and record[0] == result_hash:
                    # Same result again
                    self._no_progress[signature] = (result_hash, record[1] + 1)
                else:
                    # New result — reset counter
                    self._no_progress[signature] = (result_hash, 1)
            else:
                self._no_progress.pop(signature, None)

        # Check for halt after recording
        decision = self.before_call(tool_name, args)
        return decision

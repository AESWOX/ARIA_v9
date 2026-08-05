"""Tests for aria.core.guardrails — tool-call loop guardrails."""

import pytest
from aria.core.guardrails import (
    ToolCallGuardrailController,
    ToolCallSignature,
    _detect_tool_failure,
    GuardrailConfig,
)


class TestToolCallSignature:
    def test_identical_calls_same_signature(self):
        s1 = ToolCallSignature.from_call("read_file", {"path": "/tmp/test.txt"})
        s2 = ToolCallSignature.from_call("read_file", {"path": "/tmp/test.txt"})
        assert s1 == s2

    def test_different_calls_different_signature(self):
        s1 = ToolCallSignature.from_call("read_file", {"path": "/tmp/a.txt"})
        s2 = ToolCallSignature.from_call("read_file", {"path": "/tmp/b.txt"})
        assert s1 != s2

    def test_different_tools_different_signature(self):
        s1 = ToolCallSignature.from_call("read_file", {"path": "/tmp/test.txt"})
        s2 = ToolCallSignature.from_call("write_file", {"path": "/tmp/test.txt"})
        assert s1 != s2


class TestDetectToolFailure:
    def test_none_is_failure(self):
        assert _detect_tool_failure("read_file", None) == (True, "")

    def test_dict_with_error_is_failure(self):
        assert _detect_tool_failure("read_file", {"error": "not found"}) == (True, " [error]")

    def test_exit_code_nonzero_is_failure(self):
        assert _detect_tool_failure("terminal", {"exit_code": 1, "output": ""}) == (True, " [exit 1]")

    def test_success_is_not_failure(self):
        assert _detect_tool_failure("read_file", {"output": "content"}) == (False, "")

    def test_dict_with_success_false_is_failure(self):
        assert _detect_tool_failure("memory", {"success": False, "error": "limit exceeded"}) == (True, " [error]")


class TestToolCallGuardrailController:
    def test_initial_state_allows(self):
        controller = ToolCallGuardrailController()
        decision = controller.before_call("read_file", {"path": "/tmp/a.txt"})
        assert decision.action == "allow"

    def test_same_tool_different_args_does_not_trigger_exact_failure(self):
        config = GuardrailConfig(hard_stop_enabled=True, exact_failure_block_after=2)
        controller = ToolCallGuardrailController(config=config)

        # Same tool, different args — should NOT trigger exact failure
        controller.after_call("read_file", {"path": "/tmp/a.txt"}, {"output": "content"})
        controller.after_call("read_file", {"path": "/tmp/b.txt"}, {"output": "data"})
        decision = controller.before_call("read_file", {"path": "/tmp/a.txt"})
        assert decision.action == "allow"

    def test_repeated_exact_failure_blocks(self):
        config = GuardrailConfig(hard_stop_enabled=True, exact_failure_block_after=2)
        controller = ToolCallGuardrailController(config=config)

        controller.after_call("terminal", {"command": "rm -rf /"}, {"error": "permission denied"})
        controller.after_call("terminal", {"command": "rm -rf /"}, {"error": "permission denied"})
        decision = controller.before_call("terminal", {"command": "rm -rf /"})
        assert decision.action == "block"
        assert "repeated_exact_failure_block" in decision.code

    def test_same_tool_warning_after_threshold(self):
        config = GuardrailConfig(same_tool_failure_warn_after=2)
        controller = ToolCallGuardrailController(config=config)

        controller.after_call("terminal", {"command": "ls /nonexistent"}, {"error": "not found"})
        controller.after_call("terminal", {"command": "cat /nonexistent"}, {"error": "not found"})
        controller.after_call("terminal", {"command": "cd /nonexistent"}, {"error": "not found"})
        decision = controller.before_call("terminal", {"command": "rm /nonexistent"})
        assert decision.action == "warn"

    def test_idempotent_no_progress_block(self):
        config = GuardrailConfig(
            hard_stop_enabled=True,
            no_progress_block_after=3,
            idempotent_tools=frozenset({"read_file"}),
        )
        controller = ToolCallGuardrailController(config=config)

        controller.after_call("read_file", {"path": "/tmp/a.txt"}, {"output": "same content"})
        controller.after_call("read_file", {"path": "/tmp/a.txt"}, {"output": "same content"})
        controller.after_call("read_file", {"path": "/tmp/a.txt"}, {"output": "same content"})
        decision = controller.before_call("read_file", {"path": "/tmp/a.txt"})
        assert decision.action == "block"
        assert "idempotent_no_progress_block" in decision.code

    def test_new_result_resets_no_progress_counter(self):
        controller = ToolCallGuardrailController()

        controller.after_call("read_file", {"path": "/tmp/a.txt"}, {"output": "content v1"})
        controller.after_call("read_file", {"path": "/tmp/a.txt"}, {"output": "content v2"})
        controller.after_call("read_file", {"path": "/tmp/a.txt"}, {"output": "content v3"})
        decision = controller.before_call("read_file", {"path": "/tmp/a.txt"})
        # New content each time — no progress counter keeps resetting
        assert decision.action == "allow"

    def test_success_resets_same_tool_failure_counter(self):
        """same_tool_failure_counts resets on success, but exact_failure_counts persist."""
        config = GuardrailConfig(exact_failure_warn_after=5)  # Don't let exact_failure trigger warn
        controller = ToolCallGuardrailController(config=config)

        controller.after_call("terminal", {"command": "rm -rf /"}, {"error": "permission denied"})
        controller.after_call("terminal", {"command": "rm -rf /"}, {"error": "permission denied"})
        # Verify same_tool counter increased
        assert controller._same_tool_failure_counts.get("terminal", 0) == 2

        controller.after_call("terminal", {"command": "rm -rf /"}, {"output": "success"})
        # same_tool counter was reset
        assert "terminal" not in controller._same_tool_failure_counts
        decision = controller.before_call("terminal", {"command": "rm -rf /"})
        # exact_failure_count still >0 but warn threshold is high, so allow
        assert decision.action == "allow"

    def test_reset_for_turn_clears_state(self):
        config = GuardrailConfig(hard_stop_enabled=True, exact_failure_block_after=2)
        controller = ToolCallGuardrailController(config=config)

        controller.after_call("terminal", {"command": "rm -rf /"}, {"error": "permission denied"})
        controller.after_call("terminal", {"command": "rm -rf /"}, {"error": "permission denied"})
        controller.reset_for_turn()
        decision = controller.before_call("terminal", {"command": "rm -rf /"})
        assert decision.action == "allow"

    def test_hard_stop_disabled_never_blocks(self):
        """When hard_stop disabled, controller warns but never blocks."""
        config = GuardrailConfig(hard_stop_enabled=False, exact_failure_block_after=2)
        controller = ToolCallGuardrailController(config=config)

        controller.after_call("terminal", {"command": "rm -rf /"}, {"error": "permission denied"})
        controller.after_call("terminal", {"command": "rm -rf /"}, {"error": "permission denied"})
        controller.after_call("terminal", {"command": "rm -rf /"}, {"error": "permission denied"})
        decision = controller.before_call("terminal", {"command": "rm -rf /"})
        # Warns because past warn threshold — never blocks because hard_stop disabled
        assert decision.action == "warn"
        assert controller.halt_decision is None  # No block decision set

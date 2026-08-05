from __future__ import annotations

from aria.db.enums import TASK_TRANSITIONS, TaskStatus


class InvalidTransition(Exception):
    pass


def assert_transition_allowed(current: TaskStatus, target: TaskStatus) -> None:
    if current == target:
        return
    allowed = TASK_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransition(f"task_status {current.value} -> {target.value} is not allowed by §8.1")

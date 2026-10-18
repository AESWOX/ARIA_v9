"""PR-4 tests, clean version (no __import__ hacks)."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from aria.core import compiler, plan_validator as pv
from aria.core.executor import _set_phase
from aria.core.roles import ROLE_REGISTRY, get_role
from aria.core.taskspec import TaskSpec, default_criterion
from aria.db import repository as repo
from aria.db.base import get_engine, session_scope
from aria.db.enums import PLAN_PHASES, PlanStatus, TaskStatus
from aria.db.models import TaskPlan


def _step(sid, *, deps=None, covers=None, scope=None, tool="file_write"):
    return {
        "step_id": sid,
        "objective": f"шаг {sid}",
        "role": "coder",
        "tool_ref": tool,
        "depends_on": list(deps or []),
        "covers": list(covers or []),
        "write_scope": list(scope or []),
    }


# ─────────────────────────── PlanStep / DAG ───────────────────────────

def test_planstep_carries_dag_fields():
    step = pv.validate_plan([_step("step-1", covers=["ac-1"])])[0]
    assert step.depends_on == [] and step.covers == ["ac-1"] and step.write_scope == []


def test_depends_on_is_normalised_to_the_same_id_space_as_step_id():
    steps = pv.validate_plan([
        _step("step-1", covers=["ac-1"]),
        _step("step-2", deps=["step-1"], covers=["ac-1"]),
    ])
    assert steps[1].depends_on == [str(steps[0].step_id)]


def test_cycle_is_rejected():
    raw = [
        _step("step-1", deps=["step-2"], covers=["ac-1"]),
        _step("step-2", deps=["step-1"], covers=["ac-1"]),
    ]
    with pytest.raises(ValueError, match="цикл"):
        pv.validate_plan(raw)


def test_step_without_covers_is_rejected_when_criteria_are_known():
    with pytest.raises(ValueError, match="covers"):
        pv.validate_plan([_step("step-1")], criteria_ids=["ac-1"])


def test_uncovered_criterion_is_rejected():
    with pytest.raises(ValueError, match="не покрыты"):
        pv.validate_plan([_step("step-1", covers=["ac-1"])], criteria_ids=["ac-1", "ac-2"])


def test_valid_dag_with_two_criteria_passes():
    raw = [
        _step("step-1", covers=["ac-1"], tool="file_read"),
        _step("step-2", deps=["step-1"], covers=["ac-2"], scope=["generated/report.md"]),
    ]
    steps = pv.validate_plan(raw, criteria_ids=["ac-1", "ac-2"])
    assert [len(s.covers) for s in steps] == [1, 1]


def test_unknown_criterion_in_covers_is_rejected():
    with pytest.raises(ValueError, match="неизвестные критерии"):
        pv.validate_plan([_step("step-1", covers=["ac-9"])], criteria_ids=["ac-1"])


def test_too_many_steps_is_rejected():
    raw = [_step(f"step-{i}", covers=["ac-1"], tool="file_read") for i in range(pv.MAX_PLAN_STEPS + 1)]
    with pytest.raises(ValueError, match="MAX_PLAN_STEPS"):
        pv.validate_plan(raw, criteria_ids=["ac-1"])


def test_plans_without_criteria_still_validate_as_before():
    assert len(pv.validate_plan([_step("step-1")])) == 1


# ─────────────────────────── waves ───────────────────────────

def test_topological_waves_group_independent_steps():
    """П.1 ТЗ: параллель — только при явном `depends_on: []` и непересекающемся write_scope."""
    steps = pv.validate_plan([
        _step("step-1", deps=[], covers=["ac-1"], scope=["generated/a.txt"]),
        _step("step-2", deps=[], covers=["ac-1"], scope=["generated/b.txt"]),
        _step("step-3", deps=["step-1", "step-2"], covers=["ac-1"], scope=["generated/c.txt"], tool="file_read"),
    ])
    raw = [s.model_dump(mode="json") for s in steps]
    assert pv.topological_waves(raw) == [[0, 1], [2]]


def test_topological_waves_does_not_hang_on_a_cycle():
    raw = [
        _step("step-1", deps=["step-2"], covers=["ac-1"]),
        _step("step-2", deps=["step-1"], covers=["ac-1"]),
    ]
    normalised = pv._normalize_step_ids(raw)
    assert sorted(sum(pv.topological_waves(normalised), [])) == [0, 1]


# ─────────────────────────── COMPILE ───────────────────────────

def test_compile_task_input_carries_goal_criteria_and_constraints():
    spec = TaskSpec(
        goal="Собрать отчёт по продажам",
        constraints=["только stdlib"],
        acceptance_criteria=[default_criterion("отчёт")],
        assumptions=["квартал — текущий"],
    )
    text = compiler.compile_task_input(spec, {"matches": [{"title": "ARIA"}]})
    assert "Собрать отчёт по продажам" in text
    assert "ac-default" in text
    assert "только stdlib" in text and "квартал — текущий" in text
    assert "covers[]" in text


def test_compile_task_input_without_spec_is_empty():
    assert compiler.compile_task_input(None) == ""


def test_compiler_and_clarifier_roles_are_registered():
    assert "compiler" in ROLE_REGISTRY and "clarifier" in ROLE_REGISTRY
    assert get_role("compiler").system_prompt_asset == "prompts/compiler.md"
    assert get_role("clarifier").system_prompt_asset == "prompts/clarifier.md"


def test_role_prompt_assets_exist():
    base = Path(__file__).parent.parent / "aria/prompts"
    for role_id in ("clarifier", "compiler"):
        assert (base / f"{role_id}.md").exists(), f"prompts/{role_id}.md отсутствует"


# ─────────────────────────── phase ───────────────────────────

def test_plan_phase_is_persisted_and_validated():
    assert "compile" in PLAN_PHASES and "report" in PLAN_PHASES
    with session_scope() as db:
        s = repo.create_session(db, "pr4-phase")
        t = repo.create_task(db, s, role="general", objective="проверка фазы", draft_tz_md="# tz\n")
        repo.set_task_status(db, t, TaskStatus.approved)
        db.add(TaskPlan(task_id=t.id, plan_json=[], status=PlanStatus.in_progress.value))
        db.flush()

        _set_phase(db, t, "execute")
        plan = db.execute(select(TaskPlan).where(TaskPlan.task_id == t.id)).scalars().first()
        assert plan.phase == "execute"
        with pytest.raises(ValueError):
            _set_phase(db, t, "not-a-phase")


def test_task_plans_has_phase_column():
    cols = {c["name"] for c in inspect(get_engine()).get_columns("task_plans")}
    assert "phase" in cols

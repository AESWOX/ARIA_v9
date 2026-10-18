"""core/compiler.py — PR-4: фаза COMPILE.

Компилятор превращает TaskSpec (+ контекст vault) в план-вход для Oracle. Он не
генерирует PlanStep — это делает Oracle, — но фиксирует контракт, из которого план
обязан вывести covers[], и запрещает планировщику «забыть» критерии приёмки.
"""
from __future__ import annotations

from aria.core.taskspec import TaskSpec


def compile_task_input(spec: TaskSpec | None, vault_context: dict | None = None) -> str:
    """Собирает user-промпт Oracle из TaskSpec. Детерминированно, без LLM.

    Args:
        spec: контракт задачи (None — компиляция невозможна, вызывающий обязан передать goal).
        vault_context: результат Stage 1 (search_vault) — до 3 совпадений, чтобы не раздувать промпт.

    Returns:
        Текст с целью, ограничениями, критериями (их id обязаны попасть в covers[])
        и уже известными допущениями.
    """
    if spec is None:
        return ""

    lines = [f"Objective: {spec.goal.strip()}"]
    if spec.constraints:
        lines.append("Constraints:")
        lines.extend(f"- {c}" for c in spec.constraints)
    if spec.acceptance_criteria:
        lines.append("Acceptance criteria (каждый обязан быть покрыт covers[] хотя бы одного шага):")
        lines.extend(f"- {c.id}: {c.statement}" for c in spec.acceptance_criteria)
    if spec.assumptions:
        lines.append("Assumptions already accepted:")
        lines.extend(f"- {a}" for a in spec.assumptions)

    matches = (vault_context or {}).get("matches") or []
    if matches:
        lines.append("Vault context (может быть нерелевантным):")
        for m in matches[:3]:
            title = m.get("title") or m.get("path") or "заметка"
            lines.append(f"- {title}")

    lines.append(
        "Верни PlanStep[]: у каждого шага depends_on[] (id шагов-предшественников), "
        "covers[] (id критериев выше) и write_scope[] (пути, которые шаг пишет)."
    )
    return "\n".join(lines)

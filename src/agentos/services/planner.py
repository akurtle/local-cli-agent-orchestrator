"""Validates a manager plan and turns it into persisted tasks.

The split matters: `validate_plan` is a pure function over a plan plus the known
agent roster, so every rejection rule is testable without a database. `apply` is
the only thing that writes, and it only ever writes a plan that already passed.

The manager cannot: name an agent that does not exist, reference a task it did
not define, create a cycle, submit an empty task, or reuse a temp id.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from agentos.config import Config
from agentos.db.session import Database
from agentos.repositories.agents import AgentRepository
from agentos.repositories.objectives import ObjectiveRepository
from agentos.schemas.dto import ObjectiveView, TaskView
from agentos.schemas.enums import ObjectiveStatus
from agentos.schemas.plan import MAX_PLANNED_TASKS, ManagerPlan, PlannedTask
from agentos.services import dag
from agentos.services.tasks import TaskService


@dataclass
class PlanValidation:
    """The result of checking a plan. `ok` means it is safe to apply."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    """temp_ids in a valid creation order (dependencies first)."""

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_plan(plan: ManagerPlan, known_agents: set[str]) -> PlanValidation:
    """Check a plan against the real roster. Pure: no database, no side effects."""
    result = PlanValidation()

    if not plan.tasks:
        result.errors.append("the plan contains no tasks")
        return result

    if len(plan.tasks) > MAX_PLANNED_TASKS:
        result.errors.append(
            f"the plan has {len(plan.tasks)} tasks, more than the "
            f"{MAX_PLANNED_TASKS} allowed"
        )
        return result

    # Unique temp ids.
    seen: set[str] = set()
    duplicates: set[str] = set()
    for task in plan.tasks:
        if task.temp_id in seen:
            duplicates.add(task.temp_id)
        seen.add(task.temp_id)
    for dup in sorted(duplicates):
        result.errors.append(f"duplicate temp_id {dup!r}")

    # Every assigned agent must exist.
    for task in plan.tasks:
        if task.assigned_agent not in known_agents:
            result.errors.append(
                f"{task.temp_id}: unknown agent {task.assigned_agent!r}. "
                f"Available: {', '.join(sorted(known_agents)) or 'none'}"
            )

    # Every dependency must reference a task defined in this plan.
    for task in plan.tasks:
        for dep in task.depends_on:
            if dep == task.temp_id:
                result.errors.append(f"{task.temp_id}: depends on itself")
            elif dep not in seen:
                result.errors.append(
                    f"{task.temp_id}: depends on unknown task {dep!r}"
                )

    if result.errors:
        return result

    # Cycle check, reusing the same traversal the scheduler relies on.
    index = {task.temp_id: i for i, task in enumerate(plan.tasks)}
    nodes = {
        index[task.temp_id]: dag.TaskNode(
            id=index[task.temp_id],
            key=task.temp_id,
            status=dag.TaskStatus.PENDING,
            depends_on=frozenset(
                index[dep] for dep in task.depends_on if dep in index
            ),
            sequence=index[task.temp_id],
        )
        for task in plan.tasks
    }
    cycles = dag.find_cycles(nodes)
    if cycles:
        rendered = "; ".join(
            " -> ".join(nodes[i].key for i in cycle) for cycle in cycles
        )
        result.errors.append(f"dependency cycle: {rendered}")
        return result

    # Advisory only: these do not block approval.
    for task in plan.tasks:
        if not task.description.strip():
            result.warnings.append(f"{task.temp_id}: no description")
        if not task.acceptance_criteria:
            result.warnings.append(f"{task.temp_id}: no acceptance criteria")

    result.order = _topological_order(plan.tasks)
    return result


def _topological_order(tasks: list[PlannedTask]) -> list[str]:
    """Order temp_ids so every task comes after its dependencies.

    Called only on an acyclic plan, so this always terminates.
    """
    remaining = {t.temp_id: set(t.depends_on) for t in tasks}
    order: list[str] = []
    while remaining:
        ready = sorted(tid for tid, deps in remaining.items() if not deps)
        if not ready:  # defensive: a cycle should already have been rejected
            order.extend(sorted(remaining))
            break
        for tid in ready:
            order.append(tid)
            del remaining[tid]
        for deps in remaining.values():
            deps.difference_update(ready)
    return order


class Planner:
    def __init__(self, db: Database, config: Config, task_service: TaskService) -> None:
        self.db = db
        self.config = config
        self.tasks = task_service
        self.agents = AgentRepository(db)
        self.objectives = ObjectiveRepository(db)

    def known_agents(self) -> set[str]:
        return {a.name for a in self.agents.list()}

    def validate(self, plan: ManagerPlan) -> PlanValidation:
        return validate_plan(plan, self.known_agents())

    def apply(
        self,
        objective: ObjectiveView,
        plan: ManagerPlan,
        validation: PlanValidation | None = None,
        prefix: str | None = None,
        created_by: str = "manager",
    ) -> list[TaskView]:
        """Persist a validated plan, translating temp ids into real task keys.

        Raises ValueError if the plan is not valid, so an unchecked plan can
        never reach the database.
        """
        validation = validation or self.validate(plan)
        if not validation.ok:
            raise ValueError(
                "refusing to apply an invalid plan: " + "; ".join(validation.errors)
            )

        by_temp_id = {task.temp_id: task for task in plan.tasks}
        key_prefix = prefix or self._prefix_for(objective)
        created: dict[str, TaskView] = {}

        # Dependencies first, so each real key exists before it is referenced.
        for temp_id in validation.order:
            planned = by_temp_id[temp_id]
            real_deps = [
                created[dep].key for dep in planned.depends_on if dep in created
            ]
            task = self.tasks.create_task(
                title=planned.title,
                description=planned.description,
                agent=planned.assigned_agent,
                depends_on=real_deps,
                acceptance_criteria=planned.acceptance_criteria,
                prefix=key_prefix,
                created_by=created_by,
                objective_id=objective.id,
            )
            created[temp_id] = task

        self.objectives.set_status(objective.id, ObjectiveStatus.ACTIVE)
        self.objectives.set_plan(objective.id, json.dumps(plan.model_dump()))
        return [created[t] for t in validation.order]

    @staticmethod
    def _prefix_for(objective: ObjectiveView) -> str:
        """Derive a readable key prefix from the objective text.

        "Add Google authentication" -> "ADD". Falls back to OBJ<id> when the
        description yields nothing usable.
        """
        words = [w for w in objective.description.split() if w.isalpha()]
        for word in words:
            if len(word) >= 3:
                return word[:8].upper()
        return f"OBJ{objective.id}"

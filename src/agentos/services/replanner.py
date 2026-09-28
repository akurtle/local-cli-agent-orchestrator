"""Validates and applies a manager's graph changes.

`validate_operations` is pure: it takes the proposal plus a snapshot of the live
graph and decides what may happen. Every refusal rule is therefore testable
without a database, an agent or a scheduler.

The invariant this module defends: **completed history is immutable**. A manager
recovering from a failure must not be able to cancel finished work, rewrite a
task that already ran, or reference something that does not exist. It may only
add corrective work and rewire what has not happened yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentos.config import Config
from agentos.db.session import Database
from agentos.schemas.dto import TaskView
from agentos.schemas.enums import TaskStatus
from agentos.schemas.replan import (
    MAX_OPERATIONS,
    Operation,
    OperationType,
    ReplanProposal,
)
from agentos.services import dag
from agentos.services.events import EventBus, EventType
from agentos.services.tasks import TaskService, TaskValidationError

# Statuses whose task may not be modified at all.
IMMUTABLE_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.CANCELLED})


@dataclass
class ReplanValidation:
    """What may be applied, and why the rest may not."""

    accepted: list[Operation] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and bool(self.accepted)

    @property
    def has_anything(self) -> bool:
        return bool(self.accepted)


def validate_operations(
    proposal: ReplanProposal,
    tasks: list[TaskView],
    known_agents: set[str],
) -> ReplanValidation:
    """Decide which operations are allowed. Pure: no database, no side effects.

    Operations are checked in order, because one may reference a task an earlier
    one creates. A rejected operation does not stop the rest, but it is reported:
    silently dropping a manager's request would make a partial application look
    like a full one.
    """
    result = ReplanValidation()

    if not proposal.operations:
        result.errors.append("the replan contains no operations")
        return result

    if len(proposal.operations) > MAX_OPERATIONS:
        result.errors.append(
            f"{len(proposal.operations)} operations requested, more than the "
            f"{MAX_OPERATIONS} allowed"
        )
        return result

    by_key = {task.key: task for task in tasks}
    # Tasks this replan will create, so a later operation can depend on them.
    pending: dict[str, str] = {}
    # Edges added in this replan, for cycle checking before anything is written.
    new_edges: list[tuple[str, str]] = []

    for index, operation in enumerate(proposal.operations, start=1):
        label = f"operation {index} ({operation.type.value})"
        error = _check(
            operation, label, by_key, pending, new_edges, known_agents, result
        )
        if error:
            result.errors.append(error)
            continue
        result.accepted.append(operation)

    return result


def _check(
    operation: Operation,
    label: str,
    by_key: dict[str, TaskView],
    pending: dict[str, str],
    new_edges: list[tuple[str, str]],
    known_agents: set[str],
    result: ReplanValidation,
) -> str | None:
    """Return an error string, or None if the operation is acceptable."""
    if operation.agent and operation.agent not in known_agents:
        return (
            f"{label}: unknown agent {operation.agent!r}. "
            f"Available: {', '.join(sorted(known_agents)) or 'none'}"
        )

    if operation.type is OperationType.CREATE_TASK:
        temp = (operation.temp_id or operation.title or "").strip()
        if temp in pending or temp in by_key:
            return f"{label}: temp_id {temp!r} is already used"
        pending[temp] = operation.agent or ""
        return None

    # Everything else names an existing task, or one created earlier here.
    target = (operation.task or "").strip()
    if target in pending:
        # Referring to something this replan creates is fine.
        pass
    elif target not in by_key:
        return f"{label}: unknown task {target!r}"
    else:
        existing = by_key[target]
        if existing.status in IMMUTABLE_STATUSES:
            return (
                f"{label}: {target} is {existing.status.value} and may not be "
                "modified -- completed history is immutable"
            )
        if (
            existing.status is TaskStatus.RUNNING
            and operation.type is OperationType.CANCEL_TASK
        ):
            return (
                f"{label}: {target} is running; stop the scheduler before "
                "cancelling it"
            )
        if operation.type is OperationType.RETRY_TASK and existing.status not in {
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
        }:
            return (
                f"{label}: {target} is {existing.status.value}; only a failed or "
                "blocked task can be retried"
            )

    if operation.type is OperationType.REMOVE_DEPENDENCY:
        prerequisite = (operation.depends_on or "").strip()
        if target not in by_key:
            return f"{label}: unknown task {target!r}"
        if prerequisite not in by_key:
            return f"{label}: unknown dependency {prerequisite!r}"
        if prerequisite not in by_key[target].depends_on:
            return f"{label}: {target} does not depend on {prerequisite}"
        return None

    if operation.type is OperationType.ADD_DEPENDENCY:
        prerequisite = (operation.depends_on or "").strip()
        if prerequisite not in pending and prerequisite not in by_key:
            return f"{label}: unknown dependency {prerequisite!r}"
        if prerequisite == target:
            return f"{label}: {target} cannot depend on itself"

        # Cycle check over the live graph plus everything this replan adds, so a
        # cycle is refused before any of it is written.
        if _would_cycle(by_key, pending, new_edges, target, prerequisite):
            return (
                f"{label}: {target} depending on {prerequisite} would create a "
                "cycle"
            )
        new_edges.append((target, prerequisite))

        if target in by_key and by_key[target].status is TaskStatus.RUNNING:
            result.warnings.append(
                f"{target} is already running; the new dependency applies to "
                "future attempts only"
            )

    return None


def _would_cycle(
    by_key: dict[str, TaskView],
    pending: dict[str, str],
    new_edges: list[tuple[str, str]],
    task: str,
    prerequisite: str,
) -> bool:
    """Reuse the scheduler's own cycle detection on a projected graph."""
    keys = list(by_key) + list(pending)
    index = {key: position for position, key in enumerate(keys)}

    edges: dict[str, set[str]] = {key: set() for key in keys}
    for key, view in by_key.items():
        edges[key].update(dep for dep in view.depends_on if dep in index)
    for child, parent in [*new_edges, (task, prerequisite)]:
        if child in edges and parent in index:
            edges[child].add(parent)

    nodes = {
        index[key]: dag.TaskNode(
            id=index[key],
            key=key,
            status=TaskStatus.PENDING,
            depends_on=frozenset(index[dep] for dep in edges[key]),
            sequence=index[key],
        )
        for key in keys
    }
    return dag.has_cycle(nodes)


@dataclass
class ReplanOutcome:
    """What was actually applied."""

    created: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    retried: list[str] = field(default_factory=list)
    reassigned: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return (
            len(self.created)
            + len(self.cancelled)
            + len(self.retried)
            + len(self.reassigned)
            + len(self.dependencies)
        )


class Replanner:
    def __init__(
        self,
        db: Database,
        config: Config,
        task_service: TaskService,
        event_bus: EventBus | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.tasks = task_service
        self.events = event_bus or EventBus(db)

    def validate(self, proposal: ReplanProposal) -> ReplanValidation:
        return validate_operations(
            proposal,
            self.tasks.list_tasks(),
            {a.name for a in self.tasks.agents.list()},
        )

    def apply(
        self,
        proposal: ReplanProposal,
        validation: ReplanValidation | None = None,
        objective_id: int | None = None,
        prefix: str = "FIX",
    ) -> ReplanOutcome:
        """Apply the accepted operations.

        Raises if nothing was accepted, so a caller cannot mistake a fully
        rejected replan for a successful one.
        """
        validation = validation or self.validate(proposal)
        if not validation.has_anything:
            raise ValueError(
                "nothing to apply: " + "; ".join(validation.errors or ["no operations"])
            )

        outcome = ReplanOutcome()
        # temp_id -> real key, so later operations resolve.
        resolved: dict[str, str] = {}

        for operation in validation.accepted:
            try:
                self._apply_one(operation, resolved, outcome, objective_id, prefix)
            except (TaskValidationError, ValueError) as exc:
                # A late failure (a race with the scheduler) is reported, not
                # allowed to abort the rest of a corrective plan.
                outcome.failures.append(f"{operation.describe()}: {exc}")

        self.tasks.refresh_readiness()
        return outcome

    def _apply_one(
        self,
        operation: Operation,
        resolved: dict[str, str],
        outcome: ReplanOutcome,
        objective_id: int | None,
        prefix: str,
    ) -> None:
        def real(reference: str | None) -> str:
            key = (reference or "").strip()
            return resolved.get(key, key)

        if operation.type is OperationType.CREATE_TASK:
            created = self.tasks.create_task(
                title=operation.title or "",
                description=operation.description,
                agent=operation.agent,
                acceptance_criteria=operation.acceptance_criteria,
                prefix=prefix,
                created_by="manager",
                objective_id=objective_id,
            )
            if operation.temp_id:
                resolved[operation.temp_id.strip()] = created.key
            resolved[(operation.title or "").strip()] = created.key
            outcome.created.append(created.key)

        elif operation.type is OperationType.ADD_DEPENDENCY:
            task_key = real(operation.task)
            prerequisite = real(operation.depends_on)
            self.tasks.add_dependency(task_key, prerequisite)
            outcome.dependencies.append(f"{task_key} -> {prerequisite}")

        elif operation.type is OperationType.REMOVE_DEPENDENCY:
            task_key = real(operation.task)
            prerequisite = real(operation.depends_on)
            self.tasks.remove_dependency(task_key, prerequisite)
            outcome.dependencies.append(f"{task_key} -/-> {prerequisite}")

        elif operation.type is OperationType.CANCEL_TASK:
            key = real(operation.task)
            self.tasks.cancel(key)
            outcome.cancelled.append(key)

        elif operation.type is OperationType.RETRY_TASK:
            key = real(operation.task)
            task = self.tasks.get_task(key)
            # A blocked task needs its intervention flag cleared; a failed one
            # needs requeueing. Both mean "try again".
            if task.status is TaskStatus.BLOCKED or task.needs_intervention:
                self.tasks.unblock(key)
            else:
                self.tasks.retry(key)
            outcome.retried.append(key)

        elif operation.type is OperationType.REASSIGN_TASK:
            key = real(operation.task)
            agent = self.tasks.agents.get(operation.agent or "")
            self.tasks.tasks.assign(key, agent.id, agent.role)
            outcome.reassigned.append(f"{key} -> {agent.name}")

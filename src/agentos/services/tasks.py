"""Task lifecycle: creation, dependency wiring, cancellation, status transitions.

Status changes go through `transition` so the legal moves live in one table
instead of being implied by assignments scattered across the codebase. The
scheduler asks this service to move tasks; it does not write statuses itself.
"""

from __future__ import annotations

from agentos.config import Config
from agentos.db.session import Database
from agentos.repositories.agents import AgentNotFound, AgentRepository
from agentos.repositories.tasks import TaskNotFound, TaskRepository
from agentos.schemas.dto import TaskView
from agentos.schemas.enums import TaskStatus
from agentos.services import dag
from agentos.services.events import EventBus, EventType

# Legal task status moves, as data.
TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset(
        {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
    ),
    TaskStatus.READY: frozenset(
        {
            TaskStatus.RUNNING,
            TaskStatus.PENDING,
            TaskStatus.BLOCKED,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
            TaskStatus.REVIEW,
            TaskStatus.READY,
            TaskStatus.CANCELLED,
        }
    ),
    TaskStatus.BLOCKED: frozenset(
        {TaskStatus.READY, TaskStatus.PENDING, TaskStatus.CANCELLED}
    ),
    TaskStatus.REVIEW: frozenset(
        {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
    ),
    # Terminal states. A failed task may be retried, which is an explicit,
    # deliberate move back into the queue.
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset({TaskStatus.READY, TaskStatus.PENDING}),
    TaskStatus.CANCELLED: frozenset(),
}


class InvalidTaskTransition(RuntimeError):
    """Refused an illegal task status change."""


class TaskValidationError(ValueError):
    """The requested task or dependency is not acceptable."""


class TaskService:
    def __init__(
        self, db: Database, config: Config, event_bus: EventBus | None = None
    ) -> None:
        self.db = db
        self.config = config
        self.tasks = TaskRepository(db)
        self.agents = AgentRepository(db)
        self.events = event_bus or EventBus(db)

    # ----------------------------------------------------------------- creation

    def create_task(
        self,
        title: str,
        description: str = "",
        agent: str | None = None,
        depends_on: list[str] | None = None,
        priority: int = 100,
        acceptance_criteria: list[str] | None = None,
        prefix: str = "T",
        created_by: str | None = None,
        key: str | None = None,
        objective_id: int | None = None,
    ) -> TaskView:
        """Create one task, validating its agent and dependencies first.

        Nothing is written until every check passes, so a rejected request leaves
        no partial state behind.
        """
        clean_title = (title or "").strip()
        if not clean_title:
            raise TaskValidationError("task title must not be empty")

        assigned_agent_id: int | None = None
        assigned_role: str | None = None
        if agent is not None:
            view = self.agents.find(agent)
            if view is None:
                known = ", ".join(sorted(self.config.agents)) or "(none configured)"
                raise TaskValidationError(
                    f"unknown agent {agent!r}. Configured agents: {known}"
                )
            assigned_agent_id = view.id
            assigned_role = view.role

        creator_id: int | None = None
        if created_by is not None:
            creator = self.agents.find(created_by)
            creator_id = creator.id if creator else None

        # Resolve dependencies before creating anything.
        dep_ids: list[int] = []
        for dep_key in depends_on or []:
            dep = self.tasks.find(dep_key)
            if dep is None:
                raise TaskValidationError(f"unknown dependency {dep_key!r}")
            dep_ids.append(dep.id)

        task_key = key or self.tasks.next_key(prefix)
        if self.tasks.find(task_key) is not None:
            raise TaskValidationError(f"task key {task_key!r} already exists")

        created = self.tasks.create(
            key=task_key,
            title=clean_title,
            description=description or "",
            assigned_agent_id=assigned_agent_id,
            assigned_role=assigned_role,
            created_by_agent_id=creator_id,
            objective_id=objective_id,
            priority=priority,
            acceptance_criteria=acceptance_criteria,
        )

        for dep_id in dep_ids:
            self._add_dependency_checked(created.id, dep_id)

        # Announce creation before recomputing readiness, or the timeline shows a
        # task becoming ready before it was created.
        created_view = self.tasks.get(created.id)
        self.events.emit(
            EventType.TASK_CREATED,
            summary=created_view.title[:200],
            task_key=created_view.key,
            agent=created_view.assigned_agent,
            objective_id=created_view.objective_id,
            created_by=created_by or "human",
            depends_on=created_view.depends_on,
        )
        self.refresh_readiness()
        return self.tasks.get(created.id)

    def _add_dependency_checked(self, task_id: int, dep_id: int) -> None:
        """Add an edge, refusing anything that would create a cycle."""
        nodes = self.tasks.nodes()
        if dag.would_create_cycle(nodes, task_id, dep_id):
            task_key = nodes[task_id].key if task_id in nodes else str(task_id)
            dep_key = nodes[dep_id].key if dep_id in nodes else str(dep_id)
            raise TaskValidationError(
                f"{task_key} cannot depend on {dep_key}: that would create a cycle"
            )
        self.tasks.add_dependency(task_id, dep_id)

    def add_dependency(self, task_key: str, depends_on_key: str) -> TaskView:
        task = self.tasks.get(task_key)
        dep = self.tasks.get(depends_on_key)
        self._add_dependency_checked(task.id, dep.id)
        self.refresh_readiness()
        return self.tasks.get(task.id)

    # -------------------------------------------------------------- transitions

    def transition(
        self,
        key_or_id: str | int,
        to: TaskStatus,
        result: str | None = None,
        error: str | None = None,
        needs_intervention: bool | None = None,
    ) -> TaskView:
        """Move a task to a new status, refusing illegal moves."""
        task = self.tasks.get(key_or_id)
        if task.status is to:
            return task
        allowed = TASK_TRANSITIONS.get(task.status, frozenset())
        if to not in allowed:
            raise InvalidTaskTransition(
                f"{task.key}: cannot go from {task.status.value} to {to.value}"
            )
        return self.tasks.set_status(
            key_or_id,
            to,
            result=result,
            error=error,
            touch_started=to is TaskStatus.RUNNING,
            touch_completed=to.is_terminal,
            needs_intervention=needs_intervention,
        )

    def cancel(self, key_or_id: str | int) -> TaskView:
        """Cancel a task. Dependents become blocked on the next readiness pass."""
        task = self.tasks.get(key_or_id)
        if task.status.is_terminal:
            raise InvalidTaskTransition(
                f"{task.key} is already {task.status.value}"
            )
        cancelled = self.transition(key_or_id, TaskStatus.CANCELLED)
        self.events.emit(
            EventType.TASK_CANCELLED,
            task_key=cancelled.key,
            agent=cancelled.assigned_agent,
            objective_id=cancelled.objective_id,
        )
        self.refresh_readiness()
        return cancelled

    def retry(self, key_or_id: str | int) -> TaskView:
        """Put a failed task back in the queue."""
        task = self.tasks.get(key_or_id)
        if task.status is not TaskStatus.FAILED:
            raise InvalidTaskTransition(
                f"{task.key} is {task.status.value}, only failed tasks can be retried"
            )
        # Clearing the flag is the point of a retry: the operator is saying the
        # blocker has been dealt with.
        self.tasks.set_status(
            key_or_id, TaskStatus.PENDING, error="", needs_intervention=False
        )
        self.refresh_readiness()
        result = self.tasks.get(key_or_id)
        self.events.emit(
            EventType.TASK_RETRIED,
            summary="requeued by operator",
            task_key=result.key,
            agent=result.assigned_agent,
            objective_id=result.objective_id,
        )
        return result

    def unblock(self, key_or_id: str | int) -> TaskView:
        """Clear an agent-reported blocker so the task can be scheduled again."""
        task = self.tasks.get(key_or_id)
        if not task.needs_intervention and task.status is not TaskStatus.BLOCKED:
            raise InvalidTaskTransition(f"{task.key} is not blocked")
        self.tasks.set_status(
            key_or_id, TaskStatus.PENDING, error="", needs_intervention=False
        )
        self.refresh_readiness()
        result = self.tasks.get(key_or_id)
        self.events.emit(
            EventType.TASK_UNBLOCKED,
            summary="blocker cleared by operator",
            task_key=result.key,
            agent=result.assigned_agent,
            objective_id=result.objective_id,
        )
        return result

    # ---------------------------------------------------------------- readiness

    def refresh_readiness(self, max_rounds: int = 100) -> list[TaskView]:
        """Recompute pending/ready/blocked for the whole graph and persist it.

        Iterates to a fixed point. One round decides each task from a snapshot,
        so blockage only moves one edge at a time: marking B blocked does not, in
        that same round, tell C (which waits on B) anything new. Repeating until
        nothing changes propagates it all the way down the chain.

        The decisions themselves come from the pure functions in `dag`; this
        method only loads and saves.
        """
        changed: list[TaskView] = []
        for _round in range(max_rounds):
            decisions = dag.compute_readiness(self.tasks.nodes())
            if not decisions:
                break
            for decision in decisions:
                updated = self.tasks.set_status(decision.task_id, decision.status)
                changed.append(updated)
                if decision.status is TaskStatus.READY:
                    self.events.emit(
                        EventType.TASK_READY,
                        summary=decision.reason,
                        task_key=updated.key,
                        agent=updated.assigned_agent,
                        objective_id=updated.objective_id,
                    )
                elif decision.status is TaskStatus.BLOCKED:
                    self.events.emit(
                        EventType.TASK_BLOCKED,
                        summary=decision.reason,
                        task_key=updated.key,
                        agent=updated.assigned_agent,
                        objective_id=updated.objective_id,
                    )
        return changed

    def validate_graph(self) -> None:
        """Raise if the stored graph is unsound.

        A safety net: cycles are refused at write time, so reaching this means
        something bypassed the service or the database was edited by hand.
        """
        nodes = self.tasks.nodes()
        cycles = dag.find_cycles(nodes)
        if cycles:
            raise dag.DependencyCycle(cycles)

    # -------------------------------------------------------------------- reads

    def list_tasks(self, statuses: set[TaskStatus] | None = None) -> list[TaskView]:
        return self.tasks.list(statuses)

    def get_task(self, key_or_id: str | int) -> TaskView:
        return self.tasks.get(key_or_id)

    def completed_dependencies(self, task: TaskView) -> list[TaskView]:
        """Prerequisite tasks that finished, for prompt context."""
        result: list[TaskView] = []
        for dep_id in task.depends_on_ids:
            dep = self.tasks.find(dep_id)
            if dep is not None and dep.status is TaskStatus.COMPLETED:
                result.append(dep)
        return result


__all__ = [
    "AgentNotFound",
    "InvalidTaskTransition",
    "TaskNotFound",
    "TaskService",
    "TaskValidationError",
    "TASK_TRANSITIONS",
]

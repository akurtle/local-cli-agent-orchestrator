"""Data access for tasks and their dependency edges.

All task SQL lives here. Callers get TaskView snapshots, never ORM rows.
Acceptance criteria are stored as newline-separated text and exposed as a list,
so the column stays simple while the API stays typed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select

from agentos.db.models import Agent, Task, TaskDependency
from agentos.db.session import Database
from agentos.schemas.dto import TaskView
from agentos.schemas.enums import TaskStatus
from agentos.services.dag import TaskNode


class TaskNotFound(LookupError):
    """No task with that key or id exists."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def criteria_to_text(criteria: list[str] | None) -> str:
    return "\n".join(c.strip() for c in (criteria or []) if c.strip())


def criteria_from_text(text: str | None) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


class TaskRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ helpers

    def _view(self, session, row: Task) -> TaskView:
        dep_ids = list(
            session.scalars(
                select(TaskDependency.depends_on_task_id).where(
                    TaskDependency.task_id == row.id
                )
            ).all()
        )
        dep_keys: list[str] = []
        if dep_ids:
            dep_keys = list(
                session.scalars(
                    select(Task.key).where(Task.id.in_(dep_ids)).order_by(Task.id)
                ).all()
            )

        agent_name = None
        if row.assigned_agent_id is not None:
            agent_name = session.scalar(
                select(Agent.name).where(Agent.id == row.assigned_agent_id)
            )
        creator_name = None
        if row.created_by_agent_id is not None:
            creator_name = session.scalar(
                select(Agent.name).where(Agent.id == row.created_by_agent_id)
            )

        return TaskView(
            id=row.id,
            key=row.key,
            title=row.title,
            description=row.description or "",
            status=TaskStatus(row.status),
            assigned_agent_id=row.assigned_agent_id,
            assigned_agent=agent_name,
            assigned_role=row.assigned_role,
            created_by_agent_id=row.created_by_agent_id,
            created_by=creator_name,
            objective_id=row.objective_id,
            priority=row.priority,
            acceptance_criteria=criteria_from_text(row.acceptance_criteria),
            depends_on=sorted(dep_keys),
            depends_on_ids=sorted(dep_ids),
            result=row.result,
            error=row.error,
            attempts=row.attempts,
            created_at=row.created_at,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )

    def _require(self, session, key_or_id: str | int) -> Task:
        if isinstance(key_or_id, int):
            row = session.get(Task, key_or_id)
        else:
            row = session.scalar(select(Task).where(Task.key == key_or_id))
        if row is None:
            raise TaskNotFound(str(key_or_id))
        return row

    # -------------------------------------------------------------------- reads

    def list(self, statuses: set[TaskStatus] | None = None) -> list[TaskView]:
        with self.db.session() as session:
            stmt = select(Task).order_by(Task.id)
            if statuses:
                stmt = stmt.where(Task.status.in_([s.value for s in statuses]))
            return [self._view(session, row) for row in session.scalars(stmt).all()]

    def get(self, key_or_id: str | int) -> TaskView:
        with self.db.session() as session:
            return self._view(session, self._require(session, key_or_id))

    def find(self, key_or_id: str | int) -> TaskView | None:
        try:
            return self.get(key_or_id)
        except TaskNotFound:
            return None

    def count(self) -> int:
        with self.db.session() as session:
            return int(session.scalar(select(func.count(Task.id))) or 0)

    def nodes(self) -> dict[int, TaskNode]:
        """Load the whole graph in the shape the scheduling core expects.

        One query per table rather than per task, so a large graph stays cheap.
        """
        with self.db.session() as session:
            rows = session.execute(
                select(
                    Task.id,
                    Task.key,
                    Task.status,
                    Task.priority,
                    Task.assigned_agent_id,
                )
            ).all()
            edges = session.execute(
                select(TaskDependency.task_id, TaskDependency.depends_on_task_id)
            ).all()
            agent_names = dict(session.execute(select(Agent.id, Agent.name)).all())

        deps: dict[int, set[int]] = {}
        for task_id, dep_id in edges:
            deps.setdefault(task_id, set()).add(dep_id)

        return {
            row.id: TaskNode(
                id=row.id,
                key=row.key,
                status=TaskStatus(row.status),
                depends_on=frozenset(deps.get(row.id, ())),
                priority=row.priority,
                agent_name=agent_names.get(row.assigned_agent_id),
                sequence=row.id,
            )
            for row in rows
        }

    def next_key(self, prefix: str) -> str:
        """Allocate the next key for a prefix, e.g. AUTH-3.

        Scans existing keys rather than keeping a counter table, which keeps the
        schema simple and stays correct if tasks are deleted.
        """
        with self.db.session() as session:
            keys = session.scalars(
                select(Task.key).where(Task.key.like(f"{prefix}-%"))
            ).all()
        highest = 0
        for key in keys:
            suffix = key[len(prefix) + 1 :]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        return f"{prefix}-{highest + 1}"

    # ------------------------------------------------------------------- writes

    def create(
        self,
        key: str,
        title: str,
        description: str = "",
        assigned_agent_id: int | None = None,
        assigned_role: str | None = None,
        created_by_agent_id: int | None = None,
        objective_id: int | None = None,
        priority: int = 100,
        acceptance_criteria: list[str] | None = None,
        status: TaskStatus = TaskStatus.PENDING,
    ) -> TaskView:
        with self.db.session() as session:
            row = Task(
                key=key,
                title=title,
                description=description,
                status=status.value,
                assigned_agent_id=assigned_agent_id,
                assigned_role=assigned_role,
                created_by_agent_id=created_by_agent_id,
                objective_id=objective_id,
                priority=priority,
                acceptance_criteria=criteria_to_text(acceptance_criteria),
            )
            session.add(row)
            session.flush()
            return self._view(session, row)

    def add_dependency(self, task_id: int, depends_on_task_id: int) -> None:
        """Record an edge, ignoring a duplicate."""
        with self.db.session() as session:
            existing = session.scalar(
                select(TaskDependency).where(
                    TaskDependency.task_id == task_id,
                    TaskDependency.depends_on_task_id == depends_on_task_id,
                )
            )
            if existing is None:
                session.add(
                    TaskDependency(
                        task_id=task_id, depends_on_task_id=depends_on_task_id
                    )
                )

    def set_status(
        self,
        key_or_id: str | int,
        status: TaskStatus,
        result: str | None = None,
        error: str | None = None,
        touch_started: bool = False,
        touch_completed: bool = False,
    ) -> TaskView:
        with self.db.session() as session:
            row = self._require(session, key_or_id)
            row.status = status.value
            if result is not None:
                row.result = result
            if error is not None:
                row.error = error
            if touch_started and row.started_at is None:
                row.started_at = _utcnow()
            if touch_completed:
                row.completed_at = _utcnow()
            session.flush()
            return self._view(session, row)

    def assign(
        self, key_or_id: str | int, agent_id: int | None, role: str | None = None
    ) -> TaskView:
        with self.db.session() as session:
            row = self._require(session, key_or_id)
            row.assigned_agent_id = agent_id
            if role is not None:
                row.assigned_role = role
            session.flush()
            return self._view(session, row)

    def increment_attempts(self, key_or_id: str | int) -> int:
        with self.db.session() as session:
            row = self._require(session, key_or_id)
            row.attempts += 1
            session.flush()
            return row.attempts

    def delete(self, key_or_id: str | int) -> None:
        with self.db.session() as session:
            row = self._require(session, key_or_id)
            session.delete(row)

"""Data access for agents.

All agent SQL lives here. Services and the CLI never touch the Agent table
directly, and never receive an ORM instance -- only AgentView snapshots.
"""

from __future__ import annotations

from sqlalchemy import select

from agentos.db.models import Agent
from agentos.db.session import Database
from agentos.schemas.dto import AgentView
from agentos.schemas.enums import AgentStatus


class AgentNotFound(LookupError):
    """No agent with that name exists in the database."""


def _to_view(row: Agent) -> AgentView:
    return AgentView(
        id=row.id,
        name=row.name,
        role=row.role,
        description=row.description or "",
        runtime=row.runtime,
        status=AgentStatus(row.status),
        session_id=row.session_id,
        current_task_id=row.current_task_id,
        session_task_count=row.session_task_count,
        session_objective_id=row.session_objective_id,
        worktree_path=row.worktree_path,
        branch_name=row.branch_name,
        model=row.model,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class AgentRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -------------------------------------------------------------------- reads

    def list(self) -> list[AgentView]:
        with self.db.session() as session:
            rows = session.scalars(select(Agent).order_by(Agent.name)).all()
            return [_to_view(row) for row in rows]

    def get(self, name: str) -> AgentView:
        with self.db.session() as session:
            row = session.scalar(select(Agent).where(Agent.name == name))
            if row is None:
                raise AgentNotFound(name)
            return _to_view(row)

    def find(self, name: str) -> AgentView | None:
        try:
            return self.get(name)
        except AgentNotFound:
            return None

    def find_by_role(self, role: str) -> list[AgentView]:
        with self.db.session() as session:
            rows = session.scalars(
                select(Agent).where(Agent.role == role).order_by(Agent.name)
            ).all()
            return [_to_view(row) for row in rows]

    # ------------------------------------------------------------------- writes

    def upsert(
        self,
        name: str,
        role: str,
        description: str = "",
        runtime: str = "claude",
        model: str | None = None,
    ) -> tuple[AgentView, bool]:
        """Create the agent, or update its definition if it already exists.

        Returns (view, created). Definition fields come from config and are
        refreshed on every sync; runtime state (status, session_id) is never
        clobbered here, because config is not the system of record for state.
        """
        with self.db.session() as session:
            row = session.scalar(select(Agent).where(Agent.name == name))
            created = row is None
            if row is None:
                row = Agent(name=name, role=role)
                session.add(row)
            row.role = role
            row.description = description
            row.runtime = runtime
            row.model = model
            session.flush()
            return _to_view(row), created

    def set_status(
        self,
        name: str,
        status: AgentStatus,
        current_task_id: int | None = None,
        clear_task: bool = False,
    ) -> AgentView:
        with self.db.session() as session:
            row = session.scalar(select(Agent).where(Agent.name == name))
            if row is None:
                raise AgentNotFound(name)
            row.status = status.value
            if clear_task:
                row.current_task_id = None
            elif current_task_id is not None:
                row.current_task_id = current_task_id
            session.flush()
            return _to_view(row)

    def set_session_id(self, name: str, session_id: str | None) -> AgentView:
        with self.db.session() as session:
            row = session.scalar(select(Agent).where(Agent.name == name))
            if row is None:
                raise AgentNotFound(name)
            row.session_id = session_id
            session.flush()
            return _to_view(row)

    def set_session(
        self,
        name: str,
        session_id: str | None,
        task_count: int | None = None,
        objective_id: int | None = None,
    ) -> AgentView:
        """Update a session and its counters together.

        One write, so a session id can never be stored without its counters
        being consistent with it.
        """
        with self.db.session() as session:
            row = session.scalar(select(Agent).where(Agent.name == name))
            if row is None:
                raise AgentNotFound(name)
            row.session_id = session_id
            if task_count is not None:
                row.session_task_count = max(0, task_count)
            if objective_id is not None or session_id is None:
                row.session_objective_id = objective_id
            session.flush()
            return _to_view(row)

    def increment_session_tasks(
        self, name: str, objective_id: int | None = None
    ) -> AgentView:
        with self.db.session() as session:
            row = session.scalar(select(Agent).where(Agent.name == name))
            if row is None:
                raise AgentNotFound(name)
            row.session_task_count += 1
            if objective_id is not None:
                row.session_objective_id = objective_id
            session.flush()
            return _to_view(row)

    def set_worktree(
        self, name: str, worktree_path: str | None, branch_name: str | None
    ) -> AgentView:
        with self.db.session() as session:
            row = session.scalar(select(Agent).where(Agent.name == name))
            if row is None:
                raise AgentNotFound(name)
            row.worktree_path = worktree_path
            row.branch_name = branch_name
            session.flush()
            return _to_view(row)

    def delete(self, name: str) -> None:
        with self.db.session() as session:
            row = session.scalar(select(Agent).where(Agent.name == name))
            if row is not None:
                session.delete(row)

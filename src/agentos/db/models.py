"""SQLAlchemy models.

Design notes:
  * Session IDs are orchestrator-owned UUIDs, not values scraped from output.
  * Task dependencies live in their own table rather than a JSON blob so the
    scheduler can query readiness in SQL and so cycle detection works on real
    edges.
  * Run rows keep raw stdout/stderr. That is enough for `agentctl logs` without
    building an event-sourcing system.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)

from agentos.schemas.enums import (
    AgentStatus,
    ObjectiveStatus,
    MessageStatus,
    MessageType,
    RunStatus,
    TaskStatus,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(64), index=True)
    description: Mapped[str] = mapped_column(Text, default="")

    runtime: Mapped[str] = mapped_column(String(32), default="claude")
    session_id: Mapped[str | None] = mapped_column(String(64), default=None)
    status: Mapped[str] = mapped_column(String(16), default=AgentStatus.IDLE.value)
    current_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), default=None
    )

    worktree_path: Mapped[str | None] = mapped_column(Text, default=None)
    branch_name: Mapped[str | None] = mapped_column(String(200), default=None)
    model: Mapped[str | None] = mapped_column(String(100), default=None)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    runs: Mapped[list["Run"]] = relationship(
        back_populates="agent", cascade="all, delete-orphan", foreign_keys="Run.agent_id"
    )

    def __repr__(self) -> str:
        return f"<Agent {self.name} role={self.role} status={self.status}>"


class Objective(Base):
    """A user goal that the manager decomposes into tasks."""

    __tablename__ = "objectives"

    id: Mapped[int] = mapped_column(primary_key=True)
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(16), default=ObjectiveStatus.PLANNING.value, index=True
    )
    plan_json: Mapped[str | None] = mapped_column(Text, default=None)
    """The manager's proposed plan, kept verbatim for auditing."""

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)

    def __repr__(self) -> str:
        return f"<Objective {self.id} {self.status}>"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    """Human-facing identifier such as AUTH-2."""

    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(
        String(16), default=TaskStatus.PENDING.value, index=True
    )

    assigned_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), default=None
    )
    assigned_role: Mapped[str | None] = mapped_column(String(64), default=None)
    created_by_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), default=None
    )

    objective_id: Mapped[int | None] = mapped_column(
        ForeignKey("objectives.id", ondelete="SET NULL"), default=None, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=100)
    acceptance_criteria: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[str | None] = mapped_column(Text, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    completed_at: Mapped[datetime | None] = mapped_column(default=None)

    dependencies: Mapped[list["TaskDependency"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        foreign_keys="TaskDependency.task_id",
    )

    def __repr__(self) -> str:
        return f"<Task {self.key} {self.status}>"


class TaskDependency(Base):
    """Edge: `task_id` cannot start until `depends_on_task_id` completes."""

    __tablename__ = "task_dependencies"
    __table_args__ = (UniqueConstraint("task_id", "depends_on_task_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    depends_on_task_id: Mapped[int] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )

    task: Mapped[Task] = relationship(
        back_populates="dependencies", foreign_keys=[task_id]
    )


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    sender_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="SET NULL"), default=None
    )
    sender_name: Mapped[str] = mapped_column(String(64), default="system")
    recipient_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), default=None, index=True
    )
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), default=None
    )

    message_type: Mapped[str] = mapped_column(
        String(16), default=MessageType.INFO.value
    )
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(16), default=MessageStatus.PENDING.value, index=True
    )
    delivered_at: Mapped[datetime | None] = mapped_column(default=None)
    read_at: Mapped[datetime | None] = mapped_column(default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Run(Base):
    """One invocation of the external agent CLI."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), default=None, index=True
    )
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), default=None, index=True
    )

    session_id: Mapped[str | None] = mapped_column(String(64), default=None)
    runtime: Mapped[str] = mapped_column(String(32), default="claude")
    command: Mapped[str] = mapped_column(Text, default="")
    """JSON-encoded argv list. Stored for auditability, never re-executed."""

    status: Mapped[str] = mapped_column(String(16), default=RunStatus.PENDING.value)
    exit_code: Mapped[int | None] = mapped_column(Integer, default=None)
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    result_text: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str | None] = mapped_column(Text, default=None)

    cost_usd: Mapped[float | None] = mapped_column(Float, default=None)
    num_turns: Mapped[int | None] = mapped_column(Integer, default=None)

    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)

    agent: Mapped[Agent | None] = relationship(
        back_populates="runs", foreign_keys=[agent_id]
    )

    def __repr__(self) -> str:
        return f"<Run {self.id} status={self.status} exit={self.exit_code}>"

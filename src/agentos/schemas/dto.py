"""Read models handed out to callers.

ORM instances must not escape the repository layer. Once a Session closes,
attribute access on a detached instance raises, and the CLI has no business
holding one. Repositories convert rows into these frozen views instead.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from agentos.schemas.enums import AgentStatus, TaskStatus


class AgentView(BaseModel):
    """A snapshot of one agent."""

    model_config = ConfigDict(frozen=True)

    id: int
    name: str
    role: str
    description: str = ""
    runtime: str = "claude"
    status: AgentStatus = AgentStatus.IDLE
    session_id: str | None = None
    current_task_id: int | None = None
    worktree_path: str | None = None
    branch_name: str | None = None
    model: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def has_session(self) -> bool:
        return bool(self.session_id)

    @property
    def short_session(self) -> str:
        return self.session_id[:8] if self.session_id else "-"


class AgentRunOutcome(BaseModel):
    """What `agent run` produced, for the CLI to render."""

    model_config = ConfigDict(frozen=True)

    agent: AgentView
    run_id: int
    ok: bool
    text: str = ""
    error: str | None = None
    session_id: str | None = None
    resumed: bool = False
    """True when this turn continued an existing Claude session."""
    session_restarted: bool = False
    """True when a stale session was detected and a fresh one was started."""
    cost_usd: float | None = None
    duration_seconds: float | None = None


class TaskView(BaseModel):
    """A snapshot of one task."""

    model_config = ConfigDict(frozen=True)

    id: int
    key: str
    title: str
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    assigned_agent_id: int | None = None
    assigned_agent: str | None = None
    assigned_role: str | None = None
    created_by_agent_id: int | None = None
    created_by: str | None = None
    priority: int = 100
    acceptance_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    """Keys of prerequisite tasks, for display."""
    depends_on_ids: list[int] = Field(default_factory=list)
    result: str | None = None
    error: str | None = None
    attempts: int = 0
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status.is_terminal

    @property
    def duration_seconds(self) -> float | None:
        if self.started_at is None or self.completed_at is None:
            return None
        return (self.completed_at - self.started_at).total_seconds()


class SchedulerReport(BaseModel):
    """Outcome of a scheduler session, for the CLI to render."""

    model_config = ConfigDict(frozen=True)

    passes: int = 0
    dispatched: int = 0
    completed: list[str] = Field(default_factory=list)
    failed: list[str] = Field(default_factory=list)
    blocked: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    """Ready tasks that could not run, e.g. no available agent."""
    stop_reason: str = ""
    interrupted: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed and not self.blocked

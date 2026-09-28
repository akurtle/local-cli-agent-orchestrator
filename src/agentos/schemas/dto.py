"""Read models handed out to callers.

ORM instances must not escape the repository layer. Once a Session closes,
attribute access on a detached instance raises, and the CLI has no business
holding one. Repositories convert rows into these frozen views instead.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from agentos.schemas.enums import AgentStatus


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

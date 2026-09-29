"""Runtime-neutral DTOs for invoking an external agent process.

Nothing here mentions Claude. A future `codex` or `ollama` runtime implements
the same protocol against these types.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agentos.schemas.enums import RunStatus


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RunRequest(BaseModel):
    """Everything needed to invoke one agent turn."""

    model_config = ConfigDict(extra="forbid")

    prompt: str
    system_prompt: str | None = None
    session_id: str | None = None
    """Session to resume. When None the runtime mints a new one."""
    resume: bool = False
    cwd: str | None = None
    model: str | None = None
    timeout_seconds: float | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    disallowed_tools: list[str] = Field(default_factory=list)
    """Tools the agent must not be able to use, from its capabilities."""
    permission_mode: str | None = None
    """Passed as `--permission-mode`. Print mode cannot ask a human, so an agent
    that may edit needs `acceptEdits` or every Edit/Write is declined."""
    stream: bool = True


class StreamEvent(BaseModel):
    """One decoded line of streaming output."""

    model_config = ConfigDict(extra="allow")

    type: str = "unknown"
    subtype: str | None = None
    session_id: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class RunResult(BaseModel):
    """Outcome of one agent turn, ready to persist as a Run row."""

    model_config = ConfigDict(extra="forbid")

    status: RunStatus
    session_id: str | None = None
    exit_code: int | None = None
    text: str = ""
    """The agent's final assistant text, used for response-block parsing."""
    stdout: str = ""
    stderr: str = ""
    command: list[str] = Field(default_factory=list)
    events: list[StreamEvent] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=_utcnow)
    finished_at: datetime | None = None
    error: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float | None = None
    num_turns: int | None = None

    @property
    def ok(self) -> bool:
        return self.status is RunStatus.SUCCEEDED

    @property
    def duration_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds()

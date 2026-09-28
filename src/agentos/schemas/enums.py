"""Lifecycle vocabularies.

These are plain str-enums so they persist to SQLite as readable text and are
directly usable in JSON and Rich output.
"""

from __future__ import annotations

from enum import StrEnum


class AgentStatus(StrEnum):
    IDLE = "idle"
    WORKING = "working"
    WAITING = "waiting"
    BLOCKED = "blocked"
    FAILED = "failed"
    PAUSED = "paused"
    OFFLINE = "offline"


class TaskStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    AGENT_DONE = "agent_done"
    """The agent says it finished. Not the same as done."""
    VERIFYING = "verifying"
    """The orchestrator is checking the claim."""
    FAILED_VERIFICATION = "failed_verification"
    """The agent claimed success and the checks disagreed."""
    BLOCKED = "blocked"
    REVIEW = "review"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_TASK_STATUSES


_TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class MemoryScope(StrEnum):
    """What a remembered fact is attached to.

    The scope decides when a fact is recalled: an agent memory follows the agent,
    a project memory is always relevant, an objective memory applies while that
    objective is active, and a task memory is narrow.
    """

    AGENT = "agent"
    PROJECT = "project"
    OBJECTIVE = "objective"
    TASK = "task"


class MemoryCategory(StrEnum):
    FACT = "fact"
    """Something durably true about the repository or product."""
    DECISION = "decision"
    """A choice that was made and should not be silently revisited."""
    CONVENTION = "convention"
    """How things are done here."""
    WARNING = "warning"
    """A trap somebody already fell into."""
    HANDOFF = "handoff"
    """What one agent needs another to know."""
    SESSION_SUMMARY = "session_summary"
    """Carried across a session rotation so knowledge is not lost."""


class FailureKind(StrEnum):
    """Why a task attempt did not succeed.

    The orchestrator treats these differently: infrastructure problems are worth
    retrying, an agent reporting `blocked` is not -- retrying it would just
    reproduce the same blocker and burn usage.
    """

    LAUNCH = "launch"
    """The agent process could not be started at all."""
    TIMEOUT = "timeout"
    """The process ran too long and was killed."""
    AGENT_FAILED = "agent_failed"
    """The agent ran and reported that it failed."""
    UNPARSEABLE = "unparseable"
    """The agent ran but never produced a usable response block."""
    BLOCKED = "blocked"
    """The agent cannot proceed and needs intervention."""
    VERIFICATION_FAILED = "verification_failed"
    """The agent claimed success and the checks disagreed."""
    UNAVAILABLE = "unavailable"
    """The assigned agent was busy or paused; not the task's fault."""

    @property
    def is_infrastructure(self) -> bool:
        return self in {FailureKind.LAUNCH, FailureKind.TIMEOUT}

    @property
    def is_retryable(self) -> bool:
        """A blocker needs a human or the manager, not another attempt."""
        return self is not FailureKind.BLOCKED


class ObjectiveStatus(StrEnum):
    PLANNING = "planning"
    AWAITING_APPROVAL = "awaiting_approval"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            ObjectiveStatus.COMPLETED,
            ObjectiveStatus.FAILED,
            ObjectiveStatus.CANCELLED,
        }


class MessageStatus(StrEnum):
    """Delivery lifecycle of a message.

    The distinction between DELIVERED and READ is what stops a crash losing a
    message: DELIVERED means it was injected into a prompt we sent, READ means
    the receiving agent's run actually finished. A run that dies in between is
    returned to PENDING and injected again.
    """

    PENDING = "pending"
    DELIVERED = "delivered"
    READ = "read"


class MessageType(StrEnum):
    INFO = "info"
    QUESTION = "question"
    ANSWER = "answer"
    BLOCKER = "blocker"
    REVIEW = "review"
    SYSTEM = "system"

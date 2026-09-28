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

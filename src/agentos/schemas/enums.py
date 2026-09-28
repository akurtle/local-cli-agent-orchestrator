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


class MessageType(StrEnum):
    INFO = "info"
    QUESTION = "question"
    ANSWER = "answer"
    BLOCKER = "blocker"
    REVIEW = "review"
    SYSTEM = "system"

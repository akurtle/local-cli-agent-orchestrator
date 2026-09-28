"""The event bus.

Everything important emits an event. Events exist for debugging, observability
and status displays -- never as the system of record. Task state lives in the
tasks table; an event says that a transition happened, not what is true now.

Deliberately in-process: an asyncio fan-out to subscribers plus a row in SQLite.
No broker, because a single local orchestrator does not need one and would be
harder to reason about with one.

Emitting must never break orchestration. A failing subscriber is isolated, and a
failing write is swallowed with the event still delivered in memory, because
losing a log line is always better than losing the work it describes.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import select

from agentos.db.models import Event as EventRow
from agentos.db.session import Database


class EventType(StrEnum):
    """Every transition worth recording.

    Dotted names so a subscriber can filter by prefix, e.g. everything under
    `task.`.
    """

    OBJECTIVE_CREATED = "objective.created"
    OBJECTIVE_PLANNED = "objective.planned"
    OBJECTIVE_APPROVED = "objective.approved"
    OBJECTIVE_REJECTED = "objective.rejected"
    OBJECTIVE_COMPLETED = "objective.completed"
    OBJECTIVE_FAILED = "objective.failed"

    TASK_CREATED = "task.created"
    TASK_READY = "task.ready"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_BLOCKED = "task.blocked"
    TASK_CANCELLED = "task.cancelled"
    TASK_RETRIED = "task.retried"
    TASK_UNBLOCKED = "task.unblocked"

    AGENT_STARTED = "agent.started"
    AGENT_IDLE = "agent.idle"
    AGENT_FAILED = "agent.failed"
    AGENT_PAUSED = "agent.paused"
    AGENT_ROTATED = "agent.rotated"

    MESSAGE_CREATED = "message.created"
    MESSAGE_DELIVERED = "message.delivered"
    MESSAGE_READ = "message.read"

    HANDOFF_CREATED = "handoff.created"
    MEMORY_RECORDED = "memory.recorded"

    RUN_STARTED = "run.started"
    RUN_COMPLETED = "run.completed"

    COMMAND_STARTED = "command.started"
    COMMAND_COMPLETED = "command.completed"
    COMMAND_DENIED = "command.denied"

    GIT_WORKTREE_CREATED = "git.worktree_created"
    GIT_COMMIT_CREATED = "git.commit_created"
    GIT_MERGED = "git.merged"
    GIT_CONFLICT = "git.conflict"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_APPROVED = "approval.approved"
    APPROVAL_DENIED = "approval.denied"

    CAPABILITY_DENIED = "capability.denied"

    SCHEDULER_STARTED = "scheduler.started"
    SCHEDULER_STOPPED = "scheduler.stopped"


@dataclass(frozen=True)
class Event:
    """One thing that happened."""

    type: EventType
    summary: str = ""
    objective_id: int | None = None
    task_key: str | None = None
    agent: str | None = None
    run_id: int | None = None
    data: dict = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    id: int | None = None

    @property
    def category(self) -> str:
        return self.type.value.split(".", 1)[0]

    def describe(self) -> str:
        """A one-line rendering for a timeline."""
        subject = self.task_key or self.agent or (
            f"objective {self.objective_id}" if self.objective_id else ""
        )
        parts = [self.type.value]
        if subject:
            parts.append(subject)
        if self.summary:
            parts.append(self.summary)
        return "  ".join(parts)


Subscriber = Callable[[Event], None]


class EventBus:
    """In-process fan-out plus persistence."""

    def __init__(self, db: Database | None = None, persist: bool = True) -> None:
        self.db = db
        self.persist = persist and db is not None
        self._subscribers: list[tuple[str | None, Subscriber]] = []

    # ------------------------------------------------------------- subscribing

    def subscribe(self, handler: Subscriber, prefix: str | None = None) -> Subscriber:
        """Register a handler, optionally only for a category prefix.

        Returns the handler so it can be unsubscribed.
        """
        self._subscribers.append((prefix, handler))
        return handler

    def unsubscribe(self, handler: Subscriber) -> None:
        self._subscribers = [(p, h) for p, h in self._subscribers if h is not handler]

    def _fan_out(self, event: Event) -> None:
        for prefix, handler in list(self._subscribers):
            if prefix and not event.type.value.startswith(prefix):
                continue
            try:
                handler(event)
            except Exception:
                # A broken subscriber must not take down the orchestrator.
                pass

    # ---------------------------------------------------------------- emitting

    def emit(
        self,
        type: EventType,
        summary: str = "",
        objective_id: int | None = None,
        task_key: str | None = None,
        agent: str | None = None,
        run_id: int | None = None,
        **data: object,
    ) -> Event:
        """Record and deliver an event. Never raises."""
        event = Event(
            type=type,
            summary=summary,
            objective_id=objective_id,
            task_key=task_key,
            agent=agent,
            run_id=run_id,
            data=dict(data),
        )
        stored_id = self._write(event) if self.persist else None
        if stored_id is not None:
            event = Event(**{**event.__dict__, "id": stored_id})
        self._fan_out(event)
        return event

    async def emit_async(self, type: EventType, **kwargs) -> Event:
        """Emit from async code without blocking the loop on the write."""
        return await asyncio.to_thread(self.emit, type, **kwargs)

    def _write(self, event: Event) -> int | None:
        if self.db is None:
            return None
        try:
            with self.db.session() as session:
                row = EventRow(
                    type=event.type.value,
                    summary=event.summary[:500],
                    objective_id=event.objective_id,
                    task_key=event.task_key,
                    agent=event.agent,
                    run_id=event.run_id,
                    data_json=json.dumps(event.data, default=str)[:4000],
                    created_at=event.at,
                )
                session.add(row)
                session.flush()
                return row.id
        except Exception:
            # Losing a log line is better than losing the work it describes.
            return None

    # ----------------------------------------------------------------- reading

    def history(
        self,
        agent: str | None = None,
        task_key: str | None = None,
        objective_id: int | None = None,
        category: str | None = None,
        since_id: int | None = None,
        limit: int | None = 100,
    ) -> list[Event]:
        """Events oldest-first, which is how a timeline reads."""
        if self.db is None:
            return []
        with self.db.session() as session:
            stmt = select(EventRow)
            if agent:
                stmt = stmt.where(EventRow.agent == agent)
            if task_key:
                stmt = stmt.where(EventRow.task_key == task_key)
            if objective_id is not None:
                stmt = stmt.where(EventRow.objective_id == objective_id)
            if category:
                stmt = stmt.where(EventRow.type.like(f"{category}.%"))
            if since_id is not None:
                stmt = stmt.where(EventRow.id > since_id)

            # Newest-first for the limit, then reversed, so `--limit 20` means the
            # twenty most recent rather than the twenty oldest.
            stmt = stmt.order_by(EventRow.id.desc())
            if limit:
                stmt = stmt.limit(limit)
            rows = list(session.scalars(stmt).all())
            rows.reverse()
            return [self._to_event(row) for row in rows]

    @staticmethod
    def _to_event(row: EventRow) -> Event:
        try:
            data = json.loads(row.data_json or "{}")
        except json.JSONDecodeError:
            data = {}
        try:
            event_type = EventType(row.type)
        except ValueError:
            # An event written by a newer version: keep it readable rather than
            # failing the whole timeline.
            event_type = EventType.SCHEDULER_STOPPED
        return Event(
            id=row.id,
            type=event_type,
            summary=row.summary,
            objective_id=row.objective_id,
            task_key=row.task_key,
            agent=row.agent,
            run_id=row.run_id,
            data=data if isinstance(data, dict) else {},
            at=row.created_at,
        )

    def latest_id(self) -> int:
        if self.db is None:
            return 0
        with self.db.session() as session:
            return int(session.scalar(select(EventRow.id).order_by(EventRow.id.desc())) or 0)

"""Data access for the message bus."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from agentos.db.models import Agent, Message, Task
from agentos.db.session import Database
from agentos.schemas.dto import MessageView
from agentos.schemas.enums import MessageStatus, MessageType


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MessageRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def _view(self, session, row: Message) -> MessageView:
        recipient = None
        if row.recipient_agent_id is not None:
            recipient = session.scalar(
                select(Agent.name).where(Agent.id == row.recipient_agent_id)
            )
        task_key = None
        if row.task_id is not None:
            task_key = session.scalar(select(Task.key).where(Task.id == row.task_id))
        return MessageView(
            id=row.id,
            sender=row.sender_name,
            recipient=recipient,
            task_key=task_key,
            message_type=MessageType(row.message_type),
            body=row.body,
            status=MessageStatus(row.status),
            created_at=row.created_at,
            delivered_at=row.delivered_at,
            read_at=row.read_at,
        )

    # -------------------------------------------------------------------- reads

    def list(
        self,
        recipient_agent_id: int | None = None,
        statuses: set[MessageStatus] | None = None,
        limit: int | None = None,
    ) -> list[MessageView]:
        with self.db.session() as session:
            stmt = select(Message).order_by(Message.id)
            if recipient_agent_id is not None:
                stmt = stmt.where(Message.recipient_agent_id == recipient_agent_id)
            if statuses:
                stmt = stmt.where(Message.status.in_([s.value for s in statuses]))
            if limit:
                stmt = stmt.order_by(Message.id.desc()).limit(limit)
            rows = list(session.scalars(stmt).all())
            if limit:
                rows.reverse()
            return [self._view(session, row) for row in rows]

    def pending_for(self, recipient_agent_id: int) -> list[MessageView]:
        """Messages not yet confirmed read, oldest first.

        Includes DELIVERED as well as PENDING: a message injected into a prompt
        whose run then crashed must be delivered again rather than lost.
        """
        return self.list(
            recipient_agent_id=recipient_agent_id,
            statuses={MessageStatus.PENDING, MessageStatus.DELIVERED},
        )

    def count_unread(self, recipient_agent_id: int) -> int:
        return len(self.pending_for(recipient_agent_id))

    # ------------------------------------------------------------------- writes

    def create(
        self,
        sender_name: str,
        body: str,
        recipient_agent_id: int | None = None,
        sender_agent_id: int | None = None,
        task_id: int | None = None,
        message_type: MessageType = MessageType.INFO,
    ) -> MessageView:
        with self.db.session() as session:
            row = Message(
                sender_name=sender_name,
                sender_agent_id=sender_agent_id,
                recipient_agent_id=recipient_agent_id,
                task_id=task_id,
                message_type=message_type.value,
                body=body,
                status=MessageStatus.PENDING.value,
            )
            session.add(row)
            session.flush()
            return self._view(session, row)

    def mark_delivered(self, message_ids: list[int]) -> None:
        """Record that these messages were injected into a prompt we sent."""
        if not message_ids:
            return
        with self.db.session() as session:
            rows = session.scalars(
                select(Message).where(Message.id.in_(message_ids))
            ).all()
            now = _utcnow()
            for row in rows:
                # Keep the first delivery time; a redelivery is not a new attempt.
                if row.delivered_at is None:
                    row.delivered_at = now
                row.status = MessageStatus.DELIVERED.value

    def mark_read(self, message_ids: list[int]) -> None:
        """Record that the receiving agent's run actually completed."""
        if not message_ids:
            return
        with self.db.session() as session:
            rows = session.scalars(
                select(Message).where(Message.id.in_(message_ids))
            ).all()
            now = _utcnow()
            for row in rows:
                row.status = MessageStatus.READ.value
                row.read_at = now

    def return_to_pending(self, message_ids: list[int]) -> None:
        """Undo delivery so a crashed or failed run does not swallow a message."""
        if not message_ids:
            return
        with self.db.session() as session:
            rows = session.scalars(
                select(Message).where(Message.id.in_(message_ids))
            ).all()
            for row in rows:
                if row.status != MessageStatus.READ.value:
                    row.status = MessageStatus.PENDING.value

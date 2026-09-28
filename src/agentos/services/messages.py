"""The message bus.

Agents never invoke each other. An agent asks, in its response block, for a
message to be sent; this service validates the recipient and stores it. Before
that recipient's next invocation its unread messages are injected into the
prompt.

Delivery is three-state on purpose:

    pending   -> stored, not yet shown to anyone
    delivered -> injected into a prompt that was actually sent
    read      -> the receiving agent's run finished successfully

If a Claude process dies between injection and completion, the message goes back
to pending and is injected again. Marking it read at injection time would lose
it.
"""

from __future__ import annotations

from agentos.config import Config
from agentos.db.session import Database
from agentos.prompts.task_prompt import InboxItem
from agentos.repositories.agents import AgentRepository
from agentos.repositories.messages import MessageRepository
from agentos.repositories.tasks import TaskRepository
from agentos.schemas.dto import MessageView
from agentos.schemas.enums import MessageStatus, MessageType

HUMAN_SENDER = "human"

# One agent cannot flood another's prompt.
MAX_INBOX_ITEMS = 20


class MessageValidationError(ValueError):
    """The message could not be accepted as written."""


class Delivery:
    """A batch of messages injected into one prompt, awaiting confirmation."""

    def __init__(self, items: list[InboxItem], message_ids: list[int]) -> None:
        self.items = items
        self.message_ids = message_ids

    def __bool__(self) -> bool:
        return bool(self.items)


class MessageService:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config
        self.messages = MessageRepository(db)
        self.agents = AgentRepository(db)
        self.tasks = TaskRepository(db)

    # ------------------------------------------------------------------ sending

    def send(
        self,
        sender: str,
        recipient: str,
        body: str,
        task_key: str | int | None = None,
        message_type: MessageType = MessageType.INFO,
    ) -> MessageView:
        """Store one message after validating both ends.

        The recipient must be a real agent. An agent cannot message itself, and
        cannot address somebody who does not exist.
        """
        clean_body = (body or "").strip()
        if not clean_body:
            raise MessageValidationError("message body must not be empty")

        recipient_view = self.agents.find(recipient)
        if recipient_view is None:
            known = ", ".join(a.name for a in self.agents.list()) or "(none registered)"
            raise MessageValidationError(
                f"unknown recipient {recipient!r}. Known agents: {known}"
            )

        sender_view = self.agents.find(sender)
        if sender_view is not None and sender_view.id == recipient_view.id:
            raise MessageValidationError(
                f"{sender} cannot send a message to itself"
            )

        task_id: int | None = None
        if task_key is not None:
            task = self.tasks.find(task_key)
            # An unknown task reference is not worth rejecting the message over;
            # drop the link and keep the content.
            task_id = task.id if task else None

        return self.messages.create(
            sender_name=sender_view.name if sender_view else sender,
            sender_agent_id=sender_view.id if sender_view else None,
            recipient_agent_id=recipient_view.id,
            body=clean_body,
            task_id=task_id,
            message_type=message_type,
        )

    def send_from_human(
        self, recipient: str, body: str, task_key: str | None = None
    ) -> MessageView:
        return self.send(HUMAN_SENDER, recipient, body, task_key)

    # ----------------------------------------------------------------- delivery

    def take_inbox(self, agent_name: str) -> Delivery:
        """Claim an agent's unread messages for injection into a prompt.

        Marks them delivered. The caller must afterwards call `confirm_read` on
        success or `release` on failure.
        """
        agent = self.agents.find(agent_name)
        if agent is None:
            return Delivery([], [])

        pending = self.messages.pending_for(agent.id)[:MAX_INBOX_ITEMS]
        if not pending:
            return Delivery([], [])

        items = [InboxItem(sender=m.sender, body=m.body) for m in pending]
        ids = [m.id for m in pending]
        self.messages.mark_delivered(ids)
        return Delivery(items, ids)

    def confirm_read(self, delivery: Delivery) -> None:
        """The receiving run completed, so the messages are genuinely consumed."""
        self.messages.mark_read(delivery.message_ids)

    def release(self, delivery: Delivery) -> None:
        """The run failed or died; make the messages deliverable again."""
        self.messages.return_to_pending(delivery.message_ids)

    # -------------------------------------------------------------------- reads

    def list_all(self, limit: int | None = None) -> list[MessageView]:
        return self.messages.list(limit=limit)

    def list_for(
        self, agent_name: str, unread_only: bool = False
    ) -> list[MessageView]:
        agent = self.agents.get(agent_name)
        statuses = (
            {MessageStatus.PENDING, MessageStatus.DELIVERED} if unread_only else None
        )
        return self.messages.list(recipient_agent_id=agent.id, statuses=statuses)

    def unread_counts(self) -> dict[str, int]:
        return {
            agent.name: self.messages.count_unread(agent.id)
            for agent in self.agents.list()
        }

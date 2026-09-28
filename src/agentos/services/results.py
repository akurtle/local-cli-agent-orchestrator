"""Turns an agent's raw reply into validated state changes.

This is the trust boundary. Claude proposes; this module decides what is
actually allowed and applies only that. Nothing here executes anything, and a
malformed or hostile response can at worst be rejected.

Rules:
  * a response that will not parse changes no task state beyond being recorded
  * a message to an unknown agent is dropped with a reason, not invented
  * a requested task for an unknown agent or role is rejected
  * a requested task may only depend on tasks that already exist
"""

from __future__ import annotations

from agentos.config import Config
from agentos.db.session import Database
from agentos.repositories.agents import AgentRepository
from agentos.schemas.dto import ResultOutcome, TaskView
from agentos.schemas.enums import MessageType
from agentos.schemas.responses import (
    AgentResponse,
    RequestedTask,
    ResponseParseError,
    parse_agent_response,
)
from agentos.services.messages import MessageService, MessageValidationError
from agentos.services.tasks import TaskService, TaskValidationError

# An agent may not queue unlimited work in one turn.
MAX_REQUESTED_TASKS = 10
MAX_MESSAGES = 10


class ResultProcessor:
    def __init__(
        self,
        db: Database,
        config: Config,
        task_service: TaskService,
        message_service: MessageService,
    ) -> None:
        self.db = db
        self.config = config
        self.tasks = task_service
        self.messages = message_service
        self.agents = AgentRepository(db)

    # ------------------------------------------------------------------- parsing

    def parse(self, text: str) -> tuple[AgentResponse | None, str | None]:
        """Parse a reply. Returns (response, error) -- never raises."""
        try:
            return parse_agent_response(text), None
        except ResponseParseError as exc:
            return None, str(exc)

    # ------------------------------------------------------------------ applying

    def process(
        self,
        text: str,
        task: TaskView | None = None,
        agent_name: str = "",
        repaired: bool = False,
    ) -> ResultOutcome:
        """Validate a reply and apply whatever it legitimately asks for."""
        response, error = self.parse(text)
        if response is None:
            return ResultOutcome(parsed=False, parse_error=error, repaired=repaired)

        rejected: list[str] = []
        sent = self._deliver_messages(response, agent_name, task, rejected)
        created = self._create_requested_tasks(response, agent_name, task, rejected)

        return ResultOutcome(
            parsed=True,
            status=response.status,
            summary=response.summary,
            files_changed=response.files_changed,
            blockers=response.blockers,
            interfaces=response.interfaces,
            decisions=response.decisions,
            warnings=response.warnings,
            messages_sent=sent,
            tasks_created=created,
            rejected=rejected,
            repaired=repaired,
        )

    def _deliver_messages(
        self,
        response: AgentResponse,
        agent_name: str,
        task: TaskView | None,
        rejected: list[str],
    ) -> int:
        sent = 0
        for outbound in response.messages[:MAX_MESSAGES]:
            try:
                self.messages.send(
                    sender=agent_name or "system",
                    recipient=outbound.to,
                    body=outbound.message,
                    task_key=task.key if task else None,
                    message_type=outbound.message_type,
                )
                sent += 1
            except MessageValidationError as exc:
                rejected.append(f"message to {outbound.to!r}: {exc}")

        dropped = len(response.messages) - MAX_MESSAGES
        if dropped > 0:
            rejected.append(f"{dropped} further message(s) dropped: per-turn limit")
        return sent

    def _resolve_requested_agent(self, requested: RequestedTask) -> str:
        """Map a requested task onto a real agent name.

        Prefers an explicit agent name; otherwise picks the single agent holding
        that role. An ambiguous or unknown target is refused rather than guessed.
        """
        if requested.agent_name:
            agent = self.agents.find(requested.agent_name)
            if agent is None:
                raise TaskValidationError(
                    f"unknown agent {requested.agent_name!r}"
                )
            return agent.name

        role = (requested.agent_role or "").strip()
        candidates = self.agents.find_by_role(role)
        if not candidates:
            known = sorted({a.role for a in self.agents.list()})
            raise TaskValidationError(
                f"no agent has role {role!r}. Known roles: {', '.join(known) or 'none'}"
            )
        if len(candidates) > 1:
            names = ", ".join(a.name for a in candidates)
            raise TaskValidationError(
                f"role {role!r} is ambiguous ({names}); name the agent explicitly"
            )
        return candidates[0].name

    def _create_requested_tasks(
        self,
        response: AgentResponse,
        agent_name: str,
        task: TaskView | None,
        rejected: list[str],
    ) -> list[str]:
        created: list[str] = []
        for requested in response.requested_tasks[:MAX_REQUESTED_TASKS]:
            label = requested.title[:60]
            try:
                target = self._resolve_requested_agent(requested)
                # Only depend on tasks that already exist; an agent cannot invent
                # a prerequisite. The requesting task is a sensible default so
                # follow-up work does not run before the work it reacts to.
                depends_on = [
                    key for key in requested.depends_on if self.tasks.tasks.find(key)
                ]
                unknown = set(requested.depends_on) - set(depends_on)
                if unknown:
                    rejected.append(
                        f"task {label!r}: ignored unknown dependencies "
                        + ", ".join(sorted(unknown))
                    )
                if task is not None and task.key not in depends_on:
                    depends_on.append(task.key)

                new_task = self.tasks.create_task(
                    title=requested.title,
                    description=requested.description,
                    agent=target,
                    depends_on=depends_on,
                    acceptance_criteria=requested.acceptance_criteria,
                    prefix=self._prefix_for(task),
                    created_by=agent_name or None,
                    objective_id=task.objective_id if task else None,
                )
                created.append(new_task.key)
            except (TaskValidationError, ValueError) as exc:
                rejected.append(f"task {label!r}: {exc}")

        dropped = len(response.requested_tasks) - MAX_REQUESTED_TASKS
        if dropped > 0:
            rejected.append(f"{dropped} further task(s) dropped: per-turn limit")
        return created

    @staticmethod
    def _prefix_for(task: TaskView | None) -> str:
        """Keep follow-up work in the same key family as its parent."""
        if task is None:
            return "T"
        head = task.key.rsplit("-", 1)[0]
        return head or "T"


def build_repair_prompt(raw_text: str, error: str) -> str:
    """Ask an agent to restate its last reply in the required format.

    Deliberately narrow: it asks only for the response block, so a repair turn
    cannot smuggle in new work or redo the task.
    """
    from agentos.prompts.loader import response_contract

    excerpt = (raw_text or "").strip()[-1500:]
    return (
        "Your previous reply could not be parsed.\n\n"
        f"Problem: {error}\n\n"
        "Do NOT do any more work and do NOT change any files. Reply with the "
        "response block for the work you already did, and nothing else.\n\n"
        f"For reference, the end of your previous reply was:\n---\n{excerpt}\n---\n\n"
        + response_contract()
    )


__all__ = ["MessageType", "ResultProcessor", "build_repair_prompt"]

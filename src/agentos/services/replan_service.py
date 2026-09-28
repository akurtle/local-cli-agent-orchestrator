"""Asks the manager to repair a plan, validates the answer, applies what is legal.

Same three-step shape as objective planning, for the same reason: a human can sit
between proposing and applying.

    propose  -> run the manager, parse, validate against the live graph
    apply    -> persist only the accepted operations
    reject   -> record that nothing was changed

Automatic replanning is deliberately conservative. It fires on specific triggers,
never after every task, and it is off unless configured.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentos.config import Config
from agentos.db.session import Database
from agentos.prompts.replan_prompt import build_replan_prompt
from agentos.repositories.objectives import ObjectiveRepository
from agentos.schemas.dto import TaskView
from agentos.schemas.enums import TaskStatus
from agentos.schemas.replan import (
    ReplanParseError,
    ReplanProposal,
    ReplanTrigger,
    parse_replan,
)
from agentos.services.agents import AgentService
from agentos.services.events import EventBus, EventType
from agentos.services.messages import MessageService
from agentos.services.replanner import ReplanOutcome, Replanner, ReplanValidation
from agentos.services.tasks import TaskService

REPLAN_TIMEOUT = 420.0


@dataclass
class ReplanRequest:
    """The situation handed to the manager."""

    objective_id: int | None
    trigger: ReplanTrigger
    failed_task: TaskView | None = None
    failure_summary: str = ""
    reason: str = ""


@dataclass
class ReplanProposalResult:
    """A proposal, not yet applied."""

    request: ReplanRequest
    proposal: ReplanProposal | None
    validation: ReplanValidation | None
    raw_text: str = ""
    parse_error: str | None = None
    run_id: int | None = None

    @property
    def ok(self) -> bool:
        return (
            self.proposal is not None
            and self.validation is not None
            and self.validation.has_anything
        )

    @property
    def errors(self) -> list[str]:
        if self.parse_error:
            return [self.parse_error]
        return list(self.validation.errors) if self.validation else []


class ReplanService:
    def __init__(
        self,
        db: Database,
        config: Config,
        agent_service: AgentService,
        task_service: TaskService,
        event_bus: EventBus | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.agents = agent_service
        self.tasks = task_service
        self.events = event_bus or EventBus(db)
        self.messages = MessageService(db, config)
        self.objectives = ObjectiveRepository(db)
        self.replanner = Replanner(db, config, task_service, self.events)

    # ------------------------------------------------------------------ triggers

    def should_auto_replan(self, trigger: ReplanTrigger) -> bool:
        """Whether this trigger is configured to replan on its own.

        Defaults are off for everything except an explicit request, because an
        unattended replan loop is an expensive way to be wrong.
        """
        section = self.config.orchestrator.auto_replan
        return bool(getattr(section, trigger.value, False))

    def manager_name(self) -> str:
        from agentos.services.objectives import ObjectiveService

        return ObjectiveService(
            self.db, self.config, self.agents, self.tasks
        ).manager_name()

    # ------------------------------------------------------------------ proposing

    async def propose(
        self,
        request: ReplanRequest,
        manager: str | None = None,
        timeout_seconds: float | None = None,
    ) -> ReplanProposalResult:
        """Ask the manager what to change. Writes nothing to the graph."""
        manager_name = manager or self.manager_name()
        all_tasks = self.tasks.list_tasks()

        objective_id = request.objective_id
        scoped = [
            t
            for t in all_tasks
            if objective_id is None or t.objective_id == objective_id
        ]

        prompt = build_replan_prompt(
            objective=self._objective_text(objective_id),
            trigger=request.trigger,
            roster=self.agents.roster(),
            failed=request.failed_task,
            failure_summary=request.failure_summary,
            completed=[t for t in scoped if t.status is TaskStatus.COMPLETED],
            blocked=[t for t in scoped if t.status is TaskStatus.BLOCKED],
            outstanding=[
                t
                for t in scoped
                if t.status in {TaskStatus.PENDING, TaskStatus.READY, TaskStatus.FAILED}
            ],
            messages=self._recent_messages(),
            reason=request.reason,
        )

        self.events.emit(
            EventType.OBJECTIVE_PLANNED,
            summary=f"replan requested: {request.trigger.value}",
            objective_id=objective_id,
            task_key=request.failed_task.key if request.failed_task else None,
            trigger=request.trigger.value,
        )

        outcome = await self.agents.run_agent(
            manager_name,
            prompt,
            timeout_seconds=timeout_seconds or REPLAN_TIMEOUT,
            objective_id=objective_id,
        )

        if not outcome.ok:
            return ReplanProposalResult(
                request=request,
                proposal=None,
                validation=None,
                raw_text=outcome.text,
                parse_error=outcome.error or "the manager run failed",
                run_id=outcome.run_id,
            )

        try:
            proposal = parse_replan(outcome.text)
        except ReplanParseError as exc:
            return ReplanProposalResult(
                request=request,
                proposal=None,
                validation=None,
                raw_text=outcome.text,
                parse_error=str(exc),
                run_id=outcome.run_id,
            )

        return ReplanProposalResult(
            request=request,
            proposal=proposal,
            validation=self.replanner.validate(proposal),
            raw_text=outcome.text,
            run_id=outcome.run_id,
        )

    def _objective_text(self, objective_id: int | None) -> str:
        if objective_id is None:
            return ""
        try:
            return self.objectives.get(objective_id).description
        except Exception:
            return ""

    def _recent_messages(self) -> list[str]:
        return [
            f"[{m.sender} -> {m.recipient or '-'}] {m.body}"
            for m in self.messages.list_all(limit=8)
        ]

    # ------------------------------------------------------------------ applying

    def apply(self, result: ReplanProposalResult, prefix: str = "FIX") -> ReplanOutcome:
        """Apply the accepted operations."""
        if not result.ok or result.proposal is None:
            raise ValueError(
                "cannot apply this replan: " + "; ".join(result.errors or ["nothing accepted"])
            )
        outcome = self.replanner.apply(
            result.proposal,
            result.validation,
            objective_id=result.request.objective_id,
            prefix=prefix,
        )
        self.events.emit(
            EventType.OBJECTIVE_APPROVED,
            summary=(
                f"replan applied: {len(outcome.created)} created, "
                f"{len(outcome.dependencies)} dependency change(s)"
            ),
            objective_id=result.request.objective_id,
            created=outcome.created,
            cancelled=outcome.cancelled,
            retried=outcome.retried,
        )
        return outcome

    def reject(self, result: ReplanProposalResult) -> None:
        self.events.emit(
            EventType.OBJECTIVE_REJECTED,
            summary="replan rejected; the graph was not changed",
            objective_id=result.request.objective_id,
        )

    # -------------------------------------------------------- automatic entry point

    def request_for_failure(self, task: TaskView) -> ReplanRequest:
        """Build a request from a task that just failed or blocked."""
        trigger = (
            ReplanTrigger.BLOCKER
            if task.status is TaskStatus.BLOCKED or task.needs_intervention
            else ReplanTrigger.TASK_FAILED
        )
        return ReplanRequest(
            objective_id=task.objective_id,
            trigger=trigger,
            failed_task=task,
            failure_summary=task.error or "",
        )

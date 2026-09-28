"""Objectives: ask the manager to plan, validate the plan, then persist it.

The flow is deliberately three separate steps so a human can sit between the
second and third:

    propose  -> run the manager, parse and validate; writes nothing but the
                objective row and the raw plan
    approve  -> persist the validated plan as real tasks
    reject   -> record that the objective was abandoned

Completion is computed from task state, never asked of an agent.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentos.config import Config
from agentos.db.session import Database
from agentos.prompts.plan_prompt import build_plan_prompt
from agentos.repositories.objectives import ObjectiveNotFound, ObjectiveRepository
from agentos.repositories.tasks import TaskRepository
from agentos.schemas.dto import ObjectiveView, TaskView
from agentos.schemas.enums import ObjectiveStatus, TaskStatus
from agentos.schemas.plan import ManagerPlan, PlanParseError, parse_manager_plan
from agentos.services.agents import AgentService
from agentos.services.planner import Planner, PlanValidation
from agentos.services.tasks import TaskService
from agentos.services.verification import VerificationService

# Planning reads the repository, so it needs more headroom than a normal turn.
DEFAULT_PLANNING_TIMEOUT = 600.0


@dataclass
class Proposal:
    """A plan the manager produced, not yet persisted as tasks."""

    objective: ObjectiveView
    plan: ManagerPlan | None
    validation: PlanValidation | None
    raw_text: str = ""
    parse_error: str | None = None
    run_id: int | None = None

    @property
    def ok(self) -> bool:
        return (
            self.plan is not None
            and self.validation is not None
            and self.validation.ok
        )

    @property
    def errors(self) -> list[str]:
        if self.parse_error:
            return [self.parse_error]
        return list(self.validation.errors) if self.validation else []


class ObjectiveService:
    def __init__(
        self,
        db: Database,
        config: Config,
        agent_service: AgentService,
        task_service: TaskService,
    ) -> None:
        self.db = db
        self.config = config
        self.agents = agent_service
        self.tasks = task_service
        self.objectives = ObjectiveRepository(db)
        self.task_repo = TaskRepository(db)
        self.planner = Planner(db, config, task_service)
        self.verification = VerificationService(db, config)

    # ------------------------------------------------------------------ planning

    def manager_name(self) -> str:
        """The agent that plans.

        The planning role is configurable (`orchestrator.manager_role`), so a
        roster that calls it `lead` or `architect` works without code changes.
        Prefers an agent with that role, falls back to one literally named after
        it, and raises rather than guessing when the choice is ambiguous.
        """
        role = self.config.orchestrator.manager_role
        candidates = self.agents.agents.find_by_role(role)
        if len(candidates) == 1:
            return candidates[0].name
        if len(candidates) > 1:
            names = ", ".join(a.name for a in candidates)
            raise ValueError(
                f"several agents have the {role!r} role ({names}); "
                "use --agent to choose one"
            )
        direct = self.agents.agents.find(role)
        if direct is not None:
            return direct.name
        known = ", ".join(sorted(self.config.role_names())) or "none"
        raise ValueError(
            f"no agent has the {role!r} role, so there is nobody to plan with. "
            f"Configured roles: {known}. Add one, set "
            "orchestrator.manager_role, or pass --agent."
        )

    async def propose(
        self,
        description: str,
        manager: str | None = None,
        timeout_seconds: float | None = None,
        on_event=None,
    ) -> Proposal:
        """Create an objective and ask the manager to plan it.

        Only the objective row is written here. No task is created until the plan
        has been validated and approved.
        """
        manager_name = manager or self.manager_name()
        objective = self.objectives.create(description.strip())

        prompt = build_plan_prompt(
            objective=objective.description,
            roster=self.agents.roster(),
            project_name=self.config.project.name,
            existing_tasks=[
                t.key
                for t in self.task_repo.list()
                if not t.status.is_terminal
            ],
        )

        outcome = await self.agents.run_agent(
            manager_name,
            prompt,
            timeout_seconds=timeout_seconds or DEFAULT_PLANNING_TIMEOUT,
            on_event=on_event,
        )

        if not outcome.ok:
            self.objectives.set_status(objective.id, ObjectiveStatus.FAILED)
            return Proposal(
                objective=self.objectives.get(objective.id),
                plan=None,
                validation=None,
                raw_text=outcome.text,
                parse_error=outcome.error or "the manager run failed",
                run_id=outcome.run_id,
            )

        try:
            plan = parse_manager_plan(outcome.text)
        except PlanParseError as exc:
            self.objectives.set_status(objective.id, ObjectiveStatus.FAILED)
            return Proposal(
                objective=self.objectives.get(objective.id),
                plan=None,
                validation=None,
                raw_text=outcome.text,
                parse_error=str(exc),
                run_id=outcome.run_id,
            )

        validation = self.planner.validate(plan)
        self.objectives.set_status(
            objective.id,
            ObjectiveStatus.AWAITING_APPROVAL if validation.ok else ObjectiveStatus.FAILED,
        )
        return Proposal(
            objective=self.objectives.get(objective.id),
            plan=plan,
            validation=validation,
            raw_text=outcome.text,
            run_id=outcome.run_id,
        )

    # ------------------------------------------------------------------ decisions

    def approve(self, proposal: Proposal, created_by: str = "manager") -> list[TaskView]:
        """Persist a validated plan as real tasks."""
        if not proposal.ok:
            raise ValueError(
                "cannot approve an invalid plan: " + "; ".join(proposal.errors)
            )
        assert proposal.plan is not None
        return self.planner.apply(
            objective=proposal.objective,
            plan=proposal.plan,
            validation=proposal.validation,
            created_by=created_by,
        )

    def reject(self, proposal: Proposal) -> ObjectiveView:
        return self.objectives.set_status(
            proposal.objective.id, ObjectiveStatus.CANCELLED
        )

    # ----------------------------------------------------------------- completion

    def refresh_completion(self, objective_id: int) -> ObjectiveView:
        """Recompute an objective's status from its tasks.

        Deterministic, and never asked of an agent. Complete requires every task
        completed AND nothing failing verification on its latest attempt; failed
        when nothing can progress; otherwise active.
        """
        objective = self.objectives.get(objective_id)
        if objective.status in {ObjectiveStatus.PLANNING, ObjectiveStatus.AWAITING_APPROVAL}:
            return objective

        tasks = [t for t in self.task_repo.list() if t.objective_id == objective_id]
        if not tasks:
            return objective

        statuses = {t.status for t in tasks}

        unfinished = {
            TaskStatus.PENDING,
            TaskStatus.READY,
            TaskStatus.RUNNING,
            TaskStatus.REVIEW,
            TaskStatus.AGENT_DONE,
            TaskStatus.VERIFYING,
        }
        if statuses & unfinished:
            return self.objectives.set_status(objective_id, ObjectiveStatus.ACTIVE)

        if statuses == {TaskStatus.COMPLETED}:
            # Every task completed. Completion still requires that nothing failed
            # verification on its latest attempt: a task marked completed while
            # its checks disagreed would make the objective a lie.
            unverified = [
                t.key
                for t in tasks
                if not self.verification.passed_for(t.key)
            ]
            if unverified:
                return self.objectives.set_status(
                    objective_id, ObjectiveStatus.FAILED
                )
            return self.objectives.set_status(objective_id, ObjectiveStatus.COMPLETED)

        # Nothing left to run, and not everything succeeded.
        if statuses & {
            TaskStatus.FAILED,
            TaskStatus.FAILED_VERIFICATION,
            TaskStatus.BLOCKED,
        }:
            return self.objectives.set_status(objective_id, ObjectiveStatus.FAILED)
        if statuses == {TaskStatus.CANCELLED}:
            return self.objectives.set_status(objective_id, ObjectiveStatus.CANCELLED)
        return self.objectives.set_status(objective_id, ObjectiveStatus.COMPLETED)

    def refresh_all(self) -> list[ObjectiveView]:
        return [
            self.refresh_completion(o.id)
            for o in self.objectives.list()
            if not o.status.is_terminal
        ]

    def list_objectives(self) -> list[ObjectiveView]:
        return self.objectives.list()

    def get_objective(self, objective_id: int) -> ObjectiveView:
        return self.objectives.get(objective_id)


__all__ = ["ObjectiveNotFound", "ObjectiveService", "Proposal"]

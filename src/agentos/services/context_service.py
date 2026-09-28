"""Assembles real context for a real agent run.

`ContextBuilder` is pure; this is the part that knows where the pieces live. It
gathers identity, memories, objective, task, handoffs and inbox, and hands them
to the builder. Splitting it this way means the layering rules stay testable
without a database, and this module stays a thin gatherer.

`agentctl context <agent>` calls exactly this, so what a developer inspects is
what an agent would actually receive.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentos.config import Config
from agentos.db.session import Database
from agentos.prompts.task_prompt import InboxItem, build_task_prompt
from agentos.repositories.objectives import ObjectiveRepository
from agentos.schemas.dto import AgentView, HandoffView, TaskView
from agentos.services.context import ContextBuilder, ContextBundle
from agentos.services.memory import MemoryService
from agentos.services.tasks import TaskService

DEFAULT_INSTRUCTION = (
    "Do the work now. End your reply with the response block described in your "
    "instructions."
)

NO_TASK_INSTRUCTION = (
    "Answer the request above. End your reply with the response block described "
    "in your instructions."
)


@dataclass
class AssembledContext:
    """A bundle plus the handoffs it consumed, which must be confirmed later."""

    bundle: ContextBundle
    handoffs: list[HandoffView] = field(default_factory=list)

    @property
    def prompt(self) -> str:
        return self.bundle.render()


class ContextService:
    def __init__(
        self,
        db: Database,
        config: Config,
        task_service: TaskService,
        memory_service: MemoryService | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.tasks = task_service
        self.memory = memory_service or MemoryService(db, config)
        self.objectives = ObjectiveRepository(db)
        self.builder = ContextBuilder(budgets=config.context.budgets)

    # ------------------------------------------------------------------ gather

    def identity_text(self, agent: AgentView) -> str:
        """Stable facts about who this agent is.

        Kept separate from the role brief, which lives in the system prompt: this
        layer is about identity and current assignment, not instructions.
        """
        lines = [
            f"You are `{agent.name}`, the {agent.role} agent.",
        ]
        if agent.description:
            lines.append(agent.description)
        lines.append(f"Runtime: {agent.runtime}")
        if agent.branch_name:
            lines.append(
                f"You are working in an isolated worktree on branch "
                f"`{agent.branch_name}`. Changes you make are not visible to "
                "other agents until they are integrated."
            )
        else:
            lines.append(
                "You are working in the shared project directory, so other "
                "agents may be editing at the same time."
            )
        return "\n".join(lines)

    def objective_description(self, objective_id: int | None) -> str:
        if objective_id is None:
            return ""
        try:
            return self.objectives.get(objective_id).description
        except Exception:
            return ""

    def task_body(self, task: TaskView | None, inbox: list[InboxItem]) -> str:
        """The task itself, reusing the existing builder.

        The inbox is passed separately as its own layer, so it is excluded here
        to avoid showing the same messages twice.
        """
        if task is None:
            return ""
        return build_task_prompt(
            task=task,
            dependencies=self.tasks.completed_dependencies(task),
            inbox=None,
            retry_of=task.error if task.attempts else None,
            standalone=False,
        )

    # ------------------------------------------------------------------ assemble

    def assemble(
        self,
        agent: AgentView,
        task: TaskView | None = None,
        inbox: list[InboxItem] | None = None,
        instruction: str | None = None,
    ) -> AssembledContext:
        """Build the context for one run.

        Reads handoffs but does not consume them: the caller confirms them only
        after a successful run, so a crash cannot swallow one. That also makes
        this safe to call for inspection.
        """
        objective_id = task.objective_id if task else None
        items = inbox or []

        handoffs = self.memory.take_handoffs(agent.name)

        bundle = self.builder.build(
            agent=agent,
            identity=self.identity_text(agent),
            role_memories=self.memory.recall_agent(agent.name),
            project_memories=self.memory.recall_project(),
            objective_description=self.objective_description(objective_id),
            objective_memories=self.memory.recall_objective(objective_id),
            task=task,
            task_body=self.task_body(task, items),
            handoffs=handoffs,
            inbox=[f"[{item.sender}]\n{item.body.strip()}" for item in items],
            instruction=instruction
            or (DEFAULT_INSTRUCTION if task else NO_TASK_INSTRUCTION),
        )
        return AssembledContext(bundle=bundle, handoffs=handoffs)

    def preview(
        self, agent: AgentView, task: TaskView | None = None
    ) -> AssembledContext:
        """What would be injected, guaranteed side-effect free.

        `agentctl context` uses this: inspecting a prompt must not change what
        the next run sees, or looking would alter behaviour.
        """
        return self.assemble(agent=agent, task=task)

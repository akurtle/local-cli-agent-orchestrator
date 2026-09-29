"""The data the TUI renders, gathered in one place.

Deliberately separate from any widget. The spec's rule for phase 12 is that no
application logic lives in the interface, so the TUI's only job is to display a
`Snapshot`. That also makes the whole view testable without starting Textual.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func

from agentos.config import Config
from agentos.db.session import Database
from agentos.paths import ProjectPaths
from agentos.schemas.dto import AgentView, MessageView, ObjectiveView, TaskView
from agentos.schemas.enums import TaskStatus
from agentos.services.agents import AgentService
from agentos.services.messages import MessageService
from agentos.services.objectives import ObjectiveService
from agentos.services.tasks import TaskService
from agentos.tui.attention import (
    AttentionItem,
    Denial,
    PendingCommand,
    attention_items,
    parse_denials,
)

MAX_MESSAGES = 30
MAX_RUNS = 20


@dataclass(frozen=True)
class RunSummary:
    """One recorded invocation, flattened for display."""

    id: int
    agent: str
    task_key: str | None
    status: str
    started_at: str
    duration: str
    text: str


@dataclass(frozen=True)
class Snapshot:
    """Everything the TUI shows, as of one moment."""

    project: str
    agents: list[AgentView] = field(default_factory=list)
    tasks: list[TaskView] = field(default_factory=list)
    objectives: list[ObjectiveView] = field(default_factory=list)
    messages: list[MessageView] = field(default_factory=list)
    runs: list[RunSummary] = field(default_factory=list)
    attention: list[AttentionItem] = field(default_factory=list)
    """What is waiting on a human, most urgent first."""

    @property
    def completed(self) -> int:
        return sum(1 for t in self.tasks if t.status is TaskStatus.COMPLETED)

    @property
    def blocked(self) -> list[TaskView]:
        return [t for t in self.tasks if t.status is TaskStatus.BLOCKED]

    @property
    def failed(self) -> list[TaskView]:
        return [t for t in self.tasks if t.status is TaskStatus.FAILED]

    @property
    def active_objective(self) -> ObjectiveView | None:
        for objective in reversed(self.objectives):
            if not objective.status.is_terminal:
                return objective
        return self.objectives[-1] if self.objectives else None

    def runs_for(self, agent: str) -> list[RunSummary]:
        return [r for r in self.runs if r.agent == agent]

    def task(self, key: str) -> TaskView | None:
        for candidate in self.tasks:
            if candidate.key == key:
                return candidate
        return None

    def agent(self, name: str) -> AgentView | None:
        for candidate in self.agents:
            if candidate.name == name:
                return candidate
        return None


class SnapshotReader:
    """Builds a Snapshot from the same services the CLI uses."""

    def __init__(self, db: Database, config: Config, paths: ProjectPaths) -> None:
        self.db = db
        self.config = config
        self.paths = paths
        self.agents = AgentService(db, config, _NullRuntime(), paths.root)
        self.tasks = TaskService(db, config)
        self.messages = MessageService(db, config)
        self.objectives = ObjectiveService(db, config, self.agents, self.tasks)
        # Denials per run never change once the run is recorded, and a run's
        # stdout can be large, so each is parsed once.
        self._denials: dict[int, list[Denial]] = {}

    # The only writes the dashboard makes. Both are status changes the CLI
    # offers as `agentctl task unblock|retry`; neither starts an agent.

    def unblock(self, key: str) -> TaskView:
        return self.tasks.unblock(key)

    def retry(self, key: str) -> TaskView:
        return self.tasks.retry(key)

    def read(self) -> Snapshot:
        self.agents.sync_from_config()
        self.tasks.refresh_readiness()
        self.objectives.refresh_all()

        tasks = self.tasks.list_tasks()
        objectives = self.objectives.list_objectives()
        return Snapshot(
            project=self.config.project.name,
            agents=self.agents.list_agents(),
            tasks=tasks,
            objectives=objectives,
            messages=self.messages.list_all(limit=MAX_MESSAGES),
            runs=self._runs(),
            attention=attention_items(
                objectives,
                tasks,
                self._pending_commands(),
                self._latest_denials(tasks),
            ),
        )

    def _pending_commands(self) -> list[PendingCommand]:
        from agentos.services.command_service import CommandService

        return [
            PendingCommand(
                id=r.id,
                agent=r.agent,
                task_key=r.task_key,
                command=r.spelled,
                rule=r.denied_reason,
            )
            for r in CommandService(self.db, self.config).pending_approvals()
        ]

    def _latest_denials(self, tasks: list[TaskView]) -> dict[str, list[Denial]]:
        """What the agent's CLI refused on the last run of each stuck task.

        Usually the real reason a task is blocked: a command nobody could
        approve in a headless run.
        """
        from agentos.db.models import Run

        stuck = {
            t.id: t.key
            for t in tasks
            if t.status
            in {TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.FAILED_VERIFICATION}
        }
        if not stuck:
            return {}
        found: dict[str, list[Denial]] = {}
        with self.db.session() as session:
            latest = (
                session.query(Run.task_id, func.max(Run.id))
                .filter(Run.task_id.in_(stuck))
                .group_by(Run.task_id)
                .all()
            )
            for task_id, run_id in latest:
                if run_id not in self._denials:
                    stdout = session.get(Run, run_id).stdout
                    self._denials[run_id] = parse_denials(stdout)
                if self._denials[run_id]:
                    found[stuck[task_id]] = self._denials[run_id]
        return found

    def _runs(self) -> list[RunSummary]:
        from agentos.db.models import Agent, Run

        with self.db.session() as session:
            rows = (
                session.query(Run, Agent.name)
                .outerjoin(Agent, Run.agent_id == Agent.id)
                .order_by(Run.id.desc())
                .limit(MAX_RUNS)
                .all()
            )
            keys = {t.id: t.key for t in self.tasks.list_tasks()}
            summaries = []
            for run, agent_name in rows:
                duration = "-"
                if run.finished_at and run.started_at:
                    seconds = (run.finished_at - run.started_at).total_seconds()
                    duration = f"{seconds:.1f}s"
                summaries.append(
                    RunSummary(
                        id=run.id,
                        agent=agent_name or "(unknown)",
                        task_key=keys.get(run.task_id) if run.task_id else None,
                        status=run.status,
                        started_at=run.started_at.strftime("%H:%M:%S"),
                        duration=duration,
                        text=(run.result_text or run.error or "").strip(),
                    )
                )
        return summaries


class _NullRuntime:
    """A runtime that refuses to run anything.

    The TUI is a viewer. Giving it a real runtime would make it possible to spend
    usage by accident from a read-only screen.
    """

    name = "viewer"

    def preflight(self) -> dict[str, str]:
        return {"runtime": self.name}

    async def run(self, request, on_event=None):
        raise RuntimeError("the TUI is read-only; use `agentctl work` to run agents")

    async def resume(self, session_id, prompt, on_event=None, **overrides):
        raise RuntimeError("the TUI is read-only; use `agentctl work` to run agents")

    @staticmethod
    def is_stale_session(result) -> bool:
        return False

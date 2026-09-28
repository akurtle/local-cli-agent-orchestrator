"""Run metrics, computed from what is already stored.

Nothing is aggregated as it happens: totals are derived on demand from tasks,
runs, commands and events. That keeps a single source of truth, and means a
counter can never drift from the rows it claims to count.

Deliberately simple arithmetic. Anything resembling analytics belongs outside
this project.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select

from agentos.db.models import CommandRun, Denial, Event, Run, Task
from agentos.db.session import Database
from agentos.schemas.enums import TaskStatus


@dataclass(frozen=True)
class AgentMetrics:
    agent: str
    runs: int = 0
    failures: int = 0
    busy_seconds: float = 0.0
    cost_usd: float = 0.0
    commands: int = 0
    denials: int = 0

    @property
    def success_rate(self) -> float | None:
        if not self.runs:
            return None
        return (self.runs - self.failures) / self.runs


@dataclass(frozen=True)
class Metrics:
    tasks_total: int = 0
    tasks_by_status: dict[str, int] = field(default_factory=dict)
    task_seconds: float = 0.0
    retries: int = 0
    runs: int = 0
    run_failures: int = 0
    run_seconds: float = 0.0
    cost_usd: float = 0.0
    commands: int = 0
    commands_denied: int = 0
    capability_denials: int = 0
    events: int = 0
    agents: list[AgentMetrics] = field(default_factory=list)

    @property
    def completed(self) -> int:
        return self.tasks_by_status.get(TaskStatus.COMPLETED.value, 0)

    @property
    def failed(self) -> int:
        return self.tasks_by_status.get(TaskStatus.FAILED.value, 0)

    @property
    def blocked(self) -> int:
        return self.tasks_by_status.get(TaskStatus.BLOCKED.value, 0)

    @property
    def mean_task_seconds(self) -> float | None:
        finished = self.completed + self.failed
        if not finished or not self.task_seconds:
            return None
        return self.task_seconds / finished

    @property
    def mean_run_seconds(self) -> float | None:
        if not self.runs or not self.run_seconds:
            return None
        return self.run_seconds / self.runs


class MetricsService:
    def __init__(self, db: Database) -> None:
        self.db = db

    def collect(self) -> Metrics:
        with self.db.session() as session:
            by_status: dict[str, int] = {}
            for status, count in session.execute(
                select(Task.status, func.count(Task.id)).group_by(Task.status)
            ).all():
                by_status[status] = int(count)

            task_seconds = 0.0
            retries = 0
            for started, completed, attempts in session.execute(
                select(Task.started_at, Task.completed_at, Task.attempts)
            ).all():
                if started and completed:
                    task_seconds += max(0.0, (completed - started).total_seconds())
                # attempts counts failures, so the retries are attempts beyond
                # the first.
                retries += max(0, int(attempts or 0) - 1) if attempts else 0

            runs = 0
            run_failures = 0
            run_seconds = 0.0
            cost = 0.0
            per_agent: dict[str, dict] = {}
            for agent_id, status, started, finished, run_cost in session.execute(
                select(Run.agent_id, Run.status, Run.started_at, Run.finished_at, Run.cost_usd)
            ).all():
                runs += 1
                seconds = (
                    max(0.0, (finished - started).total_seconds())
                    if started and finished
                    else 0.0
                )
                run_seconds += seconds
                cost += float(run_cost or 0.0)
                failed = status != "succeeded"
                run_failures += 1 if failed else 0

                bucket = per_agent.setdefault(
                    agent_id,
                    {"runs": 0, "failures": 0, "seconds": 0.0, "cost": 0.0},
                )
                bucket["runs"] += 1
                bucket["failures"] += 1 if failed else 0
                bucket["seconds"] += seconds
                bucket["cost"] += float(run_cost or 0.0)

            names = dict(
                session.execute(
                    select(
                        __import__(
                            "agentos.db.models", fromlist=["Agent"]
                        ).Agent.id,
                        __import__(
                            "agentos.db.models", fromlist=["Agent"]
                        ).Agent.name,
                    )
                ).all()
            )

            commands = int(
                session.scalar(select(func.count(CommandRun.id))) or 0
            )
            commands_denied = int(
                session.scalar(
                    select(func.count(CommandRun.id)).where(
                        CommandRun.verdict == "denied"
                    )
                )
                or 0
            )
            command_counts = dict(
                session.execute(
                    select(CommandRun.agent, func.count(CommandRun.id)).group_by(
                        CommandRun.agent
                    )
                ).all()
            )
            denial_counts = dict(
                session.execute(
                    select(Denial.agent, func.count(Denial.id)).group_by(Denial.agent)
                ).all()
            )
            capability_denials = int(
                session.scalar(select(func.count(Denial.id))) or 0
            )
            events = int(session.scalar(select(func.count(Event.id))) or 0)

        agents = [
            AgentMetrics(
                agent=names.get(agent_id) or "(unknown)",
                runs=bucket["runs"],
                failures=bucket["failures"],
                busy_seconds=bucket["seconds"],
                cost_usd=bucket["cost"],
                commands=command_counts.get(names.get(agent_id), 0),
                denials=denial_counts.get(names.get(agent_id), 0),
            )
            for agent_id, bucket in per_agent.items()
        ]
        agents.sort(key=lambda a: a.agent)

        return Metrics(
            tasks_total=sum(by_status.values()),
            tasks_by_status=by_status,
            task_seconds=task_seconds,
            retries=retries,
            runs=runs,
            run_failures=run_failures,
            run_seconds=run_seconds,
            cost_usd=cost,
            commands=commands,
            commands_denied=commands_denied,
            capability_denials=capability_denials,
            events=events,
            agents=agents,
        )

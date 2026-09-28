"""Phase 16: the event bus, timeline and metrics.

The property that matters: emitting must never break orchestration, and a
timeline must explain why a task ran or became blocked.
"""

from __future__ import annotations

import pytest

from agentos.config import Config
from agentos.db.session import Database
from agentos.schemas.enums import TaskStatus
from agentos.services.events import Event, EventBus, EventType
from agentos.services.metrics import MetricsService
from agentos.services.tasks import TaskService


@pytest.fixture
def config() -> Config:
    return Config.model_validate(
        {
            "agents": {
                "backend": {"role": "backend"},
                "frontend": {"role": "frontend"},
                "qa": {"role": "qa"},
            }
        }
    )


@pytest.fixture
def bus(db: Database) -> EventBus:
    return EventBus(db)


@pytest.fixture(autouse=True)
def registered_agents(db: Database, config: Config, tmp_path):
    """Tasks can only be assigned to agents that exist in the database."""
    from agentos.services.agents import AgentService
    from tests.test_agents import StubRuntime

    AgentService(db, config, StubRuntime(), tmp_path).sync_from_config()


# --------------------------------------------------------------------- emitting


def test_emit_returns_a_persisted_event(bus: EventBus) -> None:
    event = bus.emit(EventType.TASK_CREATED, "built it", task_key="T-1")
    assert event.id is not None
    assert event.type is EventType.TASK_CREATED
    assert event.task_key == "T-1"


def test_emitted_events_are_readable_back(bus: EventBus) -> None:
    bus.emit(EventType.TASK_CREATED, task_key="T-1")
    bus.emit(EventType.TASK_COMPLETED, task_key="T-1")
    history = bus.history()
    assert [e.type for e in history] == [
        EventType.TASK_CREATED,
        EventType.TASK_COMPLETED,
    ]


def test_history_is_oldest_first(bus: EventBus) -> None:
    """A timeline reads forwards."""
    for i in range(5):
        bus.emit(EventType.TASK_CREATED, task_key=f"T-{i}")
    assert [e.task_key for e in bus.history()] == [f"T-{i}" for i in range(5)]


def test_limit_returns_the_most_recent(bus: EventBus) -> None:
    """`--limit 2` must mean the last two, not the first two."""
    for i in range(5):
        bus.emit(EventType.TASK_CREATED, task_key=f"T-{i}")
    assert [e.task_key for e in bus.history(limit=2)] == ["T-3", "T-4"]


def test_arbitrary_data_round_trips(bus: EventBus) -> None:
    bus.emit(EventType.TASK_FAILED, task_key="T-1", failure_kind="timeout", attempt=2)
    stored = bus.history()[0]
    assert stored.data["failure_kind"] == "timeout"
    assert stored.data["attempt"] == 2


def test_unserialisable_data_does_not_break_emitting(bus: EventBus) -> None:
    """A stray object must not cost us the event."""
    bus.emit(EventType.TASK_CREATED, task_key="T-1", weird=object())
    assert len(bus.history()) == 1


def test_most_columns_are_optional(bus: EventBus) -> None:
    event = bus.emit(EventType.SCHEDULER_STARTED)
    assert event.task_key is None
    assert event.agent is None
    assert event.objective_id is None


# ------------------------------------------------------------------- filtering


def test_filter_by_agent(bus: EventBus) -> None:
    bus.emit(EventType.TASK_STARTED, agent="backend", task_key="T-1")
    bus.emit(EventType.TASK_STARTED, agent="frontend", task_key="T-2")
    assert [e.task_key for e in bus.history(agent="backend")] == ["T-1"]


def test_filter_by_task(bus: EventBus) -> None:
    bus.emit(EventType.TASK_STARTED, task_key="T-1")
    bus.emit(EventType.TASK_COMPLETED, task_key="T-1")
    bus.emit(EventType.TASK_STARTED, task_key="T-2")
    assert len(bus.history(task_key="T-1")) == 2


def test_filter_by_objective(bus: EventBus) -> None:
    bus.emit(EventType.TASK_CREATED, objective_id=1)
    bus.emit(EventType.TASK_CREATED, objective_id=2)
    assert len(bus.history(objective_id=2)) == 1


def test_filter_by_category(bus: EventBus) -> None:
    bus.emit(EventType.TASK_CREATED, task_key="T-1")
    bus.emit(EventType.AGENT_ROTATED, agent="backend")
    bus.emit(EventType.COMMAND_DENIED, agent="backend")
    assert [e.type for e in bus.history(category="agent")] == [
        EventType.AGENT_ROTATED
    ]


def test_since_id_supports_tailing(bus: EventBus) -> None:
    """How `agentctl watch` advances without re-printing."""
    first = bus.emit(EventType.TASK_CREATED, task_key="T-1")
    bus.emit(EventType.TASK_CREATED, task_key="T-2")
    fresh = bus.history(since_id=first.id)
    assert [e.task_key for e in fresh] == ["T-2"]


def test_latest_id_tracks_the_newest(bus: EventBus) -> None:
    assert bus.latest_id() == 0
    event = bus.emit(EventType.TASK_CREATED)
    assert bus.latest_id() == event.id


# ----------------------------------------------------------------- subscribers


def test_subscriber_receives_events(bus: EventBus) -> None:
    seen: list[Event] = []
    bus.subscribe(seen.append)
    bus.emit(EventType.TASK_COMPLETED, task_key="T-1")
    assert [e.task_key for e in seen] == ["T-1"]


def test_subscriber_can_filter_by_prefix(bus: EventBus) -> None:
    seen: list[Event] = []
    bus.subscribe(seen.append, prefix="task.")
    bus.emit(EventType.TASK_COMPLETED, task_key="T-1")
    bus.emit(EventType.AGENT_ROTATED, agent="backend")
    assert len(seen) == 1


def test_broken_subscriber_does_not_break_emitting(bus: EventBus) -> None:
    """Observability must never take down orchestration."""
    def explode(event: Event) -> None:
        raise RuntimeError("subscriber is broken")

    good: list[Event] = []
    bus.subscribe(explode)
    bus.subscribe(good.append)

    bus.emit(EventType.TASK_COMPLETED, task_key="T-1")
    assert len(good) == 1
    assert len(bus.history()) == 1


def test_unsubscribe(bus: EventBus) -> None:
    seen: list[Event] = []
    handler = bus.subscribe(seen.append)
    bus.unsubscribe(handler)
    bus.emit(EventType.TASK_CREATED)
    assert seen == []


def test_bus_without_a_database_still_delivers() -> None:
    """Useful in tests and for a dry run; history is simply empty."""
    bus = EventBus(db=None)
    seen: list[Event] = []
    bus.subscribe(seen.append)
    bus.emit(EventType.TASK_CREATED, task_key="T-1")
    assert len(seen) == 1
    assert bus.history() == []


async def test_emit_async_does_not_block(bus: EventBus) -> None:
    event = await bus.emit_async(EventType.TASK_STARTED, task_key="T-1")
    assert event.id is not None


# ------------------------------------------------------------------ rendering


def test_describe_includes_subject_and_summary() -> None:
    event = Event(type=EventType.TASK_FAILED, summary="tests failed", task_key="T-1")
    described = event.describe()
    assert "task.failed" in described
    assert "T-1" in described
    assert "tests failed" in described


def test_category_is_the_prefix() -> None:
    assert Event(type=EventType.GIT_MERGED).category == "git"
    assert Event(type=EventType.TASK_READY).category == "task"


def test_every_event_type_has_a_category_style() -> None:
    """A new event type must not render without colour."""
    from agentos.cli.event_commands import CATEGORY_STYLE

    for event_type in EventType:
        assert event_type.value.split(".", 1)[0] in CATEGORY_STYLE


def test_render_event_survives_markup_in_a_summary() -> None:
    from agentos.cli.event_commands import render_event

    event = Event(type=EventType.TASK_FAILED, summary="failed at [line 3]")
    assert "[line 3]" in render_event(event)


# ----------------------------------------------- the timeline explains the graph


def test_timeline_explains_why_a_task_became_ready(db, config) -> None:
    """The stated goal: a developer can see why a task ran."""
    bus = EventBus(db)
    tasks = TaskService(db, config, event_bus=bus)

    first = tasks.create_task("first", agent="backend")
    second = tasks.create_task("second", agent="frontend", depends_on=[first.key])

    tasks.transition(first.key, TaskStatus.RUNNING)
    tasks.transition(first.key, TaskStatus.COMPLETED)
    tasks.refresh_readiness()

    ready = [
        e for e in bus.history() if e.type is EventType.TASK_READY and e.task_key == second.key
    ]
    assert ready
    assert "all dependencies complete" in ready[0].summary


def test_timeline_explains_why_a_task_became_blocked(db, config) -> None:
    bus = EventBus(db)
    tasks = TaskService(db, config, event_bus=bus)

    first = tasks.create_task("first", agent="backend")
    second = tasks.create_task("second", agent="frontend", depends_on=[first.key])

    tasks.transition(first.key, TaskStatus.RUNNING)
    tasks.transition(first.key, TaskStatus.FAILED)
    tasks.refresh_readiness()

    blocked = [
        e
        for e in bus.history()
        if e.type is EventType.TASK_BLOCKED and e.task_key == second.key
    ]
    assert blocked
    assert first.key in blocked[0].summary
    assert "failed" in blocked[0].summary


def test_task_creation_and_cancellation_are_recorded(db, config) -> None:
    bus = EventBus(db)
    tasks = TaskService(db, config, event_bus=bus)
    task = tasks.create_task("work", agent="backend")
    tasks.cancel(task.key)

    types = [e.type for e in bus.history(task_key=task.key)]
    assert EventType.TASK_CREATED in types
    assert EventType.TASK_CANCELLED in types


# -------------------------------------------------------------------- metrics


def test_metrics_on_an_empty_project(db: Database) -> None:
    metrics = MetricsService(db).collect()
    assert metrics.tasks_total == 0
    assert metrics.runs == 0
    assert metrics.mean_task_seconds is None
    assert metrics.agents == []


def test_metrics_count_tasks_by_status(db, config) -> None:
    tasks = TaskService(db, config, event_bus=EventBus(db))
    first = tasks.create_task("a", agent="backend")
    tasks.create_task("b", agent="frontend")
    tasks.transition(first.key, TaskStatus.RUNNING)
    tasks.transition(first.key, TaskStatus.COMPLETED)

    metrics = MetricsService(db).collect()
    assert metrics.tasks_total == 2
    assert metrics.completed == 1
    assert metrics.mean_task_seconds is not None


def test_metrics_include_runs_and_cost(db, config) -> None:
    from agentos.db.models import Agent, Run
    from datetime import datetime, timedelta, timezone

    with db.session() as session:
        # The agent already exists, registered from config by the fixture.
        agent = session.query(Agent).filter_by(name="backend").one()
        start = datetime.now(timezone.utc)
        session.add(
            Run(
                agent_id=agent.id,
                status="succeeded",
                started_at=start,
                finished_at=start + timedelta(seconds=12),
                cost_usd=0.5,
            )
        )
        session.add(
            Run(
                agent_id=agent.id,
                status="failed",
                started_at=start,
                finished_at=start + timedelta(seconds=3),
                cost_usd=0.25,
            )
        )

    metrics = MetricsService(db).collect()
    assert metrics.runs == 2
    assert metrics.run_failures == 1
    assert metrics.cost_usd == pytest.approx(0.75)
    assert metrics.run_seconds == pytest.approx(15.0)

    entry = next(a for a in metrics.agents if a.agent == "backend")
    assert entry.runs == 2
    assert entry.failures == 1
    assert entry.success_rate == pytest.approx(0.5)


def test_metrics_count_command_denials(db, config) -> None:
    from agentos.schemas.capabilities import Capability
    from agentos.services.permissions import PermissionService

    PermissionService(db, config).deny(
        "reviewer", Capability.EDIT_FILES, "edit_files", "tried"
    )
    assert MetricsService(db).collect().capability_denials == 1


def test_metrics_are_derived_not_accumulated(db, config) -> None:
    """Deleting a task changes the count, because nothing is cached."""
    tasks = TaskService(db, config, event_bus=EventBus(db))
    task = tasks.create_task("temp", agent="backend")
    assert MetricsService(db).collect().tasks_total == 1
    tasks.tasks.delete(task.id)
    assert MetricsService(db).collect().tasks_total == 0


def test_creation_is_announced_before_readiness(db, config) -> None:
    """A task cannot become ready before it exists.

    Regression: create_task refreshed readiness first, so the timeline showed
    task.ready ahead of task.created for the same task.
    """
    bus = EventBus(db)
    tasks = TaskService(db, config, event_bus=bus)
    task = tasks.create_task("work", agent="backend")

    history = [e for e in bus.history(task_key=task.key)]
    types = [e.type for e in history]
    assert types.index(EventType.TASK_CREATED) < types.index(EventType.TASK_READY)

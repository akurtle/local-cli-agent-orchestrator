"""The scheduler: dispatch, concurrency, dependencies, retries, recovery.

Uses stub runtimes, so nothing is launched and nothing is spent.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentos.config import Config
from agentos.db.session import Database
from agentos.runtime.dry_run import DryRunRuntime
from agentos.schemas.enums import AgentStatus, RunStatus, TaskStatus
from agentos.schemas.runtime import RunRequest, RunResult
from agentos.services.agents import AgentService
from agentos.services.dag import DependencyCycle
from agentos.services.scheduler import Scheduler
from agentos.services.tasks import TaskService
from tests.test_agents import StubRuntime

CONFIG_DICT = {
    "project": {"name": "Sched"},
    "orchestrator": {"max_concurrent_agents": 3, "max_task_retries": 0},
    "agents": {
        "backend": {"role": "backend"},
        "frontend": {"role": "frontend"},
        "qa": {"role": "qa"},
        "reviewer": {"role": "reviewer"},
        "docs": {"role": "docs"},
    },
}


def make_config(**orchestrator) -> Config:
    data = {k: dict(v) if isinstance(v, dict) else v for k, v in CONFIG_DICT.items()}
    data["orchestrator"] = {**CONFIG_DICT["orchestrator"], **orchestrator}
    return Config.model_validate(data)


class RecordingRuntime(StubRuntime):
    """Tracks concurrency and can be told which prompts should fail."""

    def __init__(self, delay: float = 0.05, fail_contains: set[str] | None = None):
        super().__init__()
        self.delay = delay
        self.fail_contains = fail_contains or set()
        self.active = 0
        self.max_active = 0
        self.order: list[str] = []
        self.concurrent_groups: list[set[str]] = []

    def _label(self, prompt: str) -> str:
        for line in prompt.splitlines():
            if line.startswith("## TASK "):
                return line.removeprefix("## TASK ").strip()
        return "?"

    async def run(self, request: RunRequest, on_event=None) -> RunResult:
        label = self._label(request.prompt)
        self.requests.append(request)
        self.order.append(label)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            failing = any(f in request.prompt for f in self.fail_contains)
            return RunResult(
                status=RunStatus.FAILED if failing else RunStatus.SUCCEEDED,
                session_id=request.session_id or f"s-{len(self.requests)}",
                exit_code=1 if failing else 0,
                text=f"did {label}",
                error="simulated failure" if failing else None,
            )
        finally:
            self.active -= 1


def build(
    db: Database, config: Config, runtime, tmp_path: Path
) -> tuple[Scheduler, TaskService, list[tuple[str, str]]]:
    agents = AgentService(db, config, runtime, tmp_path)
    agents.sync_from_config()
    tasks = TaskService(db, config)
    events: list[tuple[str, str]] = []
    scheduler = Scheduler(
        db=db,
        config=config,
        agent_service=agents,
        task_service=tasks,
        on_progress=lambda e, d: events.append((e, d)),
    )
    return scheduler, tasks, events


@pytest.fixture
def config() -> Config:
    return make_config()


# ------------------------------------------------------------------- basics


async def test_empty_queue_stops_immediately(db, config, tmp_path) -> None:
    scheduler, _tasks, _events = build(db, config, RecordingRuntime(), tmp_path)
    report = await scheduler.run()
    assert report.dispatched == 0
    assert report.stop_reason == "nothing left to do"


async def test_single_task_runs_and_completes(db, config, tmp_path) -> None:
    scheduler, tasks, _ = build(db, config, RecordingRuntime(), tmp_path)
    task = tasks.create_task("work", agent="backend")
    report = await scheduler.run()
    assert report.completed == [task.key]
    assert tasks.get_task(task.key).status is TaskStatus.COMPLETED


async def test_agent_output_is_stored_as_task_result(db, config, tmp_path) -> None:
    scheduler, tasks, _ = build(db, config, RecordingRuntime(), tmp_path)
    task = tasks.create_task("work", agent="backend")
    await scheduler.run()
    assert "did " + task.key in (tasks.get_task(task.key).result or "")


async def test_agent_returns_to_idle_after_scheduling(db, config, tmp_path) -> None:
    scheduler, tasks, _ = build(db, config, RecordingRuntime(), tmp_path)
    tasks.create_task("work", agent="backend")
    await scheduler.run()
    assert scheduler.agents.get_agent("backend").status is AgentStatus.IDLE


async def test_unassigned_task_is_not_dispatched(db, config, tmp_path) -> None:
    """A task with no agent must be reported, not silently run or spun on."""
    scheduler, tasks, _ = build(db, config, RecordingRuntime(), tmp_path)
    task = tasks.create_task("orphan")
    report = await scheduler.run()
    assert report.dispatched == 0
    assert task.key in report.skipped
    assert "no available agent" in report.stop_reason


# --------------------------------------------------------------- dependencies


async def test_dependency_order_is_respected(db, config, tmp_path) -> None:
    runtime = RecordingRuntime()
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    first = tasks.create_task("first", agent="backend")
    second = tasks.create_task("second", agent="frontend", depends_on=[first.key])
    report = await scheduler.run()
    assert runtime.order == [first.key, second.key]
    assert report.completed == [first.key, second.key]


async def test_fan_in_runs_prerequisites_concurrently_then_qa(
    db, config, tmp_path
) -> None:
    """The Phase 3 success criterion.

        BACKEND-1 -+
                   +-> QA-1
        FRONTEND-1 -+

    backend and frontend must overlap; QA must start only after both finish.
    """
    runtime = RecordingRuntime(delay=0.2)
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    b = tasks.create_task("backend work", agent="backend", prefix="BACKEND")
    f = tasks.create_task("frontend work", agent="frontend", prefix="FRONTEND")
    qa = tasks.create_task(
        "integration tests", agent="qa", prefix="QA", depends_on=[b.key, f.key]
    )

    report = await scheduler.run()

    assert runtime.max_active == 2, "backend and frontend did not overlap"
    assert runtime.order[:2] == [b.key, f.key] or runtime.order[:2] == [f.key, b.key]
    assert runtime.order[2] == qa.key
    assert set(report.completed) == {b.key, f.key, qa.key}
    assert tasks.get_task(qa.key).status is TaskStatus.COMPLETED


async def test_chain_of_three(db, config, tmp_path) -> None:
    runtime = RecordingRuntime()
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    a = tasks.create_task("a", agent="backend")
    b = tasks.create_task("b", agent="frontend", depends_on=[a.key])
    c = tasks.create_task("c", agent="qa", depends_on=[b.key])
    await scheduler.run()
    assert runtime.order == [a.key, b.key, c.key]


async def test_failed_dependency_blocks_dependent(db, config, tmp_path) -> None:
    runtime = RecordingRuntime(fail_contains={"## TASK T-1"})
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    a = tasks.create_task("will fail", agent="backend")
    b = tasks.create_task("dependent", agent="frontend", depends_on=[a.key])

    report = await scheduler.run()
    assert report.failed == [a.key]
    assert b.key in report.blocked
    assert tasks.get_task(b.key).status is TaskStatus.BLOCKED
    # The dependent must never have been launched.
    assert b.key not in runtime.order


async def test_completed_dependency_result_reaches_the_prompt(
    db, config, tmp_path
) -> None:
    runtime = RecordingRuntime()
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    a = tasks.create_task("first", agent="backend")
    tasks.create_task("second", agent="frontend", depends_on=[a.key])
    await scheduler.run()
    second_prompt = runtime.requests[1].prompt
    assert "COMPLETED DEPENDENCIES" in second_prompt
    assert f"did {a.key}" in second_prompt


# --------------------------------------------------------------- concurrency


async def test_three_independent_tasks_run_concurrently(db, tmp_path) -> None:
    config = make_config(max_concurrent_agents=3)
    runtime = RecordingRuntime(delay=0.2)
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    tasks.create_task("b", agent="backend")
    tasks.create_task("f", agent="frontend")
    tasks.create_task("d", agent="docs")

    loop = asyncio.get_running_loop()
    started = loop.time()
    report = await scheduler.run()
    elapsed = loop.time() - started

    assert runtime.max_active == 3
    assert len(report.completed) == 3
    assert elapsed < 0.5, f"tasks were serialised (took {elapsed:.2f}s)"


async def test_concurrency_limit_is_enforced(db, tmp_path) -> None:
    config = make_config(max_concurrent_agents=2)
    runtime = RecordingRuntime(delay=0.15)
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    for agent in ("backend", "frontend", "qa", "docs"):
        tasks.create_task(f"work {agent}", agent=agent)

    report = await scheduler.run()
    assert runtime.max_active <= 2, f"exceeded the limit ({runtime.max_active})"
    assert len(report.completed) == 4


async def test_one_task_per_agent_at_a_time(db, config, tmp_path) -> None:
    """Two tasks for the same agent must be serialised, not run together."""
    runtime = RecordingRuntime(delay=0.1)
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    tasks.create_task("first", agent="backend")
    tasks.create_task("second", agent="backend")

    report = await scheduler.run()
    assert runtime.max_active == 1
    assert len(report.completed) == 2


async def test_paused_agent_is_skipped(db, config, tmp_path) -> None:
    runtime = RecordingRuntime()
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    scheduler.agents.pause("backend")
    task = tasks.create_task("work", agent="backend")

    report = await scheduler.run()
    assert report.dispatched == 0
    assert task.key in report.skipped
    assert tasks.get_task(task.key).status is TaskStatus.READY


async def test_busy_agent_defers_work(db, config, tmp_path) -> None:
    runtime = RecordingRuntime()
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    # Occupy the agent outside the scheduler.
    other = tasks.create_task("external", agent="backend")
    tasks.transition(other.key, TaskStatus.RUNNING)
    scheduler.agents.transition("backend", AgentStatus.WORKING, current_task_id=other.id)

    task = tasks.create_task("queued", agent="backend")
    report = await scheduler.run()
    # recover_stale_running frees the agent, so both end up completing.
    assert task.key in report.completed


# ------------------------------------------------------------------- retries


async def test_failure_without_retries_marks_failed(db, tmp_path) -> None:
    config = make_config(max_task_retries=0)
    runtime = RecordingRuntime(fail_contains={"## TASK T-1"})
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    task = tasks.create_task("doomed", agent="backend")
    report = await scheduler.run()
    assert report.failed == [task.key]
    assert tasks.get_task(task.key).attempts == 1
    assert runtime.order.count(task.key) == 1


async def test_failure_is_retried_once(db, tmp_path) -> None:
    config = make_config(max_task_retries=1)
    runtime = RecordingRuntime(fail_contains={"## TASK T-1"})
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    task = tasks.create_task("doomed", agent="backend")

    report = await scheduler.run()
    assert runtime.order.count(task.key) == 2, "task was not retried exactly once"
    assert report.failed == [task.key]
    assert tasks.get_task(task.key).attempts == 2


async def test_retry_prompt_includes_previous_error(db, tmp_path) -> None:
    config = make_config(max_task_retries=1)
    runtime = RecordingRuntime(fail_contains={"## TASK T-1"})
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    tasks.create_task("doomed", agent="backend")
    await scheduler.run()
    assert "## RETRY" in runtime.requests[1].prompt
    assert "simulated failure" in runtime.requests[1].prompt


async def test_retries_are_bounded(db, tmp_path) -> None:
    """A permanently failing task must not loop forever."""
    config = make_config(max_task_retries=2)
    runtime = RecordingRuntime(fail_contains={"## TASK T-1"})
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    task = tasks.create_task("doomed", agent="backend")
    report = await scheduler.run()
    assert runtime.order.count(task.key) == 3  # initial + 2 retries
    assert report.failed == [task.key]


async def test_runtime_exception_is_recorded_not_raised(db, tmp_path) -> None:
    config = make_config(max_task_retries=0)

    class ExplodingRuntime(StubRuntime):
        async def run(self, request: RunRequest, on_event=None) -> RunResult:
            raise OSError("process died")

    scheduler, tasks, _ = build(db, config, ExplodingRuntime(), tmp_path)
    task = tasks.create_task("boom", agent="backend")
    report = await scheduler.run()
    assert report.failed == [task.key]
    assert "OSError" in (tasks.get_task(task.key).error or "")


# ------------------------------------------------------------------ recovery


async def test_stale_running_task_is_recovered(db, config, tmp_path) -> None:
    """A crash leaves tasks marked running; the next start must requeue them."""
    runtime = RecordingRuntime()
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    task = tasks.create_task("interrupted", agent="backend")
    tasks.transition(task.key, TaskStatus.RUNNING)
    scheduler.agents.transition("backend", AgentStatus.WORKING, current_task_id=task.id)

    report = await scheduler.run()
    assert report.completed == [task.key]
    assert tasks.get_task(task.key).status is TaskStatus.COMPLETED


async def test_recovery_does_not_consume_retries(db, tmp_path) -> None:
    config = make_config(max_task_retries=0)
    scheduler, tasks, _ = build(db, config, RecordingRuntime(), tmp_path)
    task = tasks.create_task("interrupted", agent="backend")
    tasks.transition(task.key, TaskStatus.RUNNING)
    await scheduler.run()
    assert tasks.get_task(task.key).attempts == 0


async def test_scheduler_resumes_from_persisted_state(tmp_path) -> None:
    """A fresh process must pick up where the last one stopped."""
    config = make_config()
    db_path = tmp_path / "resume.db"

    first = Database(db_path)
    first.create_all()
    scheduler, tasks, _ = build(first, config, RecordingRuntime(), tmp_path)
    a = tasks.create_task("a", agent="backend")
    b = tasks.create_task("b", agent="frontend", depends_on=[a.key])
    tasks.transition(a.key, TaskStatus.RUNNING)
    tasks.transition(a.key, TaskStatus.COMPLETED, result="A finished")
    first.dispose()

    second = Database(db_path)
    second.create_all()
    runtime = RecordingRuntime()
    scheduler2, tasks2, _ = build(second, config, runtime, tmp_path)
    report = await scheduler2.run()
    assert report.completed == [b.key]
    assert runtime.order == [b.key]  # `a` was not re-run
    second.dispose()


# -------------------------------------------------------------- cycle safety


async def test_cycle_in_database_is_refused(db, config, tmp_path) -> None:
    """A hand-edited database must not make the scheduler spin."""
    scheduler, tasks, _ = build(db, config, RecordingRuntime(), tmp_path)
    a = tasks.create_task("a", agent="backend")
    b = tasks.create_task("b", agent="frontend", depends_on=[a.key])
    # Bypass validation the way a manual edit would.
    tasks.tasks.add_dependency(a.id, b.id)

    with pytest.raises(DependencyCycle):
        await scheduler.run()


# ------------------------------------------------------------------- stopping


async def test_request_stop_prevents_new_dispatch(db, config, tmp_path) -> None:
    runtime = RecordingRuntime()
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    tasks.create_task("work", agent="backend")
    scheduler.request_stop()

    report = await scheduler.run()
    assert report.dispatched == 0
    assert report.interrupted
    assert runtime.order == []


async def test_blocked_only_graph_settles(db, config, tmp_path) -> None:
    runtime = RecordingRuntime(fail_contains={"## TASK T-1"})
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    a = tasks.create_task("fails", agent="backend")
    tasks.create_task("blocked", agent="frontend", depends_on=[a.key])
    report = await scheduler.run()
    # Must terminate rather than loop over an unrunnable graph.
    assert report.passes < 10


async def test_progress_events_are_emitted(db, config, tmp_path) -> None:
    scheduler, tasks, events = build(db, config, RecordingRuntime(), tmp_path)
    task = tasks.create_task("work", agent="backend")
    await scheduler.run()
    names = [name for name, _ in events]
    assert "dispatch" in names
    assert "completed" in names
    assert "stop" in names
    assert any(task.key in detail for _, detail in events)


async def test_broken_progress_callback_does_not_abort(db, config, tmp_path) -> None:
    agents = AgentService(db, config, RecordingRuntime(), tmp_path)
    agents.sync_from_config()
    tasks = TaskService(db, config)
    scheduler = Scheduler(
        db=db,
        config=config,
        agent_service=agents,
        task_service=tasks,
        on_progress=lambda e, d: (_ for _ in ()).throw(RuntimeError("bad renderer")),
    )
    task = tasks.create_task("work", agent="backend")
    report = await scheduler.run()
    assert report.completed == [task.key]


# -------------------------------------------------------------- dry run mode


async def test_dry_run_launches_nothing_and_completes(db, config, tmp_path) -> None:
    runtime = DryRunRuntime()
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    a = tasks.create_task("a", agent="backend")
    b = tasks.create_task("b", agent="qa", depends_on=[a.key])

    report = await scheduler.run()
    assert report.completed == [a.key, b.key]
    assert len(runtime.requests) == 2
    # Dependency order still holds, and every run is marked as launching nothing.
    from agentos.db.models import Run

    with db.session() as session:
        rows = session.query(Run).order_by(Run.id).all()
        assert len(rows) == 2
        assert all(r.runtime == "dry-run" for r in rows)
        assert all("(dry-run)" in r.command for r in rows)


async def test_dry_run_reports_zero_cost(db, config, tmp_path) -> None:
    runtime = DryRunRuntime()
    result = await runtime.run(RunRequest(prompt="anything"))
    assert result.cost_usd == 0.0
    assert result.ok


async def test_dry_run_can_simulate_failure(db, config, tmp_path) -> None:
    runtime = DryRunRuntime(fail_keys={"T-1"})
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    task = tasks.create_task("doomed", agent="backend")
    report = await scheduler.run()
    assert report.failed == [task.key]


def test_dry_run_preflight_needs_no_auth() -> None:
    info = DryRunRuntime().preflight()
    assert "not required" in info["auth"]

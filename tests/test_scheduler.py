"""The scheduler: dispatch, concurrency, dependencies, retries, recovery.

Uses stub runtimes, so nothing is launched and nothing is spent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agentos.config import Config
from agentos.db.session import Database
from agentos.runtime.dry_run import DryRunRuntime
from agentos.schemas.enums import AgentStatus, FailureKind, RunStatus, TaskStatus
from agentos.schemas.responses import RESPONSE_BEGIN, RESPONSE_END
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


def response_block(
    status: str = "completed",
    summary: str = "",
    messages: list[dict] | None = None,
    requested_tasks: list[dict] | None = None,
    blockers: list[str] | None = None,
) -> str:
    """Render a valid agent response block, as a real agent must."""
    payload = {
        "status": status,
        "summary": summary,
        "files_changed": [],
        "messages": messages or [],
        "requested_tasks": requested_tasks or [],
        "blockers": blockers or [],
    }
    return "\n".join([RESPONSE_BEGIN, json.dumps(payload), RESPONSE_END])


class RecordingRuntime(StubRuntime):
    """Tracks concurrency and emits valid response blocks.

    The scheduler requires a parseable response, so a stub that returns prose
    would be exercising the repair path rather than the happy path.
    """

    def __init__(
        self,
        delay: float = 0.05,
        fail_contains: set[str] | None = None,
        messages: dict[str, list[dict]] | None = None,
        requested: dict[str, list[dict]] | None = None,
        raw_text: dict[str, str] | None = None,
    ):
        super().__init__()
        self.delay = delay
        self.fail_contains = fail_contains or set()
        # task key -> extras the agent asks for on that task
        self.messages_for = messages or {}
        self.requested_for = requested or {}
        # task key -> literal reply, for testing malformed output
        self.raw_text = raw_text or {}
        self.active = 0
        self.max_active = 0
        self.order: list[str] = []

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
            if label in self.raw_text:
                text = self.raw_text[label]
            elif failing:
                text = response_block(status="failed", blockers=["simulated failure"])
            else:
                text = f"Working on {label}." + "\n\n" + response_block(
                    summary=f"did {label}",
                    messages=self.messages_for.get(label),
                    requested_tasks=self.requested_for.get(label),
                )
            return RunResult(
                status=RunStatus.FAILED if failing else RunStatus.SUCCEEDED,
                session_id=request.session_id or f"s-{len(self.requests)}",
                exit_code=1 if failing else 0,
                text=text,
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


# ------------------------------------------------- phase 4: bus integration


async def test_backend_message_reaches_frontend_prompt(db, config, tmp_path) -> None:
    """The spec demonstration: backend -> frontend via the orchestrator.

    backend emits a message in its response block; the orchestrator stores it and
    injects it into frontend's next prompt. The agents never touch each other.
    """
    runtime = RecordingRuntime(
        messages={
            "T-1": [{"to": "frontend", "message": "Endpoint is now POST /api/v2/users"}]
        }
    )
    scheduler, tasks, events = build(db, config, runtime, tmp_path)
    first = tasks.create_task("backend work", agent="backend")
    second = tasks.create_task("frontend work", agent="frontend", depends_on=[first.key])

    await scheduler.run()

    frontend_prompt = runtime.requests[1].prompt
    assert "## INBOX" in frontend_prompt
    assert "[backend]" in frontend_prompt
    assert "POST /api/v2/users" in frontend_prompt
    assert ("message", "backend sent 1 message(s)") in events
    # Consumed exactly once.
    assert not scheduler.messages.take_inbox("frontend")


async def test_human_message_is_injected(db, config, tmp_path) -> None:
    runtime = RecordingRuntime()
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    scheduler.messages.send_from_human("backend", "Check the auth middleware")
    tasks.create_task("work", agent="backend")

    await scheduler.run()
    assert "[human]" in runtime.requests[0].prompt
    assert "Check the auth middleware" in runtime.requests[0].prompt


async def test_failed_run_preserves_unread_message(db, tmp_path) -> None:
    """A failed run must not consume the inbox."""
    config = make_config(max_task_retries=0)
    runtime = RecordingRuntime(fail_contains={"## TASK T-1"})
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    scheduler.messages.send_from_human("backend", "do not lose me")
    tasks.create_task("doomed", agent="backend")

    await scheduler.run()
    still_unread = scheduler.messages.list_for("backend", unread_only=True)
    assert [m.body for m in still_unread] == ["do not lose me"]


async def test_message_redelivered_on_retry(db, tmp_path) -> None:
    config = make_config(max_task_retries=1)
    runtime = RecordingRuntime(fail_contains={"## TASK T-1"})
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    scheduler.messages.send_from_human("backend", "persistent note")
    tasks.create_task("doomed", agent="backend")

    await scheduler.run()
    # Injected into both the original attempt and the retry.
    assert sum("persistent note" in r.prompt for r in runtime.requests) == 2


async def test_requested_task_is_created_and_scheduled(db, config, tmp_path) -> None:
    """An agent asks for follow-up work; the orchestrator validates and runs it."""
    runtime = RecordingRuntime(
        requested={
            "T-1": [
                {
                    "agent_role": "qa",
                    "title": "Test the OAuth callback",
                    "description": "Verify success and failure paths.",
                }
            ]
        }
    )
    scheduler, tasks, events = build(db, config, runtime, tmp_path)
    parent = tasks.create_task("backend work", agent="backend")

    report = await scheduler.run()

    spawned = [d for e, d in events if e == "spawned"]
    assert spawned, "no follow-up task was created"
    new_key = next(k for k in report.completed if k != parent.key)
    created = tasks.get_task(new_key)
    assert created.assigned_agent == "qa"
    assert created.created_by == "backend"
    assert parent.key in created.depends_on
    # It ran after its parent, in the same scheduler session.
    assert runtime.order == [parent.key, new_key]


async def test_rejected_requested_task_is_reported_not_created(
    db, config, tmp_path
) -> None:
    runtime = RecordingRuntime(
        requested={"T-1": [{"agent_role": "astronaut", "title": "Fly to orbit"}]}
    )
    scheduler, tasks, events = build(db, config, runtime, tmp_path)
    parent = tasks.create_task("work", agent="backend")

    report = await scheduler.run()
    assert report.completed == [parent.key]
    assert tasks.tasks.count() == 1
    assert any(e == "rejected" and "astronaut" in d for e, d in events)


async def test_unparseable_response_triggers_one_repair(db, tmp_path) -> None:
    """A malformed reply gets exactly one repair attempt, then is honoured."""
    config = make_config(max_task_retries=0)
    runtime = RecordingRuntime(raw_text={"T-1": "I finished it, honestly."})
    scheduler, tasks, events = build(db, config, runtime, tmp_path)
    task = tasks.create_task("work", agent="backend")

    await scheduler.run()

    assert any(e == "repair" for e, _ in events)
    # Original attempt plus one repair turn; the repair prompt has no TASK header.
    assert runtime.order == [task.key, "?"]


async def test_repair_prompt_does_not_ask_for_more_work(db, tmp_path) -> None:
    config = make_config(max_task_retries=0)
    runtime = RecordingRuntime(raw_text={"T-1": "no block here"})
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    tasks.create_task("work", agent="backend")
    await scheduler.run()
    repair_prompt = runtime.requests[1].prompt
    assert "do NOT change any files" in repair_prompt


async def test_unrepairable_response_fails_the_task(db, tmp_path) -> None:
    """If the repair also fails to parse, the task fails rather than lying."""
    config = make_config(max_task_retries=0)

    class NeverParses(RecordingRuntime):
        async def run(self, request, on_event=None):
            self.requests.append(request)
            self.order.append(self._label(request.prompt))
            return RunResult(
                status=RunStatus.SUCCEEDED,
                session_id="s",
                exit_code=0,
                text="still no block",
            )

    scheduler, tasks, _ = build(db, config, NeverParses(), tmp_path)
    task = tasks.create_task("work", agent="backend")
    report = await scheduler.run()
    assert report.failed == [task.key]
    assert "block" in (tasks.get_task(task.key).error or "").lower()


async def test_agent_reported_blocked_is_not_retried(db, tmp_path) -> None:
    """A blocker needs intervention. Retrying would hit the same wall and cost more.

    Note max_task_retries=2 here: the point is that retries are NOT consumed.
    """
    config = make_config(max_task_retries=2)
    runtime = RecordingRuntime(
        raw_text={
            "T-1": response_block(status="blocked", blockers=["need db credentials"])
        }
    )
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    task = tasks.create_task("work", agent="backend")
    report = await scheduler.run()

    stored = tasks.get_task(task.key)
    assert stored.status is TaskStatus.BLOCKED
    assert stored.needs_intervention
    assert "credentials" in (stored.error or "")
    assert task.key in report.blocked
    assert report.failed == []
    # Ran exactly once despite two retries being allowed.
    assert runtime.order.count(task.key) == 1


async def test_summary_is_stored_as_task_result(db, config, tmp_path) -> None:
    runtime = RecordingRuntime()
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    task = tasks.create_task("work", agent="backend")
    await scheduler.run()
    # The parsed summary, not the raw reply with its JSON block.
    result = tasks.get_task(task.key).result or ""
    assert result == f"did {task.key}"
    assert RESPONSE_BEGIN not in result


# ------------------------------------------- phase 7: failure classification


def test_classify_timeout() -> None:
    from agentos.schemas.dto import AgentRunOutcome
    from agentos.services.scheduler import classify_run_failure

    class Fake:
        error = "run exceeded timeout of 30s"
        text = ""

    assert classify_run_failure(Fake()) is FailureKind.TIMEOUT


def test_classify_spawn_failure() -> None:
    from agentos.services.scheduler import classify_run_failure

    class Fake:
        error = "failed to spawn claude.exe"
        text = ""

    assert classify_run_failure(Fake()) is FailureKind.LAUNCH


def test_unknown_process_failure_defaults_to_retryable() -> None:
    """Giving up silently on a transient error would be worse than one retry."""
    from agentos.services.scheduler import classify_run_failure

    class Fake:
        error = "exited with code 1"
        text = ""

    kind = classify_run_failure(Fake())
    assert kind.is_retryable


def test_classify_result_kinds() -> None:
    from agentos.schemas.dto import ResultOutcome
    from agentos.services.scheduler import classify_result

    assert (
        classify_result(ResultOutcome(parsed=False)) is FailureKind.UNPARSEABLE
    )
    assert (
        classify_result(ResultOutcome(parsed=True, status="blocked"))
        is FailureKind.BLOCKED
    )
    assert (
        classify_result(
            ResultOutcome(parsed=True, status="completed", blockers=["x"])
        )
        is FailureKind.BLOCKED
    )
    assert (
        classify_result(ResultOutcome(parsed=True, status="failed"))
        is FailureKind.AGENT_FAILED
    )


async def test_blocked_task_stays_blocked_across_passes(db, tmp_path) -> None:
    """Readiness must not helpfully un-block an agent-reported blocker."""
    config = make_config(max_task_retries=0)
    runtime = RecordingRuntime(
        raw_text={"T-1": response_block(status="blocked", blockers=["needs a key"])}
    )
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    task = tasks.create_task("work", agent="backend")
    await scheduler.run()

    # The task has no dependencies, so a naive recompute would call it ready.
    tasks.refresh_readiness()
    assert tasks.get_task(task.key).status is TaskStatus.BLOCKED

    second = await scheduler.run()
    assert second.dispatched == 0
    assert runtime.order.count(task.key) == 1


async def test_unblock_returns_the_task_to_the_queue(db, tmp_path) -> None:
    config = make_config(max_task_retries=0)
    runtime = RecordingRuntime(
        raw_text={"T-1": response_block(status="blocked", blockers=["needs a key"])}
    )
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    task = tasks.create_task("work", agent="backend")
    await scheduler.run()

    # The operator deals with the blocker and clears it.
    tasks.unblock(task.key)
    stored = tasks.get_task(task.key)
    assert not stored.needs_intervention
    assert stored.status is TaskStatus.READY

    runtime.raw_text = {}  # blocker resolved
    report = await scheduler.run()
    assert report.completed == [task.key]


async def test_blocked_dependency_blocks_dependents(db, tmp_path) -> None:
    config = make_config(max_task_retries=0)
    runtime = RecordingRuntime(
        raw_text={"T-1": response_block(status="blocked", blockers=["needs a key"])}
    )
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    first = tasks.create_task("blocker", agent="backend")
    second = tasks.create_task("dependent", agent="frontend", depends_on=[first.key])

    report = await scheduler.run()
    assert first.key in report.blocked
    assert second.key in report.blocked
    assert second.key not in runtime.order


async def test_launch_failure_is_retried(db, tmp_path) -> None:
    """An infrastructure failure should get another attempt."""
    config = make_config(max_task_retries=1)

    class Flaky(RecordingRuntime):
        def __init__(self):
            super().__init__()
            self.calls = 0

        async def run(self, request, on_event=None):
            self.calls += 1
            self.order.append(self._label(request.prompt))
            self.requests.append(request)
            if self.calls == 1:
                raise OSError("failed to spawn claude.exe")
            return RunResult(
                status=RunStatus.SUCCEEDED,
                session_id="s",
                exit_code=0,
                text=response_block(summary="worked on retry"),
            )

    runtime = Flaky()
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    task = tasks.create_task("work", agent="backend")
    report = await scheduler.run()

    assert report.completed == [task.key]
    assert runtime.calls == 2


async def test_dry_run_task_failure_is_not_a_process_failure(db, tmp_path) -> None:
    """A task failing must not look like infrastructure breaking.

    Regression: the dry-run stub simulated a failed task by failing the process,
    which classified as LAUNCH and burned retries on something that was really
    the agent's own verdict.
    """
    config = make_config(max_task_retries=0)
    runtime = DryRunRuntime(fail_keys={"T-1"})
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    task = tasks.create_task("doomed", agent="backend")

    report = await scheduler.run()
    assert report.failed == [task.key]
    # The process succeeded; the agent reported failure.
    from agentos.db.models import Run

    with db.session() as session:
        row = session.query(Run).order_by(Run.id.desc()).first()
        assert row.exit_code == 0
        assert row.status == "succeeded"


async def test_dry_run_blocker_needs_intervention(db, tmp_path) -> None:
    config = make_config(max_task_retries=3)
    runtime = DryRunRuntime(block_keys={"T-1"})
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    task = tasks.create_task("blocked work", agent="backend")

    report = await scheduler.run()
    stored = tasks.get_task(task.key)
    assert stored.status is TaskStatus.BLOCKED
    assert stored.needs_intervention
    assert stored.attempts == 0, "a blocker must not consume retries"
    assert task.key in report.blocked


async def test_dry_run_crash_is_infrastructure_and_retries(db, tmp_path) -> None:
    config = make_config(max_task_retries=1)
    runtime = DryRunRuntime(crash_keys={"T-1"})
    scheduler, tasks, _ = build(db, config, runtime, tmp_path)
    task = tasks.create_task("crashes", agent="backend")

    report = await scheduler.run()
    assert report.failed == [task.key]
    # Initial attempt plus one retry, because a crash is worth retrying.
    assert len(runtime.requests) == 2
    assert not tasks.get_task(task.key).needs_intervention

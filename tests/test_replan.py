"""Phase 17: replanning.

`validate_operations` is pure, so every refusal rule is tested directly. The
invariant it defends is that completed history is immutable: a manager recovering
from a failure may add corrective work and rewire what has not happened, and
nothing else.
"""

from __future__ import annotations

import json

import pytest

from agentos.config import Config
from agentos.db.session import Database
from agentos.schemas.dto import TaskView
from agentos.schemas.enums import RunStatus, TaskStatus
from agentos.schemas.replan import (
    MAX_OPERATIONS,
    REPLAN_BEGIN,
    REPLAN_END,
    Operation,
    OperationType,
    ReplanParseError,
    ReplanProposal,
    ReplanTrigger,
    parse_replan,
)
from agentos.schemas.runtime import RunResult
from agentos.services.agents import AgentService
from agentos.services.events import EventBus
from agentos.services.replan_service import ReplanRequest, ReplanService
from agentos.services.replanner import Replanner, validate_operations
from agentos.services.tasks import TaskService
from tests.test_agents import StubRuntime

AGENTS = {"manager", "backend", "frontend", "qa", "reviewer"}

CONFIG = {
    "project": {"name": "Replan"},
    "orchestrator": {"max_task_retries": 0},
    "agents": {
        "manager": {"role": "manager"},
        "backend": {"role": "backend"},
        "frontend": {"role": "frontend"},
        "qa": {"role": "qa"},
        "reviewer": {"role": "reviewer"},
    },
}


def task(key: str, status: TaskStatus, agent: str = "backend", deps=None) -> TaskView:
    return TaskView(
        id=int(key.split("-")[-1]),
        key=key,
        title=f"task {key}",
        status=status,
        assigned_agent=agent,
        depends_on=deps or [],
    )


def proposal(*operations: dict, assessment: str = "something broke") -> ReplanProposal:
    return ReplanProposal.model_validate(
        {"assessment": assessment, "operations": list(operations)}
    )


def create_op(temp_id="FIX-1", agent="backend", title="Fix it") -> dict:
    return {
        "type": "create_task",
        "temp_id": temp_id,
        "agent": agent,
        "title": title,
        "description": "do the fix",
    }


# ------------------------------------------------------------------- parsing


def test_parses_a_well_formed_replan() -> None:
    payload = {"assessment": "callback is wrong", "operations": [create_op()]}
    text = "\n".join(["My reading:", REPLAN_BEGIN, json.dumps(payload), REPLAN_END])
    parsed = parse_replan(text)
    assert parsed.assessment == "callback is wrong"
    assert parsed.operations[0].type is OperationType.CREATE_TASK


def test_missing_block_raises() -> None:
    with pytest.raises(ReplanParseError, match="no .* block found"):
        parse_replan("I think we should fix the callback.")


def test_invalid_json_raises() -> None:
    with pytest.raises(ReplanParseError, match="not valid JSON"):
        parse_replan("\n".join([REPLAN_BEGIN, "{nope", REPLAN_END]))


def test_last_block_wins() -> None:
    first = json.dumps({"operations": [create_op(temp_id="OLD")]})
    second = json.dumps({"operations": [create_op(temp_id="REAL")]})
    text = (
        f"{REPLAN_BEGIN}{first}{REPLAN_END}\n{REPLAN_BEGIN}{second}{REPLAN_END}"
    )
    assert parse_replan(text).operations[0].temp_id == "REAL"


def test_create_without_a_title_is_rejected() -> None:
    payload = {"operations": [{"type": "create_task", "agent": "backend"}]}
    with pytest.raises(ReplanParseError, match="title"):
        parse_replan("\n".join([REPLAN_BEGIN, json.dumps(payload), REPLAN_END]))


def test_dependency_without_both_ends_is_rejected() -> None:
    payload = {"operations": [{"type": "add_dependency", "task": "A"}]}
    with pytest.raises(ReplanParseError, match="depends_on"):
        parse_replan("\n".join([REPLAN_BEGIN, json.dumps(payload), REPLAN_END]))


def test_unknown_operation_type_is_rejected() -> None:
    payload = {"operations": [{"type": "delete_everything", "task": "A"}]}
    with pytest.raises(ReplanParseError):
        parse_replan("\n".join([REPLAN_BEGIN, json.dumps(payload), REPLAN_END]))


def test_operation_describes_itself() -> None:
    op = Operation(type=OperationType.ADD_DEPENDENCY, task="A", depends_on="B")
    assert op.describe() == "A depends on B"


# --------------------------------------------------- the immutability invariant


def test_completed_task_may_not_be_cancelled() -> None:
    """The rule this phase exists to defend."""
    tasks = [task("A-1", TaskStatus.COMPLETED)]
    result = validate_operations(
        proposal({"type": "cancel_task", "task": "A-1"}), tasks, AGENTS
    )
    assert not result.ok
    assert any("immutable" in e for e in result.errors)


def test_completed_task_may_not_be_retried() -> None:
    tasks = [task("A-1", TaskStatus.COMPLETED)]
    result = validate_operations(
        proposal({"type": "retry_task", "task": "A-1"}), tasks, AGENTS
    )
    assert not result.ok


def test_completed_task_may_not_be_reassigned() -> None:
    tasks = [task("A-1", TaskStatus.COMPLETED)]
    result = validate_operations(
        proposal({"type": "reassign_task", "task": "A-1", "agent": "qa"}),
        tasks,
        AGENTS,
    )
    assert not result.ok


def test_cancelled_task_may_not_be_modified() -> None:
    tasks = [task("A-1", TaskStatus.CANCELLED)]
    result = validate_operations(
        proposal({"type": "retry_task", "task": "A-1"}), tasks, AGENTS
    )
    assert not result.ok


def test_running_task_may_not_be_cancelled() -> None:
    tasks = [task("A-1", TaskStatus.RUNNING)]
    result = validate_operations(
        proposal({"type": "cancel_task", "task": "A-1"}), tasks, AGENTS
    )
    assert not result.ok
    assert any("running" in e for e in result.errors)


def test_pending_task_may_be_cancelled() -> None:
    tasks = [task("A-1", TaskStatus.PENDING)]
    assert validate_operations(
        proposal({"type": "cancel_task", "task": "A-1"}), tasks, AGENTS
    ).ok


def test_only_failed_or_blocked_may_be_retried() -> None:
    for status in (TaskStatus.FAILED, TaskStatus.BLOCKED):
        assert validate_operations(
            proposal({"type": "retry_task", "task": "A-1"}),
            [task("A-1", status)],
            AGENTS,
        ).ok
    for status in (TaskStatus.READY, TaskStatus.PENDING, TaskStatus.RUNNING):
        result = validate_operations(
            proposal({"type": "retry_task", "task": "A-1"}),
            [task("A-1", status)],
            AGENTS,
        )
        assert not result.ok


# --------------------------------------------------------- reference validation


def test_unknown_task_is_rejected() -> None:
    result = validate_operations(
        proposal({"type": "cancel_task", "task": "GHOST-9"}), [], AGENTS
    )
    assert any("unknown task" in e for e in result.errors)


def test_unknown_agent_is_rejected() -> None:
    result = validate_operations(
        proposal(create_op(agent="astronaut")), [], AGENTS
    )
    assert any("astronaut" in e for e in result.errors)


def test_unknown_dependency_is_rejected() -> None:
    tasks = [task("A-1", TaskStatus.PENDING)]
    result = validate_operations(
        proposal({"type": "add_dependency", "task": "A-1", "depends_on": "NOPE"}),
        tasks,
        AGENTS,
    )
    assert any("unknown dependency" in e for e in result.errors)


def test_self_dependency_is_rejected() -> None:
    tasks = [task("A-1", TaskStatus.PENDING)]
    result = validate_operations(
        proposal({"type": "add_dependency", "task": "A-1", "depends_on": "A-1"}),
        tasks,
        AGENTS,
    )
    assert any("itself" in e for e in result.errors)


def test_a_task_created_in_this_plan_can_be_referenced() -> None:
    """The temp_id mechanism, which is the point of returning operations."""
    tasks = [task("QA-1", TaskStatus.PENDING, agent="qa")]
    result = validate_operations(
        proposal(
            create_op(temp_id="FIX-CALLBACK"),
            {"type": "add_dependency", "task": "QA-1", "depends_on": "FIX-CALLBACK"},
        ),
        tasks,
        AGENTS,
    )
    assert result.ok
    assert len(result.accepted) == 2


def test_duplicate_temp_id_is_rejected() -> None:
    result = validate_operations(
        proposal(create_op(temp_id="FIX"), create_op(temp_id="FIX", title="Other")),
        [],
        AGENTS,
    )
    assert any("already used" in e for e in result.errors)


def test_temp_id_clashing_with_a_real_key_is_rejected() -> None:
    tasks = [task("A-1", TaskStatus.PENDING)]
    result = validate_operations(proposal(create_op(temp_id="A-1")), tasks, AGENTS)
    assert any("already used" in e for e in result.errors)


# ------------------------------------------------------------- cycle refusal


def test_direct_cycle_is_refused() -> None:
    tasks = [
        task("A-1", TaskStatus.PENDING),
        task("A-2", TaskStatus.PENDING, deps=["A-1"]),
    ]
    result = validate_operations(
        proposal({"type": "add_dependency", "task": "A-1", "depends_on": "A-2"}),
        tasks,
        AGENTS,
    )
    assert any("cycle" in e for e in result.errors)


def test_transitive_cycle_is_refused() -> None:
    tasks = [
        task("A-1", TaskStatus.PENDING),
        task("A-2", TaskStatus.PENDING, deps=["A-1"]),
        task("A-3", TaskStatus.PENDING, deps=["A-2"]),
    ]
    result = validate_operations(
        proposal({"type": "add_dependency", "task": "A-1", "depends_on": "A-3"}),
        tasks,
        AGENTS,
    )
    assert any("cycle" in e for e in result.errors)


def test_cycle_across_two_operations_is_refused() -> None:
    """Neither edge is a cycle alone, so the check must consider both."""
    tasks = [
        task("A-1", TaskStatus.PENDING),
        task("A-2", TaskStatus.PENDING),
    ]
    result = validate_operations(
        proposal(
            {"type": "add_dependency", "task": "A-1", "depends_on": "A-2"},
            {"type": "add_dependency", "task": "A-2", "depends_on": "A-1"},
        ),
        tasks,
        AGENTS,
    )
    assert any("cycle" in e for e in result.errors)
    assert len(result.accepted) == 1


def test_safe_dependency_is_accepted() -> None:
    tasks = [
        task("A-1", TaskStatus.PENDING),
        task("A-2", TaskStatus.PENDING),
    ]
    assert validate_operations(
        proposal({"type": "add_dependency", "task": "A-2", "depends_on": "A-1"}),
        tasks,
        AGENTS,
    ).ok


def test_dependency_on_a_running_task_warns() -> None:
    tasks = [
        task("A-1", TaskStatus.PENDING),
        task("A-2", TaskStatus.RUNNING),
    ]
    result = validate_operations(
        proposal({"type": "add_dependency", "task": "A-2", "depends_on": "A-1"}),
        tasks,
        AGENTS,
    )
    assert result.ok
    assert any("already running" in w for w in result.warnings)


# ----------------------------------------------------------------- size limits


def test_empty_replan_is_rejected() -> None:
    result = validate_operations(proposal(), [], AGENTS)
    assert any("no operations" in e for e in result.errors)


def test_oversized_replan_is_rejected() -> None:
    operations = [create_op(temp_id=f"F{i}") for i in range(MAX_OPERATIONS + 1)]
    result = validate_operations(proposal(*operations), [], AGENTS)
    assert any("more than" in e for e in result.errors)


def test_a_bad_operation_does_not_reject_the_good_ones() -> None:
    """But it is reported, so a partial application is never mistaken for full."""
    tasks = [task("A-1", TaskStatus.COMPLETED), task("A-2", TaskStatus.PENDING)]
    result = validate_operations(
        proposal(
            {"type": "cancel_task", "task": "A-1"},
            {"type": "cancel_task", "task": "A-2"},
        ),
        tasks,
        AGENTS,
    )
    assert len(result.accepted) == 1
    assert len(result.errors) == 1
    assert not result.ok  # errors present
    assert result.has_anything


# ------------------------------------------------------------------- applying


@pytest.fixture
def config() -> Config:
    return Config.model_validate(CONFIG)


@pytest.fixture
def wiring(db: Database, config: Config, tmp_path):
    agents = AgentService(db, config, StubRuntime(), tmp_path)
    agents.sync_from_config()
    bus = EventBus(db)
    tasks = TaskService(db, config, event_bus=bus)
    return agents, tasks, Replanner(db, config, tasks, bus)


def test_apply_creates_a_corrective_task(wiring) -> None:
    _agents, tasks, replanner = wiring
    tasks.create_task("original", agent="backend", prefix="AUTH")

    outcome = replanner.apply(proposal(create_op(title="Fix the callback")))
    assert len(outcome.created) == 1
    created = tasks.get_task(outcome.created[0])
    assert created.title == "Fix the callback"
    assert created.assigned_agent == "backend"
    assert created.created_by == "manager"


def test_apply_wires_temp_ids_into_real_keys(wiring) -> None:
    """The crux: a dependency on a task created in the same replan."""
    _agents, tasks, replanner = wiring
    qa = tasks.create_task("qa work", agent="qa", prefix="AUTH")

    outcome = replanner.apply(
        proposal(
            create_op(temp_id="FIX-CALLBACK", title="Fix the callback"),
            {"type": "add_dependency", "task": qa.key, "depends_on": "FIX-CALLBACK"},
        )
    )
    fix_key = outcome.created[0]
    assert tasks.get_task(qa.key).depends_on == [fix_key]
    # No temp id leaked into storage.
    assert "FIX-CALLBACK" not in tasks.get_task(qa.key).depends_on


def test_apply_makes_the_downstream_task_wait(wiring) -> None:
    _agents, tasks, replanner = wiring
    qa = tasks.create_task("qa work", agent="qa", prefix="AUTH")
    assert tasks.get_task(qa.key).status is TaskStatus.READY

    replanner.apply(
        proposal(
            create_op(temp_id="FIX", title="Fix it"),
            {"type": "add_dependency", "task": qa.key, "depends_on": "FIX"},
        )
    )
    assert tasks.get_task(qa.key).status is TaskStatus.PENDING


def test_apply_retries_a_failed_task(wiring) -> None:
    _agents, tasks, replanner = wiring
    failed = tasks.create_task("broken", agent="backend")
    tasks.transition(failed.key, TaskStatus.RUNNING)
    tasks.transition(failed.key, TaskStatus.FAILED)

    outcome = replanner.apply(
        proposal({"type": "retry_task", "task": failed.key})
    )
    assert outcome.retried == [failed.key]
    assert tasks.get_task(failed.key).status is TaskStatus.READY


def test_apply_unblocks_a_blocked_task(wiring) -> None:
    """retry_task on a blocked task clears the intervention flag."""
    _agents, tasks, replanner = wiring
    blocked = tasks.create_task("stuck", agent="backend")
    tasks.transition(blocked.key, TaskStatus.RUNNING)
    tasks.transition(
        blocked.key, TaskStatus.BLOCKED, error="needs a key", needs_intervention=True
    )

    replanner.apply(proposal({"type": "retry_task", "task": blocked.key}))
    result = tasks.get_task(blocked.key)
    assert not result.needs_intervention
    assert result.status is TaskStatus.READY


def test_apply_cancels_and_reassigns(wiring) -> None:
    _agents, tasks, replanner = wiring
    doomed = tasks.create_task("wrong approach", agent="backend")
    moving = tasks.create_task("wrong owner", agent="backend")

    outcome = replanner.apply(
        proposal(
            {"type": "cancel_task", "task": doomed.key},
            {"type": "reassign_task", "task": moving.key, "agent": "frontend"},
        )
    )
    assert outcome.cancelled == [doomed.key]
    assert tasks.get_task(doomed.key).status is TaskStatus.CANCELLED
    assert tasks.get_task(moving.key).assigned_agent == "frontend"


def test_apply_refuses_an_all_invalid_replan(wiring) -> None:
    _agents, tasks, replanner = wiring
    done = tasks.create_task("finished", agent="backend")
    tasks.transition(done.key, TaskStatus.RUNNING)
    tasks.transition(done.key, TaskStatus.COMPLETED)

    with pytest.raises(ValueError, match="nothing to apply"):
        replanner.apply(proposal({"type": "cancel_task", "task": done.key}))
    # Nothing changed.
    assert tasks.get_task(done.key).status is TaskStatus.COMPLETED


def test_apply_leaves_the_graph_sound(wiring) -> None:
    _agents, tasks, replanner = wiring
    first = tasks.create_task("a", agent="backend")
    second = tasks.create_task("b", agent="frontend", depends_on=[first.key])

    replanner.apply(
        proposal(
            create_op(temp_id="FIX", title="Fix it"),
            {"type": "add_dependency", "task": second.key, "depends_on": "FIX"},
        )
    )
    tasks.validate_graph()


# ------------------------------------------------------------- auto triggers


def test_auto_replan_is_off_by_default(db, config, tmp_path) -> None:
    """An unattended replan loop is an expensive way to be wrong."""
    agents = AgentService(db, config, StubRuntime(), tmp_path)
    agents.sync_from_config()
    service = ReplanService(db, config, agents, TaskService(db, config))

    assert not service.should_auto_replan(ReplanTrigger.TASK_FAILED)
    assert not service.should_auto_replan(ReplanTrigger.BLOCKER)
    # An explicit request always proceeds.
    assert service.should_auto_replan(ReplanTrigger.MANUAL)


def test_auto_replan_can_be_enabled(db, tmp_path) -> None:
    config = Config.model_validate(
        {**CONFIG, "orchestrator": {"auto_replan": {"task_failed": True}}}
    )
    agents = AgentService(db, config, StubRuntime(), tmp_path)
    agents.sync_from_config()
    service = ReplanService(db, config, agents, TaskService(db, config))

    assert service.should_auto_replan(ReplanTrigger.TASK_FAILED)
    assert not service.should_auto_replan(ReplanTrigger.BLOCKER)


def test_request_for_failure_picks_the_right_trigger(db, config, tmp_path) -> None:
    agents = AgentService(db, config, StubRuntime(), tmp_path)
    agents.sync_from_config()
    tasks = TaskService(db, config)
    service = ReplanService(db, config, agents, tasks)

    failed = task("A-1", TaskStatus.FAILED)
    assert service.request_for_failure(failed).trigger is ReplanTrigger.TASK_FAILED

    blocked = task("A-2", TaskStatus.BLOCKED)
    assert service.request_for_failure(blocked).trigger is ReplanTrigger.BLOCKER


# --------------------------------------------------------- end to end proposal


async def test_propose_and_apply_end_to_end(db, config, tmp_path) -> None:
    """The success criterion: failure -> manager -> validated mutation -> runnable."""
    payload = {
        "assessment": "The callback rejects valid state tokens.",
        "operations": [
            {
                "type": "create_task",
                "temp_id": "FIX-CALLBACK",
                "agent": "backend",
                "title": "Fix the OAuth callback",
                "description": "Accept valid state tokens.",
                "acceptance_criteria": ["Valid state is accepted"],
            },
            {
                "type": "add_dependency",
                "task": "AUTH-2",
                "depends_on": "FIX-CALLBACK",
            },
        ],
    }
    reply = "\n".join([REPLAN_BEGIN, json.dumps(payload), REPLAN_END])

    class ScriptedManager(StubRuntime):
        async def run(self, request, on_event=None):
            self.requests.append(request)
            return RunResult(
                status=RunStatus.SUCCEEDED, session_id="mgr", exit_code=0, text=reply
            )

    runtime = ScriptedManager()
    agents = AgentService(db, config, runtime, tmp_path)
    agents.sync_from_config()
    bus = EventBus(db)
    tasks = TaskService(db, config, event_bus=bus)
    service = ReplanService(db, config, agents, tasks, bus)

    broken = tasks.create_task("Implement OAuth", agent="backend", prefix="AUTH")
    qa = tasks.create_task("Test OAuth", agent="qa", prefix="AUTH", depends_on=[broken.key])
    tasks.transition(broken.key, TaskStatus.RUNNING)
    tasks.transition(broken.key, TaskStatus.FAILED, error="state token rejected")
    tasks.refresh_readiness()
    assert tasks.get_task(qa.key).status is TaskStatus.BLOCKED

    result = await service.propose(
        service.request_for_failure(tasks.get_task(broken.key))
    )
    assert result.ok

    # The manager was told what failed and what is already done.
    prompt = runtime.requests[0].prompt
    assert broken.key in prompt
    assert "state token rejected" in prompt
    assert "immutable" in prompt

    outcome = service.apply(result)
    assert len(outcome.created) == 1
    fix_key = outcome.created[0]

    # The fix is runnable and QA now waits on it.
    assert tasks.get_task(fix_key).status is TaskStatus.READY
    assert fix_key in tasks.get_task(qa.key).depends_on


async def test_unparseable_replan_changes_nothing(db, config, tmp_path) -> None:
    class Rambler(StubRuntime):
        async def run(self, request, on_event=None):
            self.requests.append(request)
            return RunResult(
                status=RunStatus.SUCCEEDED,
                session_id="mgr",
                exit_code=0,
                text="I think we should probably just try again.",
            )

    agents = AgentService(db, config, Rambler(), tmp_path)
    agents.sync_from_config()
    tasks = TaskService(db, config)
    service = ReplanService(db, config, agents, tasks)
    tasks.create_task("work", agent="backend")

    result = await service.propose(
        ReplanRequest(objective_id=None, trigger=ReplanTrigger.MANUAL)
    )
    assert not result.ok
    assert result.errors
    with pytest.raises(ValueError):
        service.apply(result)
    assert tasks.tasks.count() == 1


# ------------------------------------ remove_dependency: unsticking a graph


def test_remove_dependency_requires_the_edge_to_exist() -> None:
    tasks = [task("A-1", TaskStatus.PENDING), task("A-2", TaskStatus.PENDING)]
    result = validate_operations(
        proposal({"type": "remove_dependency", "task": "A-2", "depends_on": "A-1"}),
        tasks,
        AGENTS,
    )
    assert any("does not depend on" in e for e in result.errors)


def test_remove_dependency_on_a_real_edge_is_accepted() -> None:
    tasks = [
        task("A-1", TaskStatus.CANCELLED),
        task("A-2", TaskStatus.BLOCKED, deps=["A-1"]),
    ]
    assert validate_operations(
        proposal({"type": "remove_dependency", "task": "A-2", "depends_on": "A-1"}),
        tasks,
        AGENTS,
    ).ok


def test_cannot_rewire_a_completed_task() -> None:
    """Immutability still applies to the task being rewired."""
    tasks = [
        task("A-1", TaskStatus.COMPLETED),
        task("A-2", TaskStatus.COMPLETED, deps=["A-1"]),
    ]
    result = validate_operations(
        proposal({"type": "remove_dependency", "task": "A-2", "depends_on": "A-1"}),
        tasks,
        AGENTS,
    )
    assert any("immutable" in e for e in result.errors)


def test_cancelling_without_rewiring_leaves_work_stuck(wiring) -> None:
    """The gap that motivated remove_dependency.

    Cancelling a prerequisite without dropping the edge blocks its dependents
    forever, because a cancelled dependency can never complete.
    """
    _agents, tasks, replanner = wiring
    first = tasks.create_task("superseded", agent="backend", prefix="AUTH")
    second = tasks.create_task(
        "downstream", agent="qa", prefix="AUTH", depends_on=[first.key]
    )

    replanner.apply(proposal({"type": "cancel_task", "task": first.key}))
    assert tasks.get_task(second.key).status is TaskStatus.BLOCKED


def test_removing_the_edge_unsticks_it(wiring) -> None:
    _agents, tasks, replanner = wiring
    first = tasks.create_task("superseded", agent="backend", prefix="AUTH")
    second = tasks.create_task(
        "downstream", agent="qa", prefix="AUTH", depends_on=[first.key]
    )
    replanner.apply(proposal({"type": "cancel_task", "task": first.key}))

    outcome = replanner.apply(
        proposal(
            {
                "type": "remove_dependency",
                "task": second.key,
                "depends_on": first.key,
            }
        )
    )
    assert outcome.dependencies
    result = tasks.get_task(second.key)
    assert result.depends_on == []
    assert result.status is TaskStatus.READY


def test_full_corrective_rewire(wiring) -> None:
    """Cancel the broken task, create a fix, and move the dependency across."""
    _agents, tasks, replanner = wiring
    broken = tasks.create_task("broken approach", agent="backend", prefix="AUTH")
    qa = tasks.create_task("test it", agent="qa", prefix="AUTH", depends_on=[broken.key])

    outcome = replanner.apply(
        proposal(
            create_op(temp_id="FIX", title="Targeted fix"),
            {"type": "add_dependency", "task": qa.key, "depends_on": "FIX"},
            {"type": "remove_dependency", "task": qa.key, "depends_on": broken.key},
            {"type": "cancel_task", "task": broken.key},
        )
    )
    fix_key = outcome.created[0]
    final = tasks.get_task(qa.key)
    assert final.depends_on == [fix_key]
    assert tasks.get_task(broken.key).status is TaskStatus.CANCELLED
    assert tasks.get_task(fix_key).status is TaskStatus.READY
    # And QA becomes runnable once the fix completes.
    tasks.transition(fix_key, TaskStatus.RUNNING)
    tasks.transition(fix_key, TaskStatus.COMPLETED)
    tasks.refresh_readiness()
    assert tasks.get_task(qa.key).status is TaskStatus.READY


def test_prompt_documents_the_rewiring_rule() -> None:
    from agentos.prompts.replan_prompt import build_replan_prompt

    prompt = build_replan_prompt(
        "goal", ReplanTrigger.TASK_FAILED, {"backend": "backend"}
    )
    assert "remove_dependency" in prompt
    assert "blocked forever" in prompt

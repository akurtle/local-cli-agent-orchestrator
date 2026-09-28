"""Phase 5: the objective flow, end to end with a scripted manager.

The manager's reply is fixed, so planning costs nothing. What is under test is
the approval gate, the temp-id translation and deterministic completion.
"""

from __future__ import annotations

import pytest

from agentos.config import Config
from agentos.schemas.enums import ObjectiveStatus, RunStatus, TaskStatus
from agentos.schemas.runtime import RunResult
from agentos.services.agents import AgentService
from agentos.services.objectives import ObjectiveService
from agentos.services.tasks import TaskService
from tests.test_agents import StubRuntime
from tests.test_planner import CONFIG_DICT, plan_text, task


class ScriptedManager(StubRuntime):
    """A manager whose reply is fixed, so planning launches nothing."""

    def __init__(self, reply: str, ok: bool = True) -> None:
        super().__init__()
        self.reply = reply
        self.succeeds = ok

    async def run(self, request, on_event=None) -> RunResult:
        self.requests.append(request)
        return RunResult(
            status=RunStatus.SUCCEEDED if self.succeeds else RunStatus.FAILED,
            session_id=request.session_id or "mgr-session",
            exit_code=0 if self.succeeds else 1,
            text=self.reply,
            error=None if self.succeeds else "manager crashed",
        )


GOOD_PLAN = plan_text(
    {
        "objective": "Add a health-check endpoint and tests",
        "tasks": [
            task("HEALTH-BE", agent="backend", title="Add health endpoint"),
            task(
                "HEALTH-QA",
                agent="qa",
                title="Add health endpoint tests",
                depends_on=["HEALTH-BE"],
            ),
            task(
                "HEALTH-REVIEW",
                agent="reviewer",
                title="Review implementation",
                depends_on=["HEALTH-QA"],
            ),
        ],
    }
)


@pytest.fixture
def config() -> Config:
    return Config.model_validate(CONFIG_DICT)


def build(db, config, runtime, tmp_path) -> ObjectiveService:
    agents = AgentService(db, config, runtime, tmp_path)
    agents.sync_from_config()
    return ObjectiveService(db, config, agents, TaskService(db, config))


# -------------------------------------------------------------- the happy path


async def test_propose_returns_a_valid_plan(db, config, tmp_path) -> None:
    service = build(db, config, ScriptedManager(GOOD_PLAN), tmp_path)
    proposal = await service.propose("Add a health-check endpoint and tests")
    assert proposal.ok
    assert [t.temp_id for t in proposal.plan.tasks] == [
        "HEALTH-BE",
        "HEALTH-QA",
        "HEALTH-REVIEW",
    ]


async def test_propose_creates_no_tasks_before_approval(db, config, tmp_path) -> None:
    """The approval gate must be real: nothing persisted until approved."""
    service = build(db, config, ScriptedManager(GOOD_PLAN), tmp_path)
    proposal = await service.propose("Add health checks")
    assert service.task_repo.count() == 0
    assert proposal.objective.status is ObjectiveStatus.AWAITING_APPROVAL


async def test_approve_persists_the_plan(db, config, tmp_path) -> None:
    service = build(db, config, ScriptedManager(GOOD_PLAN), tmp_path)
    proposal = await service.propose("Add health checks")
    created = service.approve(proposal)

    assert len(created) == 3
    assert service.task_repo.count() == 3
    assert service.get_objective(proposal.objective.id).status is ObjectiveStatus.ACTIVE


async def test_approved_plan_has_correct_dependency_order(db, config, tmp_path) -> None:
    service = build(db, config, ScriptedManager(GOOD_PLAN), tmp_path)
    proposal = await service.propose("Add health checks")
    created = service.approve(proposal)

    by_title = {t.title: t for t in created}
    qa = service.tasks.get_task(by_title["Add health endpoint tests"].key)
    review = service.tasks.get_task(by_title["Review implementation"].key)
    assert qa.depends_on == [by_title["Add health endpoint"].key]
    assert review.depends_on == [qa.key]
    # Only the first task is runnable.
    assert qa.status is TaskStatus.PENDING
    assert service.tasks.get_task(by_title["Add health endpoint"].key).status is (
        TaskStatus.READY
    )


async def test_reject_creates_nothing(db, config, tmp_path) -> None:
    service = build(db, config, ScriptedManager(GOOD_PLAN), tmp_path)
    proposal = await service.propose("Add health checks")
    service.reject(proposal)
    assert service.task_repo.count() == 0
    assert (
        service.get_objective(proposal.objective.id).status is ObjectiveStatus.CANCELLED
    )


# ----------------------------------------------------------- the manager prompt


async def test_manager_prompt_contains_the_roster(db, config, tmp_path) -> None:
    """The manager must be told who exists, so it cannot invent agents."""
    runtime = ScriptedManager(GOOD_PLAN)
    service = build(db, config, runtime, tmp_path)
    await service.propose("Add health checks")
    prompt = runtime.requests[0].prompt
    for name in ("backend", "frontend", "qa", "reviewer"):
        assert f"`{name}`" in prompt
    assert "cannot create new" in prompt


async def test_manager_prompt_forbids_implementation(db, config, tmp_path) -> None:
    runtime = ScriptedManager(GOOD_PLAN)
    service = build(db, config, runtime, tmp_path)
    await service.propose("Add health checks")
    assert "do not edit any files" in runtime.requests[0].prompt


async def test_manager_prompt_lists_existing_open_tasks(db, config, tmp_path) -> None:
    runtime = ScriptedManager(GOOD_PLAN)
    service = build(db, config, runtime, tmp_path)
    existing = service.tasks.create_task("pre-existing work", agent="backend")
    await service.propose("Add health checks")
    assert existing.key in runtime.requests[0].prompt


async def test_manager_session_persists(db, config, tmp_path) -> None:
    runtime = ScriptedManager(GOOD_PLAN)
    service = build(db, config, runtime, tmp_path)
    await service.propose("First objective")
    await service.propose("Second objective")
    assert runtime.requests[1].resume is True


# ------------------------------------------------------------------- rejections


async def test_unparseable_plan_fails_the_objective(db, config, tmp_path) -> None:
    service = build(db, config, ScriptedManager("I thought about it. Trust me."), tmp_path)
    proposal = await service.propose("Add health checks")
    assert not proposal.ok
    assert proposal.errors
    assert service.task_repo.count() == 0
    assert proposal.objective.status is ObjectiveStatus.FAILED


async def test_invalid_plan_is_rejected_with_reasons(db, config, tmp_path) -> None:
    bad = plan_text({"tasks": [task("A", agent="astronaut")]})
    service = build(db, config, ScriptedManager(bad), tmp_path)
    proposal = await service.propose("Do something")
    assert not proposal.ok
    assert any("astronaut" in e for e in proposal.errors)
    assert service.task_repo.count() == 0


async def test_cannot_approve_an_invalid_plan(db, config, tmp_path) -> None:
    bad = plan_text({"tasks": [task("A", agent="ghost")]})
    service = build(db, config, ScriptedManager(bad), tmp_path)
    proposal = await service.propose("Do something")
    with pytest.raises(ValueError, match="cannot approve an invalid plan"):
        service.approve(proposal)
    assert service.task_repo.count() == 0


async def test_cyclic_plan_is_rejected(db, config, tmp_path) -> None:
    bad = plan_text(
        {
            "tasks": [
                task("A", depends_on=["B"]),
                task("B", agent="qa", depends_on=["A"]),
            ]
        }
    )
    service = build(db, config, ScriptedManager(bad), tmp_path)
    proposal = await service.propose("Do something")
    assert not proposal.ok
    assert any("cycle" in e for e in proposal.errors)


async def test_failed_manager_run_is_handled(db, config, tmp_path) -> None:
    service = build(db, config, ScriptedManager("", ok=False), tmp_path)
    proposal = await service.propose("Do something")
    assert not proposal.ok
    assert proposal.objective.status is ObjectiveStatus.FAILED
    assert service.task_repo.count() == 0


async def test_raw_reply_is_kept_for_diagnosis(db, config, tmp_path) -> None:
    service = build(db, config, ScriptedManager("no plan here"), tmp_path)
    proposal = await service.propose("Do something")
    assert "no plan here" in proposal.raw_text


# ------------------------------------------------------------ manager selection


def test_missing_manager_role_is_reported(db, tmp_path) -> None:
    config = Config.model_validate({"agents": {"backend": {"role": "backend"}}})
    service = build(db, config, ScriptedManager(GOOD_PLAN), tmp_path)
    with pytest.raises(ValueError, match="no agent has the 'manager' role"):
        service.manager_name()


def test_ambiguous_manager_role_is_reported(db, tmp_path) -> None:
    config = Config.model_validate(
        {"agents": {"boss": {"role": "manager"}, "chief": {"role": "manager"}}}
    )
    service = build(db, config, ScriptedManager(GOOD_PLAN), tmp_path)
    with pytest.raises(ValueError, match="several agents have the .manager. role"):
        service.manager_name()


def test_manager_found_by_role_not_name(db, tmp_path) -> None:
    config = Config.model_validate(
        {"agents": {"boss": {"role": "manager"}, "backend": {"role": "backend"}}}
    )
    service = build(db, config, ScriptedManager(GOOD_PLAN), tmp_path)
    assert service.manager_name() == "boss"


# ------------------------------------------------------- deterministic completion


async def test_objective_completes_when_all_tasks_complete(db, config, tmp_path) -> None:
    """Completion is computed from task state, never asked of an agent."""
    service = build(db, config, ScriptedManager(GOOD_PLAN), tmp_path)
    proposal = await service.propose("Add health checks")
    created = service.approve(proposal)

    for view in created:
        # Dependents are pending until readiness is recomputed, which the
        # scheduler normally does on every pass.
        service.tasks.refresh_readiness()
        service.tasks.transition(view.key, TaskStatus.RUNNING)
        service.tasks.transition(view.key, TaskStatus.COMPLETED)

    refreshed = service.refresh_completion(proposal.objective.id)
    assert refreshed.status is ObjectiveStatus.COMPLETED
    assert refreshed.completed_at is not None


async def test_objective_stays_active_while_work_remains(db, config, tmp_path) -> None:
    service = build(db, config, ScriptedManager(GOOD_PLAN), tmp_path)
    proposal = await service.propose("Add health checks")
    created = service.approve(proposal)
    service.tasks.transition(created[0].key, TaskStatus.RUNNING)
    service.tasks.transition(created[0].key, TaskStatus.COMPLETED)
    assert (
        service.refresh_completion(proposal.objective.id).status
        is ObjectiveStatus.ACTIVE
    )


async def test_objective_fails_when_nothing_can_progress(db, config, tmp_path) -> None:
    service = build(db, config, ScriptedManager(GOOD_PLAN), tmp_path)
    proposal = await service.propose("Add health checks")
    created = service.approve(proposal)

    service.tasks.transition(created[0].key, TaskStatus.RUNNING)
    service.tasks.transition(created[0].key, TaskStatus.FAILED)
    service.tasks.refresh_readiness()  # dependents become blocked

    assert (
        service.refresh_completion(proposal.objective.id).status
        is ObjectiveStatus.FAILED
    )


async def test_completion_ignores_other_objectives_tasks(db, config, tmp_path) -> None:
    service = build(db, config, ScriptedManager(GOOD_PLAN), tmp_path)
    proposal = await service.propose("Add health checks")
    created = service.approve(proposal)
    # An unrelated unfinished task must not hold the objective open.
    service.tasks.create_task("unrelated", agent="backend")

    for view in created:
        # Dependents are pending until readiness is recomputed, which the
        # scheduler normally does on every pass.
        service.tasks.refresh_readiness()
        service.tasks.transition(view.key, TaskStatus.RUNNING)
        service.tasks.transition(view.key, TaskStatus.COMPLETED)
    assert (
        service.refresh_completion(proposal.objective.id).status
        is ObjectiveStatus.COMPLETED
    )


async def test_unapproved_objective_is_not_marked_complete(db, config, tmp_path) -> None:
    """It has no tasks, so it must not look finished."""
    service = build(db, config, ScriptedManager(GOOD_PLAN), tmp_path)
    proposal = await service.propose("Add health checks")
    assert (
        service.refresh_completion(proposal.objective.id).status
        is ObjectiveStatus.AWAITING_APPROVAL
    )


async def test_refresh_all_skips_terminal_objectives(db, config, tmp_path) -> None:
    service = build(db, config, ScriptedManager(GOOD_PLAN), tmp_path)
    proposal = await service.propose("Add health checks")
    service.reject(proposal)
    refreshed = service.refresh_all()
    assert all(o.id != proposal.objective.id for o in refreshed)

"""Phase 18: the verification pipeline.

The claim under test: `status = completed` from an agent is a *claim*, and only
the orchestrator's own checks turn it into a completed task.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentos.config import Config
from agentos.db.session import Database
from agentos.schemas.enums import RunStatus, TaskStatus
from agentos.schemas.responses import RESPONSE_BEGIN, RESPONSE_END
from agentos.schemas.runtime import RunResult
from agentos.schemas.verification import (
    CheckResult,
    CriterionKind,
    Verdict,
    VerificationStatus,
    classify_criteria,
    classify_criterion,
)
from agentos.services.agents import AgentService
from agentos.services.events import EventBus, EventType
from agentos.services.scheduler import Scheduler
from agentos.services.tasks import TaskService
from agentos.services.verification import VerificationService
from tests.test_agents import StubRuntime


def block(status: str = "completed", summary: str = "did it") -> str:
    payload = {
        "status": status,
        "summary": summary,
        "files_changed": [],
        "messages": [],
        "requested_tasks": [],
        "blockers": [],
    }
    return "\n".join([RESPONSE_BEGIN, json.dumps(payload), RESPONSE_END])


# ------------------------------------------------- classifying criteria (pure)


def test_command_criterion_is_automated() -> None:
    criterion = classify_criterion("$ pytest tests/backend")
    assert criterion.kind is CriterionKind.AUTOMATED
    assert criterion.command == ["pytest", "tests/backend"]


@pytest.mark.parametrize("prefix", ["$", "cmd:", "command:", "run:"])
def test_all_command_prefixes_work(prefix: str) -> None:
    assert classify_criterion(f"{prefix} ruff check .").is_automated


def test_review_criterion_is_not_automated() -> None:
    """Judgement cannot be mechanised, and pretending otherwise would lie."""
    criterion = classify_criterion("The code is readable and well named")
    assert criterion.kind is CriterionKind.REVIEW
    assert criterion.command == []


def test_prose_criterion_is_manual() -> None:
    criterion = classify_criterion("OAuth callback works end to end")
    assert criterion.kind is CriterionKind.MANUAL


def test_classification_is_conservative() -> None:
    """A plausible-sounding criterion must not invent a command."""
    for text in (
        "Tests pass",
        "pytest passes",
        "All tests green",
        "Build succeeds",
    ):
        assert not classify_criterion(text).is_automated


def test_blank_criterion_is_manual_not_a_crash() -> None:
    assert classify_criterion("").kind is CriterionKind.MANUAL


def test_quoted_arguments_survive() -> None:
    criterion = classify_criterion('$ pytest -k "test auth"')
    assert criterion.command == ["pytest", "-k", "test auth"]


def test_classify_criteria_skips_blanks() -> None:
    assert len(classify_criteria(["$ pytest", "", "  ", "readable"])) == 2


# ---------------------------------------------------------------- the verdict


def passing(command: str = "pytest") -> CheckResult:
    return CheckResult(command=[command], status=VerificationStatus.PASSED, exit_code=0)


def failing(command: str = "pytest") -> CheckResult:
    return CheckResult(
        command=[command], status=VerificationStatus.FAILED, exit_code=1, detail="exit 1"
    )


def test_all_passing_is_a_pass() -> None:
    assert Verdict(checks=[passing(), passing("ruff")]).passed


def test_one_failure_fails_the_verdict() -> None:
    verdict = Verdict(checks=[passing(), failing("ruff")])
    assert not verdict.passed
    assert [c.spelled for c in verdict.failures] == ["ruff"]


def test_an_error_also_fails() -> None:
    """A check that could not run is not evidence the work is fine."""
    verdict = Verdict(
        checks=[CheckResult(command=["pytest"], status=VerificationStatus.ERROR)]
    )
    assert not verdict.passed


def test_a_skipped_check_does_not_fail_but_is_not_a_pass_either() -> None:
    verdict = Verdict(
        checks=[CheckResult(command=["pytest"], status=VerificationStatus.SKIPPED)]
    )
    assert verdict.passed  # no grounds to reject
    assert not verdict.ran_anything  # but nothing was actually verified


def test_no_checks_passes_without_claiming_verification() -> None:
    """Otherwise every project without a test command would stall."""
    verdict = Verdict()
    assert verdict.passed
    assert not verdict.ran_anything
    assert "nothing configured" in verdict.render()


def test_render_lists_failures_and_unverifiable_criteria() -> None:
    verdict = Verdict(
        checks=[failing("pytest")],
        review=classify_criteria(["The code is readable"]),
        manual=classify_criteria(["OAuth works end to end"]),
    )
    rendered = verdict.render()
    assert "FAIL" in rendered
    assert "Needs review" in rendered
    assert "Needs a human" in rendered


# ------------------------------------------------------------ config resolution


def test_agent_entry_beats_role_and_default() -> None:
    config = Config.model_validate(
        {
            "verification": {
                "api": {"commands": [["pytest", "api"]]},
                "backend": {"commands": [["pytest", "role"]]},
                "default": {"commands": [["pytest", "default"]]},
            }
        }
    )
    section = config.verification
    assert section.commands_for("api", "backend") == [["pytest", "api"]]
    assert section.commands_for("worker", "backend") == [["pytest", "role"]]
    assert section.commands_for("other", "frontend") == [["pytest", "default"]]


def test_missing_entry_means_no_checks() -> None:
    config = Config.model_validate({})
    assert config.verification.commands_for("backend", "backend") == []


def test_string_commands_are_split_not_shelled() -> None:
    config = Config.model_validate(
        {"verification": {"default": {"commands": ["ruff check ."]}}}
    )
    assert config.verification.commands_for("a", "b") == [["ruff", "check", "."]]


def test_bare_list_entry_is_accepted() -> None:
    config = Config.model_validate(
        {"verification": {"default": [["pytest"], ["ruff", "check"]]}}
    )
    assert config.verification.commands_for("a", "b") == [["pytest"], ["ruff", "check"]]


# -------------------------------------------------------------------- planning


CONFIG = {
    "project": {"name": "Verify"},
    "orchestrator": {"max_task_retries": 0},
    "commands": {"allowed": ["python", "pytest", "ruff"], "denied": ["bash"]},
    "agents": {
        "backend": {"role": "backend"},
        "qa": {"role": "qa"},
        "reviewer": {"role": "reviewer"},
    },
}


def make_config(**verification) -> Config:
    data = {k: dict(v) if isinstance(v, dict) else v for k, v in CONFIG.items()}
    if verification:
        data["verification"] = verification
    return Config.model_validate(data)


@pytest.fixture
def config() -> Config:
    return make_config()


@pytest.fixture
def tasks(db: Database, config: Config, tmp_path) -> TaskService:
    AgentService(db, config, StubRuntime(), tmp_path).sync_from_config()
    return TaskService(db, config)


def test_plan_combines_config_and_criteria(db, tasks) -> None:
    config = make_config(backend={"commands": [["ruff", "check", "."]]})
    service = VerificationService(db, config)
    task = tasks.create_task(
        "work",
        agent="backend",
        acceptance_criteria=["$ pytest tests/", "The code is readable", "It works"],
    )

    checks, review, manual = service.plan(task, "backend")
    assert [c[0] for c in checks] == [["ruff", "check", "."], ["pytest", "tests/"]]
    assert [c[1] for c in checks] == ["config", "criterion"]
    assert len(review) == 1
    assert len(manual) == 1


def test_plan_is_empty_when_disabled(db, tasks) -> None:
    config = make_config(enabled=False, backend={"commands": [["pytest"]]})
    service = VerificationService(db, config)
    task = tasks.create_task("work", agent="backend")
    assert service.plan(task, "backend") == ([], [], [])
    assert not service.is_enabled()


def test_criteria_checks_can_be_switched_off(db, tasks) -> None:
    config = make_config(verify_criteria=False)
    service = VerificationService(db, config)
    task = tasks.create_task(
        "work", agent="backend", acceptance_criteria=["$ pytest tests/"]
    )
    assert service.plan(task, "backend")[0] == []


# ------------------------------------------------------------------- executing


async def test_passing_check_is_recorded(db, tasks, tmp_path) -> None:
    config = make_config(
        default={"commands": [[sys.executable, "-c", "print('ok')"]]}
    )
    service = VerificationService(db, config)
    task = tasks.create_task("work", agent="backend")

    verdict = await service.verify(task, cwd=tmp_path, boundary=tmp_path)
    assert verdict.passed
    assert verdict.ran_anything

    records = service.history(task_key=task.key)
    assert len(records) == 1
    assert records[0].status == VerificationStatus.PASSED.value
    assert records[0].exit_code == 0


async def test_failing_check_fails_the_verdict(db, tasks, tmp_path) -> None:
    config = make_config(
        default={"commands": [[sys.executable, "-c", "import sys; sys.exit(1)"]]}
    )
    service = VerificationService(db, config)
    task = tasks.create_task("work", agent="backend")

    verdict = await service.verify(task, cwd=tmp_path, boundary=tmp_path)
    assert not verdict.passed
    assert service.history(task_key=task.key)[0].status == (
        VerificationStatus.FAILED.value
    )


async def test_every_check_runs_even_after_one_fails(db, tasks, tmp_path) -> None:
    """An operator wants every problem, not just the first."""
    config = make_config(
        default={
            "commands": [
                [sys.executable, "-c", "import sys; sys.exit(1)"],
                [sys.executable, "-c", "print('second ran')"],
            ]
        }
    )
    service = VerificationService(db, config)
    task = tasks.create_task("work", agent="backend")

    verdict = await service.verify(task, cwd=tmp_path, boundary=tmp_path)
    assert len(verdict.checks) == 2
    assert len(service.history(task_key=task.key)) == 2


async def test_denied_check_is_skipped_not_passed(db, tasks, tmp_path) -> None:
    """"We could not look" must never read as "it is fine"."""
    config = make_config(default={"commands": [["bash", "-c", "true"]]})
    service = VerificationService(db, config)
    task = tasks.create_task("work", agent="backend")

    verdict = await service.verify(task, cwd=tmp_path, boundary=tmp_path)
    assert verdict.checks[0].status is VerificationStatus.SKIPPED
    assert not verdict.ran_anything


async def test_verifier_is_not_limited_by_the_agent(db, tmp_path) -> None:
    """A reviewer without run_command must still have its work verified."""
    config = Config.model_validate(
        {
            **CONFIG,
            "agents": {
                "reviewer": {"role": "reviewer", "capabilities": ["read_files"]}
            },
            "verification": {
                "default": {"commands": [[sys.executable, "-c", "print('ran')"]]}
            },
        }
    )
    AgentService(db, config, StubRuntime(), tmp_path).sync_from_config()
    tasks = TaskService(db, config)
    service = VerificationService(db, config)
    task = tasks.create_task("review", agent="reviewer")

    verdict = await service.verify(task, cwd=tmp_path, boundary=tmp_path)
    assert verdict.passed
    assert verdict.ran_anything


async def test_passed_for_uses_the_latest_attempt(db, tasks, tmp_path) -> None:
    """A later passing attempt must supersede an earlier failure."""
    failing_config = make_config(
        default={"commands": [[sys.executable, "-c", "import sys; sys.exit(1)"]]}
    )
    task = tasks.create_task("work", agent="backend")
    await VerificationService(db, failing_config).verify(
        task, cwd=tmp_path, boundary=tmp_path
    )
    assert not VerificationService(db, failing_config).passed_for(task.key)

    # Second attempt, which passes.
    tasks.tasks.increment_attempts(task.key)
    passing_config = make_config(
        default={"commands": [[sys.executable, "-c", "print('ok')"]]}
    )
    await VerificationService(db, passing_config).verify(
        tasks.get_task(task.key), cwd=tmp_path, boundary=tmp_path
    )
    assert VerificationService(db, passing_config).passed_for(task.key)


def test_passed_for_with_no_records_is_not_a_failure(db, tasks) -> None:
    service = VerificationService(db, make_config())
    assert service.passed_for("NEVER-CHECKED")


# ------------------------------------------- the lifecycle, through the scheduler


def build(db, config, runtime, tmp_path):
    agents = AgentService(db, config, runtime, tmp_path)
    agents.sync_from_config()
    bus = EventBus(db)
    tasks = TaskService(db, config, event_bus=bus)
    events: list[tuple[str, str]] = []
    scheduler = Scheduler(
        db=db,
        config=config,
        agent_service=agents,
        task_service=tasks,
        event_bus=bus,
        on_progress=lambda e, d: events.append((e, d)),
    )
    return scheduler, tasks, events, bus


class ClaimsSuccess(StubRuntime):
    async def run(self, request, on_event=None):
        self.requests.append(request)
        return RunResult(
            status=RunStatus.SUCCEEDED, session_id="s", exit_code=0, text=block()
        )


async def test_a_claim_alone_does_not_complete_a_task(db, tmp_path) -> None:
    """The headline requirement of this phase."""
    config = make_config(
        default={"commands": [[sys.executable, "-c", "import sys; sys.exit(1)"]]}
    )
    scheduler, tasks, events, _bus = build(db, config, ClaimsSuccess(), tmp_path)
    task = tasks.create_task("work", agent="backend")

    report = await scheduler.run()

    stored = tasks.get_task(task.key)
    assert stored.status is TaskStatus.FAILED_VERIFICATION
    assert stored.status is not TaskStatus.COMPLETED
    assert task.key in report.failed
    assert "verification failed" in (stored.error or "")
    assert any(e == "unverified" for e, _ in events)


async def test_a_verified_claim_completes(db, tmp_path) -> None:
    config = make_config(
        default={"commands": [[sys.executable, "-c", "print('tests pass')"]]}
    )
    scheduler, tasks, events, _bus = build(db, config, ClaimsSuccess(), tmp_path)
    task = tasks.create_task("work", agent="backend")

    report = await scheduler.run()
    assert tasks.get_task(task.key).status is TaskStatus.COMPLETED
    assert report.completed == [task.key]
    assert any(e == "verified" for e, _ in events)


async def test_no_checks_configured_still_completes(db, tmp_path) -> None:
    """Verification must not stall a project that has nothing to run."""
    scheduler, tasks, _events, _bus = build(
        db, make_config(), ClaimsSuccess(), tmp_path
    )
    task = tasks.create_task("work", agent="backend")
    await scheduler.run()
    assert tasks.get_task(task.key).status is TaskStatus.COMPLETED


async def test_verification_result_is_attached_to_the_task(db, tmp_path) -> None:
    config = make_config(
        default={"commands": [[sys.executable, "-c", "print('ok')"]]}
    )
    scheduler, tasks, _events, _bus = build(db, config, ClaimsSuccess(), tmp_path)
    task = tasks.create_task("work", agent="backend")
    await scheduler.run()
    assert "Verification:" in (tasks.get_task(task.key).result or "")


async def test_failed_verification_blocks_dependents(db, tmp_path) -> None:
    config = make_config(
        default={"commands": [[sys.executable, "-c", "import sys; sys.exit(1)"]]}
    )
    scheduler, tasks, _events, _bus = build(db, config, ClaimsSuccess(), tmp_path)
    first = tasks.create_task("work", agent="backend")
    second = tasks.create_task("after", agent="qa", depends_on=[first.key])

    report = await scheduler.run()
    assert tasks.get_task(second.key).status is TaskStatus.BLOCKED
    assert second.key in report.blocked


async def test_the_lifecycle_emits_its_events(db, tmp_path) -> None:
    config = make_config(
        default={"commands": [[sys.executable, "-c", "print('ok')"]]}
    )
    scheduler, tasks, _events, bus = build(db, config, ClaimsSuccess(), tmp_path)
    task = tasks.create_task("work", agent="backend")
    await scheduler.run()

    types = [e.type for e in bus.history(task_key=task.key)]
    assert EventType.TASK_VERIFIED in types
    assert EventType.TASK_COMPLETED in types


async def test_retry_after_failed_verification_can_succeed(db, tmp_path) -> None:
    """The recovery flow: rejected -> fixed -> verified -> completed."""
    marker = tmp_path / "fixed.txt"
    config = make_config(
        default={
            "commands": [
                [
                    sys.executable,
                    "-c",
                    f"import os,sys; sys.exit(0 if os.path.exists(r'{marker}') else 1)",
                ]
            ]
        }
    )
    scheduler, tasks, _events, _bus = build(db, config, ClaimsSuccess(), tmp_path)
    task = tasks.create_task("work", agent="backend")

    await scheduler.run()
    assert tasks.get_task(task.key).status is TaskStatus.FAILED_VERIFICATION

    # The fix lands, and the operator requeues the task.
    marker.write_text("fixed", encoding="utf-8")
    tasks.retry(task.key)

    await scheduler.run()
    assert tasks.get_task(task.key).status is TaskStatus.COMPLETED


async def test_objective_is_not_complete_while_verification_failed(
    db, tmp_path
) -> None:
    """Python decides objective completion, and a failed check prevents it."""
    from agentos.repositories.objectives import ObjectiveRepository
    from agentos.services.objectives import ObjectiveService

    config = make_config(
        default={"commands": [[sys.executable, "-c", "import sys; sys.exit(1)"]]}
    )
    scheduler, tasks, _events, _bus = build(db, config, ClaimsSuccess(), tmp_path)
    objective = ObjectiveRepository(db).create("Ship it")
    from agentos.schemas.enums import ObjectiveStatus

    ObjectiveRepository(db).set_status(objective.id, ObjectiveStatus.ACTIVE)
    task = tasks.create_task("work", agent="backend", objective_id=objective.id)

    await scheduler.run()
    service = ObjectiveService(db, config, scheduler.agents, tasks)
    assert (
        service.refresh_completion(objective.id).status is ObjectiveStatus.FAILED
    )


async def test_objective_completes_when_verification_passes(db, tmp_path) -> None:
    from agentos.repositories.objectives import ObjectiveRepository
    from agentos.schemas.enums import ObjectiveStatus
    from agentos.services.objectives import ObjectiveService

    config = make_config(
        default={"commands": [[sys.executable, "-c", "print('ok')"]]}
    )
    scheduler, tasks, _events, _bus = build(db, config, ClaimsSuccess(), tmp_path)
    objective = ObjectiveRepository(db).create("Ship it")
    ObjectiveRepository(db).set_status(objective.id, ObjectiveStatus.ACTIVE)
    tasks.create_task("work", agent="backend", objective_id=objective.id)

    await scheduler.run()
    service = ObjectiveService(db, config, scheduler.agents, tasks)
    assert (
        service.refresh_completion(objective.id).status is ObjectiveStatus.COMPLETED
    )


def test_short_form_abbreviates_a_long_command() -> None:
    """A failure message must not bury the point under 300 chars of argv."""
    check = CheckResult(
        command=[r"C:\very\long\path\to\python.exe", "-c", "x" * 300],
        status=VerificationStatus.FAILED,
    )
    assert len(check.short) <= 60
    assert "python.exe" in check.short
    # The full command is still available for the record.
    assert len(check.spelled) > 300


def test_short_form_leaves_a_normal_command_alone() -> None:
    check = CheckResult(command=["pytest", "tests/"], status=VerificationStatus.PASSED)
    assert check.short == "pytest tests/"


async def test_failure_message_stays_readable(db, tmp_path) -> None:
    config = make_config(
        default={"commands": [[sys.executable, "-c", "import sys;" + " " * 200 + "sys.exit(1)"]]}
    )
    scheduler, tasks, _events, _bus = build(db, config, ClaimsSuccess(), tmp_path)
    task = tasks.create_task("work", agent="backend")
    await scheduler.run()
    error = tasks.get_task(task.key).error or ""
    assert "verification failed" in error
    assert len(error) < 200

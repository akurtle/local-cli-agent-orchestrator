"""Task persistence, validation and status transitions."""

from __future__ import annotations

import pytest

from agentos.config import Config
from agentos.db.session import Database
from agentos.repositories.tasks import (
    TaskNotFound,
    TaskRepository,
    criteria_from_text,
    criteria_to_text,
)
from agentos.schemas.enums import TaskStatus
from agentos.services.agents import AgentService
from agentos.services.tasks import (
    InvalidTaskTransition,
    TaskService,
    TaskValidationError,
)
from tests.test_agents import CONFIG_DICT, StubRuntime


@pytest.fixture
def config() -> Config:
    return Config.model_validate(CONFIG_DICT)


@pytest.fixture
def tasks(db: Database, config: Config, tmp_path) -> TaskService:
    # Register the configured agents so tasks can be assigned to them.
    AgentService(db, config, StubRuntime(), tmp_path).sync_from_config()
    return TaskService(db=db, config=config)


# ------------------------------------------------------------------- creation


def test_create_minimal_task(tasks: TaskService) -> None:
    task = tasks.create_task("Do a thing")
    assert task.key == "T-1"
    assert task.title == "Do a thing"
    # No dependencies, so readiness runs immediately.
    assert task.status is TaskStatus.READY


def test_keys_increment_per_prefix(tasks: TaskService) -> None:
    tasks.create_task("a", prefix="AUTH")
    tasks.create_task("b", prefix="AUTH")
    tasks.create_task("c", prefix="DOCS")
    assert tasks.get_task("AUTH-2").title == "b"
    assert tasks.get_task("DOCS-1").title == "c"


def test_key_allocation_survives_deletion(tasks: TaskService) -> None:
    """Keys must not be reused after a delete."""
    tasks.create_task("a", prefix="X")
    tasks.create_task("b", prefix="X")
    tasks.tasks.delete("X-2")
    assert tasks.create_task("c", prefix="X").key == "X-2"


def test_empty_title_is_rejected(tasks: TaskService) -> None:
    with pytest.raises(TaskValidationError, match="title must not be empty"):
        tasks.create_task("   ")


def test_assignment_to_known_agent(tasks: TaskService) -> None:
    task = tasks.create_task("Build API", agent="backend")
    assert task.assigned_agent == "backend"
    assert task.assigned_role == "backend"


def test_assignment_to_unknown_agent_is_rejected(tasks: TaskService) -> None:
    with pytest.raises(TaskValidationError, match="unknown agent 'nobody'"):
        tasks.create_task("x", agent="nobody")


def test_unknown_agent_error_lists_valid_agents(tasks: TaskService) -> None:
    with pytest.raises(TaskValidationError, match="backend, frontend, manager"):
        tasks.create_task("x", agent="nobody")


def test_rejected_task_is_not_persisted(tasks: TaskService) -> None:
    """A failed validation must leave nothing behind."""
    with pytest.raises(TaskValidationError):
        tasks.create_task("x", agent="nobody")
    assert tasks.tasks.count() == 0


def test_unknown_dependency_is_rejected(tasks: TaskService) -> None:
    with pytest.raises(TaskValidationError, match="unknown dependency 'NOPE-1'"):
        tasks.create_task("x", depends_on=["NOPE-1"])


def test_dependency_rejection_creates_nothing(tasks: TaskService) -> None:
    with pytest.raises(TaskValidationError):
        tasks.create_task("x", depends_on=["NOPE-1"])
    assert tasks.tasks.count() == 0


def test_duplicate_key_is_rejected(tasks: TaskService) -> None:
    tasks.create_task("a", key="FIXED-1")
    with pytest.raises(TaskValidationError, match="already exists"):
        tasks.create_task("b", key="FIXED-1")


def test_acceptance_criteria_round_trip(tasks: TaskService) -> None:
    task = tasks.create_task("x", acceptance_criteria=["one", "  two  ", "", "three"])
    assert task.acceptance_criteria == ["one", "two", "three"]


def test_criteria_text_helpers() -> None:
    assert criteria_to_text(["a", " b ", ""]) == "a\nb"
    assert criteria_from_text("a\n\n b \n") == ["a", "b"]
    assert criteria_from_text(None) == []
    assert criteria_to_text(None) == ""


# --------------------------------------------------------------- dependencies


def test_task_with_pending_dependency_is_pending(tasks: TaskService) -> None:
    first = tasks.create_task("first", agent="backend")
    second = tasks.create_task("second", agent="frontend", depends_on=[first.key])
    assert second.status is TaskStatus.PENDING
    assert second.depends_on == [first.key]


def test_dependency_becomes_ready_when_prerequisite_completes(
    tasks: TaskService,
) -> None:
    first = tasks.create_task("first")
    second = tasks.create_task("second", depends_on=[first.key])
    tasks.transition(first.key, TaskStatus.RUNNING)
    tasks.transition(first.key, TaskStatus.COMPLETED)
    tasks.refresh_readiness()
    assert tasks.get_task(second.key).status is TaskStatus.READY


def test_fan_in_waits_for_both(tasks: TaskService) -> None:
    """BACKEND-1 + FRONTEND-1 -> QA-1 from the spec."""
    b = tasks.create_task("backend work", agent="backend")
    f = tasks.create_task("frontend work", agent="frontend")
    qa = tasks.create_task("qa work", depends_on=[b.key, f.key])

    for key in (b.key,):
        tasks.transition(key, TaskStatus.RUNNING)
        tasks.transition(key, TaskStatus.COMPLETED)
    tasks.refresh_readiness()
    assert tasks.get_task(qa.key).status is TaskStatus.PENDING

    tasks.transition(f.key, TaskStatus.RUNNING)
    tasks.transition(f.key, TaskStatus.COMPLETED)
    tasks.refresh_readiness()
    assert tasks.get_task(qa.key).status is TaskStatus.READY


def test_add_dependency_later(tasks: TaskService) -> None:
    a = tasks.create_task("a")
    b = tasks.create_task("b")
    updated = tasks.add_dependency(b.key, a.key)
    assert updated.depends_on == [a.key]
    assert updated.status is TaskStatus.PENDING


def test_self_dependency_is_rejected(tasks: TaskService) -> None:
    a = tasks.create_task("a")
    with pytest.raises(TaskValidationError, match="cycle"):
        tasks.add_dependency(a.key, a.key)


def test_cycle_is_refused_at_write_time(tasks: TaskService) -> None:
    """A cycle must never reach the database."""
    a = tasks.create_task("a")
    b = tasks.create_task("b", depends_on=[a.key])
    with pytest.raises(TaskValidationError, match="cycle"):
        tasks.add_dependency(a.key, b.key)
    # The graph is still sound.
    tasks.validate_graph()


def test_transitive_cycle_is_refused(tasks: TaskService) -> None:
    a = tasks.create_task("a")
    b = tasks.create_task("b", depends_on=[a.key])
    c = tasks.create_task("c", depends_on=[b.key])
    with pytest.raises(TaskValidationError, match="cycle"):
        tasks.add_dependency(a.key, c.key)


def test_duplicate_dependency_edge_is_idempotent(tasks: TaskService) -> None:
    a = tasks.create_task("a")
    b = tasks.create_task("b", depends_on=[a.key])
    tasks.add_dependency(b.key, a.key)
    assert tasks.get_task(b.key).depends_on == [a.key]


def test_failed_dependency_blocks_dependent(tasks: TaskService) -> None:
    a = tasks.create_task("a")
    b = tasks.create_task("b", depends_on=[a.key])
    tasks.transition(a.key, TaskStatus.RUNNING)
    tasks.transition(a.key, TaskStatus.FAILED)
    tasks.refresh_readiness()
    assert tasks.get_task(b.key).status is TaskStatus.BLOCKED


def test_cancelled_dependency_blocks_dependent(tasks: TaskService) -> None:
    a = tasks.create_task("a")
    b = tasks.create_task("b", depends_on=[a.key])
    tasks.cancel(a.key)
    assert tasks.get_task(b.key).status is TaskStatus.BLOCKED


def test_retrying_dependency_unblocks_dependent(tasks: TaskService) -> None:
    a = tasks.create_task("a")
    b = tasks.create_task("b", depends_on=[a.key])
    tasks.transition(a.key, TaskStatus.RUNNING)
    tasks.transition(a.key, TaskStatus.FAILED)
    tasks.refresh_readiness()
    assert tasks.get_task(b.key).status is TaskStatus.BLOCKED

    tasks.retry(a.key)
    tasks.transition(a.key, TaskStatus.RUNNING)
    tasks.transition(a.key, TaskStatus.COMPLETED)
    tasks.refresh_readiness()
    assert tasks.get_task(b.key).status is TaskStatus.READY


# --------------------------------------------------------------- transitions


def test_legal_transition_sets_timestamps(tasks: TaskService) -> None:
    task = tasks.create_task("a")
    running = tasks.transition(task.key, TaskStatus.RUNNING)
    assert running.started_at is not None
    done = tasks.transition(task.key, TaskStatus.COMPLETED, result="all good")
    assert done.completed_at is not None
    assert done.result == "all good"


def test_illegal_transition_is_refused(tasks: TaskService) -> None:
    task = tasks.create_task("a")
    with pytest.raises(InvalidTaskTransition, match="cannot go from ready to completed"):
        tasks.transition(task.key, TaskStatus.COMPLETED)


def test_completed_is_final(tasks: TaskService) -> None:
    task = tasks.create_task("a")
    tasks.transition(task.key, TaskStatus.RUNNING)
    tasks.transition(task.key, TaskStatus.COMPLETED)
    with pytest.raises(InvalidTaskTransition):
        tasks.transition(task.key, TaskStatus.RUNNING)


def test_transition_to_same_status_is_a_noop(tasks: TaskService) -> None:
    task = tasks.create_task("a")
    assert tasks.transition(task.key, TaskStatus.READY).status is TaskStatus.READY


def test_cancel_a_running_task(tasks: TaskService) -> None:
    task = tasks.create_task("a")
    tasks.transition(task.key, TaskStatus.RUNNING)
    assert tasks.cancel(task.key).status is TaskStatus.CANCELLED


def test_cannot_cancel_a_completed_task(tasks: TaskService) -> None:
    task = tasks.create_task("a")
    tasks.transition(task.key, TaskStatus.RUNNING)
    tasks.transition(task.key, TaskStatus.COMPLETED)
    with pytest.raises(InvalidTaskTransition, match="already completed"):
        tasks.cancel(task.key)


def test_only_failed_tasks_can_retry(tasks: TaskService) -> None:
    task = tasks.create_task("a")
    with pytest.raises(InvalidTaskTransition, match="only failed tasks"):
        tasks.retry(task.key)


# --------------------------------------------------------------- repository


def test_missing_task_raises(db: Database) -> None:
    with pytest.raises(TaskNotFound):
        TaskRepository(db).get("GHOST-1")


def test_find_missing_returns_none(db: Database) -> None:
    assert TaskRepository(db).find("GHOST-1") is None


def test_lookup_by_id_and_key(tasks: TaskService) -> None:
    task = tasks.create_task("a")
    assert tasks.get_task(task.id).key == task.key
    assert tasks.get_task(task.key).id == task.id


def test_nodes_reflect_graph(tasks: TaskService) -> None:
    a = tasks.create_task("a", agent="backend")
    b = tasks.create_task("b", agent="frontend", depends_on=[a.key])
    nodes = tasks.tasks.nodes()
    assert nodes[b.id].depends_on == frozenset({a.id})
    assert nodes[a.id].agent_name == "backend"
    assert nodes[b.id].agent_name == "frontend"


def test_status_filter(tasks: TaskService) -> None:
    a = tasks.create_task("a")
    tasks.create_task("b", depends_on=[a.key])
    ready = tasks.list_tasks({TaskStatus.READY})
    assert [t.key for t in ready] == [a.key]


def test_increment_attempts(tasks: TaskService) -> None:
    task = tasks.create_task("a")
    assert tasks.tasks.increment_attempts(task.key) == 1
    assert tasks.tasks.increment_attempts(task.key) == 2


def test_completed_dependencies_only_returns_completed(tasks: TaskService) -> None:
    a = tasks.create_task("a")
    b = tasks.create_task("b")
    c = tasks.create_task("c", depends_on=[a.key, b.key])
    tasks.transition(a.key, TaskStatus.RUNNING)
    tasks.transition(a.key, TaskStatus.COMPLETED, result="A done")
    deps = tasks.completed_dependencies(tasks.get_task(c.key))
    assert [d.key for d in deps] == [a.key]
    assert deps[0].result == "A done"


def test_tasks_survive_reopen(tmp_path, config: Config) -> None:
    """Task state must outlive the process."""
    db_path = tmp_path / "tasks.db"
    first = Database(db_path)
    first.create_all()
    AgentService(first, config, StubRuntime(), tmp_path).sync_from_config()
    service = TaskService(first, config)
    a = service.create_task("persisted", agent="backend", prefix="P")
    service.transition(a.key, TaskStatus.RUNNING)
    first.dispose()

    second = Database(db_path)
    second.create_all()
    reopened = TaskService(second, config).get_task("P-1")
    assert reopened.title == "persisted"
    assert reopened.status is TaskStatus.RUNNING
    second.dispose()


def test_readiness_propagates_blockage_in_one_call(tasks: TaskService) -> None:
    """A -> B -> C: failing A must block both B and C, not just B.

    Readiness iterates to a fixed point, because one round only moves blockage
    a single edge down the chain.
    """
    a = tasks.create_task("a", agent="backend")
    b = tasks.create_task("b", agent="frontend", depends_on=[a.key])
    c = tasks.create_task("c", agent="manager", depends_on=[b.key])

    tasks.transition(a.key, TaskStatus.RUNNING)
    tasks.transition(a.key, TaskStatus.FAILED)
    tasks.refresh_readiness()

    assert tasks.get_task(b.key).status is TaskStatus.BLOCKED
    assert tasks.get_task(c.key).status is TaskStatus.BLOCKED


def test_readiness_unblocks_a_whole_chain(tasks: TaskService) -> None:
    a = tasks.create_task("a", agent="backend")
    b = tasks.create_task("b", agent="frontend", depends_on=[a.key])
    c = tasks.create_task("c", agent="manager", depends_on=[b.key])

    tasks.transition(a.key, TaskStatus.RUNNING)
    tasks.transition(a.key, TaskStatus.FAILED)
    tasks.refresh_readiness()
    tasks.retry(a.key)
    tasks.transition(a.key, TaskStatus.RUNNING)
    tasks.transition(a.key, TaskStatus.COMPLETED)
    tasks.refresh_readiness()

    assert tasks.get_task(b.key).status is TaskStatus.READY
    # C still waits on B, which has not run yet -- pending, not blocked.
    assert tasks.get_task(c.key).status is TaskStatus.PENDING


def test_readiness_is_idempotent(tasks: TaskService) -> None:
    a = tasks.create_task("a", agent="backend")
    tasks.create_task("b", agent="frontend", depends_on=[a.key])
    tasks.refresh_readiness()
    assert tasks.refresh_readiness() == []

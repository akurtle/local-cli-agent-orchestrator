"""Phase 13: memory, handoffs and session rotation."""

from __future__ import annotations

import pytest

from agentos.config import Config
from agentos.db.session import Database
from agentos.repositories.memories import MemoryNotFound, MemoryRepository
from agentos.schemas.enums import MemoryCategory, MemoryScope
from agentos.services.agents import AgentService
from agentos.services.memory import (
    AGENT_IMPORTANCE,
    HUMAN_IMPORTANCE,
    MAX_MEMORY_CHARS,
    MemoryService,
)
from agentos.services.rotation import (
    RotationReason,
    parse_summary,
    should_rotate,
)
from agentos.services.tasks import TaskService
from tests.test_agents import StubRuntime

CONFIG = {
    "project": {"name": "Mem"},
    "agents": {
        "manager": {"role": "manager"},
        "backend": {"role": "backend"},
        "frontend": {"role": "frontend"},
        "qa": {"role": "qa"},
    },
}


@pytest.fixture
def config() -> Config:
    return Config.model_validate(CONFIG)


@pytest.fixture
def memory(db: Database, config: Config) -> MemoryService:
    return MemoryService(db, config)


@pytest.fixture
def tasks(db: Database, config: Config, tmp_path) -> TaskService:
    AgentService(db, config, StubRuntime(), tmp_path).sync_from_config()
    return TaskService(db, config)


# ------------------------------------------------------------------- recording


def test_remember_a_project_fact(memory: MemoryService) -> None:
    stored = memory.remember(MemoryScope.PROJECT, "Framework: FastAPI")
    assert stored is not None
    assert stored.content == "Framework: FastAPI"
    assert stored.importance == HUMAN_IMPORTANCE


def test_blank_content_is_not_remembered(memory: MemoryService) -> None:
    assert memory.remember(MemoryScope.PROJECT, "   ") is None
    assert memory.list_all() == []


def test_long_content_is_trimmed(memory: MemoryService) -> None:
    """A memory is a note, not a document."""
    stored = memory.remember(MemoryScope.PROJECT, "x" * (MAX_MEMORY_CHARS + 500))
    assert stored is not None
    assert len(stored.content) <= MAX_MEMORY_CHARS + 20
    assert "[trimmed]" in stored.content


def test_duplicate_content_is_merged_not_repeated(memory: MemoryService) -> None:
    """A prompt full of the same sentence is worse than useless."""
    first = memory.remember(MemoryScope.PROJECT, "Uses PostgreSQL")
    second = memory.remember(MemoryScope.PROJECT, "Uses PostgreSQL")
    assert first is not None and second is not None
    assert first.id == second.id
    assert len(memory.list_all()) == 1


def test_duplicate_across_scopes_is_kept_separately(memory: MemoryService) -> None:
    memory.remember(MemoryScope.PROJECT, "Same text")
    memory.remember(MemoryScope.AGENT, "Same text", scope_id="backend")
    assert len(memory.list_all()) == 2


def test_repeat_can_raise_importance_never_lower_it(memory: MemoryService) -> None:
    memory.remember(MemoryScope.PROJECT, "Important", importance=90)
    again = memory.remember(MemoryScope.PROJECT, "Important", importance=10)
    assert again is not None
    assert again.importance == 90


def test_agent_facts_matter_less_than_human_facts(memory: MemoryService) -> None:
    """A human writing something down is a stronger signal."""
    human = memory.remember(MemoryScope.PROJECT, "Human fact", created_by="human")
    agent = memory.remember(MemoryScope.PROJECT, "Agent fact", created_by="backend")
    assert human.importance > agent.importance
    assert agent.importance == AGENT_IMPORTANCE


def test_decisions_outrank_plain_agent_facts(memory: MemoryService) -> None:
    fact = memory.remember(
        MemoryScope.AGENT, "a fact", "backend", MemoryCategory.FACT, created_by="backend"
    )
    decision = memory.remember(
        MemoryScope.AGENT,
        "a decision",
        "backend",
        MemoryCategory.DECISION,
        created_by="backend",
    )
    assert decision.importance > fact.importance


# ----------------------------------------------------- recording from a response


def test_decisions_and_warnings_become_memories(
    memory: MemoryService, tasks: TaskService
) -> None:
    task = tasks.create_task("work", agent="backend")
    stored = memory.record_from_response(
        agent="backend",
        task=task,
        decisions=["Reused the JWT model."],
        warnings=["Callback URL must match."],
    )
    assert len(stored) == 2
    categories = {m.category for m in stored}
    assert categories == {MemoryCategory.DECISION, MemoryCategory.WARNING}


def test_objective_decisions_are_scoped_to_the_objective(
    memory: MemoryService, tasks: TaskService, db: Database, config: Config
) -> None:
    """So every agent on that objective sees them, not just the author."""
    from agentos.repositories.objectives import ObjectiveRepository

    objective = ObjectiveRepository(db).create("Add auth")
    task = tasks.create_task("work", agent="backend", objective_id=objective.id)
    stored = memory.record_from_response(
        agent="backend",
        task=task,
        decisions=["Use existing session model."],
        warnings=[],
        objective_id=objective.id,
    )
    assert stored[0].scope is MemoryScope.OBJECTIVE
    assert stored[0].scope_id == str(objective.id)
    assert memory.recall_objective(objective.id)


def test_decisions_without_an_objective_are_the_agents_own(
    memory: MemoryService, tasks: TaskService
) -> None:
    task = tasks.create_task("work", agent="backend")
    stored = memory.record_from_response(
        agent="backend", task=task, decisions=["Local lesson."], warnings=[]
    )
    assert stored[0].scope is MemoryScope.AGENT
    assert stored[0].scope_id == "backend"


def test_provenance_is_recorded(memory: MemoryService, tasks: TaskService) -> None:
    task = tasks.create_task("work", agent="backend")
    stored = memory.record_from_response(
        agent="backend", task=task, decisions=["x"], warnings=[]
    )
    assert stored[0].created_by == "backend"
    assert stored[0].task_key == task.key


def test_empty_response_records_nothing(
    memory: MemoryService, tasks: TaskService
) -> None:
    task = tasks.create_task("work", agent="backend")
    assert memory.record_from_response("backend", task, [], []) == []


# ---------------------------------------------------------------------- recall


def test_recall_is_scoped(memory: MemoryService) -> None:
    memory.remember(MemoryScope.PROJECT, "project fact")
    memory.remember(MemoryScope.AGENT, "backend fact", scope_id="backend")
    memory.remember(MemoryScope.AGENT, "frontend fact", scope_id="frontend")

    assert [m.content for m in memory.recall_project()] == ["project fact"]
    assert [m.content for m in memory.recall_agent("backend")] == ["backend fact"]
    assert [m.content for m in memory.recall_agent("qa")] == []


def test_recall_orders_by_importance(memory: MemoryService) -> None:
    """The context builder trims the tail, so ordering decides what survives."""
    memory.remember(MemoryScope.PROJECT, "minor", importance=10)
    memory.remember(MemoryScope.PROJECT, "critical", importance=95)
    memory.remember(MemoryScope.PROJECT, "middling", importance=50)
    assert [m.content for m in memory.recall_project()] == [
        "critical",
        "middling",
        "minor",
    ]


def test_recall_is_capped(memory: MemoryService) -> None:
    from agentos.services.memory import RECALL_LIMITS

    for i in range(RECALL_LIMITS[MemoryScope.PROJECT] + 10):
        memory.remember(MemoryScope.PROJECT, f"fact {i}")
    assert len(memory.recall_project()) == RECALL_LIMITS[MemoryScope.PROJECT]


def test_recall_objective_without_an_id_is_empty(memory: MemoryService) -> None:
    assert memory.recall_objective(None) == []


def test_forget(memory: MemoryService) -> None:
    stored = memory.remember(MemoryScope.PROJECT, "temporary")
    memory.forget(stored.id)
    assert memory.list_all() == []
    with pytest.raises(MemoryNotFound):
        memory.forget(stored.id)


def test_memory_survives_reopen(tmp_path, config: Config) -> None:
    path = tmp_path / "mem.db"
    first = Database(path)
    first.create_all()
    MemoryService(first, config).remember(MemoryScope.PROJECT, "durable fact")
    first.dispose()

    second = Database(path)
    second.create_all()
    assert [m.content for m in MemoryService(second, config).recall_project()] == [
        "durable fact"
    ]
    second.dispose()


# -------------------------------------------------------------------- handoffs


def test_create_and_read_a_handoff(
    memory: MemoryService, tasks: TaskService
) -> None:
    task = tasks.create_task("Build OAuth", agent="backend", prefix="AUTH")
    handoff = memory.create_handoff(
        from_agent="backend",
        task=task,
        summary="OAuth endpoint implemented.",
        to_agent="frontend",
        files=["src/auth/google.py"],
        interfaces=["POST /api/auth/google"],
        decisions=["Uses existing JWT session model."],
    )
    assert handoff.to_agent == "frontend"
    assert handoff.task_key == task.key

    waiting = memory.take_handoffs("frontend")
    assert [h.id for h in waiting] == [handoff.id]
    assert memory.take_handoffs("qa") == []


def test_handoff_is_not_consumed_until_confirmed(
    memory: MemoryService, tasks: TaskService
) -> None:
    """Same reasoning as messages: a crash must not swallow it."""
    task = tasks.create_task("work", agent="backend")
    memory.create_handoff("backend", task, "did it", to_agent="frontend")

    first = memory.take_handoffs("frontend")
    assert first
    # Read again without confirming: still there.
    assert memory.take_handoffs("frontend")

    memory.confirm_handoffs(first)
    assert memory.take_handoffs("frontend") == []


def test_handoff_carries_structure_not_conversation(
    memory: MemoryService, tasks: TaskService
) -> None:
    task = tasks.create_task("work", agent="backend", prefix="AUTH")
    memory.create_handoff(
        "backend",
        task,
        "Implemented the callback.",
        to_agent="frontend",
        interfaces=["POST /api/auth/google"],
        warnings=["Redirect URI must match exactly."],
    )
    rendered = memory.take_handoffs("frontend")[0].render()
    assert "POST /api/auth/google" in rendered
    assert "Redirect URI" in rendered
    # Short by construction: a packet, not a transcript.
    assert len(rendered) < 500


def test_list_handoffs_for_inspection(
    memory: MemoryService, tasks: TaskService
) -> None:
    task = tasks.create_task("work", agent="backend")
    memory.create_handoff("backend", task, "one", to_agent="frontend")
    memory.create_handoff("backend", task, "two", to_agent="qa")
    assert len(memory.list_handoffs()) == 2


# ------------------------------------------------------- rotation, pure policy


def test_no_rotation_without_a_session() -> None:
    """Nothing to replace, so reporting a rotation would be misleading."""
    decision = should_rotate(None, 99, None, None, 1, True)
    assert not decision


def test_rotation_on_task_budget() -> None:
    decision = should_rotate("s", 8, None, None, 8, True)
    assert decision
    assert decision.reason is RotationReason.TASK_BUDGET
    assert "limit 8" in decision.detail


def test_no_rotation_below_the_budget() -> None:
    assert not should_rotate("s", 7, None, None, 8, True)


def test_rotation_on_objective_change() -> None:
    decision = should_rotate("s", 1, 1, 2, 8, True)
    assert decision
    assert decision.reason is RotationReason.OBJECTIVE_CHANGED
    assert "1 -> 2" in decision.detail


def test_no_rotation_on_the_same_objective() -> None:
    assert not should_rotate("s", 1, 7, 7, 8, True)


def test_objective_rotation_can_be_disabled() -> None:
    assert not should_rotate("s", 1, 1, 2, 8, False)


def test_moving_into_or_out_of_an_objective_is_not_a_switch() -> None:
    """Not worth discarding a warm session for."""
    assert not should_rotate("s", 1, None, 5, 8, True)
    assert not should_rotate("s", 1, 5, None, 8, True)


def test_task_budget_wins_over_objective_change() -> None:
    decision = should_rotate("s", 20, 1, 2, 8, True)
    assert decision.reason is RotationReason.TASK_BUDGET


# ------------------------------------------------------- rotation summaries


def test_parse_summary_reads_bullets() -> None:
    text = """Here is what matters:
- Uses FastAPI with Pydantic v2.
- Tests live in tests/backend.
* Do not touch legacy/ .
"""
    assert parse_summary(text) == [
        "Uses FastAPI with Pydantic v2.",
        "Tests live in tests/backend.",
        "Do not touch legacy/ .",
    ]


def test_parse_summary_ignores_prose() -> None:
    """A rambling paragraph is exactly the context we are avoiding."""
    assert parse_summary("I did lots of things and it went well overall.") == []


def test_parse_summary_honours_nothing() -> None:
    assert parse_summary("NOTHING") == []
    assert parse_summary("nothing to carry forward") == []


def test_parse_summary_skips_the_response_block() -> None:
    text = "- a real fact\n- <<<AGENT_RESPONSE\n"
    assert parse_summary(text) == ["a real fact"]


def test_parse_summary_drops_overlong_bullets() -> None:
    text = f"- short one\n- {'x' * 500}\n"
    assert parse_summary(text) == ["short one"]


def test_parse_summary_is_capped() -> None:
    text = "\n".join(f"- fact {i}" for i in range(50))
    assert len(parse_summary(text)) == 8


def test_parse_summary_of_nothing() -> None:
    assert parse_summary("") == []

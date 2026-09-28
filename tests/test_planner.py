"""Phase 5: manager plan validation and application.

`validate_plan` is pure, so every rejection rule is tested without a database.
The manager proposes; these tests prove Python decides.
"""

from __future__ import annotations

import json

import pytest

from agentos.config import Config
from agentos.db.session import Database
from agentos.schemas.enums import ObjectiveStatus, TaskStatus
from agentos.schemas.plan import (
    MAX_PLANNED_TASKS,
    PLAN_BEGIN,
    PLAN_END,
    ManagerPlan,
    PlanParseError,
    extract_plan_block,
    parse_manager_plan,
)
from agentos.schemas.runtime import RunResult
from agentos.schemas.enums import RunStatus
from agentos.services.agents import AgentService
from agentos.services.objectives import ObjectiveService
from agentos.services.planner import Planner, validate_plan
from agentos.services.tasks import TaskService
from tests.test_agents import StubRuntime

CONFIG_DICT = {
    "project": {"name": "Planned"},
    "agents": {
        "manager": {"role": "manager"},
        "backend": {"role": "backend"},
        "frontend": {"role": "frontend"},
        "qa": {"role": "qa"},
        "reviewer": {"role": "reviewer"},
    },
}

ROSTER = {"manager", "backend", "frontend", "qa", "reviewer"}


def plan(*tasks: dict, objective: str = "Do the thing", notes: str = "") -> ManagerPlan:
    return ManagerPlan.model_validate(
        {"objective": objective, "tasks": list(tasks), "notes": notes}
    )


def task(
    temp_id: str,
    agent: str = "backend",
    depends_on: list[str] | None = None,
    title: str | None = None,
    description: str = "do it",
    criteria: list[str] | None = None,
) -> dict:
    return {
        "temp_id": temp_id,
        "title": title or f"Task {temp_id}",
        "assigned_agent": agent,
        "description": description,
        # `criteria=[]` must mean empty, not "use the default".
        "acceptance_criteria": ["it works"] if criteria is None else criteria,
        "depends_on": depends_on or [],
    }


def plan_text(payload: dict) -> str:
    return "\n".join(["Reasoning first.", PLAN_BEGIN, json.dumps(payload), PLAN_END])


@pytest.fixture
def config() -> Config:
    return Config.model_validate(CONFIG_DICT)


@pytest.fixture
def wiring(db: Database, config: Config, tmp_path):
    agents = AgentService(db, config, StubRuntime(), tmp_path)
    agents.sync_from_config()
    tasks = TaskService(db, config)
    planner = Planner(db, config, tasks)
    return agents, tasks, planner


# ---------------------------------------------------------------- plan parsing


def test_parses_a_well_formed_plan() -> None:
    parsed = parse_manager_plan(
        plan_text({"objective": "Add auth", "tasks": [task("A")]})
    )
    assert parsed.objective == "Add auth"
    assert parsed.tasks[0].temp_id == "A"


def test_missing_plan_block_raises() -> None:
    with pytest.raises(PlanParseError, match="no .* block found"):
        parse_manager_plan("Here is my plan, in prose.")


def test_invalid_json_raises() -> None:
    with pytest.raises(PlanParseError, match="not valid JSON"):
        parse_manager_plan("\n".join([PLAN_BEGIN, "{nope", PLAN_END]))


def test_non_object_plan_raises() -> None:
    with pytest.raises(PlanParseError, match="must be a JSON object"):
        parse_manager_plan("\n".join([PLAN_BEGIN, "[1,2]", PLAN_END]))


def test_code_fence_inside_block_is_tolerated() -> None:
    body = json.dumps({"tasks": [task("A")]})
    text = "\n".join([PLAN_BEGIN, "```json", body, "```", PLAN_END])
    assert parse_manager_plan(text).tasks[0].temp_id == "A"


def test_last_plan_block_wins() -> None:
    """A manager restating the format must not beat its real plan."""
    first = plan_text({"tasks": [task("OLD")]})
    second = plan_text({"tasks": [task("REAL")]})
    assert parse_manager_plan(first + second).tasks[0].temp_id == "REAL"


def test_blank_title_is_rejected_by_schema() -> None:
    with pytest.raises(PlanParseError, match="validation"):
        parse_manager_plan(plan_text({"tasks": [task("A", title="   ")]}))


def test_extract_returns_none_without_block() -> None:
    assert extract_plan_block("nothing") is None


def test_unknown_plan_fields_are_ignored() -> None:
    payload = {"tasks": [task("A")], "run_command": "rm -rf /"}
    parsed = parse_manager_plan(plan_text(payload))
    assert "run_command" not in parsed.model_dump()


# ------------------------------------------------------------- plan validation


def test_valid_plan_passes() -> None:
    result = validate_plan(
        plan(task("B"), task("Q", agent="qa", depends_on=["B"])), ROSTER
    )
    assert result.ok
    assert result.errors == []


def test_empty_plan_is_rejected() -> None:
    result = validate_plan(plan(), ROSTER)
    assert not result.ok
    assert "no tasks" in result.errors[0]


def test_unknown_agent_is_rejected() -> None:
    result = validate_plan(plan(task("A", agent="astronaut")), ROSTER)
    assert not result.ok
    assert any("astronaut" in e for e in result.errors)


def test_unknown_agent_error_lists_available_agents() -> None:
    result = validate_plan(plan(task("A", agent="nope")), ROSTER)
    assert any("backend" in e for e in result.errors)


def test_duplicate_temp_ids_are_rejected() -> None:
    result = validate_plan(plan(task("A"), task("A", agent="qa")), ROSTER)
    assert not result.ok
    assert any("duplicate temp_id" in e for e in result.errors)


def test_dependency_on_unknown_task_is_rejected() -> None:
    result = validate_plan(plan(task("A", depends_on=["GHOST"])), ROSTER)
    assert not result.ok
    assert any("GHOST" in e for e in result.errors)


def test_self_dependency_is_rejected() -> None:
    result = validate_plan(plan(task("A", depends_on=["A"])), ROSTER)
    assert not result.ok
    assert any("depends on itself" in e for e in result.errors)


def test_cycle_is_rejected() -> None:
    result = validate_plan(
        plan(
            task("A", depends_on=["C"]),
            task("B", depends_on=["A"]),
            task("C", depends_on=["B"]),
        ),
        ROSTER,
    )
    assert not result.ok
    assert any("cycle" in e for e in result.errors)


def test_oversized_plan_is_rejected() -> None:
    tasks = [task(f"T{i}") for i in range(MAX_PLANNED_TASKS + 1)]
    result = validate_plan(plan(*tasks), ROSTER)
    assert not result.ok
    assert any("more than" in e for e in result.errors)


def test_plan_at_the_limit_is_allowed() -> None:
    tasks = [task(f"T{i}") for i in range(MAX_PLANNED_TASKS)]
    assert validate_plan(plan(*tasks), ROSTER).ok


def test_missing_description_is_a_warning_not_an_error() -> None:
    result = validate_plan(plan(task("A", description="")), ROSTER)
    assert result.ok
    assert any("no description" in w for w in result.warnings)


def test_missing_criteria_is_a_warning_not_an_error() -> None:
    result = validate_plan(plan(task("A", criteria=[])), ROSTER)
    assert result.ok
    assert any("no acceptance criteria" in w for w in result.warnings)


def test_empty_roster_rejects_everything() -> None:
    result = validate_plan(plan(task("A")), set())
    assert not result.ok


# ------------------------------------------------------------------- ordering


def test_order_puts_dependencies_first() -> None:
    result = validate_plan(
        plan(
            task("REVIEW", agent="reviewer", depends_on=["QA"]),
            task("QA", agent="qa", depends_on=["BE", "FE"]),
            task("BE", agent="backend"),
            task("FE", agent="frontend"),
        ),
        ROSTER,
    )
    assert result.ok
    order = result.order
    assert order.index("BE") < order.index("QA")
    assert order.index("FE") < order.index("QA")
    assert order.index("QA") < order.index("REVIEW")


def test_order_is_deterministic() -> None:
    p = plan(task("B"), task("A"), task("C", depends_on=["A", "B"]))
    assert validate_plan(p, ROSTER).order == validate_plan(p, ROSTER).order


def test_independent_tasks_all_appear_in_order() -> None:
    result = validate_plan(plan(task("A"), task("B"), task("C")), ROSTER)
    assert sorted(result.order) == ["A", "B", "C"]


# ---------------------------------------------------------------- application


def test_apply_creates_real_tasks(wiring) -> None:
    _agents, tasks, planner = wiring
    objective = planner.objectives.create("Add Google authentication")
    p = plan(
        task("BE", agent="backend"),
        task("FE", agent="frontend"),
        task("QA", agent="qa", depends_on=["BE", "FE"]),
    )
    created = planner.apply(objective, p)

    assert len(created) == 3
    assert {t.assigned_agent for t in created} == {"backend", "frontend", "qa"}
    assert tasks.tasks.count() == 3


def test_temp_ids_become_real_dependencies(wiring) -> None:
    """The crux: temp ids must translate into persisted task keys."""
    _agents, tasks, planner = wiring
    objective = planner.objectives.create("Ship it")
    created = planner.apply(
        objective,
        plan(
            task("BE", agent="backend"),
            task("FE", agent="frontend"),
            task("QA", agent="qa", depends_on=["BE", "FE"]),
        ),
    )
    by_title = {t.title: t for t in created}
    qa = tasks.get_task(by_title["Task QA"].key)
    prerequisites = {by_title["Task BE"].key, by_title["Task FE"].key}
    assert set(qa.depends_on) == prerequisites
    # No temp id leaked into storage.
    assert "BE" not in qa.depends_on


def test_applied_tasks_get_correct_initial_statuses(wiring) -> None:
    _agents, tasks, planner = wiring
    objective = planner.objectives.create("Ship it")
    created = planner.apply(
        objective,
        plan(task("BE", agent="backend"), task("QA", agent="qa", depends_on=["BE"])),
    )
    statuses = {t.title: tasks.get_task(t.key).status for t in created}
    assert statuses["Task BE"] is TaskStatus.READY
    assert statuses["Task QA"] is TaskStatus.PENDING


def test_applied_tasks_belong_to_the_objective(wiring) -> None:
    _agents, _tasks, planner = wiring
    objective = planner.objectives.create("Ship it")
    created = planner.apply(objective, plan(task("A")))
    assert created[0].objective_id == objective.id
    assert planner.objectives.get(objective.id).task_keys == [created[0].key]


def test_apply_records_creator_and_criteria(wiring) -> None:
    _agents, tasks, planner = wiring
    objective = planner.objectives.create("Ship it")
    created = planner.apply(
        objective, plan(task("A", criteria=["one", "two"])), created_by="manager"
    )
    stored = tasks.get_task(created[0].key)
    assert stored.created_by == "manager"
    assert stored.acceptance_criteria == ["one", "two"]


def test_apply_activates_the_objective(wiring) -> None:
    _agents, _tasks, planner = wiring
    objective = planner.objectives.create("Ship it")
    planner.apply(objective, plan(task("A")))
    assert planner.objectives.get(objective.id).status is ObjectiveStatus.ACTIVE


def test_apply_stores_the_plan_for_audit(wiring) -> None:
    from agentos.db.models import Objective

    _agents, _tasks, planner = wiring
    objective = planner.objectives.create("Ship it")
    planner.apply(objective, plan(task("A")))
    with planner.db.session() as session:
        stored = session.get(Objective, objective.id)
        assert stored.plan_json
        assert "Task A" in stored.plan_json


def test_apply_refuses_an_invalid_plan(wiring) -> None:
    """An unvalidated plan must never reach the database."""
    _agents, tasks, planner = wiring
    objective = planner.objectives.create("Ship it")
    with pytest.raises(ValueError, match="refusing to apply an invalid plan"):
        planner.apply(objective, plan(task("A", agent="astronaut")))
    assert tasks.tasks.count() == 0


def test_apply_refuses_a_cyclic_plan(wiring) -> None:
    _agents, tasks, planner = wiring
    objective = planner.objectives.create("Ship it")
    with pytest.raises(ValueError):
        planner.apply(
            objective, plan(task("A", depends_on=["B"]), task("B", depends_on=["A"]))
        )
    assert tasks.tasks.count() == 0


def test_key_prefix_comes_from_the_objective(wiring) -> None:
    _agents, _tasks, planner = wiring
    objective = planner.objectives.create("Authentication overhaul")
    created = planner.apply(objective, plan(task("A")))
    assert created[0].key.startswith("AUTHENTI-")


def test_key_prefix_falls_back_for_unusable_descriptions(wiring) -> None:
    _agents, _tasks, planner = wiring
    objective = planner.objectives.create("!!! 123 ???")
    created = planner.apply(objective, plan(task("A")))
    assert created[0].key.startswith(f"OBJ{objective.id}-")

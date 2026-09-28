"""Phase 4: the message bus and structured result processing.

The central concern is that a message is never lost and never double-delivered,
even when a Claude process dies mid-run.
"""

from __future__ import annotations

import json

import pytest

from agentos.config import Config
from agentos.db.session import Database
from agentos.schemas.enums import MessageStatus, MessageType
from agentos.schemas.responses import RESPONSE_BEGIN, RESPONSE_END
from agentos.services.agents import AgentService
from agentos.services.messages import (
    HUMAN_SENDER,
    MessageService,
    MessageValidationError,
)
from agentos.services.results import ResultProcessor, build_repair_prompt
from agentos.services.tasks import TaskService
from tests.test_agents import StubRuntime

CONFIG_DICT = {
    "project": {"name": "Bus"},
    "agents": {
        "manager": {"role": "manager"},
        "backend": {"role": "backend"},
        "frontend": {"role": "frontend"},
        "qa": {"role": "qa"},
    },
}


def block(**payload) -> str:
    body = {
        "status": "completed",
        "summary": "did the thing",
        "files_changed": [],
        "messages": [],
        "requested_tasks": [],
        "blockers": [],
    }
    body.update(payload)
    return "\n".join(["prose first", RESPONSE_BEGIN, json.dumps(body), RESPONSE_END])


@pytest.fixture
def config() -> Config:
    return Config.model_validate(CONFIG_DICT)


@pytest.fixture
def wiring(db: Database, config: Config, tmp_path):
    agents = AgentService(db, config, StubRuntime(), tmp_path)
    agents.sync_from_config()
    tasks = TaskService(db, config)
    messages = MessageService(db, config)
    processor = ResultProcessor(db, config, tasks, messages)
    return agents, tasks, messages, processor


# ------------------------------------------------------------------- sending


def test_send_between_agents(wiring) -> None:
    _agents, _tasks, messages, _p = wiring
    message = messages.send("backend", "frontend", "Endpoint is POST /api/login")
    assert message.sender == "backend"
    assert message.recipient == "frontend"
    assert message.status is MessageStatus.PENDING


def test_send_from_human(wiring) -> None:
    _agents, _tasks, messages, _p = wiring
    message = messages.send_from_human("backend", "Please check the middleware")
    assert message.sender == HUMAN_SENDER


def test_unknown_recipient_is_rejected(wiring) -> None:
    _agents, _tasks, messages, _p = wiring
    with pytest.raises(MessageValidationError, match="unknown recipient 'ghost'"):
        messages.send("backend", "ghost", "hello")


def test_unknown_recipient_error_lists_known_agents(wiring) -> None:
    _agents, _tasks, messages, _p = wiring
    with pytest.raises(MessageValidationError, match="backend"):
        messages.send("backend", "ghost", "hello")


def test_empty_body_is_rejected(wiring) -> None:
    _agents, _tasks, messages, _p = wiring
    with pytest.raises(MessageValidationError, match="must not be empty"):
        messages.send("backend", "frontend", "   ")


def test_agent_cannot_message_itself(wiring) -> None:
    _agents, _tasks, messages, _p = wiring
    with pytest.raises(MessageValidationError, match="itself"):
        messages.send("backend", "backend", "note to self")


def test_rejected_message_is_not_stored(wiring) -> None:
    _agents, _tasks, messages, _p = wiring
    with pytest.raises(MessageValidationError):
        messages.send("backend", "ghost", "hello")
    assert messages.list_all() == []


def test_unknown_task_link_is_dropped_not_fatal(wiring) -> None:
    """A bad task reference must not destroy the message content."""
    _agents, _tasks, messages, _p = wiring
    message = messages.send("backend", "frontend", "hi", task_key="NOPE-9")
    assert message.task_key is None
    assert message.body == "hi"


def test_message_can_reference_a_real_task(wiring) -> None:
    _agents, tasks, messages, _p = wiring
    task = tasks.create_task("work", agent="backend")
    message = messages.send("backend", "frontend", "hi", task_key=task.key)
    assert message.task_key == task.key


# ------------------------------------------------------- delivery lifecycle


def test_inbox_starts_empty(wiring) -> None:
    _agents, _tasks, messages, _p = wiring
    assert not messages.take_inbox("frontend")


def test_take_inbox_marks_delivered_not_read(wiring) -> None:
    """Read must mean "the run finished", not "we put it in a prompt"."""
    _agents, _tasks, messages, _p = wiring
    messages.send("backend", "frontend", "endpoint moved")

    delivery = messages.take_inbox("frontend")
    assert [i.body for i in delivery.items] == ["endpoint moved"]
    stored = messages.list_for("frontend")[0]
    assert stored.status is MessageStatus.DELIVERED
    assert stored.delivered_at is not None
    assert stored.read_at is None


def test_confirm_read_completes_delivery(wiring) -> None:
    _agents, _tasks, messages, _p = wiring
    messages.send("backend", "frontend", "x")
    delivery = messages.take_inbox("frontend")
    messages.confirm_read(delivery)

    stored = messages.list_for("frontend")[0]
    assert stored.status is MessageStatus.READ
    assert stored.read_at is not None
    # Consumed, so it must not appear again.
    assert not messages.take_inbox("frontend")


def test_release_returns_message_for_redelivery(wiring) -> None:
    """A crashed run must not swallow the message."""
    _agents, _tasks, messages, _p = wiring
    messages.send("backend", "frontend", "important")

    first = messages.take_inbox("frontend")
    messages.release(first)
    assert messages.list_for("frontend")[0].status is MessageStatus.PENDING

    second = messages.take_inbox("frontend")
    assert [i.body for i in second.items] == ["important"]


def test_undelivered_message_survives_a_crash_shaped_gap(wiring) -> None:
    """Simulates the process dying after injection: still pending afterwards."""
    _agents, _tasks, messages, _p = wiring
    messages.send("backend", "frontend", "survive me")
    delivery = messages.take_inbox("frontend")
    # No confirm_read: the run died here.
    messages.release(delivery)
    assert messages.list_for("frontend", unread_only=True)


def test_release_does_not_resurrect_a_read_message(wiring) -> None:
    _agents, _tasks, messages, _p = wiring
    messages.send("backend", "frontend", "x")
    delivery = messages.take_inbox("frontend")
    messages.confirm_read(delivery)
    messages.release(delivery)
    assert messages.list_for("frontend")[0].status is MessageStatus.READ


def test_redelivery_keeps_the_original_delivered_at(wiring) -> None:
    _agents, _tasks, messages, _p = wiring
    messages.send("backend", "frontend", "x")
    first = messages.take_inbox("frontend")
    original = messages.list_for("frontend")[0].delivered_at
    messages.release(first)
    messages.take_inbox("frontend")
    assert messages.list_for("frontend")[0].delivered_at == original


def test_messages_are_delivered_oldest_first(wiring) -> None:
    _agents, _tasks, messages, _p = wiring
    for i in range(3):
        messages.send("backend", "frontend", f"m{i}")
    delivery = messages.take_inbox("frontend")
    assert [i.body for i in delivery.items] == ["m0", "m1", "m2"]


def test_inbox_is_capped(wiring) -> None:
    """One agent must not be able to flood another's prompt."""
    from agentos.services.messages import MAX_INBOX_ITEMS

    _agents, _tasks, messages, _p = wiring
    for i in range(MAX_INBOX_ITEMS + 5):
        messages.send("backend", "frontend", f"m{i}")
    assert len(messages.take_inbox("frontend").items) == MAX_INBOX_ITEMS


def test_inbox_is_per_agent(wiring) -> None:
    _agents, _tasks, messages, _p = wiring
    messages.send("backend", "frontend", "for frontend")
    assert not messages.take_inbox("manager")
    assert messages.take_inbox("frontend")


def test_unread_counts(wiring) -> None:
    _agents, _tasks, messages, _p = wiring
    messages.send("backend", "frontend", "a")
    messages.send("backend", "frontend", "b")
    messages.send("frontend", "manager", "c")
    counts = messages.unread_counts()
    assert counts["frontend"] == 2
    assert counts["manager"] == 1
    assert counts["backend"] == 0


def test_taking_inbox_of_unknown_agent_is_empty(wiring) -> None:
    _agents, _tasks, messages, _p = wiring
    assert not messages.take_inbox("nobody")


# --------------------------------------------------------- result processing


def test_valid_result_is_parsed(wiring) -> None:
    *_x, processor = wiring
    outcome = processor.process(block(summary="built it"), agent_name="backend")
    assert outcome.parsed
    assert outcome.summary == "built it"
    assert not outcome.treat_as_failure


def test_malformed_result_is_not_fatal(wiring) -> None:
    *_x, processor = wiring
    outcome = processor.process("I did it, trust me", agent_name="backend")
    assert not outcome.parsed
    assert outcome.parse_error
    assert outcome.treat_as_failure


def test_invalid_json_is_reported(wiring) -> None:
    *_x, processor = wiring
    text = "\n".join([RESPONSE_BEGIN, "{not json", RESPONSE_END])
    outcome = processor.process(text, agent_name="backend")
    assert not outcome.parsed
    assert "not valid JSON" in (outcome.parse_error or "")


def test_agent_reported_failure_is_treated_as_failure(wiring) -> None:
    *_x, processor = wiring
    outcome = processor.process(block(status="failed"), agent_name="backend")
    assert outcome.parsed
    assert outcome.treat_as_failure


def test_blocked_status_is_treated_as_failure(wiring) -> None:
    *_x, processor = wiring
    outcome = processor.process(
        block(status="blocked", blockers=["needs credentials"]), agent_name="backend"
    )
    assert outcome.blockers == ["needs credentials"]
    assert outcome.treat_as_failure


def test_messages_in_result_are_delivered(wiring) -> None:
    _agents, _tasks, messages, processor = wiring
    outcome = processor.process(
        block(messages=[{"to": "frontend", "message": "POST /api/v2/users"}]),
        agent_name="backend",
    )
    assert outcome.messages_sent == 1
    inbox = messages.take_inbox("frontend")
    assert "POST /api/v2/users" in inbox.items[0].body
    assert inbox.items[0].sender == "backend"


def test_message_to_unknown_agent_is_rejected_with_reason(wiring) -> None:
    _agents, _tasks, messages, processor = wiring
    outcome = processor.process(
        block(messages=[{"to": "nonexistent", "message": "hi"}]), agent_name="backend"
    )
    assert outcome.messages_sent == 0
    assert any("nonexistent" in r for r in outcome.rejected)
    assert messages.list_all() == []


def test_message_limit_per_turn(wiring) -> None:
    from agentos.services.results import MAX_MESSAGES

    _agents, _tasks, _m, processor = wiring
    outbound = [
        {"to": "frontend", "message": f"m{i}"} for i in range(MAX_MESSAGES + 3)
    ]
    outcome = processor.process(block(messages=outbound), agent_name="backend")
    assert outcome.messages_sent == MAX_MESSAGES
    assert any("limit" in r for r in outcome.rejected)


# --------------------------------------------------------- requested tasks


def test_requested_task_by_role_is_created(wiring) -> None:
    _agents, tasks, _m, processor = wiring
    parent = tasks.create_task("parent", agent="backend")
    outcome = processor.process(
        block(
            requested_tasks=[
                {
                    "agent_role": "frontend",
                    "title": "Wire up the form",
                    "description": "Use the new endpoint.",
                }
            ]
        ),
        task=parent,
        agent_name="backend",
    )
    assert len(outcome.tasks_created) == 1
    created = tasks.get_task(outcome.tasks_created[0])
    assert created.assigned_agent == "frontend"
    assert created.created_by == "backend"


def test_requested_task_by_name_is_created(wiring) -> None:
    _agents, tasks, _m, processor = wiring
    outcome = processor.process(
        block(requested_tasks=[{"agent_name": "qa", "title": "Test it"}]),
        agent_name="backend",
    )
    assert tasks.get_task(outcome.tasks_created[0]).assigned_agent == "qa"


def test_requested_task_depends_on_its_parent(wiring) -> None:
    """Follow-up work must not run before the work that prompted it."""
    _agents, tasks, _m, processor = wiring
    parent = tasks.create_task("parent", agent="backend")
    outcome = processor.process(
        block(requested_tasks=[{"agent_role": "qa", "title": "Test it"}]),
        task=parent,
        agent_name="backend",
    )
    created = tasks.get_task(outcome.tasks_created[0])
    assert parent.key in created.depends_on


def test_requested_task_inherits_key_prefix(wiring) -> None:
    _agents, tasks, _m, processor = wiring
    parent = tasks.create_task("parent", agent="backend", prefix="AUTH")
    outcome = processor.process(
        block(requested_tasks=[{"agent_role": "qa", "title": "Test it"}]),
        task=parent,
        agent_name="backend",
    )
    assert outcome.tasks_created[0].startswith("AUTH-")


def test_requested_task_for_unknown_role_is_rejected(wiring) -> None:
    _agents, tasks, _m, processor = wiring
    outcome = processor.process(
        block(requested_tasks=[{"agent_role": "astronaut", "title": "Fly"}]),
        agent_name="backend",
    )
    assert outcome.tasks_created == []
    assert any("astronaut" in r for r in outcome.rejected)
    assert tasks.tasks.count() == 0


def test_requested_task_for_unknown_agent_is_rejected(wiring) -> None:
    _agents, _tasks, _m, processor = wiring
    outcome = processor.process(
        block(requested_tasks=[{"agent_name": "ghost", "title": "Boo"}]),
        agent_name="backend",
    )
    assert outcome.tasks_created == []
    assert any("ghost" in r for r in outcome.rejected)


def test_ambiguous_role_is_rejected_not_guessed(db, tmp_path) -> None:
    """Two agents share a role, so the orchestrator must refuse to pick."""
    config = Config.model_validate(
        {
            "agents": {
                "api": {"role": "backend"},
                "worker": {"role": "backend"},
                "boss": {"role": "manager"},
            }
        }
    )
    AgentService(db, config, StubRuntime(), tmp_path).sync_from_config()
    tasks = TaskService(db, config)
    processor = ResultProcessor(db, config, tasks, MessageService(db, config))

    outcome = processor.process(
        block(requested_tasks=[{"agent_role": "backend", "title": "x"}]),
        agent_name="boss",
    )
    assert outcome.tasks_created == []
    assert any("ambiguous" in r for r in outcome.rejected)


def test_requested_task_without_recipient_fails_validation(wiring) -> None:
    *_x, processor = wiring
    outcome = processor.process(
        block(requested_tasks=[{"title": "orphan work"}]), agent_name="backend"
    )
    # The whole response fails schema validation rather than silently dropping it.
    assert not outcome.parsed


def test_unknown_dependency_in_requested_task_is_ignored(wiring) -> None:
    """An agent cannot invent a prerequisite."""
    _agents, tasks, _m, processor = wiring
    outcome = processor.process(
        block(
            requested_tasks=[
                {"agent_role": "qa", "title": "Test", "depends_on": ["MADE-UP-1"]}
            ]
        ),
        agent_name="backend",
    )
    assert len(outcome.tasks_created) == 1
    created = tasks.get_task(outcome.tasks_created[0])
    assert created.depends_on == []
    assert any("MADE-UP-1" in r for r in outcome.rejected)


def test_real_dependency_in_requested_task_is_honoured(wiring) -> None:
    _agents, tasks, _m, processor = wiring
    existing = tasks.create_task("existing", agent="backend")
    outcome = processor.process(
        block(
            requested_tasks=[
                {"agent_role": "qa", "title": "Test", "depends_on": [existing.key]}
            ]
        ),
        agent_name="backend",
    )
    created = tasks.get_task(outcome.tasks_created[0])
    assert existing.key in created.depends_on


def test_requested_task_limit_per_turn(wiring) -> None:
    from agentos.services.results import MAX_REQUESTED_TASKS

    _agents, _tasks, _m, processor = wiring
    requested = [
        {"agent_role": "qa", "title": f"t{i}"} for i in range(MAX_REQUESTED_TASKS + 3)
    ]
    outcome = processor.process(block(requested_tasks=requested), agent_name="backend")
    assert len(outcome.tasks_created) == MAX_REQUESTED_TASKS
    assert any("limit" in r for r in outcome.rejected)


def test_dangerous_extra_fields_are_dropped(wiring) -> None:
    """Unknown keys must never reach application state."""
    *_x, processor = wiring
    text = "\n".join(
        [
            RESPONSE_BEGIN,
            json.dumps(
                {
                    "status": "completed",
                    "summary": "ok",
                    "run_command": "rm -rf /",
                    "sql": "DROP TABLE tasks",
                }
            ),
            RESPONSE_END,
        ]
    )
    outcome = processor.process(text, agent_name="backend")
    assert outcome.parsed
    dumped = outcome.model_dump()
    assert "run_command" not in dumped
    assert "sql" not in dumped


# --------------------------------------------------------------- repair prompt


def test_repair_prompt_asks_only_for_the_block() -> None:
    prompt = build_repair_prompt("some rambling output", "no block found")
    assert "no block found" in prompt
    assert "do NOT change any files" in prompt
    assert RESPONSE_BEGIN in prompt


def test_repair_prompt_truncates_long_output() -> None:
    prompt = build_repair_prompt("x" * 50_000, "too long")
    assert len(prompt) < 10_000

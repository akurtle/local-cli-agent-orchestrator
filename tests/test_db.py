from __future__ import annotations

import json
from datetime import datetime, timezone

from agentos.db.models import Agent, Message, Run, Task, TaskDependency
from agentos.db.session import Database
from agentos.schemas.enums import AgentStatus, RunStatus, TaskStatus
from agentos.schemas.runtime import RunResult
from agentos.services.runs import record_run


def test_create_all_is_idempotent(db: Database) -> None:
    db.create_all()
    db.create_all()


def test_agent_round_trip(db: Database) -> None:
    with db.session() as session:
        session.add(Agent(name="backend", role="backend", session_id="sid-1"))
    with db.session() as session:
        agent = session.query(Agent).filter_by(name="backend").one()
        assert agent.status == AgentStatus.IDLE.value
        assert agent.session_id == "sid-1"
        assert agent.created_at is not None


def test_task_defaults_to_pending(db: Database) -> None:
    with db.session() as session:
        session.add(Task(key="AUTH-1", title="Analyse auth"))
    with db.session() as session:
        task = session.query(Task).one()
        assert task.status == TaskStatus.PENDING.value
        assert task.attempts == 0


def test_dependency_edges_persist(db: Database) -> None:
    with db.session() as session:
        a = Task(key="A", title="a")
        b = Task(key="B", title="b")
        session.add_all([a, b])
        session.flush()
        session.add(TaskDependency(task_id=b.id, depends_on_task_id=a.id))
    with db.session() as session:
        task_b = session.query(Task).filter_by(key="B").one()
        assert len(task_b.dependencies) == 1
        assert task_b.dependencies[0].depends_on_task_id is not None


def test_rollback_on_error_leaves_no_partial_write(db: Database) -> None:
    try:
        with db.session() as session:
            session.add(Task(key="X", title="x"))
            session.flush()
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with db.session() as session:
        assert session.query(Task).count() == 0


def test_message_defaults_unread(db: Database) -> None:
    with db.session() as session:
        session.add(Message(sender_name="backend", body="endpoint moved"))
    with db.session() as session:
        message = session.query(Message).one()
        assert message.read is False
        assert message.message_type == "info"


def test_record_run_persists_result(db: Database) -> None:
    result = RunResult(
        status=RunStatus.SUCCEEDED,
        session_id="sid-9",
        exit_code=0,
        text="done",
        stdout="{}\n",
        stderr="",
        command=["claude", "-p", "--output-format", "stream-json"],
        finished_at=datetime.now(timezone.utc),
        cost_usd=0.5,
        num_turns=2,
    )
    run_id = record_run(db, result)

    with db.session() as session:
        row = session.get(Run, run_id)
        assert row is not None
        assert row.status == "succeeded"
        assert row.result_text == "done"
        assert row.session_id == "sid-9"
        assert row.cost_usd == 0.5
        # argv is stored as JSON for auditing, never re-executed.
        assert json.loads(row.command)[0] == "claude"


def test_record_run_captures_failure_detail(db: Database) -> None:
    result = RunResult(
        status=RunStatus.FAILED,
        exit_code=3,
        stderr="kaboom",
        error="claude exited with code 3",
        finished_at=datetime.now(timezone.utc),
    )
    run_id = record_run(db, result)
    with db.session() as session:
        row = session.get(Run, run_id)
        assert row.status == "failed"
        assert row.error == "claude exited with code 3"
        assert row.stderr == "kaboom"


def test_task_status_terminal_flags() -> None:
    assert TaskStatus.COMPLETED.is_terminal
    assert TaskStatus.FAILED.is_terminal
    assert TaskStatus.CANCELLED.is_terminal
    assert not TaskStatus.RUNNING.is_terminal
    assert not TaskStatus.READY.is_terminal


async def test_database_is_usable_from_worker_threads(tmp_path) -> None:
    """Regression: async callers move DB work onto threads via asyncio.to_thread.

    SQLite's default same-thread check rejected this, which would have broken
    concurrent agent execution. Uses a file-backed database because that is the
    real production configuration (a pool with one connection per thread).
    """
    import asyncio

    db = Database(tmp_path / "threads.db")
    db.create_all()

    def write(key: str) -> int:
        with db.session() as session:
            task = Task(key=key, title=key)
            session.add(task)
            session.flush()
            return task.id

    ids = await asyncio.gather(*(asyncio.to_thread(write, f"T-{i}") for i in range(8)))
    assert len(set(ids)) == 8

    def count() -> int:
        with db.session() as session:
            return session.query(Task).count()

    assert await asyncio.to_thread(count) == 8
    db.dispose()

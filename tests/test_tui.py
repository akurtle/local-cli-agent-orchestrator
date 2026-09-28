"""Phase 12: the Textual dashboard.

Two things worth proving: the snapshot carries the right data (testable without
Textual at all), and the app actually mounts and renders it (driven headlessly).
The TUI must also be incapable of running an agent.
"""

from __future__ import annotations

import pytest

from agentos.config import Config
from agentos.db.session import Database
from agentos.paths import ProjectPaths
from agentos.schemas.enums import AgentStatus, TaskStatus
from agentos.services.agents import AgentService
from agentos.services.messages import MessageService
from agentos.services.tasks import TaskService
from agentos.tui.snapshot import Snapshot, SnapshotReader, _NullRuntime
from tests.test_agents import StubRuntime

textual = pytest.importorskip("textual")

CONFIG = {
    "project": {"name": "Dash"},
    "agents": {
        "manager": {"role": "manager"},
        "backend": {"role": "backend"},
        "frontend": {"role": "frontend"},
        "qa": {"role": "qa"},
    },
}


@pytest.fixture
def project(tmp_path):
    """A project with agents, tasks, messages and a run on disk."""
    paths = ProjectPaths(root=tmp_path)
    paths.ensure()
    db = Database(paths.db_file)
    db.create_all()
    config = Config.model_validate(CONFIG)

    agents = AgentService(db, config, StubRuntime(), tmp_path)
    agents.sync_from_config()
    tasks = TaskService(db, config)
    messages = MessageService(db, config)

    first = tasks.create_task("Build the API", agent="backend", prefix="API")
    tasks.transition(first.key, TaskStatus.RUNNING)
    tasks.transition(first.key, TaskStatus.COMPLETED, result="done the thing")
    second = tasks.create_task(
        "Test the API", agent="qa", prefix="API", depends_on=[first.key]
    )
    tasks.transition(second.key, TaskStatus.RUNNING)
    tasks.transition(
        second.key, TaskStatus.BLOCKED, error="needs credentials",
        needs_intervention=True,
    )
    messages.send("backend", "frontend", "Endpoint is POST /api/v2/login")

    reader = SnapshotReader(db, config, paths)
    yield reader, tasks, db
    db.dispose()


# ----------------------------------------------------------------- the snapshot


def test_snapshot_gathers_everything(project) -> None:
    reader, _tasks, _db = project
    snapshot = reader.read()
    assert snapshot.project == "Dash"
    assert {a.name for a in snapshot.agents} == {
        "manager",
        "backend",
        "frontend",
        "qa",
    }
    assert len(snapshot.tasks) == 2
    assert len(snapshot.messages) == 1


def test_snapshot_counts(project) -> None:
    reader, _tasks, _db = project
    snapshot = reader.read()
    assert snapshot.completed == 1
    assert [t.key for t in snapshot.blocked] == ["API-2"]
    assert snapshot.failed == []


def test_snapshot_lookups(project) -> None:
    reader, _tasks, _db = project
    snapshot = reader.read()
    assert snapshot.task("API-1").title == "Build the API"
    assert snapshot.task("NOPE") is None
    assert snapshot.agent("backend").role == "backend"
    assert snapshot.agent("ghost") is None


def test_snapshot_includes_runs(project) -> None:
    reader, _tasks, db = project
    from agentos.db.models import Run

    with db.session() as session:
        agent_id = reader.agents.get_agent("backend").id
        session.add(
            Run(agent_id=agent_id, status="succeeded", result_text="ran fine")
        )

    snapshot = reader.read()
    assert snapshot.runs
    assert snapshot.runs_for("backend")
    assert snapshot.runs_for("frontend") == []


def test_empty_snapshot_is_safe() -> None:
    snapshot = Snapshot(project="Empty")
    assert snapshot.completed == 0
    assert snapshot.blocked == []
    assert snapshot.active_objective is None
    assert snapshot.task("X") is None


# ----------------------------------------------------------- the viewer is read-only


async def test_tui_runtime_refuses_to_run_agents() -> None:
    """A read-only screen must not be able to spend usage."""
    runtime = _NullRuntime()
    with pytest.raises(RuntimeError, match="read-only"):
        await runtime.run(object())
    with pytest.raises(RuntimeError, match="read-only"):
        await runtime.resume("s", "p")


def test_reader_is_wired_to_the_null_runtime(project) -> None:
    reader, _tasks, _db = project
    assert isinstance(reader.agents.runtime, _NullRuntime)


# --------------------------------------------------------------- summary line


def test_summary_line_reports_progress(project) -> None:
    from agentos.tui.app import summary_line

    reader, _tasks, _db = project
    line = summary_line(reader.read())
    assert "Dash" in line
    assert "1/2 tasks complete" in line
    assert "1 blocked" in line


def test_summary_line_omits_absent_counts() -> None:
    from agentos.tui.app import summary_line

    line = summary_line(Snapshot(project="Quiet"))
    assert "Quiet" in line
    assert "failed" not in line
    assert "blocked" not in line


# ------------------------------------------------------------------- the app


async def test_app_mounts_and_renders(project) -> None:
    """Drive the real app headlessly, so a broken layout fails the suite."""
    from agentos.tui.app import AgentsPanel, DashboardApp, TasksPanel

    reader, _tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.snapshot is not None

        agents = app.query_one("#agents", AgentsPanel)
        tasks_panel = app.query_one("#tasks", TasksPanel)
        assert agents.row_count == 4
        assert tasks_panel.row_count == 2


async def test_selecting_a_task_shows_its_detail(project) -> None:
    from agentos.tui.app import DashboardApp, OutputPanel, TasksPanel

    reader, _tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.query_one("#tasks", TasksPanel).focus()
        await pilot.pause()

        from agentos.tui.app import panel_text

        output = app.query_one("#output", OutputPanel)
        rendered = panel_text(output)
        # The highlighted row drives the pane.
        assert "API-1" in rendered or "API-2" in rendered


async def test_blocked_task_detail_shows_the_error(project) -> None:
    from agentos.tui.app import DashboardApp, OutputPanel, panel_text

    reader, _tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_task("API-2")
        rendered = panel_text(app.query_one("#output", OutputPanel))
        assert "needs credentials" in rendered


async def test_agent_detail_shows_session_and_runs(project) -> None:
    from agentos.tui.app import DashboardApp, OutputPanel, panel_text

    reader, _tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_agent("backend")
        rendered = panel_text(app.query_one("#output", OutputPanel))
        assert "backend" in rendered
        assert "session" in rendered


async def test_refresh_binding_works(project) -> None:
    from agentos.tui.app import DashboardApp

    reader, tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert len(app.snapshot.tasks) == 2

        tasks.create_task("Another task", agent="frontend", prefix="API")
        await pilot.press("r")
        await pilot.pause()
        assert len(app.snapshot.tasks) == 3


async def test_a_failing_read_does_not_crash_the_ui(project) -> None:
    """A transient database error must show a message, not kill the app."""
    from agentos.tui.app import DashboardApp

    reader, _tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()

        def boom() -> Snapshot:
            raise RuntimeError("database went away")

        reader.read = boom
        app.refresh_snapshot()
        await pilot.pause()
        assert app.is_running
        from textual.widgets import Static

        from agentos.tui.app import panel_text

        assert "read failed" in panel_text(app.query_one("#summary", Static))


async def test_quit_binding(project) -> None:
    from agentos.tui.app import DashboardApp

    reader, _tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
    assert not app.is_running


async def test_selection_is_exclusive(project) -> None:
    """Selecting an agent must clear the task, and vice versa.

    The output pane has to prefer one, so setting both would silently show the
    wrong thing.
    """
    from agentos.tui.app import DashboardApp, OutputPanel, panel_text

    reader, _tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()

        app.select_task("API-1")
        assert app._selected_agent is None
        assert "API-1" in panel_text(app.query_one("#output", OutputPanel))

        app.select_agent("backend")
        assert app._selected_task is None
        rendered = panel_text(app.query_one("#output", OutputPanel))
        assert "session" in rendered

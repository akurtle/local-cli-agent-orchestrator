"""Dashboard controls: start/stop work (w), block (b), pause/cancel (s).

The work lock is tested across real processes, because the thing it prevents --
two schedulers on one database -- only happens across processes.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from agentos.paths import ProjectPaths
from agentos.schemas.enums import AgentStatus, TaskStatus
from agentos.services.tasks import InvalidTaskTransition
from agentos.worklock import (
    WorkLock,
    clear_stop,
    is_running,
    request_stop,
    stop_requested,
)
from tests.test_tui import project  # noqa: F401  (shared fixture)

textual = pytest.importorskip("textual")

SRC = str(Path(__file__).resolve().parents[1] / "src")


def holder_script(root: Path) -> list[str]:
    """A stand-in for `agentctl work`: takes the lock, exits when asked to stop."""
    code = (
        "import sys, time; sys.path.insert(0, %r)\n"
        "from pathlib import Path\n"
        "from agentos.paths import ProjectPaths\n"
        "from agentos.worklock import WorkLock, stop_requested\n"
        "paths = ProjectPaths(root=Path(%r))\n"
        "lock = WorkLock(paths)\n"
        "assert lock.acquire()\n"
        "deadline = time.time() + 20\n"
        "while not stop_requested(paths) and time.time() < deadline:\n"
        "    time.sleep(0.1)\n"
    ) % (SRC, str(root))
    return [sys.executable, "-c", code]


def wait_until(condition, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.1)
    return False


# ------------------------------------------------------------------ the lock


def test_lock_is_exclusive_across_processes(tmp_path: Path) -> None:
    paths = ProjectPaths(root=tmp_path)
    assert not is_running(paths)

    holder = subprocess.Popen(holder_script(tmp_path))
    try:
        assert wait_until(lambda: is_running(paths))
        assert not WorkLock(paths).acquire()
    finally:
        holder.kill()
        holder.wait()
    # The OS releases the lock when the process dies, however it dies.
    assert wait_until(lambda: not is_running(paths))
    assert WorkLock(paths).acquire()


def test_stop_file_round_trip(tmp_path: Path) -> None:
    paths = ProjectPaths(root=tmp_path)
    assert not stop_requested(paths)
    request_stop(paths)
    assert stop_requested(paths)
    clear_stop(paths)
    assert not stop_requested(paths)


def test_work_refuses_to_run_twice(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from agentos.cli.main import app

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--name", "T"]).exit_code == 0

    holder = subprocess.Popen(holder_script(tmp_path))
    try:
        assert wait_until(lambda: is_running(ProjectPaths(root=tmp_path)))
        result = runner.invoke(app, ["work", "--dry-run"])
        assert result.exit_code == 1
        assert "already running" in result.output
    finally:
        holder.kill()
        holder.wait()

    # With the lock free, it runs, and a stale stop request does not stop it.
    request_stop(ProjectPaths(root=tmp_path))
    result = runner.invoke(app, ["work", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert not stop_requested(ProjectPaths(root=tmp_path))


async def test_scheduler_honours_an_outside_stop(db, tmp_path: Path) -> None:
    from tests.test_scheduler import build, make_config
    from tests.test_agents import StubRuntime

    scheduler, tasks, _events = build(db, make_config(), StubRuntime(), tmp_path)
    task = tasks.create_task("work", agent="backend")
    scheduler.stop_check = lambda: True

    report = await scheduler.run()
    assert report.dispatched == 0
    assert tasks.get_task(task.key).status is not TaskStatus.RUNNING


# ----------------------------------------------------------------- holding


def test_hold_and_unblock(project) -> None:
    _reader, tasks, _db = project
    waiting = tasks.create_task("Later", agent="frontend", prefix="API")

    held = tasks.hold(waiting.key)
    assert held.status is TaskStatus.BLOCKED and held.needs_intervention
    assert held.error == tasks.HELD
    # Readiness must not quietly release it.
    tasks.refresh_readiness()
    assert tasks.get_task(waiting.key).status is TaskStatus.BLOCKED

    with pytest.raises(InvalidTaskTransition, match="already held"):
        tasks.hold(waiting.key)
    assert tasks.unblock(waiting.key).status in {TaskStatus.PENDING, TaskStatus.READY}


def test_running_tasks_cannot_be_held_or_cancelled_from_the_dashboard(project) -> None:
    reader, tasks, _db = project
    running = tasks.create_task("Busy", agent="frontend", prefix="API")
    tasks.transition(running.key, TaskStatus.RUNNING)

    with pytest.raises(InvalidTaskTransition, match="only waiting tasks"):
        reader.hold(running.key)
    with pytest.raises(InvalidTaskTransition, match="is running"):
        reader.cancel(running.key)


# ------------------------------------------------------------------- the app


async def test_b_blocks_the_selected_task(project) -> None:
    from agentos.tui.app import DashboardApp

    reader, tasks, _db = project
    waiting = tasks.create_task("Later", agent="frontend", prefix="API")
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_task(waiting.key)
        await pilot.press("b", "y")
        await pilot.pause()
        assert tasks.get_task(waiting.key).needs_intervention
        # And it now shows up as needing the human.
        assert any(i.key == f"task:{waiting.key}" for i in app.attention)


async def test_s_pauses_and_resumes_an_agent(project) -> None:
    from agentos.tui.app import AgentsPanel, DashboardApp

    reader, _tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Select the way a user does: put the cursor on the row.
        table = app.query_one("#agents", AgentsPanel)
        table.focus()
        table.move_cursor(row=table.get_row_index("frontend"))
        await pilot.pause()
        assert app._selected_agent == "frontend"
        await pilot.press("s", "y")
        await pilot.pause()
        assert reader.agents.get_agent("frontend").status is AgentStatus.PAUSED

        await pilot.press("s")  # resuming needs no confirmation
        await pilot.pause()
        assert reader.agents.get_agent("frontend").status is AgentStatus.IDLE


async def test_s_cancels_the_selected_task(project) -> None:
    from agentos.tui.app import DashboardApp

    reader, tasks, _db = project
    waiting = tasks.create_task("Unneeded", agent="frontend", prefix="API")
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_task(waiting.key)
        await pilot.press("s", "y")
        await pilot.pause()
        assert tasks.get_task(waiting.key).status is TaskStatus.CANCELLED


async def test_w_starts_and_then_stops_work(project) -> None:
    from agentos.tui.app import ConfirmScreen, DashboardApp, summary_line

    reader, _tasks, _db = project
    reader.work_command = holder_script(reader.paths.root)
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("w")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await pilot.pause()

        assert wait_until(lambda: reader.work_state() == "running")
        app.refresh_snapshot()
        assert "work running" in summary_line(app.snapshot)
        # Its output goes to a log file under .agentos/logs.
        assert list(reader.paths.logs_dir.glob("work-*.log"))

        await pilot.press("w", "y")  # while running, w asks it to stop
        await pilot.pause()
        assert stop_requested(reader.paths)
        assert wait_until(lambda: reader.work_state() is None)
        reader.work_process.wait(timeout=10)


async def test_w_refuses_a_second_scheduler(project) -> None:
    reader, _tasks, _db = project
    holder = subprocess.Popen(holder_script(reader.paths.root))
    try:
        assert wait_until(lambda: reader.work_state() == "running")
        with pytest.raises(RuntimeError, match="already running"):
            reader.start_work()
    finally:
        holder.kill()
        holder.wait()

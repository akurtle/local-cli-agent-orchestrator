"""The dashboard's "needs you" panel: everything waiting on a human.

The claims worth proving: each kind of wait is found, a task blocked only by a
stuck dependency is folded under the item that needs the human, the CLI's
declined calls are recovered from a run, and the panel shows and hides itself.
"""

from __future__ import annotations

import json

import pytest

from agentos.schemas.dto import ObjectiveView, TaskView
from agentos.schemas.enums import ObjectiveStatus, TaskStatus
from agentos.tui.attention import (
    Kind,
    PendingCommand,
    attention_items,
    merge_items,
    parse_denials,
)
from agentos.tui.changes import SHARED, AgentChanges
from agentos.vcs.manager import FileDelta
from tests.test_tui import project  # noqa: F401  (shared fixture)

textual = pytest.importorskip("textual")


def task(key: str, status: TaskStatus, **extra) -> TaskView:
    number = int(key.split("-")[1])
    return TaskView(id=number, key=key, title=f"task {key}", status=status, **extra)


# ------------------------------------------------------------------- finding


def test_each_kind_of_wait_is_found() -> None:
    items = attention_items(
        objectives=[
            ObjectiveView(id=1, description="Ship", status=ObjectiveStatus.AWAITING_APPROVAL),
            ObjectiveView(id=2, description="Done", status=ObjectiveStatus.COMPLETED),
        ],
        tasks=[
            task("T-1", TaskStatus.BLOCKED, needs_intervention=True, error="need creds"),
            task("T-2", TaskStatus.FAILED, error="boom"),
            task("T-3", TaskStatus.FAILED_VERIFICATION, error="tests fail"),
            task("T-4", TaskStatus.COMPLETED),
            task("T-5", TaskStatus.RUNNING),
        ],
        pending_commands=[PendingCommand(7, "backend", "T-9", "git push", "rule")],
    )
    kinds = [i.kind for i in items]
    # Most urgent first.
    assert kinds == [Kind.PLAN, Kind.BLOCKED, Kind.COMMAND, Kind.VERIFY, Kind.FAILED]
    blocked = items[1]
    assert blocked.reason == "need creds"
    assert "agentctl task unblock T-1" in blocked.actions
    assert "agentctl task retry T-3" in items[3].actions


def test_nothing_waiting_means_no_items() -> None:
    assert attention_items([], [task("T-1", TaskStatus.COMPLETED)]) == []


def test_dependency_blocked_tasks_fold_under_their_cause() -> None:
    """T-2 and T-3 wait on T-1 (transitively); only T-1 needs the human."""
    items = attention_items(
        [],
        [
            task("T-1", TaskStatus.BLOCKED, needs_intervention=True),
            task("T-2", TaskStatus.BLOCKED, depends_on=["T-1"]),
            task("T-3", TaskStatus.BLOCKED, depends_on=["T-2"]),
        ],
    )
    assert [i.key for i in items] == ["task:T-1"]
    assert items[0].holding_up == ["T-2", "T-3"]


def test_blocked_task_with_no_listed_cause_still_shows() -> None:
    """A dependency cancelled out from under it: nothing else explains it."""
    items = attention_items(
        [],
        [
            task("T-1", TaskStatus.CANCELLED),
            task("T-2", TaskStatus.BLOCKED, depends_on=["T-1"]),
        ],
    )
    assert [i.key for i in items] == ["task:T-2"]
    assert "T-1 (cancelled)" in items[0].reason


def test_denials_attach_to_their_task() -> None:
    from agentos.tui.attention import Denial

    denial = Denial("Bash", "curl http://localhost")
    items = attention_items(
        [],
        [task("T-1", TaskStatus.BLOCKED, needs_intervention=True)],
        denials={"T-1": [denial]},
    )
    assert items[0].denials == [denial]


def test_merge_items_only_for_branches_with_commits() -> None:
    changes = [
        AgentChanges("backend", "main", "agent/backend", [FileDelta("a", 1)], 2),
        AgentChanges("qa", "main", "agent/qa", [FileDelta("b", 1)], 0),
        AgentChanges(SHARED, "HEAD", "main", [FileDelta("c", 1)]),
    ]
    items = merge_items(changes)
    assert [i.key for i in items] == ["merge:backend"]
    assert "agentctl integrate --apply" in items[0].actions


# ------------------------------------------------------------ parsing denials


def _result_line(denials) -> str:
    return json.dumps({"type": "result", "permission_denials": denials})


def test_parse_denials_reads_the_result_event() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "assistant", "message": {}}),
            _result_line(
                [
                    {"tool_name": "Bash", "tool_input": {"command": "npm  run\nlint"}},
                    {"tool_name": "Bash", "tool_input": {"command": "npm run lint"}},
                    {"tool_name": "Write", "tool_input": {"file_path": "src/a.ts"}},
                ]
            ),
        ]
    )
    denials = parse_denials(stdout)
    # Whitespace is normalised, so the repeat collapses into one.
    assert [d.spelled for d in denials] == ["Bash: npm run lint", "Write: src/a.ts"]


def test_parse_denials_tolerates_junk() -> None:
    assert parse_denials("") == []
    assert parse_denials("not json\n{broken") == []
    assert parse_denials(_result_line(None)) == []
    assert parse_denials(_result_line(["nonsense", {"tool_name": "Bash"}]))[0].tool == "Bash"


def test_reader_recovers_denials_from_the_latest_run(project) -> None:
    from agentos.db.models import Run

    reader, tasks, db = project
    blocked = next(t for t in tasks.list_tasks() if t.status is TaskStatus.BLOCKED)
    with db.session() as session:
        session.add(Run(task_id=blocked.id, stdout=_result_line([])))
        session.add(
            Run(
                task_id=blocked.id,
                stdout=_result_line(
                    [{"tool_name": "PowerShell", "tool_input": {"command": "npm test"}}]
                ),
            )
        )
        session.commit()

    item = next(i for i in reader.read().attention if i.key == f"task:{blocked.key}")
    assert [d.spelled for d in item.denials] == ["PowerShell: npm test"]


# ------------------------------------------------------------------- the app


async def test_panel_lists_what_needs_the_human(project) -> None:
    from agentos.tui.app import AttentionPanel, DashboardApp, summary_line

    reader, _tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one("#attention", AttentionPanel)
        # The fixture has one blocked task that needs intervention.
        assert panel.display and panel.row_count == 1
        assert "1 need you" in summary_line(app.snapshot)


async def test_panel_hides_when_nothing_is_waiting(project) -> None:
    from agentos.tui.app import AttentionPanel, DashboardApp

    reader, tasks, _db = project
    blocked = next(t for t in tasks.list_tasks() if t.needs_intervention)
    tasks.cancel(blocked.key)

    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.query_one("#attention", AttentionPanel).display


async def test_focusing_the_panel_shows_how_to_resolve(project) -> None:
    from agentos.tui.app import DashboardApp, OutputPanel, panel_text

    reader, _tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause()
        rendered = panel_text(app.query_one("#output", OutputPanel))
        assert "needs credentials" in rendered
        assert "agentctl task unblock API-2" in rendered
        assert app._selected_attention == "task:API-2"


async def test_refreshes_keep_the_cursor_and_selection(project) -> None:
    """Every refresh rebuilds the tables; the user's place must survive it.

    `DataTable.clear()` resets the cursor to the first row, which used to drag
    the selection back to the top every two seconds.
    """
    from agentos.tui.app import DashboardApp, TasksPanel

    reader, _tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.press("down")
        await pilot.pause()
        chosen = app._selected_task
        assert chosen is not None

        for _ in range(3):
            app.refresh_snapshot()
            await pilot.pause()
        table = app.query_one("#tasks", TasksPanel)
        row = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        assert row == chosen
        assert app._selected_task == chosen


async def test_an_unfocused_table_does_not_take_the_selection(project) -> None:
    """The attention panel rebuilding must not pull the pane away from a task."""
    from agentos.tui.app import DashboardApp

    reader, _tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("t")
        await pilot.pause()
        chosen = app._selected_task
        for _ in range(3):
            app.refresh_snapshot()
            await pilot.pause()
        assert app._selected_task == chosen
        assert app._selected_attention is None


# ------------------------------------------------------- unblock and retry keys


async def test_u_unblocks_the_selected_item_after_a_yes(project) -> None:
    from agentos.tui.app import ConfirmScreen, DashboardApp

    reader, tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n")  # needs you: API-2, blocked
        await pilot.press("u")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("y")
        await pilot.pause()

        task = tasks.get_task("API-2")
        assert task.status is not TaskStatus.BLOCKED
        assert not task.needs_intervention
        # Once resolved it drops off the panel.
        assert not any(i.key == "task:API-2" for i in app.attention)


async def test_answering_no_changes_nothing(project) -> None:
    from agentos.tui.app import DashboardApp

    reader, tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("n", "u")
        await pilot.pause()
        await pilot.press("n")  # "no" inside the dialog, not "needs you"
        await pilot.pause()
        assert tasks.get_task("API-2").status is TaskStatus.BLOCKED


async def test_shift_r_retries_a_failed_task(project) -> None:
    from agentos.tui.app import DashboardApp

    reader, tasks, _db = project
    failed = tasks.create_task("Flaky", agent="backend", prefix="API")
    tasks.transition(failed.key, TaskStatus.RUNNING)
    tasks.transition(failed.key, TaskStatus.FAILED, error="boom")

    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_task(failed.key)
        await pilot.press("R")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert tasks.get_task(failed.key).status in {TaskStatus.PENDING, TaskStatus.READY}


async def test_a_refused_action_is_reported_not_raised(project) -> None:
    """Retrying a blocked task is not allowed; the app says so and carries on."""
    from agentos.tui.app import DashboardApp

    reader, tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_task("API-2")
        await pilot.press("R")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert app.is_running
        assert tasks.get_task("API-2").status is TaskStatus.BLOCKED
        assert any("Could not retry" in n.message for n in app._notifications)


async def test_keys_need_a_selected_task(project) -> None:
    from agentos.tui.app import ConfirmScreen, DashboardApp

    reader, _tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.select_agent("backend")
        await pilot.press("u")
        await pilot.pause()
        assert not isinstance(app.screen, ConfirmScreen)
        assert any("Select a task" in n.message for n in app._notifications)

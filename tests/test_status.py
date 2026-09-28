"""Phase 8: the terminal presentation layer.

Rendering is tested for content rather than exact layout, so these do not break
every time a column width changes. The point is that the numbers are right and
nothing crashes on awkward input.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from agentos.cli.status_commands import (
    OBJECTIVE_COLOURS,
    objectives_panel,
    progress_bar,
    task_lines,
)
from agentos.schemas.dto import ObjectiveView, TaskView
from agentos.schemas.enums import ObjectiveStatus, TaskStatus


def task(key: str, status: TaskStatus, title: str = "work", **kwargs) -> TaskView:
    return TaskView(id=int(key.split("-")[-1]), key=key, title=title, status=status, **kwargs)


def render(renderable) -> str:
    """Render to plain text, as a narrow terminal would."""
    console = Console(width=100, no_color=True, record=True)
    console.print(renderable)
    return console.export_text()


# ------------------------------------------------------------------- progress


def test_progress_counts_completed() -> None:
    tasks = [
        task("T-1", TaskStatus.COMPLETED),
        task("T-2", TaskStatus.COMPLETED),
        task("T-3", TaskStatus.READY),
        task("T-4", TaskStatus.PENDING),
    ]
    assert "2/4 complete" in render(progress_bar(tasks))


def test_progress_reports_failures_and_blocks() -> None:
    tasks = [
        task("T-1", TaskStatus.COMPLETED),
        task("T-2", TaskStatus.FAILED),
        task("T-3", TaskStatus.BLOCKED),
    ]
    text = render(progress_bar(tasks))
    assert "1 failed" in text
    assert "1 blocked" in text


def test_progress_omits_zero_counts() -> None:
    text = render(progress_bar([task("T-1", TaskStatus.COMPLETED)]))
    assert "failed" not in text
    assert "blocked" not in text


def test_progress_on_empty_list_is_blank() -> None:
    assert render(progress_bar([])).strip() == ""


def test_progress_bar_is_full_when_all_complete() -> None:
    tasks = [task(f"T-{i}", TaskStatus.COMPLETED) for i in range(1, 4)]
    text = render(progress_bar(tasks))
    assert "3/3 complete" in text
    assert "." not in text.split("3/3")[0]


# ---------------------------------------------------------------- task lines


def test_task_lines_show_keys_and_titles() -> None:
    text = render(
        task_lines(
            [
                task("AUTH-1", TaskStatus.COMPLETED, "Analyze auth"),
                task("AUTH-2", TaskStatus.RUNNING, "Implement backend"),
            ]
        )
    )
    assert "AUTH-1" in text and "Analyze auth" in text
    assert "AUTH-2" in text and "Implement backend" in text


def test_task_lines_flag_intervention() -> None:
    text = render(
        task_lines([task("T-1", TaskStatus.BLOCKED, needs_intervention=True)])
    )
    assert "needs intervention" in text


def test_task_lines_truncate_with_a_count() -> None:
    tasks = [task(f"T-{i}", TaskStatus.PENDING) for i in range(1, 40)]
    text = render(task_lines(tasks, limit=5))
    assert "T-5" in text
    assert "T-6" not in text
    assert "and 34 more" in text


def test_task_lines_handle_no_tasks() -> None:
    assert "No tasks yet" in render(task_lines([]))


def test_task_lines_survive_awkward_titles() -> None:
    """Agent-written titles are arbitrary text and must not break rendering."""
    nasty = task("T-1", TaskStatus.READY, "emoji \U0001f600 and [markup] and 中文")
    text = render(task_lines([nasty]))
    assert "T-1" in text


# ---------------------------------------------------------------- objectives


def objective(oid: int, status: ObjectiveStatus, description: str = "Do it"):
    return ObjectiveView(id=oid, description=description, status=status)


def test_objectives_panel_prefers_active() -> None:
    panel = objectives_panel(
        [
            objective(1, ObjectiveStatus.COMPLETED, "old work"),
            objective(2, ObjectiveStatus.ACTIVE, "current work"),
        ]
    )
    text = render(panel)
    assert "current work" in text
    assert "old work" not in text


def test_objectives_panel_falls_back_to_recent_when_none_active() -> None:
    panel = objectives_panel([objective(1, ObjectiveStatus.COMPLETED, "finished work")])
    assert "finished work" in render(panel)


def test_objectives_panel_is_none_when_empty() -> None:
    assert objectives_panel([]) is None


@pytest.mark.parametrize("status", list(ObjectiveStatus))
def test_every_objective_status_has_a_colour(status: ObjectiveStatus) -> None:
    assert status in OBJECTIVE_COLOURS


@pytest.mark.parametrize("status", list(TaskStatus))
def test_every_task_status_renders(status: TaskStatus) -> None:
    """A new status must not blow up the dashboard."""
    text = render(task_lines([task("T-1", status)]))
    assert "T-1" in text

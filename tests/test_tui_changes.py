"""The dashboard's changes panel: what each agent changed, and where.

The git side runs against real repositories, like test_vcs, because the numbers
that matter (merge base, untracked files, commits on the branch) are git's own
behaviour. The rendering side is driven with fixed data.
"""

from __future__ import annotations

import asyncio
import shutil
from datetime import datetime

import pytest

from agentos.paths import ProjectPaths
from agentos.schemas.dto import AgentView, TaskView
from agentos.schemas.enums import AgentStatus, TaskStatus
from agentos.tui.changes import (
    ROOT_AREA,
    SHARED,
    AgentChanges,
    ChangesReader,
    agent_summary,
    area_of,
    areas_of,
    latest_task,
)
from agentos.tui.snapshot import Snapshot
from agentos.vcs.manager import FileDelta, parse_numstat
from tests.test_tui import project  # noqa: F401  (shared fixture)
from tests.test_vcs import init_repo

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


# --------------------------------------------------------------- pure helpers


def test_parse_numstat_reads_counts_and_binaries() -> None:
    deltas = parse_numstat("12\t3\tsrc/app.py\n-\t-\tlogo.png\n\n")
    assert deltas[0] == FileDelta("src/app.py", 12, 3)
    assert deltas[1].binary and deltas[1].churn == 0


def test_area_is_the_first_two_directories() -> None:
    assert area_of("src/lib/utils.ts") == "src/lib"
    assert area_of("src/lib/deep/nested/x.ts") == "src/lib"
    assert area_of("supabase/tests/a.test.ts") == "supabase/tests"
    assert area_of("src/main.ts") == "src"
    assert area_of("README.md") == ROOT_AREA
    assert area_of("src\\lib\\utils.ts") == "src/lib"


def test_areas_are_ranked_by_lines_touched() -> None:
    areas = areas_of(
        [
            FileDelta("src/lib/a.ts", 5, 0),
            FileDelta("src/lib/b.ts", 5, 5),
            FileDelta("supabase/tests/t.ts", 100, 0),
        ]
    )
    assert [a.path for a in areas] == ["supabase/tests", "src/lib"]
    assert (areas[1].files, areas[1].added, areas[1].removed) == (2, 10, 5)


def test_major_files_are_biggest_first() -> None:
    entry = AgentChanges(
        agent="backend",
        base="main",
        files=[FileDelta("a", 1, 0), FileDelta("b", 50, 50), FileDelta("c", 10, 0)],
    )
    assert [f.path for f in entry.major(2)] == ["b", "c"]
    assert (entry.added, entry.removed) == (61, 50)


def test_summary_drops_the_appended_reports() -> None:
    result = "Added the tests.\n\nBranch: agent/backend\nDiff: +1 -0"
    assert agent_summary(result) == "Added the tests."
    assert agent_summary("Nothing.\n\nNo file changes detected.") == "Nothing."
    assert agent_summary(None) == ""


def _task(id: int, agent: str, started: int | None) -> TaskView:
    return TaskView(
        id=id,
        key=f"T-{id}",
        title=f"task {id}",
        status=TaskStatus.COMPLETED,
        assigned_agent=agent,
        started_at=datetime(2026, 1, 1, started) if started is not None else None,
    )


def test_latest_task_prefers_the_current_one() -> None:
    snapshot = Snapshot(
        project="p",
        agents=[
            AgentView(
                id=1, name="backend", role="backend",
                status=AgentStatus.WORKING, current_task_id=1,
            )
        ],
        tasks=[_task(1, "backend", 1), _task(2, "backend", 5)],
    )
    assert latest_task(snapshot, "backend").key == "T-1"


def test_latest_task_falls_back_to_the_most_recent() -> None:
    snapshot = Snapshot(
        project="p",
        tasks=[_task(1, "backend", 1), _task(2, "backend", 5), _task(3, "backend", None)],
    )
    assert latest_task(snapshot, "backend").key == "T-2"
    assert latest_task(snapshot, "frontend") is None


# ------------------------------------------------------------- real git scans


@needs_git
async def test_reader_measures_a_worktree_against_the_base(tmp_path) -> None:
    root = tmp_path / "project"
    git = await init_repo(root)
    paths = ProjectPaths(root=root)
    tree = await git.create_worktree("backend")

    # A committed change, an uncommitted one and a brand-new file.
    (tree.path / "src").mkdir()
    (tree.path / "src" / "api.py").write_text("a\nb\nc\n", encoding="utf-8")
    await git._run("add", "--all", cwd=tree.path)
    await git._run("commit", "-m", "api", cwd=tree.path)
    (tree.path / "README.md").write_text("hello\nworld\n", encoding="utf-8")
    (tree.path / "notes.txt").write_text("one\ntwo", encoding="utf-8")

    # Work landing on the base afterwards is not the agent's.
    (root / "OTHER.md").write_text("x\n", encoding="utf-8")
    await git._run("add", "OTHER.md")
    await git._run("commit", "-m", "unrelated")

    changes = await asyncio.to_thread(ChangesReader(paths).read)
    assert [c.agent for c in changes] == ["backend"]
    entry = changes[0]
    by_path = {f.path: f for f in entry.files}
    assert set(by_path) == {"src/api.py", "README.md", "notes.txt"}
    assert by_path["src/api.py"].is_new and by_path["src/api.py"].added == 3
    assert not by_path["README.md"].is_new and by_path["README.md"].added == 1
    assert by_path["notes.txt"].is_new and by_path["notes.txt"].added == 2
    assert entry.commits_ahead == 1
    assert entry.base == "main"
    assert entry.branch == tree.branch


@needs_git
async def test_reader_skips_untouched_worktrees(tmp_path) -> None:
    root = tmp_path / "project"
    git = await init_repo(root)
    await git.create_worktree("qa")
    assert await asyncio.to_thread(ChangesReader(ProjectPaths(root=root)).read) == []


@needs_git
async def test_reader_reports_the_shared_directory(tmp_path) -> None:
    """Agents without a worktree edit the project itself; that must show too."""
    from agentos.config import Config

    root = tmp_path / "project"
    await init_repo(root)
    (root / "README.md").write_text("hello\nagain\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "new.py").write_text("x = 1\n", encoding="utf-8")
    config = Config.model_validate(
        {
            "project": {"name": "p"},
            "agents": {
                "backend": {"role": "backend"},
                "frontend": {"role": "frontend", "worktree": True},
            },
        }
    )

    changes = await asyncio.to_thread(
        ChangesReader(ProjectPaths(root=root), config).read
    )
    assert [c.agent for c in changes] == [SHARED]
    entry = changes[0]
    assert entry.shared and entry.agents == ["backend"]
    assert {f.path for f in entry.files} == {"README.md", "src/new.py"}


def test_reader_is_empty_without_worktrees(tmp_path) -> None:
    assert ChangesReader(ProjectPaths(root=tmp_path)).read() == []


# ------------------------------------------------------------------- the app


class FixedChanges:
    def __init__(self, changes: list[AgentChanges]) -> None:
        self.changes = changes

    def read(self) -> list[AgentChanges]:
        return self.changes


SAMPLE = [
    AgentChanges(
        agent="backend",
        base="main",
        branch="agent/backend",
        files=[
            FileDelta("supabase/tests/auth-profiles.test.ts", 180, 0, is_new=True),
            FileDelta("src/lib/utils.ts", 4, 2),
        ],
        commits_ahead=1,
    )
]


async def test_panel_lists_agents_with_changes(project) -> None:
    from agentos.tui.app import ChangesPanel, DashboardApp

    reader, _tasks, _db = project
    app = DashboardApp(reader, changes=FixedChanges(SAMPLE))
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one("#changes", ChangesPanel).row_count == 1


async def test_selecting_changes_shows_where_and_summary(project) -> None:
    from agentos.tui.app import DashboardApp, OutputPanel, panel_text

    reader, _tasks, _db = project
    app = DashboardApp(reader, changes=FixedChanges(SAMPLE))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.apply_changes(SAMPLE)
        app.select_changes("backend")
        rendered = panel_text(app.query_one("#output", OutputPanel))
        assert "supabase/tests" in rendered
        assert "auth-profiles.test.ts" in rendered
        # The fixture's backend task reported "done the thing".
        assert "done the thing" in rendered
        assert app._selected_agent is None and app._selected_task is None

        app.select_task("API-1")
        assert app._selected_changes is None


async def test_shared_changes_name_every_candidate(project) -> None:
    from agentos.tui.app import DashboardApp, OutputPanel, panel_text

    shared = [
        AgentChanges(
            agent=SHARED,
            base="HEAD",
            branch="main",
            files=[FileDelta("src/lib/utils.ts", 4, 2)],
            agents=["backend", "qa"],
        )
    ]
    reader, _tasks, _db = project
    app = DashboardApp(reader, changes=FixedChanges(shared))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.apply_changes(shared)
        app.select_changes(SHARED)
        rendered = panel_text(app.query_one("#output", OutputPanel))
        assert "project directory" in rendered
        assert "API-1" in rendered and "API-2" in rendered
        assert "git diff" in rendered


async def test_a_failing_scan_does_not_crash_the_ui(project) -> None:
    from agentos.tui.app import ChangesPanel, DashboardApp

    class Broken:
        def read(self):
            raise RuntimeError("git went away")

    reader, _tasks, _db = project
    app = DashboardApp(reader, changes=Broken())
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.is_running
        assert "scan failed" in str(app.query_one("#changes", ChangesPanel).border_subtitle)

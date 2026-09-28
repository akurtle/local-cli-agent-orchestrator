"""Phase 10: integration of agent branches.

The property that matters most: a conflict is reported, never resolved by
guessing, and nothing an agent wrote is discarded.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agentos.config import Config
from agentos.paths import ProjectPaths
from agentos.schemas.enums import TaskStatus
from agentos.services.integration import (
    BranchState,
    IntegrationService,
    compute_overlaps,
)
from agentos.services.tasks import TaskService
from tests.test_vcs import init_repo

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)

CONFIG = {
    "agents": {
        "backend": {"role": "backend", "worktree": True},
        "frontend": {"role": "frontend", "worktree": True},
        "qa": {"role": "qa", "worktree": True},
    }
}


def state(agent: str, files: list[str], commits: int = 1, clean: bool = True):
    return BranchState(
        agent=agent,
        branch=f"agent/{agent}",
        exists=True,
        commits=commits,
        files=files,
        merges_cleanly=clean,
    )


@pytest.fixture
async def project(tmp_path: Path):
    """A repo with an agent worktree helper and an IntegrationService."""
    root = tmp_path / "project"
    git = await init_repo(root)
    paths = ProjectPaths(root=root)
    paths.ensure()

    from agentos.db.session import Database
    from agentos.services.agents import AgentService
    from tests.test_agents import StubRuntime

    db = Database(paths.db_file)
    db.create_all()
    config = Config.model_validate(CONFIG)
    AgentService(db, config, StubRuntime(), root).sync_from_config()
    tasks = TaskService(db, config)
    service = IntegrationService(db, config, paths, tasks, git)
    yield root, git, service, tasks
    db.dispose()


async def agent_commit(git, agent: str, path: str, content: str) -> None:
    """Make a real commit on an agent branch, as an agent would."""
    worktree = await git.create_worktree(agent)
    target = worktree.path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    await git.commit(f"{agent}: edit {path}", cwd=worktree.path)


# ------------------------------------------------------------- pure overlaps


def test_overlaps_detects_shared_files() -> None:
    overlaps = compute_overlaps(
        [
            state("backend", ["shared.py", "only-backend.py"]),
            state("frontend", ["shared.py", "only-frontend.py"]),
        ]
    )
    assert [o.path for o in overlaps] == ["shared.py"]
    assert overlaps[0].agents == ["backend", "frontend"]


def test_no_overlap_when_files_are_disjoint() -> None:
    assert compute_overlaps(
        [state("backend", ["a.py"]), state("frontend", ["b.py"])]
    ) == []


def test_overlaps_ignore_branches_without_work() -> None:
    overlaps = compute_overlaps(
        [
            state("backend", ["shared.py"]),
            state("frontend", ["shared.py"], commits=0),
        ]
    )
    assert overlaps == []


def test_overlap_across_three_agents() -> None:
    overlaps = compute_overlaps(
        [
            state("backend", ["x.py"]),
            state("frontend", ["x.py"]),
            state("qa", ["x.py"]),
        ]
    )
    assert overlaps[0].agents == ["backend", "frontend", "qa"]


def test_overlap_description() -> None:
    overlaps = compute_overlaps(
        [state("a", ["f.py"]), state("b", ["f.py"])]
    )
    assert overlaps[0].describe() == "f.py (a, b)"


# ------------------------------------------------------------------ planning


async def test_plan_reports_no_branches_initially(project) -> None:
    _root, _git, service, _tasks = project
    plan = await service.plan()
    assert all(not b.exists for b in plan.branches)
    assert not plan.has_anything_to_do


async def test_plan_sees_committed_work(project) -> None:
    _root, git, service, _tasks = project
    await agent_commit(git, "backend", "server.py", "print('x')\n")

    plan = await service.plan(base="main")
    backend = next(b for b in plan.branches if b.agent == "backend")
    assert backend.has_work
    assert backend.commits == 1
    assert "server.py" in backend.files
    assert backend.merges_cleanly


async def test_plan_ignores_a_branch_with_no_commits(project) -> None:
    _root, git, service, _tasks = project
    await git.create_worktree("backend")  # branch exists, nothing committed
    plan = await service.plan(base="main")
    backend = next(b for b in plan.branches if b.agent == "backend")
    assert backend.exists
    assert not backend.has_work
    assert backend.agent in [b.agent for b in plan.empty]


async def test_plan_detects_a_conflict_without_changing_anything(project) -> None:
    """Conflict detection must be side-effect free."""
    root, git, service, _tasks = project
    await agent_commit(git, "backend", "README.md", "backend version\n")
    (root / "README.md").write_text("main version\n", encoding="utf-8")
    await git.commit("main edit")

    plan = await service.plan(base="main")
    backend = next(b for b in plan.branches if b.agent == "backend")
    assert backend.has_work
    assert not backend.merges_cleanly
    assert not plan.is_clean
    # Untouched.
    assert (root / "README.md").read_text(encoding="utf-8") == "main version\n"
    assert await git.current_branch() == "main"


async def test_plan_flags_overlapping_files(project) -> None:
    _root, git, service, _tasks = project
    await agent_commit(git, "backend", "shared.py", "backend\n")
    await agent_commit(git, "frontend", "shared.py", "frontend\n")

    plan = await service.plan(base="main")
    assert any(o.path == "shared.py" for o in plan.overlaps)


# --------------------------------------------------------------- integrating


async def test_integrate_merges_independent_branches(project) -> None:
    root, git, service, _tasks = project
    await agent_commit(git, "backend", "server.py", "server\n")
    await agent_commit(git, "frontend", "client.js", "client\n")

    plan = await service.plan(base="main")
    result = await service.integrate(plan)

    assert set(result.merged) == {"backend", "frontend"}
    assert result.ok
    assert await git.current_branch() == "integration/current"
    assert (root / "server.py").is_file()
    assert (root / "client.js").is_file()


async def test_integration_happens_on_a_separate_branch(project) -> None:
    """The operator's branch must not be modified."""
    root, git, service, _tasks = project
    await agent_commit(git, "backend", "server.py", "server\n")

    plan = await service.plan(base="main")
    await service.integrate(plan)

    assert await git.current_branch() == "integration/current"
    await git._run("checkout", "main")
    assert not (root / "server.py").exists()


async def test_conflicting_branch_is_not_merged(project) -> None:
    root, git, service, _tasks = project
    await agent_commit(git, "backend", "README.md", "backend version\n")
    (root / "README.md").write_text("main version\n", encoding="utf-8")
    await git.commit("main edit")

    plan = await service.plan(base="main")
    result = await service.integrate(plan)

    assert result.merged == []
    assert result.failed == ["backend"]
    assert not result.ok
    # The agent's branch still holds its work: nothing was discarded.
    log = await git.log(limit=20)
    branch_log = await git._run("log", "--oneline", "agent/backend", check=False)
    assert "backend: edit README.md" in branch_log.stdout


async def test_clean_branches_merge_even_when_another_conflicts(project) -> None:
    """One bad branch must not block the rest."""
    root, git, service, _tasks = project
    await agent_commit(git, "backend", "README.md", "backend version\n")
    await agent_commit(git, "frontend", "client.js", "client\n")
    (root / "README.md").write_text("main version\n", encoding="utf-8")
    await git.commit("main edit")

    plan = await service.plan(base="main")
    result = await service.integrate(plan)

    assert result.merged == ["frontend"]
    assert result.failed == ["backend"]
    assert (root / "client.js").is_file()


async def test_conflict_creates_a_resolution_task(project) -> None:
    root, git, service, tasks = project
    await agent_commit(git, "backend", "README.md", "backend version\n")
    (root / "README.md").write_text("main version\n", encoding="utf-8")
    await git.commit("main edit")

    plan = await service.plan(base="main")
    result = await service.integrate(plan)

    assert len(result.resolution_tasks) == 1
    task = tasks.get_task(result.resolution_tasks[0])
    assert task.assigned_agent == "backend"
    assert "merge conflict" in task.title.lower()
    assert task.acceptance_criteria
    assert task.status is TaskStatus.READY


async def test_resolution_task_can_be_assigned_to_a_resolver(project) -> None:
    root, git, service, tasks = project
    await agent_commit(git, "backend", "README.md", "backend version\n")
    (root / "README.md").write_text("main version\n", encoding="utf-8")
    await git.commit("main edit")

    plan = await service.plan(base="main")
    result = await service.integrate(plan, resolver="qa")
    assert tasks.get_task(result.resolution_tasks[0]).assigned_agent == "qa"


async def test_resolution_tasks_can_be_suppressed(project) -> None:
    root, git, service, tasks = project
    await agent_commit(git, "backend", "README.md", "backend version\n")
    (root / "README.md").write_text("main version\n", encoding="utf-8")
    await git.commit("main edit")

    plan = await service.plan(base="main")
    result = await service.integrate(plan, create_resolution_tasks=False)
    assert result.resolution_tasks == []
    assert tasks.tasks.count() == 0


async def test_integrating_nothing_is_a_no_op(project) -> None:
    _root, git, service, _tasks = project
    plan = await service.plan(base="main")
    result = await service.integrate(plan)
    assert result.merged == []
    assert result.integration_branch is None
    # No integration branch was created for nothing.
    assert not await git.branch_exists("integration/current")


async def test_rerunning_integration_starts_from_the_base(project) -> None:
    """A second attempt must not stack on a stale half-finished one."""
    _root, git, service, _tasks = project
    await agent_commit(git, "backend", "server.py", "server\n")

    plan = await service.plan(base="main")
    await service.integrate(plan)
    first = await git._run("rev-list", "--count", "main..integration/current")

    await git._run("checkout", "main")
    plan2 = await service.plan(base="main")
    await service.integrate(plan2)
    second = await git._run("rev-list", "--count", "main..integration/current")

    assert first.text == second.text


async def test_objective_scoped_branch_name(project) -> None:
    _root, _git, service, _tasks = project
    assert service.integration_branch_name(7) == "integration/objective-7"
    assert service.integration_branch_name() == "integration/current"


async def test_sequential_conflict_gets_a_resolution_task(project) -> None:
    """Two branches can each merge cleanly against the base yet conflict.

    Regression: the second branch landed in `failed` with no resolution task,
    because the conflict only appeared after the first branch was merged.
    """
    _root, git, service, tasks = project
    await agent_commit(git, "backend", "shared.txt", "line one\nbackend\n")
    await agent_commit(git, "frontend", "shared.txt", "line one\nfrontend\n")

    plan = await service.plan(base="main")
    # Both look fine on their own.
    assert {b.agent for b in plan.mergeable} == {"backend", "frontend"}
    assert plan.sequential_risk

    result = await service.integrate(plan)
    assert result.merged == ["backend"]
    assert result.failed == ["frontend"]
    assert len(result.resolution_tasks) == 1

    task = tasks.get_task(result.resolution_tasks[0])
    assert task.assigned_agent == "frontend"
    assert "conflict" in task.title.lower()


async def test_sequential_conflict_leaves_a_clean_tree(project) -> None:
    root, git, service, _tasks = project
    await agent_commit(git, "backend", "shared.txt", "line one\nbackend\n")
    await agent_commit(git, "frontend", "shared.txt", "line one\nfrontend\n")

    plan = await service.plan(base="main")
    await service.integrate(plan)

    status = await git.status(root)
    # No conflict markers, no half-merged state.
    tracked = [c for c in status.changes if not c.is_untracked]
    assert tracked == []
    assert "backend" in (root / "shared.txt").read_text(encoding="utf-8")


async def test_no_sequential_risk_without_overlap(project) -> None:
    _root, git, service, _tasks = project
    await agent_commit(git, "backend", "server.py", "server\n")
    await agent_commit(git, "frontend", "client.js", "client\n")
    plan = await service.plan(base="main")
    assert not plan.sequential_risk

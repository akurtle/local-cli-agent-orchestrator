"""Phase 6: agent workspaces, and verifying what an agent claims against git."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agentos.config import Config
from agentos.paths import ProjectPaths
from agentos.schemas.dto import AgentView
from agentos.services.workspaces import WorkspaceService
from agentos.vcs.manager import GitManager
from tests.test_vcs import init_repo

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


def make_config(isolated: set[str]) -> Config:
    return Config.model_validate(
        {
            "agents": {
                "backend": {"role": "backend", "worktree": "backend" in isolated},
                "frontend": {"role": "frontend", "worktree": "frontend" in isolated},
                "manager": {"role": "manager", "worktree": "manager" in isolated},
            }
        }
    )


def agent(name: str, role: str = "backend") -> AgentView:
    return AgentView(id=1, name=name, role=role)


@pytest.fixture
async def project(tmp_path: Path):
    """A real repo plus a WorkspaceService pointed at it."""
    root = tmp_path / "project"
    git = await init_repo(root)
    paths = ProjectPaths(root=root)
    return root, git, paths


# ------------------------------------------------------------------- preparing


async def test_isolated_agent_gets_a_worktree(project) -> None:
    root, git, paths = project
    service = WorkspaceService(make_config({"backend"}), paths, git)

    workspace = await service.prepare(agent("backend"))
    assert workspace.isolated
    assert workspace.branch == "agent/backend"
    assert workspace.path != root
    assert workspace.path.is_dir()
    assert workspace.warning is None


async def test_non_isolated_agent_uses_the_project_directory(project) -> None:
    root, git, paths = project
    service = WorkspaceService(make_config(set()), paths, git)

    workspace = await service.prepare(agent("backend"))
    assert not workspace.isolated
    assert workspace.path == root
    assert workspace.branch is None


async def test_two_isolated_agents_get_separate_trees(project) -> None:
    _root, git, paths = project
    service = WorkspaceService(make_config({"backend", "frontend"}), paths, git)

    backend = await service.prepare(agent("backend"))
    frontend = await service.prepare(agent("frontend", "frontend"))
    assert backend.path != frontend.path
    assert backend.branch != frontend.branch


async def test_prepare_is_idempotent(project) -> None:
    _root, git, paths = project
    service = WorkspaceService(make_config({"backend"}), paths, git)
    first = await service.prepare(agent("backend"))
    second = await service.prepare(agent("backend"))
    assert first.path == second.path


async def test_missing_repository_falls_back_with_a_warning(tmp_path: Path) -> None:
    """A task must not stall because the project is not a git repo."""
    root = tmp_path / "plain"
    root.mkdir()
    paths = ProjectPaths(root=root)
    service = WorkspaceService(
        make_config({"backend"}), paths, GitManager(root=root)
    )

    workspace = await service.prepare(agent("backend"))
    assert not workspace.isolated
    assert workspace.path == root
    assert workspace.warning and "worktree unavailable" in workspace.warning


# ------------------------------------------------------------------- capturing


async def test_capture_reports_actual_changes(project) -> None:
    _root, git, paths = project
    service = WorkspaceService(make_config({"backend"}), paths, git)
    workspace = await service.prepare(agent("backend"))

    (workspace.path / "server.py").write_text("print('x')\n", encoding="utf-8")
    report = await service.capture(workspace, claimed_files=["server.py"])

    assert report.files_changed == ["server.py"]
    assert report.branch == "agent/backend"
    assert report.unverified_claims == []
    assert not report.is_empty


async def test_capture_on_a_clean_tree_is_empty(project) -> None:
    _root, git, paths = project
    service = WorkspaceService(make_config({"backend"}), paths, git)
    workspace = await service.prepare(agent("backend"))
    report = await service.capture(workspace)
    assert report.is_empty
    assert "No file changes" in report.render()


async def test_unverified_claim_is_flagged(project) -> None:
    """An agent claiming a file it did not touch must be caught, not trusted."""
    _root, git, paths = project
    service = WorkspaceService(make_config({"backend"}), paths, git)
    workspace = await service.prepare(agent("backend"))

    (workspace.path / "real.py").write_text("x\n", encoding="utf-8")
    report = await service.capture(
        workspace, claimed_files=["real.py", "imaginary.py"]
    )

    assert "real.py" in report.files_changed
    assert report.unverified_claims == ["imaginary.py"]
    assert "imaginary.py" in report.render()


async def test_claim_with_windows_separators_still_matches(project) -> None:
    _root, git, paths = project
    service = WorkspaceService(make_config({"backend"}), paths, git)
    workspace = await service.prepare(agent("backend"))

    nested = workspace.path / "src"
    nested.mkdir()
    (nested / "app.py").write_text("x\n", encoding="utf-8")

    report = await service.capture(workspace, claimed_files=["src\\app.py"])
    assert report.unverified_claims == []


async def test_capture_can_commit(project) -> None:
    _root, git, paths = project
    service = WorkspaceService(make_config({"backend"}), paths, git)
    workspace = await service.prepare(agent("backend"))

    (workspace.path / "feature.py").write_text("x\n", encoding="utf-8")
    report = await service.capture(workspace, commit_message="agent: add feature")

    assert report.committed_sha
    assert (await git.status(workspace.path)).is_clean
    assert "Committed:" in report.render()


async def test_capture_does_not_commit_by_default(project) -> None:
    """The MVP leaves work uncommitted for the operator to inspect."""
    _root, git, paths = project
    service = WorkspaceService(make_config({"backend"}), paths, git)
    workspace = await service.prepare(agent("backend"))

    (workspace.path / "feature.py").write_text("x\n", encoding="utf-8")
    report = await service.capture(workspace)

    assert report.committed_sha is None
    assert not (await git.status(workspace.path)).is_clean


async def test_capture_without_a_repository_marks_claims_unverified(
    tmp_path: Path,
) -> None:
    root = tmp_path / "plain"
    root.mkdir()
    paths = ProjectPaths(root=root)
    service = WorkspaceService(
        make_config({"backend"}), paths, GitManager(root=root)
    )
    workspace = await service.prepare(agent("backend"))
    report = await service.capture(workspace, claimed_files=["something.py"])
    assert report.unverified_claims == ["something.py"]


async def test_report_render_includes_diff_summary(project) -> None:
    _root, git, paths = project
    service = WorkspaceService(make_config({"backend"}), paths, git)
    workspace = await service.prepare(agent("backend"))
    (workspace.path / "README.md").write_text("changed\n", encoding="utf-8")
    report = await service.capture(workspace)
    rendered = report.render()
    assert "Branch: agent/backend" in rendered
    assert "Diff: +" in rendered


async def test_work_in_one_worktree_is_invisible_to_another(project) -> None:
    """The isolation guarantee, end to end through the service."""
    root, git, paths = project
    service = WorkspaceService(make_config({"backend", "frontend"}), paths, git)

    backend = await service.prepare(agent("backend"))
    frontend = await service.prepare(agent("frontend", "frontend"))

    (backend.path / "only-backend.py").write_text("x\n", encoding="utf-8")

    backend_report = await service.capture(backend)
    frontend_report = await service.capture(frontend)

    assert backend_report.files_changed == ["only-backend.py"]
    assert frontend_report.is_empty
    assert not (root / "only-backend.py").exists()


async def test_shared_directory_changes_are_not_attributed(project, tmp_path) -> None:
    """Regression: a non-isolated agent was credited with unrelated changes.

    In a shared checkout, the orchestrator's own state directory and any other
    agent's edits appear as changes. Attributing them to one task is wrong, so
    the scheduler only captures for isolated worktrees.
    """
    root, git, paths = project
    service = WorkspaceService(make_config(set()), paths, git)

    # Noise that is not this agent's work at all.
    (root / ".agentos").mkdir(exist_ok=True)
    (root / ".agentos" / "state.db").write_text("x", encoding="utf-8")
    (root / "someone-elses-file.py").write_text("x\n", encoding="utf-8")

    workspace = await service.prepare(agent("backend"))
    assert not workspace.isolated

    # capture() would happily report all of it, which is exactly why the
    # scheduler must not call it for a shared directory.
    report = await service.capture(workspace)
    assert len(report.files_changed) >= 2


def test_init_gitignores_our_artifacts(tmp_path: Path) -> None:
    """Otherwise .agentos/ and worktrees/ show as untracked forever."""
    from agentos.cli.main import _ignore_our_artifacts

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    assert _ignore_our_artifacts(root)

    content = (root / ".gitignore").read_text(encoding="utf-8")
    assert "/.agentos/" in content
    assert "/worktrees/" in content

    # Idempotent: a second call adds nothing.
    assert _ignore_our_artifacts(root) is False
    assert (root / ".gitignore").read_text(encoding="utf-8") == content


def test_init_preserves_an_existing_gitignore(tmp_path: Path) -> None:
    from agentos.cli.main import _ignore_our_artifacts

    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    (root / ".gitignore").write_text("*.pyc\n__pycache__/\n", encoding="utf-8")

    assert _ignore_our_artifacts(root)
    content = (root / ".gitignore").read_text(encoding="utf-8")
    assert "*.pyc" in content
    assert "__pycache__/" in content
    assert "/worktrees/" in content


def test_init_leaves_non_git_directories_alone(tmp_path: Path) -> None:
    from agentos.cli.main import _ignore_our_artifacts

    root = tmp_path / "plain"
    root.mkdir()
    assert _ignore_our_artifacts(root) is False
    assert not (root / ".gitignore").exists()

"""Phase 6: git worktree isolation, against real temporary repositories.

Real repos rather than mocks: the failures worth catching here are git's own
rules (an indexed branch, a leftover directory, a repo with no commits), which a
mock would simply not have.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agentos.vcs.manager import (
    GitError,
    GitManager,
    NotARepository,
    branch_for,
    parse_porcelain,
    parse_shortstat,
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


async def init_repo(path: Path, commit: bool = True) -> GitManager:
    """Create a throwaway repository with deterministic identity."""
    path.mkdir(parents=True, exist_ok=True)
    manager = GitManager(root=path, worktrees_dir=path / "worktrees")
    await manager._run("init", "--initial-branch=main")
    await manager._run("config", "user.email", "test@example.com")
    await manager._run("config", "user.name", "Test")
    if commit:
        (path / "README.md").write_text("hello\n", encoding="utf-8")
        await manager._run("add", "README.md")
        await manager._run("commit", "-m", "initial commit")
    return manager


@pytest.fixture
async def repo(tmp_path: Path) -> GitManager:
    return await init_repo(tmp_path / "project")


# --------------------------------------------------------------- pure parsing


def test_parse_porcelain_handles_common_states() -> None:
    output = " M src/app.py\n?? new.txt\nA  added.py\n"
    changes = parse_porcelain(output)
    assert [c.path for c in changes] == ["src/app.py", "new.txt", "added.py"]
    assert changes[1].is_untracked
    assert not changes[0].is_untracked


def test_parse_porcelain_reports_new_path_for_renames() -> None:
    changes = parse_porcelain("R  old.py -> new.py\n")
    assert changes[0].path == "new.py"


def test_parse_porcelain_unquotes_paths() -> None:
    changes = parse_porcelain('?? "with space.txt"\n')
    assert changes[0].path == "with space.txt"


def test_parse_porcelain_ignores_junk() -> None:
    assert parse_porcelain("") == []
    assert parse_porcelain("\n\nx\n") == []


def test_parse_shortstat() -> None:
    assert parse_shortstat(" 2 files changed, 143 insertions(+), 18 deletions(-)") == (
        143,
        18,
    )
    assert parse_shortstat(" 1 file changed, 5 insertions(+)") == (5, 0)
    assert parse_shortstat(" 1 file changed, 3 deletions(-)") == (0, 3)
    assert parse_shortstat("") == (0, 0)


def test_branch_names_are_sanitised() -> None:
    assert branch_for("backend") == "agent/backend"
    assert branch_for("my agent") == "agent/my-agent"
    # Shell metacharacters become dashes, and git refnames may not end in one.
    assert branch_for("a;rm -rf /") == "agent/a-rm--rf"
    assert branch_for("   ") == "agent/agent"
    assert branch_for("..") == "agent/agent"


# ------------------------------------------------------------------ repository


async def test_detects_a_repository(repo: GitManager) -> None:
    assert await repo.is_repository()
    assert await repo.has_commits()
    await repo.ensure_repository()


async def test_non_repository_is_reported(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    manager = GitManager(root=plain)
    assert not await manager.is_repository()
    with pytest.raises(NotARepository):
        await manager.ensure_repository()


async def test_repository_without_commits_is_reported(tmp_path: Path) -> None:
    """A worktree cannot be created from a repo with no HEAD."""
    manager = await init_repo(tmp_path / "empty", commit=False)
    assert await manager.is_repository()
    assert not await manager.has_commits()
    with pytest.raises(GitError, match="no commits"):
        await manager.ensure_repository()


async def test_current_branch(repo: GitManager) -> None:
    assert await repo.current_branch() == "main"


async def test_failed_command_raises_with_detail(repo: GitManager) -> None:
    with pytest.raises(GitError) as excinfo:
        await repo._run("checkout", "no-such-branch")
    assert excinfo.value.exit_code != 0
    assert "no-such-branch" in str(excinfo.value)


# ------------------------------------------------------------------- worktrees


async def test_create_worktree(repo: GitManager) -> None:
    worktree = await repo.create_worktree("backend")
    assert worktree.created
    assert worktree.branch == "agent/backend"
    assert worktree.path.is_dir()
    assert (worktree.path / "README.md").is_file()
    assert await repo.current_branch(worktree.path) == "agent/backend"


async def test_worktrees_are_isolated(repo: GitManager) -> None:
    """The whole point: one agent's edits must not appear in another's tree."""
    backend = await repo.create_worktree("backend")
    frontend = await repo.create_worktree("frontend")

    (backend.path / "server.py").write_text("print('server')\n", encoding="utf-8")

    assert (backend.path / "server.py").is_file()
    assert not (frontend.path / "server.py").exists()
    assert not (repo.root / "server.py").exists()


async def test_create_worktree_is_idempotent(repo: GitManager) -> None:
    """It runs before every task, so a second call must reuse, not fail."""
    first = await repo.create_worktree("backend")
    second = await repo.create_worktree("backend")
    assert second.created is False
    assert second.path == first.path
    assert second.branch == first.branch


async def test_reattaches_to_an_existing_branch(repo: GitManager) -> None:
    """An agent's history must survive its worktree being removed."""
    worktree = await repo.create_worktree("backend")
    (worktree.path / "work.py").write_text("x = 1\n", encoding="utf-8")
    sha = await repo.commit("agent work", cwd=worktree.path)
    assert sha

    await repo.remove_worktree("backend")
    recreated = await repo.create_worktree("backend")

    assert recreated.branch == "agent/backend"
    assert (recreated.path / "work.py").is_file()
    assert any("agent work" in line for line in await repo.log(recreated.path))


async def test_distinct_branches_per_agent(repo: GitManager) -> None:
    await repo.create_worktree("backend")
    await repo.create_worktree("frontend")
    branches = set((await repo.list_worktrees()).values())
    assert {"agent/backend", "agent/frontend"} <= branches


async def test_leftover_empty_directory_is_reclaimed(repo: GitManager) -> None:
    """A stale empty dir would otherwise make `worktree add` fail."""
    stale = repo.worktrees_dir / "backend"
    stale.mkdir(parents=True)
    worktree = await repo.create_worktree("backend")
    assert worktree.created
    assert worktree.path.is_dir()


async def test_remove_worktree(repo: GitManager) -> None:
    worktree = await repo.create_worktree("backend")
    assert await repo.remove_worktree("backend")
    assert not worktree.path.exists()
    # Removing again is a no-op, not an error.
    assert await repo.remove_worktree("backend") is False


async def test_remove_keeps_the_branch_by_default(repo: GitManager) -> None:
    """The branch holds the work; discarding it silently would destroy it."""
    await repo.create_worktree("backend")
    await repo.remove_worktree("backend")
    assert await repo.branch_exists("agent/backend")


async def test_remove_can_delete_the_branch_explicitly(repo: GitManager) -> None:
    await repo.create_worktree("backend")
    await repo.remove_worktree("backend", delete_branch=True)
    assert not await repo.branch_exists("agent/backend")


async def test_remove_dirty_worktree_needs_force(repo: GitManager) -> None:
    worktree = await repo.create_worktree("backend")
    (worktree.path / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    with pytest.raises(GitError):
        await repo.remove_worktree("backend")
    assert await repo.remove_worktree("backend", force=True)


# ---------------------------------------------------------------------- status


async def test_status_of_a_clean_tree(repo: GitManager) -> None:
    worktree = await repo.create_worktree("backend")
    status = await repo.status(worktree.path)
    assert status.is_clean
    assert status.branch == "agent/backend"
    assert status.diff_summary == "+0 -0"


async def test_status_reports_untracked_and_modified(repo: GitManager) -> None:
    worktree = await repo.create_worktree("backend")
    (worktree.path / "brand-new.py").write_text("new\n", encoding="utf-8")
    (worktree.path / "README.md").write_text("hello\nchanged\n", encoding="utf-8")

    status = await repo.status(worktree.path)
    assert not status.is_clean
    assert set(status.paths) == {"brand-new.py", "README.md"}
    assert status.insertions >= 1


async def test_status_counts_are_reported(repo: GitManager) -> None:
    worktree = await repo.create_worktree("backend")
    (worktree.path / "README.md").write_text("a\nb\nc\nd\n", encoding="utf-8")
    status = await repo.status(worktree.path)
    assert status.insertions > 0
    assert "+" in status.diff_summary


async def test_diff_shows_content(repo: GitManager) -> None:
    worktree = await repo.create_worktree("backend")
    (worktree.path / "README.md").write_text("hello\nsecond line\n", encoding="utf-8")
    diff = await repo.diff(worktree.path)
    assert "second line" in diff
    assert "README.md" in diff


async def test_diff_name_only(repo: GitManager) -> None:
    worktree = await repo.create_worktree("backend")
    (worktree.path / "README.md").write_text("changed\n", encoding="utf-8")
    assert (await repo.diff(worktree.path, name_only=True)).strip() == "README.md"


async def test_agent_diff_requires_a_worktree(repo: GitManager) -> None:
    with pytest.raises(GitError, match="no worktree"):
        await repo.agent_diff("nobody")


async def test_agent_diff_by_name(repo: GitManager) -> None:
    worktree = await repo.create_worktree("backend")
    (worktree.path / "README.md").write_text("edited\n", encoding="utf-8")
    assert "edited" in await repo.agent_diff("backend")


# --------------------------------------------------------------------- commits


async def test_commit_creates_a_sha(repo: GitManager) -> None:
    worktree = await repo.create_worktree("backend")
    (worktree.path / "feature.py").write_text("def f(): pass\n", encoding="utf-8")
    sha = await repo.commit("add feature", cwd=worktree.path)
    assert sha and len(sha) >= 7
    assert (await repo.status(worktree.path)).is_clean


async def test_commit_with_nothing_to_do_returns_none(repo: GitManager) -> None:
    worktree = await repo.create_worktree("backend")
    assert await repo.commit("empty", cwd=worktree.path) is None


async def test_commit_message_is_not_shell_interpreted(repo: GitManager) -> None:
    """A message is one argv element, so it cannot become a second command."""
    worktree = await repo.create_worktree("backend")
    (worktree.path / "x.txt").write_text("x\n", encoding="utf-8")
    nasty = 'fix; rm -rf / && echo "$(whoami)" `id`'
    assert await repo.commit(nasty, cwd=worktree.path)

    log = await repo.log(worktree.path, limit=1)
    assert nasty in log[0]
    # Nothing was executed: the tree still has the file it should.
    assert (worktree.path / "x.txt").is_file()
    assert (worktree.path / "README.md").is_file()


async def test_commit_in_one_worktree_does_not_touch_another(repo: GitManager) -> None:
    backend = await repo.create_worktree("backend")
    frontend = await repo.create_worktree("frontend")
    (backend.path / "only-backend.py").write_text("x\n", encoding="utf-8")
    await repo.commit("backend work", cwd=backend.path)

    assert not (frontend.path / "only-backend.py").exists()
    assert (await repo.status(frontend.path)).is_clean


async def test_log_returns_oneline_entries(repo: GitManager) -> None:
    entries = await repo.log(limit=5)
    assert entries
    assert "initial commit" in entries[0]


# ----------------------------------------------------------------- integration


async def test_clean_merge_is_detected(repo: GitManager) -> None:
    worktree = await repo.create_worktree("backend")
    (worktree.path / "new-file.py").write_text("x = 1\n", encoding="utf-8")
    await repo.commit("independent change", cwd=worktree.path)
    assert await repo.can_merge_cleanly("agent/backend", into="main")


async def test_conflicting_merge_is_detected_without_touching_the_tree(
    repo: GitManager,
) -> None:
    """Conflict detection must not modify anything."""
    worktree = await repo.create_worktree("backend")
    (worktree.path / "README.md").write_text("agent version\n", encoding="utf-8")
    await repo.commit("agent edit", cwd=worktree.path)

    (repo.root / "README.md").write_text("main version\n", encoding="utf-8")
    await repo.commit("main edit")

    assert not await repo.can_merge_cleanly("agent/backend", into="main")
    # The main checkout is untouched by the check.
    assert (repo.root / "README.md").read_text(encoding="utf-8") == "main version\n"


async def test_merge_returns_conflict_instead_of_raising(repo: GitManager) -> None:
    worktree = await repo.create_worktree("backend")
    (worktree.path / "README.md").write_text("agent\n", encoding="utf-8")
    await repo.commit("agent edit", cwd=worktree.path)
    (repo.root / "README.md").write_text("main\n", encoding="utf-8")
    await repo.commit("main edit")

    result = await repo.merge("agent/backend")
    assert not result.ok  # reported, not raised
    await repo._run("merge", "--abort", check=False)


async def test_merge_base_exists(repo: GitManager) -> None:
    await repo.create_worktree("backend")
    assert await repo.merge_base("agent/backend", "main")

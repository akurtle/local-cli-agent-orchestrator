"""What each agent has changed, for the dashboard's changes panel.

Kept apart from `Snapshot` because it reads git rather than the database: a
scan runs several git processes per worktree, so the app runs it on a worker
thread at its own pace instead of on every two-second database refresh.

Nothing here writes. Every git call is a read, and `GIT_OPTIONAL_LOCKS=0` in
the manager keeps even `git status` from touching an agent's index mid-edit.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from agentos.branding import CONFIG_FILENAME
from agentos.config import Config
from agentos.paths import ProjectPaths
from agentos.schemas.dto import TaskView
from agentos.tui.snapshot import Snapshot
from agentos.vcs.manager import FileDelta, GitManager

# How many directory levels name an area: `src/lib`, `supabase/tests`.
AREA_DEPTH = 2
ROOT_AREA = "(root)"
# The row for agents without a worktree, which all edit the project directory.
SHARED = "(shared)"


@dataclass(frozen=True)
class Area:
    """A directory and how much of the change landed in it."""

    path: str
    files: int
    added: int
    removed: int

    @property
    def churn(self) -> int:
        return self.added + self.removed


@dataclass(frozen=True)
class AgentChanges:
    """One agent's worktree, measured against the project's base branch."""

    agent: str
    base: str
    branch: str | None = None
    files: list[FileDelta] = field(default_factory=list)
    commits_ahead: int = 0
    agents: list[str] = field(default_factory=list)
    """Who could have made these changes. For a worktree, just its agent; for
    the shared directory, every agent configured without a worktree."""

    @property
    def shared(self) -> bool:
        return self.agent == SHARED

    @property
    def added(self) -> int:
        return sum(f.added for f in self.files)

    @property
    def removed(self) -> int:
        return sum(f.removed for f in self.files)

    @property
    def new_files(self) -> int:
        return sum(1 for f in self.files if f.is_new)

    @property
    def areas(self) -> list[Area]:
        return areas_of(self.files)

    def major(self, limit: int = 8) -> list[FileDelta]:
        """The files with the most lines touched, biggest first."""
        return sorted(self.files, key=lambda f: (-f.churn, f.path))[:limit]


def area_of(path: str, depth: int = AREA_DEPTH) -> str:
    parent = PurePosixPath(path.replace("\\", "/")).parent.parts
    return "/".join(parent[:depth]) if parent else ROOT_AREA


def areas_of(files: list[FileDelta], depth: int = AREA_DEPTH) -> list[Area]:
    """Group files by directory, most-changed first."""
    grouped: dict[str, list[FileDelta]] = {}
    for delta in files:
        grouped.setdefault(area_of(delta.path, depth), []).append(delta)
    found = [
        Area(
            path=name,
            files=len(members),
            added=sum(m.added for m in members),
            removed=sum(m.removed for m in members),
        )
        for name, members in grouped.items()
    ]
    return sorted(found, key=lambda a: (-a.churn, -a.files, a.path))


def latest_task(snapshot: Snapshot, agent: str) -> TaskView | None:
    """The task an agent is on, or else the last one it touched."""
    view = snapshot.agent(agent)
    if view is not None and view.current_task_id:
        for task in snapshot.tasks:
            if task.id == view.current_task_id:
                return task
    mine = [t for t in snapshot.tasks if t.assigned_agent == agent and t.started_at]
    return max(mine, key=lambda t: (t.started_at, t.id), default=None)


# The scheduler appends git and verification reports to the agent's own words;
# the summary is what comes before them.
_APPENDED_REPORTS = ("\n\nBranch:", "\n\nNo file changes detected.", "\n\nVerification")


def agent_summary(result: str | None) -> str:
    """The agent's own description of its work, without the appended reports."""
    text = (result or "").strip()
    for marker in _APPENDED_REPORTS:
        text = text.split(marker, 1)[0]
    return text.strip()


class ChangesReader:
    """Scans every agent worktree. Synchronous, for use from a worker thread."""

    def __init__(self, paths: ProjectPaths, config: Config | None = None) -> None:
        self.paths = paths
        self.config = config
        self.git = GitManager(root=paths.root, worktrees_dir=paths.worktrees_dir)

    def read(self) -> list[AgentChanges]:
        return asyncio.run(self._read())

    async def _read(self) -> list[AgentChanges]:
        if not await self.git.is_repository():
            return []
        base = await self.git.current_branch() or "HEAD"

        found = []
        shared = await self._shared()
        if shared is not None:
            found.append(shared)

        worktrees = self.paths.worktrees_dir
        if not worktrees.is_dir():
            return found
        for directory in sorted(p for p in worktrees.iterdir() if p.is_dir()):
            files = await self.git.changes_since(base, directory)
            ahead = await self.git.commits_ahead(base, directory)
            if not files and not ahead:
                continue
            found.append(
                AgentChanges(
                    agent=directory.name,
                    base=base,
                    branch=await self.git.current_branch(directory),
                    files=files,
                    commits_ahead=ahead,
                    agents=[directory.name],
                )
            )
        return found

    async def _shared(self) -> AgentChanges | None:
        """Uncommitted work in the project directory itself.

        Agents without `worktree: true` all edit here, so git cannot say which
        of them changed what; the entry names every candidate instead.
        """
        # Our own config and directories are not the agents' work, even when a
        # project's .gitignore has not been told about them.
        ours = tuple(
            f"{p.relative_to(self.paths.root).as_posix()}/"
            for p in (self.paths.worktrees_dir, self.paths.state_dir)
        )
        files = [
            f for f in await self.git.changes_since("HEAD", self.paths.root)
            if not f.path.replace("\\", "/").startswith(ours)
            and f.path != CONFIG_FILENAME
        ]
        if not files:
            return None
        agents = []
        if self.config is not None:
            agents = sorted(
                name for name, section in self.config.agents.items()
                if not section.worktree
            )
        return AgentChanges(
            agent=SHARED,
            base="HEAD",
            branch=await self.git.current_branch(),
            files=files,
            agents=agents,
        )

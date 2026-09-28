"""Decides where each agent works, and reports what it actually changed.

Two responsibilities, both deliberately conservative:

  * prepare: give an agent an isolated worktree when it is configured for one,
    otherwise let it work in the project directory
  * capture: after a task, record what git says changed

`files_changed` from an agent is a claim. This module produces the facts, and
the scheduler stores both so a reviewer can see any discrepancy.

Nothing here merges. Integration is a separate, explicit decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from agentos.config import Config
from agentos.paths import ProjectPaths
from agentos.schemas.dto import AgentView
from agentos.vcs.manager import GitError, GitManager, WorkingTreeStatus


@dataclass(frozen=True)
class Workspace:
    """Where one agent will run."""

    agent: str
    path: Path
    branch: str | None = None
    isolated: bool = False
    warning: str | None = None
    """Set when isolation was wanted but could not be provided."""


@dataclass(frozen=True)
class WorkReport:
    """What git observed after a task, versus what the agent claimed."""

    branch: str | None = None
    files_changed: list[str] = field(default_factory=list)
    diff_summary: str = "+0 -0"
    committed_sha: str | None = None
    claimed_files: list[str] = field(default_factory=list)
    unverified_claims: list[str] = field(default_factory=list)
    """Files the agent said it changed that git does not show as changed."""
    isolated: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.files_changed

    def render(self) -> str:
        """A short block appended to the task result for a human reader."""
        if self.is_empty:
            return "No file changes detected."
        lines = [f"Branch: {self.branch or '(none)'}", f"Diff: {self.diff_summary}"]
        lines.append("Files changed:")
        lines += [f"  {path}" for path in self.files_changed[:50]]
        if len(self.files_changed) > 50:
            lines.append(f"  ...and {len(self.files_changed) - 50} more")
        if self.committed_sha:
            lines.append(f"Committed: {self.committed_sha[:12]}")
        if self.unverified_claims:
            lines.append(
                "Claimed but not changed according to git: "
                + ", ".join(self.unverified_claims[:20])
            )
        return "\n".join(lines)


class WorkspaceService:
    def __init__(
        self,
        config: Config,
        paths: ProjectPaths,
        git: GitManager | None = None,
    ) -> None:
        self.config = config
        self.paths = paths
        self.git = git or GitManager(
            root=paths.root, worktrees_dir=paths.worktrees_dir
        )

    def wants_isolation(self, agent_name: str) -> bool:
        section = self.config.agents.get(agent_name)
        return bool(section and section.worktree)

    async def prepare(self, agent: AgentView) -> Workspace:
        """Ensure the agent has somewhere to work.

        If isolation is configured but git cannot provide it, we fall back to the
        project directory and say so, rather than refusing to run the task. The
        operator asked for work to happen; a missing repository is a warning, not
        a reason to stall the queue.
        """
        if not self.wants_isolation(agent.name):
            return Workspace(agent=agent.name, path=self.paths.root, isolated=False)

        try:
            worktree = await self.git.create_worktree(agent.name)
        except GitError as exc:
            return Workspace(
                agent=agent.name,
                path=self.paths.root,
                isolated=False,
                warning=f"worktree unavailable, using the project directory: {exc}",
            )

        return Workspace(
            agent=agent.name,
            path=worktree.path,
            branch=worktree.branch,
            isolated=True,
        )

    async def capture(
        self,
        workspace: Workspace,
        claimed_files: list[str] | None = None,
        commit_message: str | None = None,
    ) -> WorkReport:
        """Record what changed. Optionally commit it.

        Committing is opt-in: for the MVP a task can complete with an uncommitted
        worktree, which the operator can inspect with `agentctl diff`.

        Only meaningful for an isolated workspace. In a shared project directory
        the changes seen here belong to whoever happened to be running, so the
        scheduler does not call this for non-isolated agents.
        """
        claimed = list(claimed_files or [])
        try:
            status = await self.git.status(workspace.path)
        except GitError:
            # Not a repository, or git is unavailable: report the claim only,
            # clearly unverified.
            return WorkReport(
                branch=workspace.branch,
                claimed_files=claimed,
                unverified_claims=claimed,
                isolated=workspace.isolated,
            )

        sha: str | None = None
        if commit_message and not status.is_clean:
            try:
                sha = await self.git.commit(commit_message, cwd=workspace.path)
            except GitError:
                sha = None

        return self._build_report(workspace, status, claimed, sha)

    @staticmethod
    def _build_report(
        workspace: Workspace,
        status: WorkingTreeStatus,
        claimed: list[str],
        sha: str | None,
    ) -> WorkReport:
        actual = set(status.paths)
        # Compare on normalised separators so a Windows-style claim still matches.
        normalised = {p.replace("\\", "/") for p in actual}
        unverified = [
            path
            for path in claimed
            if path.replace("\\", "/").lstrip("./") not in normalised
            and not any(
                n.endswith(path.replace("\\", "/").lstrip("./")) for n in normalised
            )
        ]
        return WorkReport(
            branch=status.branch or workspace.branch,
            files_changed=sorted(actual),
            diff_summary=status.diff_summary,
            committed_sha=sha,
            claimed_files=claimed,
            unverified_claims=unverified,
            isolated=workspace.isolated,
        )

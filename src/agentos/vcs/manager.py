"""Git operations for agent isolation.

The only module that runs git. Everything is an argument array passed to
`create_subprocess_exec` -- never a shell string -- so a branch name or path
containing a space, quote or semicolon cannot become a second command.

Scope is deliberately narrow. This creates worktrees, reports status and diffs,
and commits on request. It does NOT merge automatically: integration is a
separate, explicit decision (phase 10).

Named `vcs` rather than `git` so it cannot shadow anything importable.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Long-running clones are not our concern; these are local metadata operations.
DEFAULT_TIMEOUT = 120.0

# Branch prefix for agent work.
BRANCH_PREFIX = "agent"


class GitError(RuntimeError):
    """A git command failed."""

    def __init__(self, argv: list[str], exit_code: int, stderr: str) -> None:
        self.argv = argv
        self.exit_code = exit_code
        self.stderr = stderr.strip()
        super().__init__(
            f"git {' '.join(argv[1:])} exited {exit_code}: {self.stderr[:400]}"
        )


class NotARepository(GitError):
    """The directory is not inside a git repository."""

    def __init__(self, path: Path) -> None:
        self.path = path
        RuntimeError.__init__(self, f"not a git repository: {path}")


@dataclass(frozen=True)
class GitResult:
    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def text(self) -> str:
        return self.stdout.strip()


@dataclass(frozen=True)
class FileChange:
    status: str
    """Two-character porcelain code, e.g. ' M', '??', 'A '."""
    path: str

    @property
    def is_untracked(self) -> bool:
        return self.status.strip() == "??"


@dataclass(frozen=True)
class WorkingTreeStatus:
    """What an agent actually changed, as opposed to what it claimed."""

    branch: str | None = None
    changes: list[FileChange] = field(default_factory=list)
    insertions: int = 0
    deletions: int = 0

    @property
    def is_clean(self) -> bool:
        return not self.changes

    @property
    def paths(self) -> list[str]:
        return [c.path for c in self.changes]

    @property
    def diff_summary(self) -> str:
        return f"+{self.insertions} -{self.deletions}"


@dataclass(frozen=True)
class Worktree:
    agent: str
    path: Path
    branch: str
    created: bool
    """False when it already existed and was reused."""


def parse_porcelain(output: str) -> list[FileChange]:
    """Parse `git status --porcelain`.

    Handles renames (`R  old -> new`) by reporting the new path, which is what a
    reviewer needs to look at.
    """
    changes: list[FileChange] = []
    for line in (output or "").splitlines():
        if len(line) < 4:
            continue
        status, path = line[:2], line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        # Paths with unusual characters come back quoted.
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        changes.append(FileChange(status=status, path=path))
    return changes


def parse_shortstat(output: str) -> tuple[int, int]:
    """Pull insertion and deletion counts out of `git diff --shortstat`."""
    insertions = deletions = 0
    for part in (output or "").split(","):
        cleaned = part.strip()
        digits = "".join(c for c in cleaned if c.isdigit())
        if not digits:
            continue
        if "insertion" in cleaned:
            insertions = int(digits)
        elif "deletion" in cleaned:
            deletions = int(digits)
    return insertions, deletions


@dataclass(frozen=True)
class FileDelta:
    """Line counts for one file, relative to the branch's starting point."""

    path: str
    added: int = 0
    removed: int = 0
    is_new: bool = False
    binary: bool = False

    @property
    def churn(self) -> int:
        return self.added + self.removed


def parse_numstat(output: str) -> list[FileDelta]:
    """Parse `git diff --numstat --no-renames`. Binary files report `-`."""
    deltas: list[FileDelta] = []
    for line in (output or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or not parts[2].strip():
            continue
        added, removed, path = parts
        binary = added == "-" or removed == "-"
        deltas.append(
            FileDelta(
                path=path.strip(),
                added=0 if binary else int(added),
                removed=0 if binary else int(removed),
                binary=binary,
            )
        )
    return deltas


# An untracked file bigger than this is counted, not read.
MAX_COUNTED_BYTES = 2_000_000


def count_new_file(path: Path, relative: str) -> FileDelta:
    """Describe an untracked file, which `git diff` cannot."""
    try:
        if path.stat().st_size > MAX_COUNTED_BYTES:
            return FileDelta(path=relative, is_new=True, binary=True)
        data = path.read_bytes()
    except OSError:
        return FileDelta(path=relative, is_new=True)
    if b"\0" in data[:8000]:
        return FileDelta(path=relative, is_new=True, binary=True)
    lines = data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)
    return FileDelta(path=relative, added=lines, is_new=True)


def branch_for(agent: str) -> str:
    """Branch name for an agent, sanitised for git's refname rules."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in agent.strip())
    safe = safe.strip("-.") or "agent"
    return f"{BRANCH_PREFIX}/{safe}"


class GitManager:
    def __init__(self, root: Path, worktrees_dir: Path | None = None) -> None:
        self.root = Path(root)
        self.worktrees_dir = Path(worktrees_dir or self.root / "worktrees")
        self._executable: str | None = None

    # ------------------------------------------------------------------ plumbing

    @property
    def executable(self) -> str:
        if self._executable is None:
            found = shutil.which("git")
            if found is None:
                raise GitError(["git"], 127, "git was not found on PATH")
            self._executable = found
        return self._executable

    async def _run(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> GitResult:
        """Run one git command. Arguments are never concatenated into a string."""
        argv = [self.executable, *args]
        env = os.environ.copy()
        # Keep git non-interactive: a credential or editor prompt would hang the
        # orchestrator with no way to answer it.
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_OPTIONAL_LOCKS"] = "0"
        env.setdefault("GIT_EDITOR", "true")

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd or self.root),
                env=env,
            )
        except OSError as exc:
            raise GitError(argv, 127, str(exc)) from exc

        try:
            raw_out, raw_err = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except (TimeoutError, asyncio.TimeoutError) as exc:
            with contextlib_suppress():
                proc.kill()
            raise GitError(argv, 124, f"timed out after {timeout}s") from exc

        result = GitResult(
            argv=argv,
            exit_code=proc.returncode or 0,
            stdout=raw_out.decode("utf-8", errors="replace"),
            stderr=raw_err.decode("utf-8", errors="replace"),
        )
        if check and not result.ok:
            raise GitError(argv, result.exit_code, result.stderr)
        return result

    # ---------------------------------------------------------------- repository

    async def is_repository(self) -> bool:
        result = await self._run(
            "rev-parse", "--is-inside-work-tree", check=False
        )
        return result.ok and result.text == "true"

    async def has_commits(self) -> bool:
        """A worktree cannot be created from a repository with no commits."""
        result = await self._run("rev-parse", "--verify", "HEAD", check=False)
        return result.ok

    async def ensure_repository(self) -> None:
        """Verify the project is a usable git repository, or say why not."""
        if not await self.is_repository():
            raise NotARepository(self.root)
        if not await self.has_commits():
            raise GitError(
                ["git", "rev-parse", "HEAD"],
                1,
                "the repository has no commits yet; make one before using "
                "worktrees",
            )

    async def current_branch(self, cwd: Path | None = None) -> str | None:
        result = await self._run(
            "rev-parse", "--abbrev-ref", "HEAD", cwd=cwd, check=False
        )
        if not result.ok:
            return None
        return result.text or None

    async def branch_exists(self, branch: str) -> bool:
        result = await self._run(
            "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False
        )
        return result.ok

    # ----------------------------------------------------------------- worktrees

    async def list_worktrees(self) -> dict[Path, str]:
        """Map worktree path -> branch, as git sees it."""
        result = await self._run("worktree", "list", "--porcelain", check=False)
        if not result.ok:
            return {}
        found: dict[Path, str] = {}
        path: Path | None = None
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                path = Path(line.removeprefix("worktree ").strip())
            elif line.startswith("branch ") and path is not None:
                found[path] = line.removeprefix("branch ").strip().removeprefix(
                    "refs/heads/"
                )
                path = None
        return found

    async def create_worktree(
        self, agent: str, base: str | None = None
    ) -> Worktree:
        """Create (or reuse) an isolated checkout for one agent.

        Idempotent: calling it again returns the existing worktree rather than
        failing, because agents are long-lived and this runs before every task.
        """
        await self.ensure_repository()
        branch = branch_for(agent)
        path = (self.worktrees_dir / agent).resolve()

        existing = await self.list_worktrees()
        if path in existing:
            return Worktree(agent=agent, path=path, branch=existing[path], created=False)

        # A leftover directory from a deleted worktree would make `git worktree
        # add` fail; only remove it if git does not know about it and it is empty.
        if path.exists() and not any(path.iterdir()):
            path.rmdir()

        self.worktrees_dir.mkdir(parents=True, exist_ok=True)

        if await self.branch_exists(branch):
            # Reattach to the agent's existing branch, keeping its history.
            await self._run("worktree", "add", str(path), branch)
        else:
            start = base or await self.current_branch() or "HEAD"
            await self._run("worktree", "add", "-b", branch, str(path), start)

        return Worktree(agent=agent, path=path, branch=branch, created=True)

    async def remove_worktree(
        self, agent: str, force: bool = False, delete_branch: bool = False
    ) -> bool:
        """Remove an agent's worktree. Returns False if there was nothing to do.

        The branch is kept by default: it holds the agent's work, and discarding
        it silently would destroy the thing the operator still needs to review.
        """
        path = (self.worktrees_dir / agent).resolve()
        existing = await self.list_worktrees()
        if path not in existing:
            return False

        args = ["worktree", "remove", str(path)]
        if force:
            args.append("--force")
        await self._run(*args)

        if delete_branch:
            await self._run("branch", "-D", branch_for(agent), check=False)
        return True

    async def prune_worktrees(self) -> None:
        await self._run("worktree", "prune", check=False)

    # -------------------------------------------------------------------- status

    async def status(self, cwd: Path | None = None) -> WorkingTreeStatus:
        """What changed in a working tree, including untracked files."""
        target = Path(cwd) if cwd else self.root
        porcelain = await self._run(
            "status", "--porcelain", "--untracked-files=all", cwd=target
        )
        changes = parse_porcelain(porcelain.stdout)

        # --shortstat covers tracked changes; untracked files are not counted by
        # git diff, so the numbers describe modifications, not new files.
        shortstat = await self._run("diff", "--shortstat", "HEAD", cwd=target, check=False)
        insertions, deletions = parse_shortstat(shortstat.stdout)

        return WorkingTreeStatus(
            branch=await self.current_branch(target),
            changes=changes,
            insertions=insertions,
            deletions=deletions,
        )

    async def diff(
        self,
        cwd: Path | None = None,
        staged: bool = False,
        name_only: bool = False,
        context_lines: int | None = None,
    ) -> str:
        args = ["diff", "HEAD"]
        if staged:
            args = ["diff", "--cached"]
        if name_only:
            args.append("--name-only")
        if context_lines is not None:
            args.append(f"--unified={max(0, context_lines)}")
        result = await self._run(*args, cwd=Path(cwd) if cwd else self.root, check=False)
        return result.stdout

    async def agent_diff(self, agent: str, name_only: bool = False) -> str:
        path = (self.worktrees_dir / agent).resolve()
        if not path.is_dir():
            raise GitError(
                ["git", "diff"], 1, f"{agent} has no worktree at {path}"
            )
        return await self.diff(cwd=path, name_only=name_only)

    async def changes_since(self, base: str, cwd: Path) -> list[FileDelta]:
        """Everything a worktree has that `base` does not, file by file.

        Measured from the merge base, so commits on the agent branch and
        uncommitted edits both count, while work that landed on `base` after
        the branch was cut does not. Untracked files are counted separately
        because `git diff` never sees them.
        """
        target = Path(cwd)
        fork = await self._run("merge-base", base, "HEAD", cwd=target, check=False)
        start = fork.text if fork.ok and fork.text else "HEAD"
        numstat = await self._run(
            "diff", "--numstat", "--no-renames", start, cwd=target, check=False
        )
        deltas = parse_numstat(numstat.stdout)

        added = await self._run(
            "diff", "--name-only", "--no-renames", "--diff-filter=A", start,
            cwd=target, check=False,
        )
        created = set(added.stdout.splitlines())
        deltas = [
            FileDelta(d.path, d.added, d.removed, d.path in created, d.binary)
            for d in deltas
        ]

        untracked = await self._run(
            "ls-files", "--others", "--exclude-standard", cwd=target, check=False
        )
        for relative in untracked.stdout.splitlines():
            if relative.strip():
                deltas.append(count_new_file(target / relative, relative))
        return deltas

    async def commits_ahead(self, base: str, cwd: Path) -> int:
        result = await self._run(
            "rev-list", "--count", f"{base}..HEAD", cwd=Path(cwd), check=False
        )
        return int(result.text) if result.ok and result.text.isdigit() else 0

    # -------------------------------------------------------------------- commits

    async def commit(
        self, message: str, cwd: Path | None = None, add_all: bool = True
    ) -> str | None:
        """Commit the working tree. Returns the new sha, or None if nothing changed.

        The message goes through `-m` as a single argument, so its content can
        never be interpreted as options or shell syntax.
        """
        target = Path(cwd) if cwd else self.root
        if add_all:
            await self._run("add", "--all", cwd=target)

        staged = await self._run("diff", "--cached", "--quiet", cwd=target, check=False)
        if staged.ok:
            return None  # exit 0 from --quiet means no staged changes

        await self._run("commit", "-m", message, cwd=target)
        head = await self._run("rev-parse", "HEAD", cwd=target)
        return head.text

    async def log(self, cwd: Path | None = None, limit: int = 10) -> list[str]:
        result = await self._run(
            "log", f"-{max(1, limit)}", "--oneline", "--no-decorate",
            cwd=Path(cwd) if cwd else self.root,
            check=False,
        )
        return [line for line in result.stdout.splitlines() if line.strip()]

    # --------------------------------------------------------------- integration

    async def merge_base(self, branch: str, other: str = "HEAD") -> str | None:
        result = await self._run("merge-base", branch, other, check=False)
        return result.text or None

    async def can_merge_cleanly(self, branch: str, into: str | None = None) -> bool:
        """Dry-run a merge without touching the working tree.

        Uses merge-tree, which computes the result in memory. Nothing is
        modified, so this is safe to call on a live checkout.
        """
        target = into or await self.current_branch() or "HEAD"
        result = await self._run(
            "merge-tree", "--write-tree", "--name-only", target, branch, check=False
        )
        # merge-tree exits non-zero when the merge has conflicts.
        return result.ok

    async def merge(
        self, branch: str, cwd: Path | None = None, message: str | None = None
    ) -> GitResult:
        """Merge a branch. Never called automatically by the scheduler.

        Returns the result rather than raising on conflict, so the caller can
        report the conflict and create a resolution task instead of guessing.
        """
        args = ["merge", "--no-ff", branch]
        if message:
            args += ["-m", message]
        return await self._run(
            *args, cwd=Path(cwd) if cwd else self.root, check=False
        )


class contextlib_suppress:
    """Tiny local suppressor, to avoid importing contextlib for one use."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> bool:
        return True

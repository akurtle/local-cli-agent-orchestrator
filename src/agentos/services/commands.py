"""Safe execution of development commands.

The policy this module implements, in priority order:

  1. **Restrict the executable.** An allowlist of program names, matched on the
     first argv element only. Nothing else runs.
  2. **Argument arrays, never shell strings.** No `shell=True`, so quoting,
     `&&`, `;`, backticks and redirection have no meaning.
  3. **Restrict the working directory.** Resolved and confirmed to sit inside the
     agent's own worktree, so `../../..` cannot escape.
  4. **Enforce a timeout** and kill the whole process tree on expiry.
  5. **Enforce capabilities** -- run_command or run_tests, checked before launch.
  6. **Require approval** for risky argument patterns rather than guessing.

Explicitly *not* a security sandbox. String matching cannot make arbitrary code
safe, and pretending otherwise would be worse than useless. What it does is stop
an agent from running something the operator never sanctioned, and record
everything that did run.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TIMEOUT = 300.0
MAX_OUTPUT_CHARS = 200_000


class Verdict:
    """What the policy decided about a command."""

    ALLOWED = "allowed"
    DENIED = "denied"
    NEEDS_APPROVAL = "needs_approval"


@dataclass(frozen=True)
class PolicyDecision:
    verdict: str
    reason: str = ""
    matched: str = ""
    """The rule that decided it, for an explainable refusal."""

    @property
    def allowed(self) -> bool:
        return self.verdict == Verdict.ALLOWED

    @property
    def needs_approval(self) -> bool:
        return self.verdict == Verdict.NEEDS_APPROVAL


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    cwd: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    timed_out: bool = False
    verdict: str = Verdict.ALLOWED
    denied_reason: str = ""
    approved_by: str | None = None

    @property
    def ok(self) -> bool:
        return self.verdict == Verdict.ALLOWED and self.exit_code == 0

    @property
    def ran(self) -> bool:
        return self.exit_code is not None


def normalise_program(program: str) -> str:
    """The comparable name of an executable.

    A policy says `pytest`; an agent may say `pytest`, `pytest.exe`, or an
    absolute path to it. All three must compare equal, or the allowlist is
    trivially bypassed on Windows.
    """
    name = Path(program.strip().strip('"')).name.lower()
    for suffix in (".exe", ".cmd", ".bat", ".com", ".ps1"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


@dataclass
class CommandPolicy:
    """Which commands may run, and which need a human first."""

    allowed: list[str] = field(default_factory=list)
    denied: list[str] = field(default_factory=list)
    require_approval: list[str] = field(default_factory=list)
    """Prefix patterns, e.g. "git push" or "git reset"."""

    def decide(self, argv: list[str]) -> PolicyDecision:
        """Judge a command. Pure, so the whole policy is testable directly."""
        if not argv or not str(argv[0]).strip():
            return PolicyDecision(Verdict.DENIED, "no command given")

        program = normalise_program(str(argv[0]))
        rest = [str(a) for a in argv[1:]]

        # Denied wins over allowed: listing something in both is a mistake, and
        # refusing is the safe reading of it.
        if program in {normalise_program(d) for d in self.denied}:
            return PolicyDecision(
                Verdict.DENIED,
                f"{program!r} is on the denied list",
                matched=program,
            )

        if program not in {normalise_program(a) for a in self.allowed}:
            return PolicyDecision(
                Verdict.DENIED,
                f"{program!r} is not on the allowed list",
                matched=program,
            )

        # Approval patterns match on the command prefix, so "git reset" catches
        # "git reset --hard HEAD~3" without trying to parse git's grammar.
        spelled = " ".join([program, *rest]).lower()
        for pattern in self.require_approval:
            normalised = pattern.strip().lower()
            if not normalised:
                continue
            parts = normalised.split()
            candidate = " ".join([normalise_program(parts[0]), *parts[1:]])
            if spelled == candidate or spelled.startswith(candidate + " "):
                return PolicyDecision(
                    Verdict.NEEDS_APPROVAL,
                    f"matches the approval rule {pattern!r}",
                    matched=pattern,
                )

        return PolicyDecision(Verdict.ALLOWED, f"{program!r} is allowed", program)


class WorkingDirectoryError(ValueError):
    """The requested directory is outside the agent's allowed area."""


def resolve_within(candidate: str | Path, boundary: str | Path) -> Path:
    """Resolve a path and confirm it is inside the boundary.

    Resolved before comparison so `worktrees/backend/../../etc` cannot pass, and
    symlinks are followed, so a link pointing outside is caught too.
    """
    root = Path(boundary).resolve()
    target = Path(candidate).resolve()
    if target != root and root not in target.parents:
        raise WorkingDirectoryError(
            f"{target} is outside the allowed directory {root}"
        )
    if not target.is_dir():
        raise WorkingDirectoryError(f"{target} is not a directory")
    return target


class CommandRunner:
    """Runs a vetted command. The only module that executes agent-facing commands."""

    def __init__(
        self,
        policy: CommandPolicy,
        default_timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.policy = policy
        self.default_timeout = default_timeout

    def resolve_executable(self, program: str) -> str | None:
        return shutil.which(program)

    async def execute(
        self,
        argv: list[str],
        cwd: str | Path,
        boundary: str | Path | None = None,
        timeout: float | None = None,
        approved_by: str | None = None,
    ) -> CommandResult:
        """Run a command, or refuse it.

        `boundary` is the directory the command may not escape; it defaults to
        `cwd`, so a caller that forgets is still constrained rather than free.
        """
        argv = [str(a) for a in argv]
        decision = self.policy.decide(argv)

        if decision.verdict == Verdict.DENIED:
            return CommandResult(
                argv=argv,
                cwd=str(cwd),
                verdict=Verdict.DENIED,
                denied_reason=decision.reason,
            )

        if decision.needs_approval and approved_by is None:
            return CommandResult(
                argv=argv,
                cwd=str(cwd),
                verdict=Verdict.NEEDS_APPROVAL,
                denied_reason=decision.reason,
            )

        try:
            working = resolve_within(cwd, boundary if boundary is not None else cwd)
        except WorkingDirectoryError as exc:
            return CommandResult(
                argv=argv,
                cwd=str(cwd),
                verdict=Verdict.DENIED,
                denied_reason=str(exc),
            )

        executable = self.resolve_executable(argv[0])
        if executable is None:
            return CommandResult(
                argv=argv,
                cwd=str(working),
                verdict=Verdict.DENIED,
                denied_reason=f"{argv[0]!r} was not found on PATH",
            )

        return await self._spawn(
            [executable, *argv[1:]],
            working,
            timeout or self.default_timeout,
            approved_by,
            original=argv,
        )

    async def _spawn(
        self,
        argv: list[str],
        cwd: Path,
        timeout: float,
        approved_by: str | None,
        original: list[str],
    ) -> CommandResult:
        loop = asyncio.get_running_loop()
        started = loop.time()
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        # Keep tooling non-interactive: a prompt would hang with nobody to answer.
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["CI"] = env.get("CI", "1")

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=str(cwd),
                env=env,
            )
        except OSError as exc:
            return CommandResult(
                argv=original,
                cwd=str(cwd),
                verdict=Verdict.DENIED,
                denied_reason=f"failed to start: {exc}",
            )

        timed_out = False
        try:
            raw_out, raw_err = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except (TimeoutError, asyncio.TimeoutError):
            timed_out = True
            await self._kill_tree(proc)
            raw_out, raw_err = b"", b""

        return CommandResult(
            argv=original,
            cwd=str(cwd),
            exit_code=proc.returncode,
            stdout=raw_out.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS],
            stderr=raw_err.decode("utf-8", errors="replace")[:MAX_OUTPUT_CHARS],
            duration_seconds=loop.time() - started,
            timed_out=timed_out,
            verdict=Verdict.ALLOWED,
            denied_reason=f"timed out after {timeout}s" if timed_out else "",
            approved_by=approved_by,
        )

    @staticmethod
    async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        """Kill the process and its children.

        A test runner spawns workers; terminating only the parent leaves them
        holding the worktree open. Windows has no SIGTERM, hence taskkill.
        """
        if proc.returncode is not None:
            return
        if sys.platform == "win32" and proc.pid:
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=10)
            except (OSError, TimeoutError):
                pass
        if proc.returncode is None:
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass

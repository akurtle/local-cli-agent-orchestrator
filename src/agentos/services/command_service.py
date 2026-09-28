"""Agent-facing command execution: capability check, policy, run, record.

One entry point, so there is a single place where a command can be refused and a
single place where one is recorded. Callers pass an agent and an argv list; they
never construct a subprocess themselves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from agentos.config import Config
from agentos.db.models import CommandRun
from agentos.db.session import Database
from agentos.schemas.capabilities import Capability
from agentos.services.commands import (
    CommandPolicy,
    CommandResult,
    CommandRunner,
    Verdict,
)
from agentos.services.permissions import PermissionService

# Commands that are really "run the tests", so run_tests alone is enough.
TEST_PROGRAMS = frozenset({"pytest", "tox", "jest", "vitest"})
TEST_SUBCOMMANDS = frozenset({"test", "tests"})


@dataclass(frozen=True)
class CommandRecord:
    id: int
    agent: str
    task_key: str | None
    executable: str
    arguments: list[str]
    cwd: str
    verdict: str
    denied_reason: str
    exit_code: int | None
    timed_out: bool
    approval_required: bool
    approved_by: str | None
    started_at: datetime | None = None
    duration_seconds: float | None = None

    @property
    def spelled(self) -> str:
        return " ".join([self.executable, *self.arguments])


def required_capability(argv: list[str]) -> Capability:
    """Which capability a command needs.

    Running tests is a narrower privilege than running anything, so an agent may
    hold run_tests without run_command. Deciding from the program and its first
    subcommand keeps that distinction honest without parsing every tool's grammar.
    """
    if not argv:
        return Capability.RUN_COMMAND
    from agentos.services.commands import normalise_program

    program = normalise_program(str(argv[0]))
    if program in TEST_PROGRAMS:
        return Capability.RUN_TESTS
    if len(argv) > 1 and str(argv[1]).strip().lower() in TEST_SUBCOMMANDS:
        # `npm test`, `cargo test`, and friends.
        return Capability.RUN_TESTS
    return Capability.RUN_COMMAND


class CommandService:
    def __init__(
        self,
        db: Database,
        config: Config,
        permissions: PermissionService | None = None,
        runner: CommandRunner | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.permissions = permissions or PermissionService(db, config)
        section = config.commands
        self.policy = CommandPolicy(
            allowed=list(section.allowed),
            denied=list(section.denied),
            require_approval=list(section.require_approval),
        )
        self.runner = runner or CommandRunner(
            self.policy, default_timeout=section.timeout_seconds
        )

    async def run(
        self,
        agent: str,
        argv: list[str],
        cwd: str | Path,
        boundary: str | Path | None = None,
        task_key: str | None = None,
        timeout: float | None = None,
        approved_by: str | None = None,
    ) -> CommandResult:
        """Check, run and record one command."""
        argv = [str(a) for a in argv]
        capability = required_capability(argv)

        # Capability first: an agent with no right to run anything should not even
        # reach the policy, and the refusal belongs in the denial trail.
        if not self.permissions.check(
            agent,
            capability,
            capability.value,
            f"command refused: {' '.join(argv)[:200]}",
            task_key,
        ):
            result = CommandResult(
                argv=argv,
                cwd=str(cwd),
                verdict=Verdict.DENIED,
                denied_reason=f"{agent} lacks {capability.value}",
            )
            self._record(agent, task_key, result)
            return result

        result = await self.runner.execute(
            argv=argv,
            cwd=cwd,
            boundary=boundary,
            timeout=timeout,
            approved_by=approved_by,
        )
        self._record(agent, task_key, result)
        return result

    def decide(self, argv: list[str]) -> object:
        """Policy verdict without running anything, for `agentctl commands check`."""
        return self.policy.decide([str(a) for a in argv])

    # --------------------------------------------------------------- persistence

    def _record(
        self, agent: str, task_key: str | None, result: CommandResult
    ) -> CommandRecord:
        with self.db.session() as session:
            row = CommandRun(
                agent=agent,
                task_key=task_key,
                executable=result.argv[0] if result.argv else "",
                arguments=json.dumps(result.argv[1:]),
                cwd=result.cwd,
                verdict=result.verdict,
                denied_reason=result.denied_reason,
                exit_code=result.exit_code,
                stdout=result.stdout[:100_000],
                stderr=result.stderr[:100_000],
                timed_out=result.timed_out,
                approval_required=result.verdict == Verdict.NEEDS_APPROVAL,
                approved_by=result.approved_by,
                finished_at=datetime.now(timezone.utc) if result.ran else None,
            )
            session.add(row)
            session.flush()
            return self._view(row, result.duration_seconds)

    @staticmethod
    def _view(row: CommandRun, duration: float | None = None) -> CommandRecord:
        try:
            arguments = json.loads(row.arguments or "[]")
        except json.JSONDecodeError:
            arguments = []
        return CommandRecord(
            id=row.id,
            agent=row.agent,
            task_key=row.task_key,
            executable=row.executable,
            arguments=arguments,
            cwd=row.cwd,
            verdict=row.verdict,
            denied_reason=row.denied_reason,
            exit_code=row.exit_code,
            timed_out=row.timed_out,
            approval_required=row.approval_required,
            approved_by=row.approved_by,
            started_at=row.started_at,
            duration_seconds=duration,
        )

    def history(
        self,
        agent: str | None = None,
        verdict: str | None = None,
        limit: int | None = 30,
    ) -> list[CommandRecord]:
        with self.db.session() as session:
            stmt = select(CommandRun).order_by(CommandRun.id.desc())
            if agent:
                stmt = stmt.where(CommandRun.agent == agent)
            if verdict:
                stmt = stmt.where(CommandRun.verdict == verdict)
            if limit:
                stmt = stmt.limit(limit)
            rows = list(session.scalars(stmt).all())
            rows.reverse()
            return [self._view(row) for row in rows]

    def pending_approvals(self) -> list[CommandRecord]:
        """Commands that stopped because they need a human."""
        return self.history(verdict=Verdict.NEEDS_APPROVAL, limit=None)

    def get(self, command_id: int) -> CommandRecord | None:
        with self.db.session() as session:
            row = session.get(CommandRun, command_id)
            return self._view(row) if row else None

"""Checks an agent's claim of completion.

The separation this exists to enforce:

    agent claims done     -> task status AGENT_DONE
    orchestrator verifies -> COMPLETED, or FAILED_VERIFICATION

Only Python decides. An agent saying `status: completed` moves a task to
AGENT_DONE and no further; the checks decide the rest.

Checks run through the same CommandService as anything else an agent does, so
they are subject to the command policy, the working-directory boundary and the
timeout. A check that is denied by policy is recorded as SKIPPED rather than
counted as a pass, because "we could not look" is not "it is fine".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from agentos.config import Config
from agentos.db.models import Verification
from agentos.db.session import Database
from agentos.schemas.dto import TaskView
from agentos.schemas.verification import (
    CheckResult,
    Criterion,
    CriterionKind,
    Verdict,
    VerificationStatus,
    classify_criteria,
)
from agentos.services.command_service import CommandService
from agentos.services.commands import Verdict as PolicyVerdict

# The verifier acts on the orchestrator's behalf, not the agent's, so it is not
# limited by the agent's own capabilities.
VERIFIER = "verification"


@dataclass(frozen=True)
class VerificationRecord:
    id: int
    task_key: str
    agent: str
    attempt: int
    command: list[str]
    source: str
    status: str
    exit_code: int | None
    detail: str
    stdout: str = ""
    stderr: str = ""
    started_at: datetime | None = None

    @property
    def spelled(self) -> str:
        return " ".join(self.command)


class VerificationService:
    def __init__(
        self,
        db: Database,
        config: Config,
        command_service: CommandService | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.commands = command_service or CommandService(db, config)

    # ------------------------------------------------------------------ planning

    def plan(self, task: TaskView, agent_role: str = "") -> tuple[
        list[tuple[list[str], str]], list[Criterion], list[Criterion]
    ]:
        """What to run, and what cannot be run.

        Returns (checks, review criteria, manual criteria). Pure with respect to
        execution: nothing is launched here, so a caller can show a plan.
        """
        section = self.config.verification
        if not section.enabled:
            return [], [], []

        checks: list[tuple[list[str], str]] = [
            (argv, "config")
            for argv in section.commands_for(task.assigned_agent or "", agent_role)
        ]

        criteria = classify_criteria(task.acceptance_criteria)
        if section.verify_criteria:
            checks += [
                (c.command, "criterion") for c in criteria if c.is_automated
            ]

        review = [c for c in criteria if c.kind is CriterionKind.REVIEW]
        manual = [c for c in criteria if c.kind is CriterionKind.MANUAL]
        return checks, review, manual

    def is_enabled(self) -> bool:
        return bool(self.config.verification.enabled)

    def has_checks(self, task: TaskView, agent_role: str = "") -> bool:
        checks, _review, _manual = self.plan(task, agent_role)
        return bool(checks)

    # ----------------------------------------------------------------- executing

    async def verify(
        self,
        task: TaskView,
        cwd: str | Path,
        boundary: str | Path | None = None,
        agent_role: str = "",
    ) -> Verdict:
        """Run the checks for one task and record each one."""
        checks, review, manual = self.plan(task, agent_role)
        results: list[CheckResult] = []

        for argv, source in checks:
            result = await self._run_one(task, argv, source, cwd, boundary)
            results.append(result)
            # A failing check makes the verdict negative already; keep going so
            # the operator sees every problem, not just the first.

        return Verdict(checks=results, manual=manual, review=review)

    async def _run_one(
        self,
        task: TaskView,
        argv: list[str],
        source: str,
        cwd: str | Path,
        boundary: str | Path | None,
    ) -> CheckResult:
        outcome = await self.commands.run(
            agent=VERIFIER,
            argv=argv,
            cwd=cwd,
            boundary=boundary,
            task_key=task.key,
            timeout=self.config.verification.timeout_seconds,
        )

        if outcome.verdict != PolicyVerdict.ALLOWED:
            # Could not look. Recorded as skipped, never as a pass.
            result = CheckResult(
                command=argv,
                status=VerificationStatus.SKIPPED,
                detail=outcome.denied_reason or "not permitted",
                source=source,
            )
        elif outcome.timed_out:
            result = CheckResult(
                command=argv,
                status=VerificationStatus.ERROR,
                exit_code=outcome.exit_code,
                stdout=outcome.stdout,
                stderr=outcome.stderr,
                detail="timed out",
                source=source,
            )
        elif outcome.exit_code == 0 or not self.config.verification.require_clean_exit:
            result = CheckResult(
                command=argv,
                status=VerificationStatus.PASSED,
                exit_code=outcome.exit_code,
                stdout=outcome.stdout,
                stderr=outcome.stderr,
                source=source,
            )
        else:
            result = CheckResult(
                command=argv,
                status=VerificationStatus.FAILED,
                exit_code=outcome.exit_code,
                stdout=outcome.stdout,
                stderr=outcome.stderr,
                detail=f"exit {outcome.exit_code}",
                source=source,
            )

        self._record(task, result)
        return result

    # --------------------------------------------------------------- persistence

    def _record(self, task: TaskView, result: CheckResult) -> VerificationRecord:
        with self.db.session() as session:
            row = Verification(
                task_id=task.id,
                task_key=task.key,
                agent=task.assigned_agent or "",
                # attempts is incremented on failure, so +1 names this attempt.
                attempt=max(1, task.attempts + 1),
                command=json.dumps(result.command),
                source=result.source,
                status=result.status.value,
                exit_code=result.exit_code,
                stdout=result.stdout[:100_000],
                stderr=result.stderr[:100_000],
                detail=result.detail,
                finished_at=datetime.now(timezone.utc),
            )
            session.add(row)
            session.flush()
            return self._view(row)

    @staticmethod
    def _view(row: Verification) -> VerificationRecord:
        try:
            command = json.loads(row.command or "[]")
        except json.JSONDecodeError:
            command = []
        return VerificationRecord(
            id=row.id,
            task_key=row.task_key,
            agent=row.agent,
            attempt=row.attempt,
            command=command,
            source=row.source,
            status=row.status,
            exit_code=row.exit_code,
            detail=row.detail,
            stdout=row.stdout,
            stderr=row.stderr,
            started_at=row.started_at,
        )

    def history(
        self, task_key: str | None = None, limit: int | None = 50
    ) -> list[VerificationRecord]:
        with self.db.session() as session:
            stmt = select(Verification).order_by(Verification.id.desc())
            if task_key:
                stmt = stmt.where(Verification.task_key == task_key)
            if limit:
                stmt = stmt.limit(limit)
            rows = list(session.scalars(stmt).all())
            rows.reverse()
            return [self._view(row) for row in rows]

    def passed_for(self, task_key: str) -> bool:
        """Whether the latest attempt's checks all passed.

        Used by objective completion, which must not accept a task whose checks
        failed on its most recent attempt.
        """
        records = self.history(task_key=task_key, limit=None)
        if not records:
            return True  # nothing was checked; not evidence of failure
        latest = max(r.attempt for r in records)
        return all(
            r.status != VerificationStatus.FAILED.value
            and r.status != VerificationStatus.ERROR.value
            for r in records
            if r.attempt == latest
        )

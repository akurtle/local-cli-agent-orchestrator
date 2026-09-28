"""Verifying an agent's claim.

`status = completed` in a response block is a *claim*. This module is about the
difference between a claim and a fact.

Acceptance criteria are split honestly rather than pretended to be checkable:

    automated  a criterion that names a command, so it can be run
    review     a criterion that asks for judgement
    manual     everything else: a human has to look

Only `automated` criteria contribute to a mechanical verdict. The others are
reported so nobody mistakes "the tests passed" for "every criterion was met".
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from dataclasses import dataclass, field
from enum import StrEnum


class VerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    """Not run: denied, unavailable, or nothing configured."""
    ERROR = "error"
    """The check itself could not run, which is not the same as failing."""


class CriterionKind(StrEnum):
    AUTOMATED = "automated"
    REVIEW = "review"
    MANUAL = "manual"


# A criterion an agent or human wrote as a command, e.g. "$ pytest tests/" or
# "cmd: ruff check .".
_COMMAND_PREFIX = re.compile(r"^\s*(?:\$|cmd:|command:|run:)\s*(?P<body>.+)$", re.I)

# Words that mean a person has to form an opinion.
REVIEW_MARKERS = (
    "review",
    "readable",
    "maintainable",
    "idiomatic",
    "well named",
    "well-named",
    "sensible",
    "appropriate",
    "clean",
    "documented",
    "no obvious",
    "looks",
)


@dataclass(frozen=True)
class Criterion:
    """One acceptance criterion, classified."""

    text: str
    kind: CriterionKind
    command: list[str] = field(default_factory=list)
    """Populated only for an automated criterion."""

    @property
    def is_automated(self) -> bool:
        return self.kind is CriterionKind.AUTOMATED


def classify_criterion(text: str) -> Criterion:
    """Decide whether a criterion can be checked mechanically.

    Deliberately conservative: only an explicit command counts as automated.
    Guessing that "OAuth callback works" means running some particular command
    would be inventing a check nobody asked for.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return Criterion(text="", kind=CriterionKind.MANUAL)

    match = _COMMAND_PREFIX.match(cleaned)
    if match:
        body = match.group("body").strip()
        try:
            argv = shlex.split(body, posix=False)
        except ValueError:
            argv = body.split()
        # Strip the quotes shlex leaves behind in non-posix mode.
        argv = [part.strip('"').strip("'") for part in argv if part.strip()]
        if argv:
            return Criterion(
                text=cleaned, kind=CriterionKind.AUTOMATED, command=argv
            )

    lowered = cleaned.lower()
    if any(marker in lowered for marker in REVIEW_MARKERS):
        return Criterion(text=cleaned, kind=CriterionKind.REVIEW)

    return Criterion(text=cleaned, kind=CriterionKind.MANUAL)


def classify_criteria(items: list[str]) -> list[Criterion]:
    return [classify_criterion(item) for item in items if (item or "").strip()]


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one verification command."""

    command: list[str]
    status: VerificationStatus
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    detail: str = ""
    source: str = "config"
    """Where the check came from: config, or an acceptance criterion."""

    @property
    def spelled(self) -> str:
        return " ".join(self.command)

    @property
    def short(self) -> str:
        """The command, abbreviated for a one-line message.

        A verification command can be long (an inline script, a full path), and a
        failure message that buries the point under 300 characters of argv is
        useless to read.
        """
        text = self.spelled
        if len(text) <= 60:
            return text
        program = Path(self.command[0]).name if self.command else ""
        rest = " ".join(self.command[1:])
        abbreviated = f"{program} {rest}".strip()
        if len(abbreviated) <= 60:
            return abbreviated
        return abbreviated[:57].rstrip() + "..."

    @property
    def passed(self) -> bool:
        return self.status is VerificationStatus.PASSED


@dataclass(frozen=True)
class Verdict:
    """Whether a claim survived checking."""

    checks: list[CheckResult] = field(default_factory=list)
    manual: list[Criterion] = field(default_factory=list)
    review: list[Criterion] = field(default_factory=list)

    @property
    def failures(self) -> list[CheckResult]:
        return [
            c
            for c in self.checks
            if c.status in {VerificationStatus.FAILED, VerificationStatus.ERROR}
        ]

    @property
    def ran_anything(self) -> bool:
        return any(c.status is not VerificationStatus.SKIPPED for c in self.checks)

    @property
    def passed(self) -> bool:
        """True when nothing that ran disagreed with the claim.

        With no checks configured this is True: the orchestrator has no grounds to
        reject, and inventing one would stall every project that has no test
        command. What it does not do is *claim* the work was verified -- that is
        what `ran_anything` is for.
        """
        return not self.failures

    def render(self) -> str:
        """A short block appended to the task result."""
        lines: list[str] = []
        if not self.checks:
            lines.append("Verification: nothing configured, so nothing was checked.")
        else:
            for check in self.checks:
                marker = {
                    VerificationStatus.PASSED: "pass",
                    VerificationStatus.FAILED: "FAIL",
                    VerificationStatus.ERROR: "ERROR",
                    VerificationStatus.SKIPPED: "skip",
                }[check.status]
                suffix = f" ({check.detail})" if check.detail else ""
                lines.append(f"  [{marker}] {check.short}{suffix}")
            lines.insert(0, "Verification:")

        if self.review:
            lines.append(
                "Needs review (not mechanically checkable): "
                + "; ".join(c.text for c in self.review)
            )
        if self.manual:
            lines.append(
                "Needs a human to confirm: "
                + "; ".join(c.text for c in self.manual)
            )
        return "\n".join(lines)

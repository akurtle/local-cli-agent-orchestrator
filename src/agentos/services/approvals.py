"""Approval gates.

Which actions need a human yes, decided deterministically from config. This
module answers "is approval required here?" -- it never prompts, because asking
is a UI concern and the same rules must hold for a TUI or a non-interactive run.

Non-interactive safety: when a gate is required but nobody can answer, the
action is refused rather than assumed. A gate that silently passes because no
terminal was attached would be worse than no gate at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Gate(StrEnum):
    """Points at which a human may be required to agree."""

    MANAGER_PLAN = "manager_plan"
    """Before a proposed plan becomes real tasks."""
    MERGE = "merge"
    """Before agent branches are merged."""
    FINAL_COMPLETION = "final_completion"
    """Before an objective is declared finished."""
    DANGEROUS_COMMAND = "dangerous_command"
    """Reserved: before running a command an agent asked for."""


@dataclass(frozen=True)
class Decision:
    """Whether an action may proceed."""

    gate: Gate
    required: bool
    approved: bool
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.approved or not self.required


class ApprovalRequired(RuntimeError):
    """A gate needs a human, and there is nobody to ask."""

    # The flag that grants each gate without a prompt.
    FLAGS = {
        Gate.MANAGER_PLAN: "--auto-approve",
        Gate.MERGE: "--yes",
        Gate.FINAL_COMPLETION: "--yes",
        Gate.DANGEROUS_COMMAND: "--yes",
    }

    def __init__(self, gate: Gate) -> None:
        self.gate = gate
        flag = self.FLAGS.get(gate, "--yes")
        super().__init__(
            f"the {gate.value!r} gate requires approval, but this run is "
            f"non-interactive. Pass {flag}, or set "
            f"approvals.{gate.value}: false in your config."
        )


class ApprovalService:
    def __init__(self, approvals) -> None:
        # An ApprovalsSection, or anything with matching boolean attributes.
        self._approvals = approvals

    def is_required(self, gate: Gate) -> bool:
        return bool(getattr(self._approvals, gate.value, False))

    def required_gates(self) -> list[Gate]:
        return [gate for gate in Gate if self.is_required(gate)]

    def evaluate(
        self,
        gate: Gate,
        approved: bool | None = None,
        interactive: bool = True,
    ) -> Decision:
        """Decide whether an action may proceed.

        `approved` is the answer already obtained, if any: True for an explicit
        yes (including an --auto-approve flag), False for an explicit no, None for
        "not asked".
        """
        required = self.is_required(gate)

        if not required:
            return Decision(
                gate=gate, required=False, approved=True, reason="no gate configured"
            )

        if approved is True:
            return Decision(gate=gate, required=True, approved=True, reason="approved")
        if approved is False:
            return Decision(gate=gate, required=True, approved=False, reason="declined")

        if not interactive:
            # Refuse rather than assume. A gate that passes because nobody was
            # watching is not a gate.
            raise ApprovalRequired(gate)

        return Decision(
            gate=gate, required=True, approved=False, reason="not yet answered"
        )

    def describe(self, gate: Gate) -> str:
        return "required" if self.is_required(gate) else "not required"

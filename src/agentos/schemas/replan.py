"""The manager's proposed changes to an existing task graph.

Replanning is riskier than planning: the graph already has history, agents may be
mid-flight, and completed work must not be rewritten. So the manager returns
*operations* rather than a whole new plan, and every operation is validated
against the live graph before any of it is applied.

What Python refuses, regardless of what the manager asks for:

  * touching a task that has already completed
  * cancelling work that is currently running
  * referencing a task or agent that does not exist
  * creating a cycle
  * an unbounded number of changes
"""

from __future__ import annotations

import json
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

REPLAN_BEGIN = "<<<REPLAN"
REPLAN_END = "REPLAN>>>"

_BLOCK_RE = re.compile(
    re.escape(REPLAN_BEGIN) + r"(?P<body>.*?)" + re.escape(REPLAN_END), re.DOTALL
)

# A corrective plan should be small. A manager asking for thirty changes has
# misunderstood the situation.
MAX_OPERATIONS = 12


class OperationType(StrEnum):
    CREATE_TASK = "create_task"
    ADD_DEPENDENCY = "add_dependency"
    REMOVE_DEPENDENCY = "remove_dependency"
    CANCEL_TASK = "cancel_task"
    RETRY_TASK = "retry_task"
    REASSIGN_TASK = "reassign_task"


class ReplanTrigger(StrEnum):
    """Why a replan was asked for."""

    TASK_FAILED = "task_failed"
    BLOCKER = "blocker"
    REVIEW_REJECTION = "review_rejection"
    VERIFICATION_FAILED = "verification_failed"
    CONFLICT = "conflict"
    MANUAL = "manual"


class Operation(BaseModel):
    """One requested change. Fields used depend on `type`."""

    model_config = ConfigDict(extra="ignore")

    type: OperationType
    temp_id: str | None = Field(default=None, max_length=64)
    """For create_task, so later operations can reference it."""
    task: str | None = Field(default=None, max_length=64)
    """An existing task key, or a temp_id defined earlier in this plan."""
    depends_on: str | None = Field(default=None, max_length=64)
    agent: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=200)
    description: str = Field(default="", max_length=20000)
    acceptance_criteria: list[str] = Field(default_factory=list)
    reason: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def _required_fields_per_type(self) -> "Operation":
        if self.type is OperationType.CREATE_TASK:
            if not (self.title or "").strip():
                raise ValueError("create_task requires a title")
            if not (self.agent or "").strip():
                raise ValueError("create_task requires an agent")
        elif self.type in {
            OperationType.ADD_DEPENDENCY,
            OperationType.REMOVE_DEPENDENCY,
        }:
            if not self.task or not self.depends_on:
                raise ValueError(
                    f"{self.type.value} requires task and depends_on"
                )
        elif self.type is OperationType.REASSIGN_TASK:
            if not self.task or not self.agent:
                raise ValueError("reassign_task requires task and agent")
        elif not self.task:
            raise ValueError(f"{self.type.value} requires a task")
        return self

    def describe(self) -> str:
        if self.type is OperationType.CREATE_TASK:
            return f"create {self.temp_id or self.title!r} for {self.agent}"
        if self.type is OperationType.ADD_DEPENDENCY:
            return f"{self.task} depends on {self.depends_on}"
        if self.type is OperationType.REMOVE_DEPENDENCY:
            return f"{self.task} no longer depends on {self.depends_on}"
        if self.type is OperationType.REASSIGN_TASK:
            return f"reassign {self.task} to {self.agent}"
        return f"{self.type.value.replace('_', ' ')} {self.task}"


class ReplanProposal(BaseModel):
    """What the manager wants to change."""

    model_config = ConfigDict(extra="ignore")

    assessment: str = Field(default="", max_length=4000)
    """The manager's reading of what went wrong."""
    operations: list[Operation] = Field(default_factory=list)


class ReplanParseError(ValueError):
    """The manager's reply contained no usable set of operations."""


def extract_replan_block(text: str) -> str | None:
    matches = _BLOCK_RE.findall(text or "")
    return matches[-1].strip() if matches else None


def parse_replan(text: str) -> ReplanProposal:
    """Parse and schema-check a replan. Graph validation happens in the service."""
    block = extract_replan_block(text)
    if block is None:
        raise ReplanParseError(
            f"no {REPLAN_BEGIN}...{REPLAN_END} block found in the manager's reply"
        )
    block = re.sub(r"^```(?:json)?\s*|\s*```$", "", block.strip())
    try:
        payload = json.loads(block)
    except json.JSONDecodeError as exc:
        raise ReplanParseError(f"replan block is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReplanParseError(
            f"replan must be a JSON object, got {type(payload).__name__}"
        )
    try:
        return ReplanProposal.model_validate(payload)
    except Exception as exc:
        raise ReplanParseError(f"replan failed validation: {exc}") from exc

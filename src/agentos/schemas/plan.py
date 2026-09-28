"""The manager's proposed plan.

The manager proposes; Python decides. This schema is the only shape a plan may
take, and `services/planner.py` validates every reference in it against the real
roster and graph before anything is persisted. The manager never writes to the
database.

Temporary ids exist because the manager cannot know real task keys in advance.
They are local to one plan and are translated into persisted keys on approval.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

PLAN_BEGIN = "<<<PLAN"
PLAN_END = "PLAN>>>"

_BLOCK_RE = re.compile(
    re.escape(PLAN_BEGIN) + r"(?P<body>.*?)" + re.escape(PLAN_END), re.DOTALL
)

# A plan larger than this is almost certainly the manager over-decomposing.
MAX_PLANNED_TASKS = 25


class PlannedTask(BaseModel):
    model_config = ConfigDict(extra="ignore")

    temp_id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    assigned_agent: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=20000)
    acceptance_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    """Other tasks' temp_ids."""

    @field_validator("temp_id")
    @classmethod
    def _sane_temp_id(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("temp_id must not be blank")
        return cleaned

    @field_validator("title")
    @classmethod
    def _sane_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("title must not be blank")
        return cleaned


class ManagerPlan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    objective: str = Field(default="", max_length=2000)
    tasks: list[PlannedTask] = Field(default_factory=list)
    notes: str = Field(default="", max_length=4000)


class PlanParseError(ValueError):
    """The manager's reply contained no usable plan."""


def extract_plan_block(text: str) -> str | None:
    """Return the raw JSON between the plan delimiters, last block winning."""
    matches = _BLOCK_RE.findall(text or "")
    if not matches:
        return None
    return matches[-1].strip()


def parse_manager_plan(text: str) -> ManagerPlan:
    """Parse and schema-check a plan. Semantic validation happens in the planner."""
    block = extract_plan_block(text)
    if block is None:
        raise PlanParseError(
            f"no {PLAN_BEGIN}...{PLAN_END} block found in the manager's reply"
        )
    block = re.sub(r"^```(?:json)?\s*|\s*```$", "", block.strip())
    try:
        payload = json.loads(block)
    except json.JSONDecodeError as exc:
        raise PlanParseError(f"plan block is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise PlanParseError(
            f"plan must be a JSON object, got {type(payload).__name__}"
        )
    try:
        return ManagerPlan.model_validate(payload)
    except Exception as exc:
        raise PlanParseError(f"plan failed validation: {exc}") from exc

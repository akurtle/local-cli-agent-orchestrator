"""The structured contract an agent must satisfy when it finishes a task.

Agents emit prose *and* a JSON block. We parse the JSON block, validate it, and
only then mutate application state. Free-form prose is kept for humans; it never
drives scheduling.

Security note: nothing in here is executed. `files_changed` is treated as a
claim to be verified against `git diff`, never as an instruction, and no field
is ever passed to a shell.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agentos.schemas.enums import MessageType

# Agents wrap their JSON in this fence so we can find it unambiguously even when
# the model surrounds it with commentary.
RESPONSE_BEGIN = "<<<AGENT_RESPONSE"
RESPONSE_END = "AGENT_RESPONSE>>>"

_BLOCK_RE = re.compile(
    re.escape(RESPONSE_BEGIN) + r"(?P<body>.*?)" + re.escape(RESPONSE_END),
    re.DOTALL,
)


class OutboundMessage(BaseModel):
    """A message an agent wants delivered to another agent."""

    model_config = ConfigDict(extra="ignore")

    to: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=8000)
    message_type: MessageType = MessageType.INFO


class RequestedTask(BaseModel):
    """Work an agent wants queued for somebody else.

    An agent may name either a specific agent or a role. Either way the
    orchestrator resolves it against the real roster and rejects anything that
    does not exist; an agent cannot conjure a worker into being.
    """

    model_config = ConfigDict(extra="ignore")

    agent_name: str | None = Field(default=None, max_length=64)
    agent_role: str | None = Field(default=None, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20000)
    acceptance_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _needs_a_recipient(self) -> "RequestedTask":
        if not (self.agent_name or self.agent_role):
            raise ValueError("requested task must name an agent_name or agent_role")
        return self


class AgentResponse(BaseModel):
    """Validated result of one agent turn."""

    model_config = ConfigDict(extra="ignore")

    status: str = Field(default="completed")
    summary: str = Field(default="", max_length=20000)
    files_changed: list[str] = Field(default_factory=list)
    messages: list[OutboundMessage] = Field(default_factory=list)
    requested_tasks: list[RequestedTask] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)

    # Optional, and used to build handoffs and memories. Absent in an older
    # response, which stays valid: these default to empty.
    interfaces: list[str] = Field(default_factory=list)
    """Contracts other agents will consume, e.g. "POST /api/auth/google"."""
    decisions: list[str] = Field(default_factory=list)
    """Choices that should not be silently revisited later."""
    warnings: list[str] = Field(default_factory=list)
    """Traps the next agent should know about."""

    @field_validator("status")
    @classmethod
    def _known_status(cls, value: str) -> str:
        allowed = {"completed", "failed", "blocked", "needs_review", "in_progress"}
        normalised = value.strip().lower()
        if normalised not in allowed:
            raise ValueError(f"status must be one of {sorted(allowed)}, got {value!r}")
        return normalised

    @property
    def is_blocked(self) -> bool:
        return self.status == "blocked" or bool(self.blockers)


class ResponseParseError(ValueError):
    """Raised when agent output contains no usable response block."""


def extract_response_block(text: str) -> str | None:
    """Return the raw JSON text between the delimiters, if present.

    Uses the *last* block so that an agent restating the format earlier in its
    reasoning does not win over its actual answer.
    """
    matches = _BLOCK_RE.findall(text or "")
    if not matches:
        return None
    return matches[-1].strip()


def parse_agent_response(text: str) -> AgentResponse:
    """Parse and validate an agent turn. Raises ResponseParseError on failure."""
    block = extract_response_block(text)
    if block is None:
        raise ResponseParseError(
            f"no {RESPONSE_BEGIN}...{RESPONSE_END} block found in agent output"
        )
    # Tolerate a ```json fence inside the delimiters; models add them habitually.
    block = re.sub(r"^```(?:json)?\s*|\s*```$", "", block.strip())
    try:
        payload = json.loads(block)
    except json.JSONDecodeError as exc:
        raise ResponseParseError(f"response block is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ResponseParseError(
            f"response block must be a JSON object, got {type(payload).__name__}"
        )
    try:
        return AgentResponse.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError
        raise ResponseParseError(f"response failed validation: {exc}") from exc

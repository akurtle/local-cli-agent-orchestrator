"""YAML project configuration.

The config is validated with pydantic so a typo fails loudly at load time
rather than halfway through an orchestration run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

DEFAULT_ROLES = ("manager", "backend", "frontend", "qa", "reviewer")


class ProjectSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "Untitled Project"
    description: str = ""


class OrchestratorSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrent_agents: int = Field(default=3, ge=1, le=32)
    default_timeout_seconds: float = Field(default=900.0, gt=0)
    max_task_retries: int = Field(default=1, ge=0, le=10)
    manager_role: str = Field(default="manager", min_length=1)
    """Which role plans objectives. Rename it if your roster uses another word."""


class RuntimeSection(BaseModel):
    """Which external agent CLI backs the agents.

    Only "claude" is implemented; the field exists so other runtimes can be
    added without reshaping the config.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = "claude"
    executable: str | None = None
    """Explicit path to the CLI. When None we resolve it from PATH."""
    model: str | None = None
    extra_args: list[str] = Field(default_factory=list)


class ContextSection(BaseModel):
    """How much history an agent carries, and when its session is replaced.

    A persistent Claude session accumulates context indefinitely, which costs
    more and reasons worse. Rotation caps that, and persisted memory is what
    survives the boundary.
    """

    model_config = ConfigDict(extra="forbid")

    max_tasks_per_session: int = Field(default=8, ge=1, le=200)
    rotate_on_objective_change: bool = True
    summarise_on_rotation: bool = True
    """Ask the agent for a short summary before replacing its session."""
    budgets: dict[str, int] = Field(default_factory=dict)
    """Per-layer character budgets; see services/context.DEFAULT_BUDGETS."""


class ApprovalsSection(BaseModel):
    """Which actions need a human yes.

    Defaults are deliberately asymmetric: the two gates that change the
    repository or create work are on, and declaring an objective finished is off,
    because that is computed from task state and adding a prompt there would just
    be noise.
    """

    model_config = ConfigDict(extra="forbid")

    manager_plan: bool = True
    merge: bool = True
    final_completion: bool = False
    dangerous_command: bool = True


class AgentSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    description: str = ""
    model: str | None = None
    prompt: str | None = None
    """Path to a custom role brief, relative to the project root."""
    worktree: bool = False
    """Whether this agent gets an isolated git worktree (phase 6)."""


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectSection = Field(default_factory=ProjectSection)
    orchestrator: OrchestratorSection = Field(default_factory=OrchestratorSection)
    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
    approvals: ApprovalsSection = Field(default_factory=ApprovalsSection)
    context: ContextSection = Field(default_factory=ContextSection)
    agents: dict[str, AgentSection] = Field(default_factory=dict)

    @field_validator("runtime", mode="before")
    @classmethod
    def _accept_runtime_shorthand(cls, value: object) -> object:
        """Allow `runtime: claude` as well as `runtime: {name: claude}`.

        The scalar form is what most configs want, and rejecting it would be a
        pointless papercut.
        """
        if isinstance(value, str):
            return {"name": value}
        return value

    @field_validator("agents")
    @classmethod
    def _non_empty_names(
        cls, value: dict[str, AgentSection]
    ) -> dict[str, AgentSection]:
        for name in value:
            if not name or not name.strip():
                raise ValueError("agent names must be non-empty")
        return value

    def role_names(self) -> set[str]:
        return {a.role for a in self.agents.values()}

    def agents_with_role(self, role: str) -> list[str]:
        return sorted(
            name for name, section in self.agents.items() if section.role == role
        )


class ConfigError(RuntimeError):
    """Raised when a config file is missing or invalid."""


def load_config(path: Path) -> Config:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    try:
        return Config.model_validate(raw)
    except Exception as exc:
        raise ConfigError(f"invalid configuration in {path}: {exc}") from exc


def default_config_yaml(project_name: str) -> str:
    """The starter config written by `agentctl init`.

    The five default agents are a convenience, not a requirement: roles are
    arbitrary strings, and an unknown role gets a neutral prompt.
    """
    agent_lines = "\n".join(
        f"  {role}:\n    role: {role}" for role in DEFAULT_ROLES
    )
    return f"""project:
  name: {project_name}
  description: ""

orchestrator:
  max_concurrent_agents: 3
  default_timeout_seconds: 900
  max_task_retries: 1

# Only "claude" is implemented today. The section exists so other agent CLIs
# (codex, gemini, ollama) can be plugged in later without config churn.
# How much context an agent carries between tasks.
context:
  max_tasks_per_session: 8
  rotate_on_objective_change: true

# Which actions need a human yes before they happen.
approvals:
  manager_plan: true
  merge: true
  final_completion: false

runtime:
  name: claude
  # executable: C:/path/to/claude.exe   # optional; resolved from PATH if unset
  # model: claude-sonnet-5

agents:
{agent_lines}
"""

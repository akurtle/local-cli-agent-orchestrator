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


class AgentSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    description: str = ""
    model: str | None = None
    worktree: bool = False
    """Whether this agent gets an isolated git worktree (phase 6)."""


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: ProjectSection = Field(default_factory=ProjectSection)
    orchestrator: OrchestratorSection = Field(default_factory=OrchestratorSection)
    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
    agents: dict[str, AgentSection] = Field(default_factory=dict)

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
    """The starter config written by `agentctl init`."""
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
runtime:
  name: claude
  # executable: C:/path/to/claude.exe   # optional; resolved from PATH if unset
  # model: claude-sonnet-5

agents:
{agent_lines}
"""

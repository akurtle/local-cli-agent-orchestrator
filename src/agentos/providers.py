"""Model providers, and which model each agent gets.

Two ideas live here:

  * **Tiers.** Planning is where a mistake costs the most -- every task inherits
    the plan -- so the manager gets the provider's strongest model, and the
    agents doing the work get a cheaper, faster one. An agent can override its
    tier or name a model outright.
  * **The active provider.** `runtime.name` in agentos.yaml is the default; the
    dashboard (or `agentctl provider`) can override it per project. The override
    is a one-line file under .agentos/ rather than an edit to agentos.yaml,
    because rewriting a user's YAML would lose their comments and layout.

A model name belongs to its provider, so every model named in agentos.yaml is
set aside while a different provider is active; the tier defaults take over.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from agentos.config import Config
from agentos.paths import ProjectPaths

Tier = Literal["planning", "execution"]
PLANNING: Tier = "planning"
EXECUTION: Tier = "execution"

OVERRIDE_FILENAME = "provider"


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    label: str
    command: str
    """The CLI's name on PATH."""
    planning: str
    execution: str


# Defaults when agentos.yaml names no model. The strongest model for planning,
# a fast one for doing the work.
PROVIDERS: dict[str, ProviderInfo] = {
    "claude": ProviderInfo(
        name="claude",
        label="Claude",
        command="claude",
        planning="claude-opus-5-5",
        execution="claude-sonnet-5",
    ),
    "codex": ProviderInfo(
        name="codex",
        label="Codex",
        command="codex",
        planning="gpt-6-astra",
        execution="gpt-6-luna",
    ),
}


def known_providers() -> list[str]:
    return list(PROVIDERS)


# ------------------------------------------------------------------- the override


def override_path(paths: ProjectPaths) -> Path:
    return paths.state_dir / OVERRIDE_FILENAME


def read_override(paths: ProjectPaths) -> str | None:
    """The provider chosen in the dashboard, if any and if it is one we know."""
    try:
        value = override_path(paths).read_text(encoding="utf-8").strip().lower()
    except OSError:
        return None
    return value if value in PROVIDERS else None


def write_override(paths: ProjectPaths, name: str | None) -> None:
    """Choose a provider for this project, or None to go back to agentos.yaml."""
    target = override_path(paths)
    if name is None:
        target.unlink(missing_ok=True)
        return
    if name not in PROVIDERS:
        raise ValueError(f"unknown provider {name!r}; known: {known_providers()}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(name + "\n", encoding="utf-8")


def apply_override(config: Config, name: str | None) -> Config:
    """The config as the given provider sees it.

    Anything in the file that is specific to the file's own provider -- its
    executable, extra arguments and every model name -- is dropped when another
    provider is active, so Codex is never handed a Claude model or path.
    """
    if not name or name == config.runtime.name:
        return config
    runtime = config.runtime.model_copy(
        update={"name": name, "executable": None, "extra_args": [], "model": None}
    )
    agents = {
        agent: section.model_copy(update={"model": None})
        for agent, section in config.agents.items()
    }
    return config.model_copy(update={"runtime": runtime, "agents": agents})


def effective_config(config: Config, paths: ProjectPaths) -> Config:
    return apply_override(config, read_override(paths))


# ------------------------------------------------------------------------ models


def tier_for(config: Config, agent: str) -> Tier:
    section = config.agents.get(agent)
    if section is not None and section.tier:
        return section.tier
    manager_role = config.orchestrator.manager_role
    if agent == manager_role or (section is not None and section.role == manager_role):
        return PLANNING
    return EXECUTION


def tier_model(config: Config, tier: Tier, provider: str | None = None) -> str | None:
    """A provider's model for a tier: agentos.yaml first, then the defaults.

    `runtime.model`, the older single-model setting, sits between the two, so a
    project that pinned one model keeps it until it configures tiers.
    """
    name = provider or config.runtime.name
    configured = config.providers.get(name)
    if configured is not None and getattr(configured, tier):
        return getattr(configured, tier)
    if name == config.runtime.name and config.runtime.model:
        return config.runtime.model
    info = PROVIDERS.get(name)
    return getattr(info, tier) if info else None


def resolve_model(config: Config, agent: str) -> str | None:
    """The model one agent runs on: its own `model`, else its tier's."""
    section = config.agents.get(agent)
    if section is not None and section.model:
        return section.model
    return tier_model(config, tier_for(config, agent))


# ------------------------------------------------------------------------ status


@dataclass(frozen=True)
class ProviderStatus:
    name: str
    label: str
    installed: bool
    active: bool
    planning: str | None
    execution: str | None


def provider_statuses(config: Config, active: str) -> list[ProviderStatus]:
    """Every provider, whether its CLI is installed, and the models it would use.

    `config` is the file's config; each provider's models are computed as that
    provider would see them.
    """
    statuses = []
    for name, info in PROVIDERS.items():
        view = apply_override(config, name)
        section = config.providers.get(name)
        executable = section.executable if section and section.executable else None
        if name == config.runtime.name and config.runtime.executable:
            executable = config.runtime.executable
        installed = bool(
            shutil.which(executable or info.command)
            or (executable and Path(executable).expanduser().is_file())
        )
        statuses.append(
            ProviderStatus(
                name=name,
                label=info.label,
                installed=installed,
                active=name == active,
                planning=tier_model(view, PLANNING, name),
                execution=tier_model(view, EXECUTION, name),
            )
        )
    return statuses

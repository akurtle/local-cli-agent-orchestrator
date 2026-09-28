"""Shared CLI plumbing: locate the project, load config, open the database."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer

from agentos.branding import CLI_NAME, CONFIG_FILENAME
from agentos.config import Config, ConfigError, load_config
from agentos.db.session import Database
from agentos.paths import ProjectPaths, find_project_root
from agentos.runtime.base import RuntimeNotAvailable
from agentos.runtime.registry import build_runtime
from agentos.services.agents import AgentService


@dataclass
class AppContext:
    paths: ProjectPaths
    config: Config
    db: Database


def load_context(start: Path | None = None) -> AppContext:
    """Resolve the project or exit with a helpful message."""
    root = find_project_root(start)
    if root is None:
        typer.secho(
            f"No {CONFIG_FILENAME} found in this directory or any parent.\n"
            f"Run `{CLI_NAME} init` to create one.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    paths = ProjectPaths(root=root)
    try:
        config = load_config(paths.config_file)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from exc

    paths.ensure()
    db = Database(paths.db_file)
    db.create_all()
    return AppContext(paths=paths, config=config, db=db)


def build_agent_service(ctx: AppContext, preflight: bool = True) -> AgentService:
    """Construct the agent service, failing early if the runtime is unusable."""
    try:
        runtime = build_runtime(ctx.config)
        if preflight:
            runtime.preflight()
    except RuntimeNotAvailable as exc:
        typer.secho(f"Runtime unavailable: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    return AgentService(
        db=ctx.db,
        config=ctx.config,
        runtime=runtime,
        project_root=ctx.paths.root,
    )

"""`agentctl provider`: which model provider this project uses."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from agentos.cli.context import load_context
from agentos.config import load_config
from agentos.providers import (
    known_providers,
    provider_statuses,
    read_override,
    tier_for,
    write_override,
)

console = Console()


def provider_command(name: str | None = None, reset: bool = False) -> None:
    """Show the providers, or switch this project to one."""
    ctx = load_context()
    file_config = load_config(ctx.paths.config_file)

    if reset:
        write_override(ctx.paths, None)
        console.print(
            f"Back to agentos.yaml: [bold]{file_config.runtime.name}[/]."
        )
    elif name:
        chosen = name.strip().lower()
        if chosen not in known_providers():
            console.print(
                f"[red]Unknown provider {name!r}.[/] Known: {', '.join(known_providers())}"
            )
            ctx.db.dispose()
            raise typer.Exit(code=2)
        write_override(ctx.paths, chosen)
        console.print(f"Switched this project to [bold]{chosen}[/].")

    active = read_override(ctx.paths) or file_config.runtime.name
    table = Table(show_header=True, header_style="bold")
    for column in ("", "Provider", "Installed", "Planning (manager)", "Execution (others)"):
        table.add_column(column)
    for status in provider_statuses(file_config, active):
        table.add_row(
            "[green]>[/]" if status.active else "",
            f"[bold]{status.label}[/]" if status.active else status.label,
            "yes" if status.installed else "[red]no[/]",
            status.planning or "(CLI default)",
            status.execution or "(CLI default)",
        )
    console.print(table)

    planners = sorted(a for a in file_config.agents if tier_for(file_config, a) == "planning")
    if planners:
        console.print(f"[dim]Planning tier: {', '.join(planners)}. Everyone else: execution.[/]")
    if name or reset:
        console.print(
            "[dim]Takes effect on the next `agentctl work` or `agentctl run`. "
            "Agents start new sessions on the new provider.[/]"
        )
    ctx.db.dispose()

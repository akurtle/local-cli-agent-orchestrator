"""`agentctl agents` and the `agentctl agent ...` command group.

These commands only call AgentService. They never touch the database or the
runtime directly.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agentos.cli.context import build_agent_service, load_context
from agentos.cli.glyphs import glyph, safe
from agentos.repositories.agents import AgentNotFound
from agentos.schemas.enums import AgentStatus
from agentos.services.agents import AgentBusy, AgentPaused

console = Console()

agent_app = typer.Typer(
    name="agent", help="Inspect and invoke individual agents.", no_args_is_help=True
)

STATUS_COLOURS = {
    AgentStatus.IDLE: "green",
    AgentStatus.WORKING: "cyan",
    AgentStatus.WAITING: "blue",
    AgentStatus.BLOCKED: "yellow",
    AgentStatus.FAILED: "red",
    AgentStatus.PAUSED: "magenta",
    AgentStatus.OFFLINE: "bright_black",
}

# Filled when the agent is busy, hollow otherwise -- matches the task glyphs.
BUSY_STATUSES = {AgentStatus.WORKING, AgentStatus.WAITING}


def status_text(status: AgentStatus) -> str:
    colour = STATUS_COLOURS.get(status, "white")
    marker = glyph("filled" if status in BUSY_STATUSES else "hollow")
    return f"[{colour}]{marker} {status.value}[/]"


def agents_table(views) -> Table:
    table = Table(show_header=True, header_style="bold")
    table.add_column("Agent")
    table.add_column("Role")
    table.add_column("Status")
    table.add_column("Task")
    table.add_column("Session")
    for view in views:
        table.add_row(
            view.name,
            view.role,
            status_text(view.status),
            str(view.current_task_id) if view.current_task_id else "-",
            view.short_session,
        )
    return table


def list_agents_command() -> None:
    """Show the agent roster, creating any agents missing from the database."""
    ctx = load_context()
    service = build_agent_service(ctx, preflight=False)
    views, created = service.sync_from_config()

    if not views:
        console.print(
            "[yellow]No agents configured.[/] Add an `agents:` section to your config."
        )
        ctx.db.dispose()
        return

    console.print(f"[bold]{ctx.config.project.name}[/]")
    console.print(agents_table(views))
    if created:
        console.print(f"[dim]Registered new agents: {', '.join(created)}[/]")
    ctx.db.dispose()


@agent_app.command("show")
def show_agent(
    name: Annotated[str, typer.Argument(help="Agent name.")],
    prompt: Annotated[
        bool, typer.Option("--prompt", help="Also print the assembled system prompt.")
    ] = False,
) -> None:
    """Show one agent in detail."""
    ctx = load_context()
    service = build_agent_service(ctx, preflight=False)
    try:
        agent = service.get_agent(name)
    except AgentNotFound as exc:
        console.print(f"[red]{exc}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1) from exc

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column(overflow="fold")
    grid.add_row("name", agent.name)
    grid.add_row("role", agent.role)
    grid.add_row("description", agent.description or "-")
    grid.add_row("runtime", agent.runtime)
    grid.add_row("status", status_text(agent.status))
    grid.add_row("model", agent.model or "(runtime default)")
    grid.add_row("session", agent.session_id or "[dim](none yet)[/]")
    grid.add_row("current task", str(agent.current_task_id or "-"))
    grid.add_row("worktree", agent.worktree_path or "-")
    grid.add_row("branch", agent.branch_name or "-")
    grid.add_row("created", str(agent.created_at))
    grid.add_row("updated", str(agent.updated_at))
    console.print(grid)

    if prompt:
        console.print(
            Panel(
                service.system_prompt_for(agent),
                title=f"system prompt for {agent.name}",
                border_style="blue",
            )
        )
    ctx.db.dispose()


@agent_app.command("run")
def run_agent(
    name: Annotated[str, typer.Argument(help="Agent name.")],
    prompt: Annotated[str, typer.Argument(help="What to ask the agent.")],
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Seconds before giving up.")
    ] = None,
    fresh: Annotated[
        bool,
        typer.Option("--fresh", help="Discard the stored session and start a new one."),
    ] = False,
    show_events: Annotated[
        bool, typer.Option("--events", help="Print stream events as they arrive.")
    ] = False,
) -> None:
    """Invoke one agent, resuming its persistent Claude session."""
    ctx = load_context()
    service = build_agent_service(ctx)

    try:
        if fresh:
            service.reset_session(name)
        agent = service.get_agent(name)
    except AgentNotFound as exc:
        console.print(f"[red]{exc}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1) from exc

    mode = "resuming" if agent.has_session else "starting new session"
    console.print(f"[dim]{agent.name} ({agent.role}) - {mode}[/]")

    def on_event(event) -> None:
        if show_events:
            label = f"/{event.subtype}" if event.subtype else ""
            console.print(f"[dim]event[/] {event.type}{label}")

    try:
        with console.status(f"{agent.name} working..."):
            outcome = asyncio.run(
                service.run_agent(
                    name,
                    prompt,
                    timeout_seconds=timeout,
                    on_event=on_event,
                )
            )
    except (AgentBusy, AgentPaused) as exc:
        console.print(f"[yellow]{exc}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1) from exc

    colour = "green" if outcome.ok else "red"
    console.print(
        Panel(
            safe(outcome.text) or "[dim](no text returned)[/]",
            title=f"[{colour}]{agent.name}: {'ok' if outcome.ok else 'failed'}[/]",
            border_style=colour,
        )
    )

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold")
    summary.add_column()
    summary.add_row("run id", str(outcome.run_id))
    summary.add_row("status", outcome.agent.status.value)
    summary.add_row("session", outcome.session_id or "-")
    summary.add_row("resumed", "yes" if outcome.resumed else "no")
    if outcome.session_restarted:
        summary.add_row(
            "session", "[yellow]stored session was gone; started a fresh one[/]"
        )
    if outcome.duration_seconds:
        summary.add_row("duration", f"{outcome.duration_seconds:.2f}s")
    if outcome.cost_usd is not None:
        summary.add_row("cost", f"${outcome.cost_usd:.4f}")
    if outcome.error:
        summary.add_row("error", f"[red]{outcome.error}[/]")
    console.print(summary)

    ctx.db.dispose()
    if not outcome.ok:
        raise typer.Exit(code=1)


def pause_agent_command(name: str, paused: bool) -> None:
    """Pause or resume one agent.

    A paused agent is skipped by the scheduler; its tasks stay ready and are
    reported as skipped rather than failed.
    """
    ctx = load_context()
    service = build_agent_service(ctx, preflight=False)
    try:
        agent = service.pause(name) if paused else service.unpause(name)
    except AgentNotFound as exc:
        console.print(f"[red]{exc}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1) from exc
    except AgentBusy as exc:
        console.print(f"[yellow]{exc}[/]")
        console.print("[dim]Wait for the task to finish, or cancel it first.[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1) from exc

    console.print(f"{agent.name} -> {status_text(agent.status)}")
    ctx.db.dispose()

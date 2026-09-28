"""`agentctl timeline`, `agentctl watch`, `agentctl stats`."""

from __future__ import annotations

import time
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from agentos.cli.context import load_context
from agentos.cli.glyphs import literal
from agentos.services.events import EventBus, EventType
from agentos.services.metrics import MetricsService

console = Console()

# How each category is coloured, so a timeline can be skimmed.
CATEGORY_STYLE = {
    "objective": "blue",
    "task": "cyan",
    "agent": "magenta",
    "message": "bright_magenta",
    "handoff": "bright_blue",
    "memory": "bright_black",
    "run": "white",
    "command": "yellow",
    "git": "green",
    "approval": "yellow",
    "capability": "red",
    "scheduler": "bright_black",
}

# Events that mean something went wrong, highlighted regardless of category.
PROBLEM_TYPES = {
    EventType.TASK_FAILED,
    EventType.TASK_BLOCKED,
    EventType.OBJECTIVE_FAILED,
    EventType.COMMAND_DENIED,
    EventType.CAPABILITY_DENIED,
    EventType.APPROVAL_DENIED,
    EventType.GIT_CONFLICT,
}

WATCH_INTERVAL = 1.0


def _bus(ctx) -> EventBus:
    return EventBus(ctx.db)


def render_event(event) -> str:
    style = CATEGORY_STYLE.get(event.category, "white")
    if event.type in PROBLEM_TYPES:
        style = "red"
    when = event.at.strftime("%H:%M:%S") if event.at else "--:--:--"

    subject = event.task_key or event.agent or (
        f"objective {event.objective_id}" if event.objective_id else ""
    )
    line = f"[dim]{when}[/] [{style}]{event.type.value:26}[/]"
    if subject:
        line += f" [bold]{subject:12}[/]"
    if event.summary:
        line += f" {literal(event.summary)}"
    return line


def timeline_command(
    agent: str | None = None,
    task: str | None = None,
    objective: int | None = None,
    category: str | None = None,
    limit: int = 100,
) -> None:
    """Show what happened, oldest first."""
    ctx = load_context()
    bus = _bus(ctx)

    events = bus.history(
        agent=agent,
        task_key=task,
        objective_id=objective,
        category=category,
        limit=limit,
    )
    if not events:
        console.print(
            "[dim]No events recorded.[/] Run `agentctl work` to produce some."
        )
        ctx.db.dispose()
        return

    filters = [
        f"agent={agent}" if agent else "",
        f"task={task}" if task else "",
        f"objective={objective}" if objective is not None else "",
        f"category={category}" if category else "",
    ]
    active = ", ".join(f for f in filters if f)
    console.print(f"[bold]Timeline[/]{f'  ({active})' if active else ''}")

    for event in events:
        console.print(render_event(event))

    console.print(f"\n[dim]{len(events)} event(s)[/]")
    ctx.db.dispose()


def watch_command(interval: float = WATCH_INTERVAL) -> None:
    """Print new events as they appear.

    Polls the database rather than subscribing in-process, because the scheduler
    usually runs in a different terminal. That is the limitation: an event is seen
    within one poll, not instantly, and there is no daemon to subscribe to.
    """
    ctx = load_context()
    bus = _bus(ctx)
    cursor = bus.latest_id()

    console.print(
        f"[dim]watching for new events (polling every {max(0.2, interval):g}s); "
        "Ctrl+C to stop[/]"
    )
    console.print(f"[dim]starting after event #{cursor}[/]")
    try:
        while True:
            fresh = bus.history(since_id=cursor, limit=200)
            for event in fresh:
                console.print(render_event(event))
                if event.id is not None:
                    cursor = max(cursor, event.id)
            time.sleep(max(0.2, interval))
    except KeyboardInterrupt:
        console.print("[dim]stopped watching[/]")
    finally:
        ctx.db.dispose()


def stats_command() -> None:
    """Show counts and durations derived from stored rows."""
    ctx = load_context()
    metrics = MetricsService(ctx.db).collect()

    if not metrics.tasks_total and not metrics.runs:
        console.print("[dim]Nothing recorded yet.[/]")
        ctx.db.dispose()
        return

    overview = Table.grid(padding=(0, 2))
    overview.add_column(style="bold")
    overview.add_column()
    overview.add_row("tasks", str(metrics.tasks_total))
    for status, count in sorted(metrics.tasks_by_status.items()):
        overview.add_row(f"  {status}", str(count))
    if metrics.mean_task_seconds is not None:
        overview.add_row("mean task time", f"{metrics.mean_task_seconds:.1f}s")
    overview.add_row("retries", str(metrics.retries))
    overview.add_row("", "")
    overview.add_row("agent invocations", str(metrics.runs))
    overview.add_row("  failed", str(metrics.run_failures))
    if metrics.mean_run_seconds is not None:
        overview.add_row("  mean duration", f"{metrics.mean_run_seconds:.1f}s")
    overview.add_row("  total busy time", f"{metrics.run_seconds:.0f}s")
    if metrics.cost_usd:
        overview.add_row("  reported cost", f"${metrics.cost_usd:.4f}")
    overview.add_row("", "")
    overview.add_row("commands run", str(metrics.commands))
    overview.add_row("  denied", str(metrics.commands_denied))
    overview.add_row("capability denials", str(metrics.capability_denials))
    overview.add_row("events", str(metrics.events))
    console.print(overview)

    if metrics.agents:
        table = Table(show_header=True, header_style="bold")
        table.add_column("Agent")
        table.add_column("Runs", justify="right")
        table.add_column("Failed", justify="right")
        table.add_column("Success", justify="right")
        table.add_column("Busy", justify="right")
        table.add_column("Commands", justify="right")
        table.add_column("Denials", justify="right")
        for entry in metrics.agents:
            rate = (
                f"{entry.success_rate * 100:.0f}%"
                if entry.success_rate is not None
                else "-"
            )
            table.add_row(
                entry.agent,
                str(entry.runs),
                f"[red]{entry.failures}[/]" if entry.failures else "0",
                rate,
                f"{entry.busy_seconds:.0f}s",
                str(entry.commands),
                f"[red]{entry.denials}[/]" if entry.denials else "0",
            )
        console.print(table)

    console.print(
        "[dim]All figures are derived from stored rows on each call, so a counter "
        "cannot drift from what it counts.[/]"
    )
    ctx.db.dispose()

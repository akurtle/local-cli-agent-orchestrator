"""`agentctl status` and `agentctl logs`.

Presentation only. Every number comes from a service; nothing here queries the
database directly or decides state.
"""

from __future__ import annotations

import json
import time
from typing import Annotated

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agentos.cli.agent_commands import agents_table, status_text
from agentos.cli.context import build_agent_service, load_context
from agentos.cli.glyphs import glyph, safe
from agentos.cli.message_commands import messages_table
from agentos.cli.task_commands import STATUS_COLOURS, task_glyph
from agentos.db.models import Run
from agentos.repositories.agents import AgentNotFound
from agentos.schemas.dto import ObjectiveView, TaskView
from agentos.schemas.enums import ObjectiveStatus, TaskStatus
from agentos.services.messages import MessageService
from agentos.services.objectives import ObjectiveService
from agentos.services.tasks import TaskService

console = Console()

# Watch mode refreshes on a timer; anything faster just burns CPU redrawing.
MIN_WATCH_INTERVAL = 1.0

OBJECTIVE_COLOURS = {
    ObjectiveStatus.PLANNING: "cyan",
    ObjectiveStatus.AWAITING_APPROVAL: "yellow",
    ObjectiveStatus.ACTIVE: "blue",
    ObjectiveStatus.COMPLETED: "green",
    ObjectiveStatus.FAILED: "red",
    ObjectiveStatus.CANCELLED: "bright_black",
}


def task_lines(tasks: list[TaskView], limit: int = 25) -> Text:
    """Compact glyph list, the shape the spec asks for."""
    body = Text()
    for task in tasks[:limit]:
        colour = STATUS_COLOURS.get(task.status, "white")
        marker = task_glyph(task.status)
        body.append_text(Text.from_markup(marker))
        body.append(f" {task.key} ", style="bold")
        body.append(safe(task.title), style=colour)
        if task.needs_intervention:
            body.append("  needs intervention", style="yellow")
        body.append("\n")
    if len(tasks) > limit:
        body.append(f"...and {len(tasks) - limit} more\n", style="dim")
    if not tasks:
        body.append("No tasks yet.\n", style="dim")
    return body


def progress_bar(tasks: list[TaskView], width: int = 24) -> Text:
    """A one-line completion bar, so progress is visible without counting rows."""
    if not tasks:
        return Text("")
    done = sum(1 for t in tasks if t.status is TaskStatus.COMPLETED)
    total = len(tasks)
    filled = int(width * done / total)
    bar = Text()
    bar.append("#" * filled, style="green")
    bar.append("." * (width - filled), style="bright_black")
    bar.append(f"  {done}/{total} complete")
    failed = sum(1 for t in tasks if t.status is TaskStatus.FAILED)
    blocked = sum(1 for t in tasks if t.status is TaskStatus.BLOCKED)
    if failed:
        bar.append(f"  {failed} failed", style="red")
    if blocked:
        bar.append(f"  {blocked} blocked", style="yellow")
    return bar


def objectives_panel(objectives: list[ObjectiveView]) -> Table | None:
    active = [o for o in objectives if not o.status.is_terminal]
    shown = active or objectives[-3:]
    if not shown:
        return None
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    for objective in shown:
        colour = OBJECTIVE_COLOURS.get(objective.status, "white")
        table.add_row(
            f"#{objective.id}",
            f"[{colour}]{objective.status.value}[/]  {safe(objective.description)}",
        )
    return table


def build_status(ctx, service, task_service, message_service, objective_service):
    """Assemble the whole status view as one renderable."""
    agents = service.sync_from_config()[0]
    task_service.refresh_readiness()
    objective_service.refresh_all()

    tasks = task_service.list_tasks()
    objectives = objective_service.list_objectives()
    recent_messages = message_service.list_all(limit=6)
    unread = {k: v for k, v in message_service.unread_counts().items() if v}

    sections: list = [
        Text(ctx.config.project.name, style="bold"),
    ]

    objectives_view = objectives_panel(objectives)
    if objectives_view is not None:
        sections.append(Panel(objectives_view, title="objectives", border_style="blue"))

    sections.append(Panel(agents_table(agents), title="agents"))

    task_group = Group(progress_bar(tasks), Text(""), task_lines(tasks))
    sections.append(Panel(task_group, title="tasks"))

    if recent_messages:
        sections.append(
            Panel(
                messages_table(recent_messages, show_status=False),
                title="recent messages",
                border_style="magenta",
            )
        )
    if unread:
        summary = ", ".join(f"{name} ({n})" for name, n in sorted(unread.items()))
        sections.append(Text(f"unread: {summary}", style="dim"))

    needs_help = [t for t in tasks if t.needs_intervention]
    if needs_help:
        body = Text()
        for task in needs_help:
            body.append(f"{task.key} ", style="bold yellow")
            body.append(safe(task.error or "blocked") + "\n")
        body.append("Clear with: agentctl task unblock <id>", style="dim")
        sections.append(Panel(body, title="needs intervention", border_style="yellow"))

    return Group(*sections)


def status_command(watch: float | None = None) -> None:
    """Show project, objectives, agents, tasks and messages."""
    ctx = load_context()
    service = build_agent_service(ctx, preflight=False)
    task_service = TaskService(ctx.db, ctx.config)
    message_service = MessageService(ctx.db, ctx.config)
    objective_service = ObjectiveService(
        db=ctx.db, config=ctx.config, agent_service=service, task_service=task_service
    )

    def render():
        return build_status(
            ctx, service, task_service, message_service, objective_service
        )

    if watch is None:
        console.print(render())
        ctx.db.dispose()
        return

    interval = max(MIN_WATCH_INTERVAL, watch)
    console.print(f"[dim]refreshing every {interval:g}s; Ctrl+C to stop[/]")
    try:
        # WAL mode means these reads never block a running scheduler.
        with Live(render(), console=console, refresh_per_second=4) as live:
            while True:
                time.sleep(interval)
                live.update(render())
    except KeyboardInterrupt:
        console.print("[dim]stopped watching[/]")
    finally:
        ctx.db.dispose()


# ------------------------------------------------------------------------ logs


def logs_command(
    agent: str,
    limit: int = 3,
    raw: bool = False,
    run_id: int | None = None,
) -> None:
    """Show an agent's recent runs."""
    ctx = load_context()
    service = build_agent_service(ctx, preflight=False)
    try:
        view = service.get_agent(agent)
    except AgentNotFound as exc:
        console.print(f"[red]{exc}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1) from exc

    task_service = TaskService(ctx.db, ctx.config)

    with ctx.db.session() as session:
        query = session.query(Run).filter(Run.agent_id == view.id)
        if run_id is not None:
            query = query.filter(Run.id == run_id)
        rows = query.order_by(Run.id.desc()).limit(max(1, limit)).all()
        # Detach what we need before the session closes.
        entries = [
            {
                "id": row.id,
                "task_id": row.task_id,
                "status": row.status,
                "exit_code": row.exit_code,
                "started_at": row.started_at,
                "finished_at": row.finished_at,
                "cost_usd": row.cost_usd,
                "result_text": row.result_text,
                "stdout": row.stdout,
                "stderr": row.stderr,
                "error": row.error,
                "command": row.command,
                "session_id": row.session_id,
            }
            for row in rows
        ]

    if not entries:
        console.print(f"[dim]No runs recorded for {agent}.[/]")
        ctx.db.dispose()
        return

    for entry in reversed(entries):
        task_label = "-"
        if entry["task_id"]:
            task = task_service.tasks.find(entry["task_id"])
            task_label = task.key if task else str(entry["task_id"])

        if raw:
            console.print(f"[bold]--- run {entry['id']} ---[/]")
            console.print(entry["stdout"] or "(no output captured)")
            continue

        colour = {"succeeded": "green", "failed": "red", "timeout": "yellow"}.get(
            entry["status"], "white"
        )
        header = f"[bold]{agent.upper()} / {task_label}[/]  run {entry['id']}"
        console.print(f"\n{header}")

        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold")
        grid.add_column(overflow="fold")
        grid.add_row("status", f"[{colour}]{entry['status']}[/]")
        grid.add_row("exit", str(entry["exit_code"]))
        grid.add_row("started", str(entry["started_at"]))
        if entry["finished_at"] and entry["started_at"]:
            seconds = (entry["finished_at"] - entry["started_at"]).total_seconds()
            grid.add_row("duration", f"{seconds:.1f}s")
        if entry["cost_usd"] is not None:
            grid.add_row("cost", f"${entry['cost_usd']:.4f}")
        grid.add_row("session", (entry["session_id"] or "-")[:8])
        if entry["error"]:
            grid.add_row("error", f"[red]{safe(entry['error'])}[/]")
        console.print(grid)

        if entry["result_text"]:
            console.print(
                Panel(
                    safe(entry["result_text"][:4000]),
                    title="output",
                    border_style=colour,
                )
            )
        if entry["stderr"] and entry["stderr"].strip():
            console.print(
                Panel(
                    safe(entry["stderr"].strip()[:1500]),
                    title="stderr",
                    border_style="red",
                )
            )

    if not raw:
        console.print(
            f"\n[dim]{glyph('arrow')} full transcript: "
            f"agentctl logs {agent} --raw[/]"
        )
    ctx.db.dispose()

"""`agentctl tasks`, the `agentctl task ...` group, and `agentctl work`.

These commands call TaskService and Scheduler only. They never write task
statuses or touch the database directly.
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
from agentos.repositories.tasks import TaskNotFound
from agentos.runtime.dry_run import DryRunRuntime
from agentos.schemas.dto import TaskView
from agentos.schemas.enums import TaskStatus
from agentos.services.dag import DependencyCycle
from agentos.services.scheduler import Scheduler, SchedulerEvent
from agentos.services.workspaces import WorkspaceService
from agentos.services.tasks import (
    InvalidTaskTransition,
    TaskService,
    TaskValidationError,
)

console = Console()

task_app = typer.Typer(
    name="task", help="Create and inspect tasks.", no_args_is_help=True
)

STATUS_COLOURS = {
    TaskStatus.PENDING: "bright_black",
    TaskStatus.READY: "blue",
    TaskStatus.RUNNING: "cyan",
    TaskStatus.BLOCKED: "yellow",
    TaskStatus.REVIEW: "magenta",
    TaskStatus.COMPLETED: "green",
    TaskStatus.FAILED: "red",
    TaskStatus.CANCELLED: "bright_black",
}

STATUS_GLYPHS = {
    TaskStatus.COMPLETED: "check",
    TaskStatus.FAILED: "cross",
    TaskStatus.RUNNING: "filled",
    TaskStatus.CANCELLED: "dash",
}


def task_glyph(status: TaskStatus) -> str:
    colour = STATUS_COLOURS.get(status, "white")
    return f"[{colour}]{glyph(STATUS_GLYPHS.get(status, 'hollow'))}[/]"


def status_label(status: TaskStatus) -> str:
    return f"[{STATUS_COLOURS.get(status, 'white')}]{status.value}[/]"


def tasks_table(tasks: list[TaskView]) -> Table:
    table = Table(show_header=True, header_style="bold")
    table.add_column("")
    table.add_column("ID")
    table.add_column("Status")
    table.add_column("Agent")
    table.add_column("Title", overflow="ellipsis", max_width=46)
    table.add_column("Depends on")
    for task in tasks:
        table.add_row(
            task_glyph(task.status),
            task.key,
            status_label(task.status),
            task.assigned_agent or "[dim]-[/]",
            safe(task.title),
            ", ".join(task.depends_on) or "[dim]-[/]",
        )
    return table


def _service(ctx) -> TaskService:
    return TaskService(db=ctx.db, config=ctx.config)


# ----------------------------------------------------------------- list tasks


def list_tasks_command(
    status: str | None = None,
) -> None:
    """Show all tasks."""
    ctx = load_context()
    service = _service(ctx)
    # Keep readiness current so the listing never shows a stale blocked/ready.
    service.refresh_readiness()

    wanted: set[TaskStatus] | None = None
    if status:
        try:
            wanted = {TaskStatus(status.strip().lower())}
        except ValueError:
            valid = ", ".join(s.value for s in TaskStatus)
            console.print(f"[red]Unknown status {status!r}.[/] Valid: {valid}")
            ctx.db.dispose()
            raise typer.Exit(code=2)

    tasks = service.list_tasks(wanted)
    if not tasks:
        console.print("[dim]No tasks yet.[/] Create one with `agentctl task create`.")
        ctx.db.dispose()
        return

    console.print(f"[bold]{ctx.config.project.name}[/]")
    console.print(tasks_table(tasks))
    ctx.db.dispose()


# --------------------------------------------------------------- create task


@task_app.command("create")
def create_task(
    title: Annotated[str, typer.Argument(help="Short task title.")],
    agent: Annotated[
        str | None, typer.Option("--agent", "-a", help="Agent to assign.")
    ] = None,
    description: Annotated[
        str, typer.Option("--description", "-d", help="Full task description.")
    ] = "",
    depends_on: Annotated[
        list[str] | None,
        typer.Option("--depends-on", help="Task key this depends on (repeatable)."),
    ] = None,
    criteria: Annotated[
        list[str] | None,
        typer.Option("--criteria", "-c", help="Acceptance criterion (repeatable)."),
    ] = None,
    priority: Annotated[
        int, typer.Option("--priority", "-p", help="Lower runs first.")
    ] = 100,
    prefix: Annotated[
        str, typer.Option("--prefix", help="Key prefix, e.g. AUTH gives AUTH-1.")
    ] = "T",
) -> None:
    """Create a task, optionally with dependencies."""
    ctx = load_context()
    # Registering configured agents first means --agent works on a fresh project.
    build_agent_service(ctx, preflight=False).sync_from_config()
    service = _service(ctx)

    try:
        task = service.create_task(
            title=title,
            description=description,
            agent=agent,
            depends_on=list(depends_on or []),
            priority=priority,
            acceptance_criteria=list(criteria or []),
            prefix=prefix,
        )
    except (TaskValidationError, DependencyCycle) as exc:
        console.print(f"[red]{exc}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=2) from exc

    console.print(
        f"{task_glyph(task.status)} [bold]{task.key}[/] {safe(task.title)} "
        f"({status_label(task.status)})"
    )
    if task.depends_on:
        console.print(f"[dim]depends on: {', '.join(task.depends_on)}[/]")
    ctx.db.dispose()


# ----------------------------------------------------------------- show task


@task_app.command("show")
def show_task(key: Annotated[str, typer.Argument(help="Task key, e.g. AUTH-1.")]) -> None:
    """Show one task in detail."""
    ctx = load_context()
    service = _service(ctx)
    service.refresh_readiness()
    try:
        task = service.get_task(key)
    except TaskNotFound:
        console.print(f"[red]No task with key {key!r}.[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1)

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column(overflow="fold")
    grid.add_row("key", task.key)
    grid.add_row("title", safe(task.title))
    grid.add_row("status", status_label(task.status))
    grid.add_row("agent", task.assigned_agent or "-")
    grid.add_row("role", task.assigned_role or "-")
    grid.add_row("priority", str(task.priority))
    grid.add_row("attempts", str(task.attempts))
    grid.add_row("depends on", ", ".join(task.depends_on) or "-")
    grid.add_row("created by", task.created_by or "human")
    grid.add_row("created", str(task.created_at))
    grid.add_row("started", str(task.started_at or "-"))
    grid.add_row("completed", str(task.completed_at or "-"))
    if task.duration_seconds is not None:
        grid.add_row("duration", f"{task.duration_seconds:.1f}s")
    console.print(grid)

    if task.description.strip():
        console.print(Panel(safe(task.description), title="description"))
    if task.acceptance_criteria:
        body = "\n".join(f"- {c}" for c in task.acceptance_criteria)
        console.print(Panel(safe(body), title="acceptance criteria"))
    if task.result:
        console.print(Panel(safe(task.result), title="result", border_style="green"))
    if task.error:
        console.print(Panel(safe(task.error), title="error", border_style="red"))
    ctx.db.dispose()


# --------------------------------------------------------------- cancel/retry


@task_app.command("cancel")
def cancel_task(key: Annotated[str, typer.Argument(help="Task key.")]) -> None:
    """Cancel a task. Its dependents become blocked."""
    ctx = load_context()
    service = _service(ctx)
    try:
        task = service.cancel(key)
    except TaskNotFound:
        console.print(f"[red]No task with key {key!r}.[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1)
    except InvalidTaskTransition as exc:
        console.print(f"[yellow]{exc}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1) from exc

    console.print(f"{task_glyph(task.status)} {task.key} cancelled")
    newly_blocked = [
        t.key for t in service.list_tasks({TaskStatus.BLOCKED})
    ]
    if newly_blocked:
        console.print(f"[yellow]now blocked:[/] {', '.join(newly_blocked)}")
    ctx.db.dispose()


@task_app.command("retry")
def retry_task(key: Annotated[str, typer.Argument(help="Task key.")]) -> None:
    """Put a failed task back in the queue."""
    ctx = load_context()
    service = _service(ctx)
    try:
        task = service.retry(key)
    except TaskNotFound:
        console.print(f"[red]No task with key {key!r}.[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1)
    except InvalidTaskTransition as exc:
        console.print(f"[yellow]{exc}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1) from exc
    console.print(f"{task_glyph(task.status)} {task.key} -> {status_label(task.status)}")
    ctx.db.dispose()


# ---------------------------------------------------------------------- work


EVENT_STYLES = {
    SchedulerEvent.DISPATCH: ("cyan", "start "),
    SchedulerEvent.COMPLETED: ("green", "done  "),
    SchedulerEvent.FAILED: ("red", "fail  "),
    SchedulerEvent.RETRY: ("yellow", "retry "),
    SchedulerEvent.STOP: ("bright_black", "stop  "),
    SchedulerEvent.WORKSPACE: ("blue", "tree  "),
    SchedulerEvent.CHANGES: ("green", "files "),
    SchedulerEvent.MESSAGE: ("magenta", "msg   "),
    SchedulerEvent.SPAWNED: ("cyan", "spawn "),
    SchedulerEvent.REPAIR: ("yellow", "repair"),
    SchedulerEvent.REJECTED: ("yellow", "reject"),
}


def work_command(
    dry_run: bool = False,
    max_passes: int = 1000,
) -> None:
    """Run ready tasks until nothing can progress."""
    ctx = load_context()
    agent_service = build_agent_service(ctx, preflight=not dry_run)
    agent_service.sync_from_config()

    if dry_run:
        # Swap in a runtime that launches nothing, so scheduling can be
        # exercised without spending model usage.
        agent_service.runtime = DryRunRuntime()
        console.print("[yellow]dry run:[/] no agent processes will be launched")

    task_service = _service(ctx)

    def on_progress(event: str, detail: str) -> None:
        colour, label = EVENT_STYLES.get(event, ("white", event))
        console.print(f"[{colour}]{label}[/] {safe(detail)}")

    scheduler = Scheduler(
        db=ctx.db,
        config=ctx.config,
        agent_service=agent_service,
        task_service=task_service,
        workspace_service=WorkspaceService(ctx.config, ctx.paths),
        on_progress=on_progress,
    )

    console.print(
        f"[dim]concurrency limit: {ctx.config.orchestrator.max_concurrent_agents}, "
        f"retries: {ctx.config.orchestrator.max_task_retries}[/]"
    )

    try:
        report = asyncio.run(scheduler.run(max_passes=max_passes))
    except DependencyCycle as exc:
        console.print(f"[red]{exc}[/]")
        console.print("[dim]Fix the graph before running the scheduler.[/]")
        ctx.db.dispose()
        raise typer.Exit(code=2) from exc
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted; database state preserved[/]")
        ctx.db.dispose()
        raise typer.Exit(code=130)

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold")
    summary.add_column()
    summary.add_row("passes", str(report.passes))
    summary.add_row("dispatched", str(report.dispatched))
    summary.add_row("completed", ", ".join(report.completed) or "-")
    if report.failed:
        summary.add_row("failed", f"[red]{', '.join(report.failed)}[/]")
    if report.blocked:
        summary.add_row("blocked", f"[yellow]{', '.join(report.blocked)}[/]")
    summary.add_row("stopped", report.stop_reason)
    console.print(summary)

    console.print(tasks_table(task_service.list_tasks()))
    ctx.db.dispose()
    if report.failed:
        raise typer.Exit(code=1)

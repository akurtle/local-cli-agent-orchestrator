"""`agentctl run` -- the Paperclip-style entry point, plus `agentctl objectives`.

The manager proposes a plan, the operator approves it, and only then are tasks
created. Approval is required by default.
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
from agentos.cli.task_commands import tasks_table, work_command
from agentos.repositories.objectives import ObjectiveNotFound
from agentos.schemas.enums import ObjectiveStatus
from agentos.services.objectives import ObjectiveService, Proposal
from agentos.services.tasks import TaskService

console = Console()

OBJECTIVE_COLOURS = {
    ObjectiveStatus.PLANNING: "cyan",
    ObjectiveStatus.AWAITING_APPROVAL: "yellow",
    ObjectiveStatus.ACTIVE: "blue",
    ObjectiveStatus.COMPLETED: "green",
    ObjectiveStatus.FAILED: "red",
    ObjectiveStatus.CANCELLED: "bright_black",
}


def _build(ctx, preflight: bool = True) -> ObjectiveService:
    agent_service = build_agent_service(ctx, preflight=preflight)
    agent_service.sync_from_config()
    return ObjectiveService(
        db=ctx.db,
        config=ctx.config,
        agent_service=agent_service,
        task_service=TaskService(ctx.db, ctx.config),
    )


def render_plan(proposal: Proposal) -> None:
    """Show the proposed plan in dependency order."""
    plan = proposal.plan
    if plan is None:
        return

    order = proposal.validation.order if proposal.validation else []
    by_id = {t.temp_id: t for t in plan.tasks}
    sequence = [by_id[t] for t in order if t in by_id] or plan.tasks

    console.print("\n[bold]Proposed plan[/]")
    for index, task in enumerate(sequence, start=1):
        console.print(f"\n[bold]{index}. {task.assigned_agent}[/]  {safe(task.title)}")
        if task.description.strip():
            console.print(f"   {safe(task.description.strip()[:300])}")
        if task.depends_on:
            console.print(f"   [dim]depends on: {', '.join(task.depends_on)}[/]")
        for criterion in task.acceptance_criteria:
            console.print(f"   [dim]{glyph('hollow')} {safe(criterion)}[/]")

    if plan.notes.strip():
        console.print(Panel(safe(plan.notes), title="manager notes"))

    if proposal.validation and proposal.validation.warnings:
        for warning in proposal.validation.warnings:
            console.print(f"[yellow]warning:[/] {warning}")


def run_objective_command(
    description: str,
    auto_approve: bool = False,
    manager: str | None = None,
    then_work: bool = False,
    timeout: float | None = None,
) -> None:
    """Ask the manager to plan an objective, then persist it once approved."""
    ctx = load_context()
    service = _build(ctx)

    try:
        manager_name = manager or service.manager_name()
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=2) from exc

    console.print(f"[bold]Objective:[/] {safe(description)}")
    console.print(f"[dim]{manager_name} planning... (this reads the repository)[/]")

    with console.status(f"{manager_name} planning..."):
        proposal = asyncio.run(
            service.propose(description, manager=manager_name, timeout_seconds=timeout)
        )

    if not proposal.ok:
        console.print("[red]The manager did not produce a usable plan.[/]")
        for error in proposal.errors:
            console.print(f"  [red]-[/] {safe(error)}")
        if proposal.raw_text:
            console.print(
                Panel(
                    safe(proposal.raw_text[-1500:]),
                    title="manager reply (tail)",
                    border_style="red",
                )
            )
        console.print(
            f"[dim]Nothing was created. Objective {proposal.objective.id} is "
            "recorded as failed.[/]"
        )
        ctx.db.dispose()
        raise typer.Exit(code=1)

    render_plan(proposal)

    if not auto_approve:
        console.print()
        approved = typer.confirm("Approve this plan?", default=False)
        if not approved:
            service.reject(proposal)
            console.print("[yellow]Plan rejected. No tasks were created.[/]")
            ctx.db.dispose()
            raise typer.Exit(code=1)

    created = service.approve(proposal, created_by=manager_name)
    console.print(
        f"\n[green]Created {len(created)} task(s)[/] for objective "
        f"{proposal.objective.id}"
    )
    console.print(tasks_table(created))
    ctx.db.dispose()

    if then_work:
        console.print()
        work_command()
    else:
        console.print("[dim]Run `agentctl work` to execute them.[/]")


def list_objectives_command() -> None:
    """Show objectives and their progress."""
    ctx = load_context()
    service = _build(ctx, preflight=False)
    service.refresh_all()
    objectives = service.list_objectives()

    if not objectives:
        console.print('[dim]No objectives yet.[/] Try `agentctl run "..."`.')
        ctx.db.dispose()
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", justify="right")
    table.add_column("Status")
    table.add_column("Tasks")
    table.add_column("Objective", overflow="fold")
    for objective in objectives:
        colour = OBJECTIVE_COLOURS.get(objective.status, "white")
        table.add_row(
            str(objective.id),
            f"[{colour}]{objective.status.value}[/]",
            str(len(objective.task_keys)) or "-",
            safe(objective.description),
        )
    console.print(table)
    ctx.db.dispose()


def show_objective_command(objective_id: int) -> None:
    """Show one objective and its tasks."""
    ctx = load_context()
    service = _build(ctx, preflight=False)
    try:
        service.refresh_completion(objective_id)
        objective = service.get_objective(objective_id)
    except ObjectiveNotFound:
        console.print(f"[red]No objective with id {objective_id}.[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1)

    colour = OBJECTIVE_COLOURS.get(objective.status, "white")
    console.print(f"[bold]Objective {objective.id}[/]  [{colour}]{objective.status.value}[/]")
    console.print(safe(objective.description))
    console.print()

    task_service = TaskService(ctx.db, ctx.config)
    owned = [
        t for t in task_service.list_tasks() if t.objective_id == objective.id
    ]
    if owned:
        console.print(tasks_table(owned))
    else:
        console.print("[dim]No tasks.[/]")
    ctx.db.dispose()

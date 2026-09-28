"""`agentctl replan` -- ask the manager to repair the plan."""

from __future__ import annotations

import asyncio
import sys
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel

from agentos.cli.context import build_agent_service, load_context
from agentos.cli.glyphs import literal, safe
from agentos.cli.task_commands import tasks_table
from agentos.repositories.tasks import TaskNotFound
from agentos.schemas.enums import TaskStatus
from agentos.schemas.replan import ReplanTrigger
from agentos.services.approvals import (
    ApprovalRequired,
    ApprovalService,
    Gate,
)
from agentos.services.events import EventBus
from agentos.services.replan_service import (
    ReplanProposalResult,
    ReplanRequest,
    ReplanService,
)
from agentos.services.tasks import TaskService

console = Console()


def render_proposal(result: ReplanProposalResult) -> None:
    proposal = result.proposal
    if proposal is None:
        return

    if proposal.assessment.strip():
        console.print(Panel(literal(proposal.assessment), title="manager's assessment"))

    accepted = result.validation.accepted if result.validation else []
    console.print("\n[bold]Proposed changes[/]")
    for index, operation in enumerate(accepted, start=1):
        console.print(f"  [green]{index}.[/] {literal(operation.describe())}")
        if operation.reason.strip():
            console.print(f"     [dim]{literal(operation.reason)}[/]")

    if result.validation:
        for error in result.validation.errors:
            console.print(f"  [red]refused:[/] {literal(error)}")
        for warning in result.validation.warnings:
            console.print(f"  [yellow]warning:[/] {literal(warning)}")


def replan_command(
    reason: str | None = None,
    task: str | None = None,
    objective: int | None = None,
    auto_approve: bool = False,
    timeout: float | None = None,
) -> None:
    """Ask the manager for corrective operations and apply them once approved."""
    ctx = load_context()
    agent_service = build_agent_service(ctx)
    agent_service.sync_from_config()
    bus = EventBus(ctx.db)
    tasks = TaskService(ctx.db, ctx.config, event_bus=bus)
    service = ReplanService(ctx.db, ctx.config, agent_service, tasks, bus)

    failed_task = None
    if task:
        try:
            failed_task = tasks.get_task(task)
        except TaskNotFound:
            console.print(f"[red]No task with key {task!r}.[/]")
            ctx.db.dispose()
            raise typer.Exit(code=1)
    else:
        # Default to whatever is actually stuck, which is usually the point.
        stuck = [
            t
            for t in tasks.list_tasks()
            if t.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}
        ]
        failed_task = stuck[0] if stuck else None

    if failed_task is None and not reason:
        console.print(
            "[yellow]Nothing is failed or blocked, and no reason was given.[/]"
        )
        console.print('[dim]Try `agentctl replan --reason "API contract changed"`.[/]')
        ctx.db.dispose()
        raise typer.Exit(code=1)

    request = (
        service.request_for_failure(failed_task)
        if failed_task is not None
        else ReplanRequest(
            objective_id=objective, trigger=ReplanTrigger.MANUAL, reason=reason or ""
        )
    )
    if reason:
        request = ReplanRequest(
            objective_id=request.objective_id if objective is None else objective,
            trigger=request.trigger,
            failed_task=request.failed_task,
            failure_summary=request.failure_summary,
            reason=reason,
        )

    try:
        manager = service.manager_name()
    except ValueError as exc:
        console.print(f"[red]{exc}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=2) from exc

    subject = failed_task.key if failed_task else f"objective {request.objective_id}"
    console.print(f"[bold]Replanning[/] ({request.trigger.value}) around {subject}")
    console.print(f"[dim]{manager} is reviewing the situation...[/]")

    with console.status(f"{manager} replanning..."):
        result = asyncio.run(service.propose(request, timeout_seconds=timeout))

    if not result.ok:
        console.print("[red]The manager did not produce usable changes.[/]")
        for error in result.errors:
            console.print(f"  [red]-[/] {literal(error)}")
        if result.raw_text:
            console.print(
                Panel(
                    safe(result.raw_text[-1200:]),
                    title="manager reply (tail)",
                    border_style="red",
                )
            )
        console.print("[dim]The task graph was not changed.[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1)

    render_proposal(result)

    # Reuse the manager-plan gate: this mutates the graph just as a plan does.
    approvals = ApprovalService(ctx.config.approvals)
    try:
        decision = approvals.evaluate(
            Gate.MANAGER_PLAN,
            approved=True if auto_approve else None,
            interactive=sys.stdin.isatty(),
        )
    except ApprovalRequired as exc:
        console.print(f"[red]{exc}[/]")
        service.reject(result)
        ctx.db.dispose()
        raise typer.Exit(code=2) from exc

    if not decision.allowed:
        console.print()
        if not typer.confirm("Apply these changes?", default=False):
            service.reject(result)
            console.print("[yellow]Rejected. The task graph was not changed.[/]")
            ctx.db.dispose()
            raise typer.Exit(code=1)

    outcome = service.apply(result)
    console.print(f"\n[green]Applied {outcome.changed} change(s)[/]")
    if outcome.created:
        console.print(f"  created: {', '.join(outcome.created)}")
    if outcome.dependencies:
        console.print(f"  dependencies: {'; '.join(outcome.dependencies)}")
    if outcome.retried:
        console.print(f"  retried: {', '.join(outcome.retried)}")
    if outcome.cancelled:
        console.print(f"  cancelled: {', '.join(outcome.cancelled)}")
    if outcome.reassigned:
        console.print(f"  reassigned: {'; '.join(outcome.reassigned)}")
    for failure in outcome.failures:
        console.print(f"  [red]failed to apply:[/] {literal(failure)}")

    console.print()
    console.print(tasks_table(tasks.list_tasks()))
    console.print("[dim]Run `agentctl work` to execute the corrected plan.[/]")
    ctx.db.dispose()

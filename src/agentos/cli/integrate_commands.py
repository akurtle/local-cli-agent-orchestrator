"""`agentctl integrate` -- inspect and merge agent branches.

Dry by default: it shows the plan and changes nothing unless asked.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from agentos.cli.context import load_context
from agentos.cli.glyphs import glyph, safe
from agentos.services.integration import IntegrationPlan, IntegrationService
from agentos.services.tasks import TaskService
from agentos.vcs.manager import GitError

console = Console()


def _service(ctx) -> IntegrationService:
    return IntegrationService(
        db=ctx.db,
        config=ctx.config,
        paths=ctx.paths,
        task_service=TaskService(ctx.db, ctx.config),
    )


def render_plan(plan: IntegrationPlan) -> None:
    table = Table(show_header=True, header_style="bold")
    table.add_column("")
    table.add_column("Agent")
    table.add_column("Branch")
    table.add_column("Commits", justify="right")
    table.add_column("Files", justify="right")
    table.add_column("Merges")

    for state in plan.branches:
        if not state.exists:
            marker, verdict = glyph("dash"), "[dim]no branch[/]"
        elif not state.has_work:
            marker, verdict = glyph("dash"), "[dim]nothing to merge[/]"
        elif state.merges_cleanly:
            marker, verdict = f"[green]{glyph('check')}[/]", "[green]cleanly[/]"
        else:
            marker, verdict = f"[red]{glyph('cross')}[/]", "[red]CONFLICT[/]"
        table.add_row(
            marker,
            state.agent,
            state.branch,
            str(state.commits) if state.exists else "-",
            str(len(state.files)) if state.exists else "-",
            verdict,
        )
    console.print(table)

    if plan.overlaps:
        console.print("\n[yellow]Files changed by more than one branch:[/]")
        for overlap in plan.overlaps:
            console.print(f"  {safe(overlap.describe())}")
        console.print(
            "[dim]An overlap is not always a conflict, but it is worth a look.[/]"
        )
    if plan.sequential_risk:
        console.print(
            "[yellow]Note:[/] each branch is compared against the base "
            "individually, so two that both say 'cleanly' can still conflict "
            "with each other once the first is merged."
        )


def integrate_command(
    apply: bool = False,
    base: str | None = None,
    agent: list[str] | None = None,
    resolver: str | None = None,
    no_tasks: bool = False,
) -> None:
    """Show what would be integrated, and optionally do it."""
    ctx = load_context()
    service = _service(ctx)

    try:
        plan = asyncio.run(service.plan(agents=list(agent or []) or None, base=base))
    except GitError as exc:
        console.print(f"[red]{exc}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1) from exc

    console.print(f"[bold]Integration into {plan.base}[/]")
    render_plan(plan)

    if not plan.has_anything_to_do:
        console.print("\n[dim]Nothing to integrate.[/]")
        ctx.db.dispose()
        return

    if not apply:
        console.print(
            f"\n[dim]Would merge {len(plan.mergeable)} branch(es) onto "
            f"{plan.integration_branch}. Re-run with --apply to do it.[/]"
        )
        if plan.conflicted:
            console.print(
                f"[yellow]{len(plan.conflicted)} branch(es) conflict and will not "
                "be merged.[/]"
            )
        ctx.db.dispose()
        return

    console.print(
        f"\n[dim]Merging onto {plan.integration_branch} "
        f"(your checkout moves to that branch)[/]"
    )
    try:
        result = asyncio.run(
            service.integrate(
                plan, create_resolution_tasks=not no_tasks, resolver=resolver
            )
        )
    except GitError as exc:
        console.print(f"[red]{exc}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1) from exc

    if result.merged:
        console.print(f"[green]merged:[/] {', '.join(result.merged)}")
    if result.skipped:
        console.print(f"[dim]skipped (no work): {', '.join(result.skipped)}[/]")
    if result.failed:
        console.print(f"[red]not merged:[/] {', '.join(result.failed)}")
        console.print("[dim]Their branches are untouched; nothing was discarded.[/]")
    if result.resolution_tasks:
        console.print(
            f"[yellow]created resolution tasks:[/] "
            f"{', '.join(result.resolution_tasks)}"
        )
        console.print("[dim]Run `agentctl work` to have them addressed.[/]")

    ctx.db.dispose()
    if result.failed:
        raise typer.Exit(code=1)

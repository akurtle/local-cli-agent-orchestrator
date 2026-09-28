"""`agentctl commands` and `agentctl approvals`."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from agentos.cli.context import load_context
from agentos.cli.glyphs import glyph, literal
from agentos.services.command_service import CommandService
from agentos.services.commands import Verdict

console = Console()

VERDICT_STYLE = {
    Verdict.ALLOWED: "green",
    Verdict.DENIED: "red",
    Verdict.NEEDS_APPROVAL: "yellow",
}


def _service(ctx) -> CommandService:
    return CommandService(ctx.db, ctx.config)


def list_commands_command(
    agent: str | None = None,
    verdict: str | None = None,
    limit: int = 30,
) -> None:
    """Show commands agents have run, including refused ones."""
    ctx = load_context()
    service = _service(ctx)

    if verdict and verdict not in VERDICT_STYLE:
        valid = ", ".join(VERDICT_STYLE)
        console.print(f"[red]Unknown verdict {verdict!r}.[/] Valid: {valid}")
        ctx.db.dispose()
        raise typer.Exit(code=2)

    records = service.history(agent=agent, verdict=verdict, limit=limit)
    if not records:
        console.print("[dim]No commands recorded.[/]")
        ctx.db.dispose()
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("When")
    table.add_column("Agent")
    table.add_column("Verdict")
    table.add_column("Exit", justify="right")
    table.add_column("Task")
    table.add_column("Command", overflow="fold")
    for record in records:
        style = VERDICT_STYLE.get(record.verdict, "white")
        when = record.started_at.strftime("%H:%M:%S") if record.started_at else "-"
        exit_code = "-" if record.exit_code is None else str(record.exit_code)
        table.add_row(
            when,
            record.agent,
            f"[{style}]{record.verdict}[/]",
            exit_code,
            record.task_key or "-",
            literal(record.spelled),
        )
    console.print(table)

    refused = [r for r in records if r.verdict == Verdict.DENIED]
    if refused:
        console.print("\n[bold]Refusals:[/]")
        for record in refused:
            console.print(
                f"  [red]{literal(record.spelled)}[/] -- {literal(record.denied_reason)}"
            )

    pending = service.pending_approvals()
    if pending:
        console.print(
            f"\n[yellow]{len(pending)} command(s) waiting for approval.[/] "
            "See `agentctl approvals`."
        )
    ctx.db.dispose()


def check_command(argv: list[str]) -> None:
    """Show what the policy would do with a command, without running it."""
    ctx = load_context()
    service = _service(ctx)
    decision = service.decide(argv)
    style = VERDICT_STYLE.get(decision.verdict, "white")
    marker = {
        Verdict.ALLOWED: glyph("check"),
        Verdict.DENIED: glyph("cross"),
        Verdict.NEEDS_APPROVAL: glyph("hollow"),
    }.get(decision.verdict, "?")
    console.print(
        f"[{style}]{marker} {decision.verdict}[/]  {literal(' '.join(argv))}"
    )
    console.print(f"[dim]{literal(decision.reason)}[/]")
    ctx.db.dispose()
    if decision.verdict == Verdict.DENIED:
        raise typer.Exit(code=1)


def list_approvals_command() -> None:
    """Show commands that stopped because they need a human."""
    ctx = load_context()
    service = _service(ctx)
    pending = service.pending_approvals()

    if not pending:
        console.print("[dim]Nothing waiting for approval.[/]")
        ctx.db.dispose()
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", justify="right")
    table.add_column("When")
    table.add_column("Agent")
    table.add_column("Task")
    table.add_column("Command", overflow="fold")
    table.add_column("Rule", overflow="fold")
    for record in pending:
        when = record.started_at.strftime("%H:%M:%S") if record.started_at else "-"
        table.add_row(
            str(record.id),
            when,
            record.agent,
            record.task_key or "-",
            literal(record.spelled),
            literal(record.denied_reason),
        )
    console.print(table)
    console.print(
        "\n[dim]These did not run. Approval is granted per invocation, so there "
        "is deliberately no way to bless one retroactively -- adjust "
        "commands.require_approval if a rule is wrong.[/]"
    )
    ctx.db.dispose()

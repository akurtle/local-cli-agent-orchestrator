"""`agentctl permissions` and `agentctl permission grant|revoke|denials`."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from agentos.cli.context import build_agent_service, load_context
from agentos.cli.glyphs import literal
from agentos.repositories.agents import AgentNotFound
from agentos.schemas.capabilities import Capability
from agentos.services.permissions import PermissionService

console = Console()

permission_app = typer.Typer(
    name="permission",
    help="Inspect and adjust what agents may do.",
    no_args_is_help=True,
)


def _services(ctx):
    agent_service = build_agent_service(ctx, preflight=False)
    agent_service.sync_from_config()
    return agent_service, PermissionService(ctx.db, ctx.config)


def list_permissions_command(agent: str | None = None) -> None:
    """Show every agent's capabilities."""
    ctx = load_context()
    agent_service, permissions = _services(ctx)

    views = agent_service.list_agents()
    if agent:
        views = [v for v in views if v.name == agent]
        if not views:
            console.print(f"[red]Unknown agent {agent!r}.[/]")
            ctx.db.dispose()
            raise typer.Exit(code=1)

    # A column per capability does not fit a terminal: thirteen headings truncate
    # to noise. Two wrapped lists stay readable at any width.
    table = Table(show_header=True, header_style="bold")
    table.add_column("Agent")
    table.add_column("Role")
    table.add_column("May", overflow="fold")
    table.add_column("May not", overflow="fold")

    for view in views:
        grants = permissions.grants_for(view)
        may = sorted(c.value for c in Capability if grants.has(c))
        may_not = sorted(c.value for c in Capability if not grants.has(c))
        table.add_row(
            view.name,
            view.role,
            f"[green]{', '.join(may) or '-'}[/]",
            f"[red]{', '.join(may_not) or '-'}[/]",
        )
    console.print(table)

    for view in views:
        grants = permissions.grants_for(view)
        denied = grants.denied_tools
        suffix = f"; CLI tools denied: {', '.join(denied)}" if denied else ""
        console.print(f"[dim]{view.name}: {grants.source}{suffix}[/]")
        if grants.unknown:
            # A typo must not silently grant or withhold.
            console.print(
                f"  [yellow]unknown capability names ignored:[/] "
                f"{', '.join(grants.unknown)}"
            )
    console.print(
        "\n[dim]Denied tools are passed to the CLI, so a missing capability is "
        "prevented, not just discouraged.[/]"
    )
    ctx.db.dispose()


def _resolve(name: str) -> Capability:
    try:
        return Capability(name.strip().lower())
    except ValueError:
        valid = ", ".join(c.value for c in Capability)
        console.print(f"[red]Unknown capability {name!r}.[/]")
        console.print(f"[dim]valid: {valid}[/]")
        raise typer.Exit(code=2)


def _change(agent: str, capability: str, granting: bool) -> None:
    ctx = load_context()
    agent_service, permissions = _services(ctx)
    try:
        view = agent_service.get_agent(agent)
    except AgentNotFound as exc:
        console.print(f"[red]{exc}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1) from exc

    target = _resolve(capability)
    grants = (
        permissions.grant(view.name, target)
        if granting
        else permissions.revoke(view.name, target)
    )
    verb = "granted" if granting else "revoked"
    console.print(f"[green]{verb}[/] {target.value} for {view.name}")
    console.print(f"[dim]now: {', '.join(sorted(c.value for c in grants.capabilities))}[/]")
    console.print(
        "[yellow]Note:[/] this applies to the current process only. Put it in "
        f"`agents.{view.name}.capabilities` to make it permanent."
    )
    ctx.db.dispose()


@permission_app.command("grant")
def grant(
    agent: Annotated[str, typer.Argument(help="Agent name.")],
    capability: Annotated[str, typer.Argument(help="Capability to grant.")],
) -> None:
    """Grant a capability for this process."""
    _change(agent, capability, granting=True)


@permission_app.command("revoke")
def revoke(
    agent: Annotated[str, typer.Argument(help="Agent name.")],
    capability: Annotated[str, typer.Argument(help="Capability to revoke.")],
) -> None:
    """Revoke a capability for this process."""
    _change(agent, capability, granting=False)


@permission_app.command("denials")
def denials(
    agent: Annotated[
        str | None, typer.Argument(help="Show only this agent's denials.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 30,
) -> None:
    """Show actions the orchestrator refused."""
    ctx = load_context()
    _agent_service, permissions = _services(ctx)
    records = permissions.denials(agent=agent, limit=limit)

    if not records:
        console.print("[dim]No denials recorded.[/]")
        ctx.db.dispose()
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("When")
    table.add_column("Agent")
    table.add_column("Capability")
    table.add_column("Task")
    table.add_column("Detail", overflow="fold")
    for record in records:
        when = record.created_at.strftime("%H:%M:%S") if record.created_at else "-"
        table.add_row(
            when,
            record.agent,
            f"[red]{record.capability}[/]",
            record.task_key or "-",
            literal(record.detail),
        )
    console.print(table)
    ctx.db.dispose()

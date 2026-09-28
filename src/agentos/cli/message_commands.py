"""`agentctl messages` and `agentctl message`.

Humans send messages the same way agents do: through the bus, stored and then
injected into the recipient's next prompt.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from agentos.cli.context import build_agent_service, load_context
from agentos.cli.glyphs import glyph, safe
from agentos.repositories.agents import AgentNotFound
from agentos.schemas.dto import MessageView
from agentos.schemas.enums import MessageStatus
from agentos.services.messages import MessageService, MessageValidationError

console = Console()

STATUS_COLOURS = {
    MessageStatus.PENDING: "yellow",
    MessageStatus.DELIVERED: "cyan",
    MessageStatus.READ: "green",
}


def messages_table(messages: list[MessageView], show_status: bool = True) -> Table:
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", justify="right")
    table.add_column("From")
    table.add_column("")
    table.add_column("To")
    if show_status:
        table.add_column("Status")
    table.add_column("Task")
    table.add_column("Message", overflow="fold")

    arrow = glyph("arrow")
    for message in messages:
        row = [
            str(message.id),
            message.sender,
            arrow,
            message.recipient or "-",
        ]
        if show_status:
            colour = STATUS_COLOURS.get(message.status, "white")
            row.append(f"[{colour}]{message.status.value}[/]")
        row.append(message.task_key or "-")
        row.append(safe(message.body.replace("\n", " ")))
        table.add_row(*row)
    return table


def list_messages_command(
    agent: str | None = None,
    unread: bool = False,
    limit: int | None = None,
) -> None:
    """Show messages on the bus, optionally for one agent."""
    ctx = load_context()
    build_agent_service(ctx, preflight=False).sync_from_config()
    service = MessageService(ctx.db, ctx.config)

    try:
        if agent:
            messages = service.list_for(agent, unread_only=unread)
        else:
            messages = service.list_all(limit=limit)
            if unread:
                messages = [m for m in messages if m.is_unread]
    except AgentNotFound as exc:
        console.print(f"[red]Unknown agent: {exc}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1) from exc

    if not messages:
        scope = f" for {agent}" if agent else ""
        kind = "unread " if unread else ""
        console.print(f"[dim]No {kind}messages{scope}.[/]")
        ctx.db.dispose()
        return

    heading = f"Inbox: {agent}" if agent else "Message bus"
    console.print(f"[bold]{heading}[/]")
    console.print(messages_table(messages))

    if not agent:
        counts = {k: v for k, v in service.unread_counts().items() if v}
        if counts:
            summary = ", ".join(f"{name} ({n})" for name, n in sorted(counts.items()))
            console.print(f"[dim]unread: {summary}[/]")
    ctx.db.dispose()


def send_message_command(recipient: str, body: str) -> None:
    """Send a message from the human operator to an agent."""
    ctx = load_context()
    build_agent_service(ctx, preflight=False).sync_from_config()
    service = MessageService(ctx.db, ctx.config)

    try:
        message = service.send_from_human(recipient, body)
    except MessageValidationError as exc:
        console.print(f"[red]{exc}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=2) from exc

    console.print(
        f"[green]queued[/] #{message.id} {message.sender} {glyph('arrow')} "
        f"{message.recipient}"
    )
    console.print(
        f"[dim]It will be injected into {message.recipient}'s next prompt.[/]"
    )
    ctx.db.dispose()

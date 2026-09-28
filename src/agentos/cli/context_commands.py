"""`agentctl context`, `agentctl memory`, `agentctl handoffs`.

Inspection for debugging. `context` prints exactly what the orchestrator would
inject, layer by layer, with what each layer cost against its budget -- which is
the only practical way to work out why an agent behaved oddly.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agentos.cli.context import build_agent_service, load_context
from agentos.cli.glyphs import literal, safe
from agentos.repositories.agents import AgentNotFound
from agentos.repositories.memories import MemoryNotFound
from agentos.repositories.tasks import TaskNotFound
from agentos.schemas.enums import MemoryCategory, MemoryScope
from agentos.services.context_service import ContextService
from agentos.services.memory import MemoryService
from agentos.services.tasks import TaskService

console = Console()

memory_app = typer.Typer(
    name="memory", help="Inspect and edit what the orchestrator remembers.",
    no_args_is_help=True,
)

CATEGORY_COLOURS = {
    MemoryCategory.FACT: "cyan",
    MemoryCategory.DECISION: "green",
    MemoryCategory.CONVENTION: "blue",
    MemoryCategory.WARNING: "yellow",
    MemoryCategory.HANDOFF: "magenta",
    MemoryCategory.SESSION_SUMMARY: "bright_black",
}


def _services(ctx):
    agent_service = build_agent_service(ctx, preflight=False)
    agent_service.sync_from_config()
    tasks = TaskService(ctx.db, ctx.config)
    memory = MemoryService(ctx.db, ctx.config)
    context = ContextService(ctx.db, ctx.config, tasks, memory)
    return agent_service, tasks, memory, context


def context_command(
    agent: str,
    task: str | None = None,
    full: bool = False,
    layer: str | None = None,
) -> None:
    """Show the context that would be injected into an agent's next run."""
    ctx = load_context()
    agent_service, tasks, _memory, context = _services(ctx)

    try:
        view = agent_service.get_agent(agent)
    except AgentNotFound as exc:
        console.print(f"[red]{exc}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1) from exc

    task_view = None
    if task:
        try:
            task_view = tasks.get_task(task)
        except TaskNotFound:
            console.print(f"[red]No task with key {task!r}.[/]")
            ctx.db.dispose()
            raise typer.Exit(code=1)
    else:
        # Default to what this agent would actually pick up next.
        ready = [
            t
            for t in tasks.list_tasks()
            if t.assigned_agent == view.name and not t.status.is_terminal
        ]
        task_view = ready[0] if ready else None

    assembled = context.preview(view, task_view)
    bundle = assembled.bundle

    header = f"[bold]{view.name}[/]  session: {view.session_id or '(none)'}"
    header += f"  tasks in session: {view.session_task_count}"
    console.print(header)

    rotation = agent_service.rotation_decision(
        view, task_view.objective_id if task_view else None
    )
    if rotation.rotate and rotation.reason is not None:
        console.print(
            f"[yellow]next run rotates the session[/] "
            f"({rotation.reason.value}: {rotation.detail})"
        )
    if task_view is not None:
        console.print(f"[dim]assuming task {task_view.key}[/]")
    else:
        console.print("[dim]no pending task; showing context without a task[/]")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Layer")
    table.add_column("Chars", justify="right")
    table.add_column("Budget", justify="right")
    table.add_column("Dropped", justify="right")
    for name, size, budget, dropped in bundle.summary_rows():
        used = "[dim]-[/]" if size == 0 else str(size)
        flag = f"[yellow]{dropped}[/]" if dropped else "[dim]0[/]"
        table.add_column
        table.add_row(name, used, str(budget), flag)
    console.print(table)
    console.print(
        f"[dim]total {bundle.total_size} chars across "
        f"{sum(1 for l in bundle.layers if not l.is_empty)} populated layer(s)[/]"
    )

    if layer:
        target = bundle.layer(layer)
        if target is None:
            valid = ", ".join(l.name for l in bundle.layers)
            console.print(f"[red]Unknown layer {layer!r}.[/] Valid: {valid}")
            ctx.db.dispose()
            raise typer.Exit(code=2)
        console.print(
            Panel(literal(target.body) or "[dim](empty)[/]", title=target.heading)
        )
    elif full:
        console.print(
            Panel(
                literal(bundle.render()) or "[dim](empty)[/]",
                title="what the agent receives",
                border_style="blue",
            )
        )
    else:
        console.print("[dim]--full to print it, --layer <name> for one section[/]")

    ctx.db.dispose()


# ----------------------------------------------------------------------- memory


def memories_table(memories) -> Table:
    table = Table(show_header=True, header_style="bold")
    table.add_column("ID", justify="right")
    table.add_column("Scope")
    table.add_column("Category")
    table.add_column("Imp", justify="right")
    table.add_column("By")
    table.add_column("Content", overflow="fold")
    for memory in memories:
        colour = CATEGORY_COLOURS.get(memory.category, "white")
        table.add_row(
            str(memory.id),
            memory.label,
            f"[{colour}]{memory.category.value}[/]",
            str(memory.importance),
            memory.created_by,
            literal(memory.content),
        )
    return table


def list_memory_command(agent: str | None = None, limit: int | None = None) -> None:
    """Show what the orchestrator remembers."""
    ctx = load_context()
    _agent_service, _tasks, memory, _context = _services(ctx)

    if agent:
        items = memory.recall_agent(agent)
        heading = f"Memory for {agent}"
        if not items:
            console.print(f"[dim]Nothing remembered for {agent}.[/]")
            ctx.db.dispose()
            return
    else:
        items = memory.list_all(limit=limit)
        heading = "Memory"
        if not items:
            console.print(
                "[dim]Nothing remembered yet.[/] Add a fact with "
                '`agentctl memory add "..."`.'
            )
            ctx.db.dispose()
            return

    console.print(f"[bold]{heading}[/]")
    console.print(memories_table(items))
    ctx.db.dispose()


@memory_app.command("add")
def add_memory(
    content: Annotated[str, typer.Argument(help="The fact to remember.")],
    scope: Annotated[
        str, typer.Option("--scope", help="project, agent, objective or task.")
    ] = "project",
    scope_id: Annotated[
        str | None,
        typer.Option("--for", help="Agent name, objective id or task key."),
    ] = None,
    category: Annotated[
        str, typer.Option("--category", help="fact, decision, convention, warning.")
    ] = "fact",
    importance: Annotated[
        int, typer.Option("--importance", help="0-100; higher survives trimming.")
    ] = 70,
) -> None:
    """Record a fact for agents to be given."""
    ctx = load_context()
    _agent_service, _tasks, memory, _context = _services(ctx)

    try:
        memory_scope = MemoryScope(scope.strip().lower())
        memory_category = MemoryCategory(category.strip().lower())
    except ValueError:
        scopes = ", ".join(s.value for s in MemoryScope)
        categories = ", ".join(c.value for c in MemoryCategory)
        console.print(f"[red]Invalid scope or category.[/]")
        console.print(f"[dim]scopes: {scopes}[/]")
        console.print(f"[dim]categories: {categories}[/]")
        ctx.db.dispose()
        raise typer.Exit(code=2)

    if memory_scope is not MemoryScope.PROJECT and not scope_id:
        console.print(
            f"[red]--for is required for {memory_scope.value} scope.[/]"
        )
        ctx.db.dispose()
        raise typer.Exit(code=2)

    stored = memory.remember(
        scope=memory_scope,
        scope_id=scope_id,
        content=content,
        category=memory_category,
        importance=importance,
        created_by="human",
    )
    if stored is None:
        console.print("[red]Nothing to remember.[/]")
        ctx.db.dispose()
        raise typer.Exit(code=2)

    console.print(f"[green]remembered[/] #{stored.id} ({stored.label})")
    ctx.db.dispose()


@memory_app.command("forget")
def forget_memory(
    memory_id: Annotated[int, typer.Argument(help="Memory id from `memory`.")],
) -> None:
    """Delete a remembered fact."""
    ctx = load_context()
    _agent_service, _tasks, memory, _context = _services(ctx)
    try:
        memory.forget(memory_id)
    except MemoryNotFound:
        console.print(f"[red]No memory with id {memory_id}.[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1)
    console.print(f"[green]forgotten[/] #{memory_id}")
    ctx.db.dispose()


@memory_app.command("handoffs")
def list_handoffs(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
) -> None:
    """Show handoff packets passed between agents."""
    ctx = load_context()
    _agent_service, _tasks, memory, _context = _services(ctx)
    handoffs = memory.list_handoffs(limit=limit)
    if not handoffs:
        console.print("[dim]No handoffs yet.[/]")
        ctx.db.dispose()
        return

    for handoff in handoffs:
        state = "[dim]consumed[/]" if handoff.consumed else "[yellow]pending[/]"
        console.print(
            f"\n[bold]#{handoff.id}[/] {handoff.from_agent} -> "
            f"{handoff.to_agent or '(anyone)'}  {handoff.task_key}  {state}"
        )
        console.print(Panel(literal(handoff.render())))
    ctx.db.dispose()

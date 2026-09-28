"""The `agentctl` command line.

Phase 1 scope: init, doctor, claude-test, runs, run-show. Agent/task/message
commands arrive in later phases.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from agentos import __version__
from agentos.branding import APP_NAME, CLI_NAME, CONFIG_FILENAME, STATE_DIRNAME
from agentos.cli.agent_commands import (
    agent_app,
    list_agents_command,
    pause_agent_command,
)
from agentos.cli.context import load_context
from agentos.cli.git_commands import diff_command, git_app
from agentos.cli.run_commands import (
    list_objectives_command,
    run_objective_command,
    show_objective_command,
)
from agentos.cli.status_commands import logs_command, status_command
from agentos.cli.message_commands import (
    list_messages_command,
    send_message_command,
)
from agentos.cli.task_commands import (
    cancel_task,
    list_tasks_command,
    task_app,
    work_command,
)
from agentos.config import default_config_yaml
from agentos.db.models import Run
from agentos.paths import ProjectPaths
from agentos.runtime.base import RuntimeNotAvailable
from agentos.runtime.registry import build_runtime
from agentos.schemas.runtime import RunRequest
from agentos.services.runs import record_run

app = typer.Typer(
    name=CLI_NAME,
    help=f"{APP_NAME}: orchestrate multiple Claude CLI agents locally.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

app.add_typer(agent_app)
app.add_typer(task_app)
app.add_typer(git_app)


@app.command("status")
def status(
    watch: Annotated[
        float | None,
        typer.Option(
            "--watch",
            "-w",
            help="Refresh every N seconds until interrupted (minimum 1).",
        ),
    ] = None,
) -> None:
    """Show the project, objectives, agents, tasks and recent messages."""
    status_command(watch=watch)


@app.command("logs")
def logs(
    agent: Annotated[str, typer.Argument(help="Agent name.")],
    limit: Annotated[
        int, typer.Option("--limit", "-n", help="How many recent runs to show.")
    ] = 3,
    raw: Annotated[
        bool, typer.Option("--raw", help="Dump the captured transcript instead.")
    ] = False,
    run_id: Annotated[
        int | None, typer.Option("--run", help="Show one specific run id.")
    ] = None,
) -> None:
    """Show an agent's recent runs."""
    logs_command(agent, limit=limit, raw=raw, run_id=run_id)


@app.command("pause")
def pause(agent: Annotated[str, typer.Argument(help="Agent name.")]) -> None:
    """Stop assigning work to an agent."""
    pause_agent_command(agent, paused=True)


@app.command("resume")
def resume(agent: Annotated[str, typer.Argument(help="Agent name.")]) -> None:
    """Allow an agent to take work again."""
    pause_agent_command(agent, paused=False)


@app.command("cancel")
def cancel(key: Annotated[str, typer.Argument(help="Task key.")]) -> None:
    """Cancel a task. An alias for `task cancel`."""
    cancel_task(key)


@app.command("agents")
def agents() -> None:
    """List all agents with their role, status and session."""
    list_agents_command()


@app.command("tasks")
def tasks(
    status: Annotated[
        str | None, typer.Option("--status", "-s", help="Filter by task status.")
    ] = None,
) -> None:
    """List tasks with their status, agent and dependencies."""
    list_tasks_command(status=status)


@app.command("run")
def run_objective(
    objective: Annotated[str, typer.Argument(help="What you want done.")],
    auto_approve: Annotated[
        bool,
        typer.Option("--auto-approve", "-y", help="Skip the approval prompt."),
    ] = False,
    manager: Annotated[
        str | None, typer.Option("--agent", help="Which agent plans (default: the manager role).")
    ] = None,
    work: Annotated[
        bool, typer.Option("--work", help="Run the scheduler immediately after approval.")
    ] = False,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Seconds allowed for planning.")
    ] = None,
) -> None:
    """Have the manager plan an objective, then create the tasks once approved."""
    run_objective_command(
        objective,
        auto_approve=auto_approve,
        manager=manager,
        then_work=work,
        timeout=timeout,
    )


@app.command("objectives")
def objectives() -> None:
    """List objectives and their progress."""
    list_objectives_command()


@app.command("objective")
def objective(
    objective_id: Annotated[int, typer.Argument(help="Objective id.")],
) -> None:
    """Show one objective and its tasks."""
    show_objective_command(objective_id)


@app.command("diff")
def diff(
    agent: Annotated[str, typer.Argument(help="Agent whose changes to show.")],
    name_only: Annotated[
        bool, typer.Option("--name-only", help="List paths instead of the diff.")
    ] = False,
    stat: Annotated[
        bool, typer.Option("--stat", help="Summarise per file.")
    ] = False,
) -> None:
    """Show what an agent changed in its worktree."""
    diff_command(agent, name_only=name_only, stat=stat)


@app.command("messages")
def messages(
    agent: Annotated[
        str | None, typer.Argument(help="Show only this agent's inbox.")
    ] = None,
    unread: Annotated[
        bool, typer.Option("--unread", help="Only messages not yet consumed.")
    ] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", "-n", help="Show only the newest N.")
    ] = None,
) -> None:
    """Show messages on the bus."""
    list_messages_command(agent=agent, unread=unread, limit=limit)


@app.command("message")
def message(
    agent: Annotated[str, typer.Argument(help="Recipient agent name.")],
    body: Annotated[str, typer.Argument(help="What to tell them.")],
) -> None:
    """Send a message to an agent, delivered on its next invocation."""
    send_message_command(agent, body)


@app.command("work")
def work(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Exercise scheduling without launching agents or spending usage.",
        ),
    ] = False,
    max_passes: Annotated[
        int, typer.Option("--max-passes", help="Safety ceiling on scheduler passes.")
    ] = 1000,
) -> None:
    """Run ready tasks, respecting dependencies and concurrency limits."""
    work_command(dry_run=dry_run, max_passes=max_passes)


def _run_async(coro):
    """Run a coroutine from sync Typer code.

    Note: we deliberately do NOT install WindowsSelectorEventLoopPolicy.
    asyncio subprocesses require the Proactor loop on Windows, which is the
    default -- switching policies would break process spawning entirely.
    """
    return asyncio.run(coro)


@app.callback()
def _root(
    version: Annotated[
        bool, typer.Option("--version", help="Show version and exit.")
    ] = False,
) -> None:
    if version:
        console.print(f"{APP_NAME} {__version__}")
        raise typer.Exit()


# --------------------------------------------------------------------- init


@app.command()
def init(
    name: Annotated[
        str | None, typer.Option("--name", help="Project name.")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Overwrite an existing config.")
    ] = False,
) -> None:
    """Create a project config and initialise the SQLite database."""
    root = Path.cwd()
    paths = ProjectPaths(root=root)
    project_name = name or root.name

    if paths.config_file.exists() and not force:
        console.print(
            f"[yellow]{CONFIG_FILENAME} already exists.[/] "
            "Use --force to overwrite."
        )
    else:
        paths.config_file.write_text(
            default_config_yaml(project_name), encoding="utf-8"
        )
        console.print(f"[green]Wrote[/] {paths.config_file}")

    paths.ensure()
    from agentos.db.session import Database

    db = Database(paths.db_file)
    db.create_all()
    db.dispose()
    console.print(f"[green]Initialised database[/] {paths.db_file}")

    if _ignore_our_artifacts(root):
        console.print("[green]Updated[/] .gitignore")

    console.print(f"\nNext: [bold]{CLI_NAME} doctor[/]")


def _ignore_our_artifacts(root: Path) -> bool:
    """Add our state and worktree directories to .gitignore.

    Without this they show up as untracked forever, which buries the changes an
    operator actually wants to review. Only touches a file inside a git repo, and
    never rewrites entries that are already present.
    """
    if not (root / ".git").exists():
        return False

    gitignore = root / ".gitignore"
    wanted = [f"/{STATE_DIRNAME}/", "/worktrees/"]
    try:
        existing = gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    except OSError:
        return False

    present = {line.strip() for line in existing.splitlines()}
    missing = [entry for entry in wanted if entry not in present]
    if not missing:
        return False

    prefix = "" if (not existing or existing.endswith("\n")) else "\n"
    block = prefix + "\n# agentos\n" + "\n".join(missing) + "\n"
    try:
        with gitignore.open("a", encoding="utf-8") as handle:
            handle.write(block)
    except OSError:
        return False
    return True


# ------------------------------------------------------------------- doctor


@app.command()
def doctor() -> None:
    """Check that the project, database and agent runtime are all usable."""
    ctx = load_context()
    table = Table(show_header=True, header_style="bold")
    table.add_column("Check")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    table.add_row("config", "[green]ok[/]", str(ctx.paths.config_file))
    table.add_row("project", "[green]ok[/]", ctx.config.project.name)
    table.add_row("database", "[green]ok[/]", str(ctx.paths.db_file))
    table.add_row(
        "agents configured",
        "[green]ok[/]" if ctx.config.agents else "[yellow]none[/]",
        ", ".join(sorted(ctx.config.agents)) or "(add some to the config)",
    )

    exit_code = 0
    try:
        runtime = build_runtime(ctx.config)
        info = runtime.preflight()
        table.add_row("runtime", "[green]ok[/]", info.get("runtime", "?"))
        table.add_row("executable", "[green]ok[/]", info.get("executable", "?"))
        table.add_row("version", "[green]ok[/]", info.get("version", "?"))
        table.add_row("auth", "[green]ok[/]", info.get("auth", "?"))
    except RuntimeNotAvailable as exc:
        table.add_row("runtime", "[red]FAIL[/]", str(exc))
        exit_code = 1

    console.print(table)
    ctx.db.dispose()
    if exit_code:
        raise typer.Exit(code=exit_code)


# -------------------------------------------------------------- claude-test


@app.command("claude-test")
def claude_test(
    prompt: Annotated[str, typer.Argument(help="Prompt to send.")],
    model: Annotated[
        str | None, typer.Option("--model", help="Override the model.")
    ] = None,
    session: Annotated[
        str | None,
        typer.Option("--session", help="Resume this session id instead of starting one."),
    ] = None,
    timeout: Annotated[
        float | None, typer.Option("--timeout", help="Seconds before giving up.")
    ] = None,
    show_events: Annotated[
        bool, typer.Option("--events", help="Print each stream event as it arrives.")
    ] = False,
) -> None:
    """Send one prompt through the runtime and persist it as a Run.

    This is the Phase 1 end-to-end proof: config -> runtime -> subprocess ->
    stream parsing -> database.
    """
    ctx = load_context()
    try:
        runtime = build_runtime(ctx.config)
        runtime.preflight()
    except RuntimeNotAvailable as exc:
        console.print(f"[red]Runtime unavailable:[/] {exc}")
        ctx.db.dispose()
        raise typer.Exit(code=1) from exc

    request = RunRequest(
        prompt=prompt,
        session_id=session,
        resume=session is not None,
        cwd=str(ctx.paths.root),
        model=model,
        timeout_seconds=timeout or ctx.config.orchestrator.default_timeout_seconds,
        stream=True,
    )

    def on_event(event) -> None:
        if show_events:
            label = event.subtype or ""
            console.print(f"[dim]event[/] {event.type}{'/' + label if label else ''}")

    with console.status("Running agent..."):
        result = _run_async(runtime.run(request, on_event=on_event))

    run_id = record_run(ctx.db, result, runtime=runtime.name)

    colour = "green" if result.ok else "red"
    console.print(
        Panel(
            result.text or "[dim](no text returned)[/]",
            title=f"[{colour}]{result.status.value}[/]",
            border_style=colour,
        )
    )

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold")
    summary.add_column()
    summary.add_row("run id", str(run_id))
    summary.add_row("session", result.session_id or "-")
    summary.add_row("exit code", str(result.exit_code))
    summary.add_row("events", str(len(result.events)))
    duration = result.duration_seconds
    summary.add_row("duration", f"{duration:.2f}s" if duration else "-")
    if result.cost_usd is not None:
        summary.add_row("cost", f"${result.cost_usd:.4f}")
    if result.error:
        summary.add_row("error", f"[red]{result.error}[/]")
    console.print(summary)

    if result.stderr.strip():
        console.print(f"[dim]stderr:[/] {result.stderr.strip()[:1000]}")

    ctx.db.dispose()
    if not result.ok:
        raise typer.Exit(code=1)


# ----------------------------------------------------------------- run logs


@app.command("runs")
def list_runs(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
) -> None:
    """List recorded runs, newest first."""
    ctx = load_context()
    with ctx.db.session() as session:
        rows = (
            session.query(Run).order_by(Run.id.desc()).limit(max(1, limit)).all()
        )

    if not rows:
        console.print("[dim]No runs recorded yet.[/]")
        ctx.db.dispose()
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("id", justify="right")
    table.add_column("status")
    table.add_column("exit", justify="right")
    table.add_column("started")
    table.add_column("session", overflow="fold")
    table.add_column("text", overflow="ellipsis", max_width=48)

    for row in rows:
        colour = {"succeeded": "green", "failed": "red", "timeout": "yellow"}.get(
            row.status, "white"
        )
        table.add_row(
            str(row.id),
            f"[{colour}]{row.status}[/]",
            "-" if row.exit_code is None else str(row.exit_code),
            row.started_at.strftime("%Y-%m-%d %H:%M:%S"),
            (row.session_id or "-")[:8],
            (row.result_text or "").replace("\n", " ")[:200],
        )
    console.print(table)
    ctx.db.dispose()


@app.command("run-show")
def show_run(
    run_id: Annotated[int, typer.Argument(help="Run id from `runs`.")],
    raw: Annotated[
        bool, typer.Option("--raw", help="Dump captured stdout instead.")
    ] = False,
) -> None:
    """Show the detail of one recorded run."""
    ctx = load_context()
    with ctx.db.session() as session:
        row = session.get(Run, run_id)
        if row is None:
            console.print(f"[red]No run with id {run_id}.[/]")
            ctx.db.dispose()
            raise typer.Exit(code=1)

        if raw:
            sys.stdout.write(row.stdout)
            ctx.db.dispose()
            return

        console.print(f"[bold]Run {row.id}[/]  ({row.runtime})")
        console.print(f"Status:    {row.status}")
        console.print(f"Exit code: {row.exit_code}")
        console.print(f"Session:   {row.session_id}")
        console.print(f"Started:   {row.started_at}")
        console.print(f"Finished:  {row.finished_at}")
        if row.cost_usd is not None:
            console.print(f"Cost:      ${row.cost_usd:.4f}")
        try:
            argv = json.loads(row.command)
            console.print(f"Command:   {' '.join(argv)}")
        except (json.JSONDecodeError, TypeError):
            console.print(f"Command:   {row.command}")
        if row.error:
            console.print(f"[red]Error:     {row.error}[/]")
        console.print(Panel(row.result_text or "[dim](empty)[/]", title="result"))
        if row.stderr.strip():
            console.print(Panel(row.stderr.strip()[:4000], title="stderr"))
    ctx.db.dispose()


if __name__ == "__main__":
    app()

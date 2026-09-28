"""`agentctl diff <agent>` and `agentctl git status`.

Read-only views over agent worktrees. Nothing here merges or commits; the
operator does integration deliberately.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.console import Console
from rich.syntax import Syntax
from rich.table import Table

from agentos.cli.context import load_context
from agentos.cli.glyphs import safe
from agentos.vcs.manager import GitError, GitManager

console = Console()

git_app = typer.Typer(
    name="git", help="Inspect agent worktrees and branches.", no_args_is_help=True
)


def _manager(ctx) -> GitManager:
    return GitManager(root=ctx.paths.root, worktrees_dir=ctx.paths.worktrees_dir)


def diff_command(agent: str, name_only: bool = False, stat: bool = False) -> None:
    """Show what an agent changed in its worktree."""
    ctx = load_context()
    git = _manager(ctx)

    async def gather():
        status = await git.status(ctx.paths.worktrees_dir / agent)
        text = await git.diff(ctx.paths.worktrees_dir / agent, name_only=name_only)
        return status, text

    try:
        status, text = asyncio.run(gather())
    except GitError as exc:
        console.print(f"[red]{exc}[/]")
        console.print(f"[dim]Does {agent} have a worktree? Try `agentctl git status`.[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1) from exc

    console.print(f"[bold]{agent}[/]  branch: {status.branch or '-'}  {status.diff_summary}")

    if status.is_clean:
        console.print("[dim]No changes.[/]")
        ctx.db.dispose()
        return

    if stat or name_only:
        for change in status.changes:
            marker = "new" if change.is_untracked else change.status.strip() or "mod"
            console.print(f"  [cyan]{marker:>3}[/] {safe(change.path)}")
        ctx.db.dispose()
        return

    if text.strip():
        console.print(Syntax(text, "diff", theme="ansi_dark", word_wrap=False))
    untracked = [c.path for c in status.changes if c.is_untracked]
    if untracked:
        # git diff does not show untracked files, so name them explicitly.
        console.print("\n[bold]Untracked (not in the diff above):[/]")
        for path in untracked:
            console.print(f"  [green]+[/] {safe(path)}")
    ctx.db.dispose()


@git_app.command("status")
def git_status() -> None:
    """Show every agent worktree and what it has changed."""
    ctx = load_context()
    git = _manager(ctx)

    async def gather():
        try:
            await git.ensure_repository()
        except GitError as exc:
            return None, str(exc)
        trees = await git.list_worktrees()
        rows = []
        for path, branch in sorted(trees.items()):
            try:
                status = await git.status(path)
            except GitError:
                continue
            rows.append((path, branch, status))
        return rows, None

    rows, error = asyncio.run(gather())
    if error:
        console.print(f"[red]{error}[/]")
        console.print(
            "[dim]Worktrees need a git repository with at least one commit.[/]"
        )
        ctx.db.dispose()
        raise typer.Exit(code=1)

    if not rows:
        console.print("[dim]No worktrees. Set `worktree: true` on an agent.[/]")
        ctx.db.dispose()
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Branch")
    table.add_column("Changes", justify="right")
    table.add_column("Diff")
    table.add_column("Path", overflow="fold")
    for path, branch, status in rows:
        colour = "green" if status.is_clean else "yellow"
        table.add_row(
            branch,
            f"[{colour}]{len(status.changes)}[/]",
            status.diff_summary,
            str(path),
        )
    console.print(table)
    console.print("[dim]Use `agentctl diff <agent>` to see the changes.[/]")
    ctx.db.dispose()


@git_app.command("clean")
def git_clean(
    agent: Annotated[str, typer.Argument(help="Agent whose worktree to remove.")],
    force: Annotated[
        bool, typer.Option("--force", help="Remove even with uncommitted changes.")
    ] = False,
    delete_branch: Annotated[
        bool, typer.Option("--delete-branch", help="Also delete the agent's branch.")
    ] = False,
) -> None:
    """Remove an agent's worktree. The branch is kept unless asked otherwise."""
    ctx = load_context()
    git = _manager(ctx)

    if delete_branch:
        confirmed = typer.confirm(
            f"Delete branch {agent}'s work is on? This discards it permanently.",
            default=False,
        )
        if not confirmed:
            console.print("[yellow]Cancelled.[/]")
            ctx.db.dispose()
            raise typer.Exit(code=1)

    try:
        removed = asyncio.run(
            git.remove_worktree(agent, force=force, delete_branch=delete_branch)
        )
    except GitError as exc:
        console.print(f"[red]{exc}[/]")
        console.print("[dim]Use --force to discard uncommitted changes.[/]")
        ctx.db.dispose()
        raise typer.Exit(code=1) from exc

    if removed:
        console.print(f"[green]Removed[/] worktree for {agent}")
    else:
        console.print(f"[dim]{agent} had no worktree.[/]")
    ctx.db.dispose()

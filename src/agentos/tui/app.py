"""The Textual dashboard.

Layout, per the spec:

    +---- AGENTS ----+  +---- TASKS ----+
    |                |  |               |
    +----------------+  +---------------+
    +------------ NEEDS YOU ------------+   (only while something does)
    +--- CHANGES ----+  +-- MESSAGES ---+
    +----------- AGENT OUTPUT ----------+

Widgets display a `Snapshot` and translate keystrokes into selections. All
state lives in the services; this module contains no orchestration logic and
cannot run an agent -- the reader is wired to a runtime that refuses.

The one exception to read-only is task status: `u` unblocks and `R` retries the
selected task, after a confirmation, exactly as `agentctl task unblock|retry`
would. The task then waits for `agentctl work`; nothing runs from here.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from agentos.repositories.tasks import TaskNotFound
from agentos.schemas.enums import AgentStatus, TaskStatus
from agentos.services.agents import AgentBusy
from agentos.services.tasks import InvalidTaskTransition
from agentos.tui.attention import (
    KIND_STYLE,
    Kind,
    AttentionItem,
    merge_items,
    sort_items,
)
from agentos.tui.changes import (
    AgentChanges,
    ChangesReader,
    agent_summary,
    latest_task,
)
from agentos.tui.snapshot import Snapshot, SnapshotReader

REFRESH_SECONDS = 2.0
# A changes scan runs git in every worktree, so it runs less often than the
# database refresh, and off the UI thread.
CHANGES_SECONDS = 5.0

AGENT_STATUS_STYLE = {
    AgentStatus.IDLE: "green",
    AgentStatus.WORKING: "cyan",
    AgentStatus.WAITING: "blue",
    AgentStatus.BLOCKED: "yellow",
    AgentStatus.FAILED: "red",
    AgentStatus.PAUSED: "magenta",
    AgentStatus.OFFLINE: "bright_black",
}

TASK_STATUS_STYLE = {
    TaskStatus.PENDING: "bright_black",
    TaskStatus.READY: "blue",
    TaskStatus.RUNNING: "cyan",
    TaskStatus.BLOCKED: "yellow",
    TaskStatus.REVIEW: "magenta",
    TaskStatus.COMPLETED: "green",
    TaskStatus.FAILED: "red",
    TaskStatus.CANCELLED: "bright_black",
}


def panel_text(widget: Static) -> str:
    """The text a Static is currently showing.

    Textual has moved this attribute between versions, so it is read in one place
    rather than from every caller.
    """
    for attribute in ("content", "renderable"):
        value = getattr(widget, attribute, None)
        if value is not None:
            return str(value)
    return ""


def summary_line(snapshot: Snapshot) -> str:
    """One line of headline numbers."""
    total = len(snapshot.tasks)
    parts = [f"[bold]{snapshot.project}[/]"]
    provider = snapshot.provider
    if provider is not None:
        parts.append(
            f"[cyan]{provider.label}[/] [dim]{provider.planning or 'default'}"
            f" / {provider.execution or 'default'}[/]"
        )
    objective = snapshot.active_objective
    if objective is not None:
        parts.append(f"objective #{objective.id} [italic]{objective.status.value}[/]")
    if total:
        parts.append(f"{snapshot.completed}/{total} tasks complete")
    if snapshot.failed:
        parts.append(f"[red]{len(snapshot.failed)} failed[/]")
    if snapshot.blocked:
        parts.append(f"[yellow]{len(snapshot.blocked)} blocked[/]")
    if snapshot.attention:
        parts.append(f"[bold yellow]{len(snapshot.attention)} need you[/]")
    if snapshot.work == "running":
        parts.append("[bold green]work running[/]")
    elif snapshot.work == "stopping":
        parts.append("[yellow]work stopping[/]")
    return "   ".join(parts)


class KeptCursorTable(DataTable):
    """A table rebuilt on every refresh that keeps its cursor on the same row.

    `clear()` sends the cursor back to the first row, so without this the
    cursor, and with it the selection, jumped to the top every refresh.
    """

    _kept: str | None = None

    def clear(self, columns: bool = False):
        self._kept = None
        if self.row_count:
            try:
                cell = self.coordinate_to_cell_key(self.cursor_coordinate)
                self._kept = cell.row_key.value
            except Exception:  # cursor outside the rows
                pass
        return super().clear(columns)

    def add_row(self, *cells, key: str | None = None, **kwargs):
        row_key = super().add_row(*cells, key=key, **kwargs)
        if key is not None and key == self._kept:
            self._kept = None
            self.move_cursor(row=self.row_count - 1, animate=False)
        return row_key


class AgentsPanel(KeptCursorTable):
    """Agent roster. Selecting a row filters the output pane."""

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_columns("Agent", "Role", "Status", "Task")

    def show(self, snapshot: Snapshot) -> None:
        self.clear()
        for agent in snapshot.agents:
            style = AGENT_STATUS_STYLE.get(agent.status, "white")
            task = "-"
            if agent.current_task_id:
                match = next(
                    (t for t in snapshot.tasks if t.id == agent.current_task_id), None
                )
                task = match.key if match else str(agent.current_task_id)
            self.add_row(
                agent.name,
                agent.role,
                f"[{style}]{agent.status.value}[/]",
                task,
                key=agent.name,
            )


class TasksPanel(KeptCursorTable):
    """Task list. Selecting a row shows its detail in the output pane."""

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_columns("ID", "Status", "Agent", "Title")

    def show(self, snapshot: Snapshot) -> None:
        self.clear()
        for task in snapshot.tasks:
            style = TASK_STATUS_STYLE.get(task.status, "white")
            label = task.status.value
            if task.needs_intervention:
                label += "!"
            self.add_row(
                task.key,
                f"[{style}]{label}[/]",
                task.assigned_agent or "-",
                task.title[:48],
                key=task.key,
            )


def lines_label(added: int, removed: int) -> str:
    return f"[green]+{added}[/] [red]-{removed}[/]"


class ChangesPanel(KeptCursorTable):
    """Per-agent change totals. Selecting a row shows the breakdown."""

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_columns("Agent", "Task", "Lines", "Files", "Where")

    def show(self, snapshot: Snapshot, changes: list[AgentChanges]) -> None:
        self.clear()
        for entry in changes:
            if entry.shared:
                running = [
                    t.key for name in entry.agents
                    if (t := latest_task(snapshot, name))
                    and t.status is TaskStatus.RUNNING
                ]
                label = ", ".join(running) or "-"
            else:
                task = latest_task(snapshot, entry.agent)
                label = task.key if task else "-"
            where = ", ".join(a.path for a in entry.areas[:2])
            if len(entry.areas) > 2:
                where += f" +{len(entry.areas) - 2}"
            self.add_row(
                entry.agent,
                label,
                lines_label(entry.added, entry.removed),
                str(len(entry.files)),
                where,
                key=entry.agent,
            )


class AttentionPanel(KeptCursorTable):
    """Everything waiting on a human. Hidden while nothing is."""

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.add_columns("Needs", "Item", "Agent", "Why")

    def show(self, items: list[AttentionItem]) -> None:
        self.clear()
        self.display = bool(items)
        self.border_title = f"needs you ({len(items)})"
        for item in items:
            style = KIND_STYLE[item.kind]
            why = " ".join(item.reason.split())
            if item.holding_up:
                why = f"[dim]holds {len(item.holding_up)}[/] {why}"
            self.add_row(
                f"[{style}]{item.label}[/]",
                item.title[:60],
                item.agent or "-",
                why[:90],
                key=item.key,
            )


class MessagesPanel(Static):
    def show(self, snapshot: Snapshot) -> None:
        if not snapshot.messages:
            self.update("[dim]No messages.[/]")
            return
        lines = []
        for message in snapshot.messages[-8:]:
            body = message.body.replace("\n", " ")[:80]
            lines.append(
                f"[bold]{message.sender}[/] -> [bold]{message.recipient or '-'}[/]"
                f"  [dim]{message.status.value}[/]  {body}"
            )
        self.update("\n".join(lines))


class OutputPanel(Static):
    """Detail for whatever is selected."""

    def show_agent(self, snapshot: Snapshot, name: str) -> None:
        agent = snapshot.agent(name)
        if agent is None:
            self.update("[dim]Unknown agent.[/]")
            return
        runs = snapshot.runs_for(name)
        lines = [
            f"[bold]{agent.name}[/]  {agent.role}  [italic]{agent.status.value}[/]",
            f"session: {agent.session_id or '(none)'}",
        ]
        if agent.branch_name:
            lines.append(f"branch: {agent.branch_name}")
        lines.append("")
        if not runs:
            lines.append("[dim]No runs recorded.[/]")
        for run in runs[:3]:
            lines.append(
                f"[bold]run {run.id}[/] {run.task_key or '-'} "
                f"[dim]{run.status} {run.started_at} {run.duration}[/]"
            )
            if run.text:
                lines.append(run.text[:600])
            lines.append("")
        self.update("\n".join(lines))

    def show_changes(
        self, snapshot: Snapshot, changes: list[AgentChanges], agent: str
    ) -> None:
        entry = next((c for c in changes if c.agent == agent), None)
        if entry is None:
            self.update(f"[dim]{agent} has no changes.[/]")
            return
        if entry.shared:
            lines = [
                f"[bold]project directory[/]  {entry.branch or '-'}, uncommitted   "
                f"{lines_label(entry.added, entry.removed)}   "
                f"{len(entry.files)} files ({entry.new_files} new)",
                "[dim]Agents without a worktree all edit here, so these changes "
                "cannot be pinned to one of them. Set `worktree: true` on an agent "
                "to separate its work.[/]",
            ]
        else:
            lines = [
                f"[bold]{entry.agent}[/]  {entry.branch or '-'} vs {entry.base}   "
                f"{lines_label(entry.added, entry.removed)}   "
                f"{len(entry.files)} files ({entry.new_files} new)   "
                f"{entry.commits_ahead} commit(s) ahead",
            ]

        lines += ["", "[bold]summary[/]"]
        tasks = [
            (name, t) for name in entry.agents or [entry.agent]
            if (t := latest_task(snapshot, name))
        ]
        if not tasks:
            lines.append("[dim]No task recorded for this work.[/]")
        for name, task in tasks:
            who = f"{name} " if entry.shared else ""
            lines.append(
                f"{who}[bold]{task.key}[/] {task.title}  "
                f"[italic]{task.status.value}[/]"
            )
            summary = agent_summary(task.result)
            limit = 300 if entry.shared else 800
            lines.append(
                f"  {summary[:limit]}"
                if summary
                else "  [dim]No summary until the task reports.[/]"
            )

        lines += ["", "[bold]where[/]"]
        for area in entry.areas[:8]:
            lines.append(
                f"  {area.path:<32} {area.files:>3} files  "
                f"{lines_label(area.added, area.removed)}"
            )

        major = entry.major()
        lines += ["", "[bold]largest changes[/]"]
        for delta in major:
            if delta.binary:
                size = "[dim]binary[/]"
            else:
                size = lines_label(delta.added, delta.removed)
            marker = "[green]new[/] " if delta.is_new else "    "
            lines.append(f"  {marker}{delta.path}  {size}")
        if len(entry.files) > len(major):
            lines.append(f"  [dim]...and {len(entry.files) - len(major)} more[/]")
        hint = "git diff" if entry.shared else f"agentctl diff {entry.agent}"
        lines += ["", f"[dim]full diff: {hint}[/]"]
        self.update("\n".join(lines))

    def show_attention(self, items: list[AttentionItem], key: str) -> None:
        item = next((i for i in items if i.key == key), None)
        if item is None:
            self.update("[dim]Resolved.[/]")
            return
        style = KIND_STYLE[item.kind]
        lines = [f"[{style}]{item.label}[/]  [bold]{item.title}[/]"]
        if item.agent:
            lines.append(f"agent: {item.agent}")
        lines += ["", "[bold]why[/]", item.reason]
        if item.denials:
            lines += ["", "[bold]declined in the agent's session[/]"]
            lines += [f"  [red]x[/] {d.spelled}" for d in item.denials[:12]]
            if len(item.denials) > 12:
                lines.append(f"  [dim]...and {len(item.denials) - 12} more[/]")
        if item.holding_up:
            lines += [
                "",
                "[bold]holding up[/]  " + ", ".join(item.holding_up)
                + "  [dim](these resume once this is resolved)[/]",
            ]
        if item.notes:
            lines.append("")
            lines += [f"[dim]{note}[/]" for note in item.notes]
        lines += ["", "[bold]to resolve[/]"]
        lines += [f"  [cyan]{action}[/]" for action in item.actions]
        if item.key.startswith("task:"):
            key = "R" if item.kind in {Kind.FAILED, Kind.VERIFY} else "u"
            verb = "retry" if key == "R" else "unblock"
            lines.append(f"  [dim]or press[/] [bold]{key}[/] [dim]here to {verb} it[/]")
        self.update("\n".join(lines))

    def show_task(self, snapshot: Snapshot, key: str) -> None:
        task = snapshot.task(key)
        if task is None:
            self.update("[dim]Unknown task.[/]")
            return
        lines = [
            f"[bold]{task.key}[/]  {task.title}",
            f"status: {task.status.value}   agent: {task.assigned_agent or '-'}"
            f"   attempts: {task.attempts}",
        ]
        if task.depends_on:
            lines.append(f"depends on: {', '.join(task.depends_on)}")
        if task.acceptance_criteria:
            lines.append("")
            lines.append("[bold]acceptance criteria[/]")
            lines += [f"  - {c}" for c in task.acceptance_criteria]
        if task.description.strip():
            lines += ["", task.description.strip()[:600]]
        if task.result:
            lines += ["", "[bold]result[/]", task.result[:800]]
        if task.error:
            lines += ["", f"[red]error:[/] {task.error[:400]}"]
        self.update("\n".join(lines))


class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question. Dismisses with True only on an explicit yes."""

    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    ConfirmScreen > Static {
        width: 72; height: auto; padding: 1 2;
        border: thick $warning; background: $surface;
    }
    """

    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n", "answer(False)", "No"),
        Binding("escape", "answer(False)", "Cancel"),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        yield Static(f"{self.question}\n\n[bold]y[/] yes    [bold]n[/] no")

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)


class ProviderScreen(ModalScreen[str | None]):
    """Pick the provider the next `agentctl work` uses."""

    DEFAULT_CSS = """
    ProviderScreen { align: center middle; }
    ProviderScreen > Vertical {
        width: 84; height: auto; padding: 1 2;
        border: thick $accent; background: $surface;
    }
    ProviderScreen OptionList { height: auto; margin: 1 0; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, providers) -> None:
        super().__init__()
        self.providers = providers

    def compose(self) -> ComposeResult:
        options = []
        for status in self.providers:
            marker = "[green]> [/]" if status.active else "  "
            models = (
                f"planning [bold]{status.planning or 'CLI default'}[/]   "
                f"execution [bold]{status.execution or 'CLI default'}[/]"
            )
            note = "" if status.installed else "   [red]not installed[/]"
            options.append(
                Option(
                    f"{marker}{status.label:<8} {models}{note}",
                    id=status.name,
                    disabled=not status.installed,
                )
            )
        with Vertical():
            yield Static("[bold]Model provider[/]   the manager plans on the planning "
                         "model; every other agent works on the execution model.")
            yield OptionList(*options)
            yield Static("[dim]enter choose   esc cancel.   Applies to the next "
                         "`agentctl work`; agents start new sessions.[/]")

    def on_mount(self) -> None:
        options = self.query_one(OptionList)
        active = next(
            (i for i, s in enumerate(self.providers) if s.active), 0
        )
        options.highlighted = active
        options.focus()

    def on_option_list_option_selected(self, event) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DashboardApp(App):
    """Read-only dashboard over a project."""

    CSS = """
    Screen { layout: vertical; }
    #top { height: 35%; }
    #attention { height: auto; max-height: 10; border: round yellow; }
    #middle { height: 25%; }
    #agents, #tasks { width: 1fr; border: round $accent; }
    #changes { width: 1fr; border: round green; }
    #messages { width: 1fr; border: round magenta; }
    #output { height: 1fr; border: round $accent; overflow-y: auto; }
    #summary { height: auto; padding: 0 1; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("a", "focus_agents", "Agents"),
        Binding("t", "focus_tasks", "Tasks"),
        Binding("c", "focus_changes", "Changes"),
        Binding("n", "focus_attention", "Needs you"),
        Binding("u", "unblock", "Unblock"),
        Binding("R", "retry", "Retry"),
        Binding("p", "provider", "Provider"),
        Binding("w", "work", "Work"),
        Binding("b", "block", "Block"),
        Binding("s", "stop", "Stop"),
    ]

    snapshot: reactive[Snapshot | None] = reactive(None)

    def __init__(
        self, reader: SnapshotReader, changes: ChangesReader | None = None
    ) -> None:
        super().__init__()
        self.reader = reader
        self.changes_reader = changes or ChangesReader(reader.paths, reader.config)
        self.changes: list[AgentChanges] = []
        self._selected_agent: str | None = None
        self._selected_task: str | None = None
        self._selected_changes: str | None = None
        self._selected_attention: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="summary")
        with Horizontal(id="top"):
            yield AgentsPanel(id="agents")
            yield TasksPanel(id="tasks")
        yield AttentionPanel(id="attention")
        with Horizontal(id="middle"):
            yield ChangesPanel(id="changes")
            yield MessagesPanel("", id="messages")
        with Vertical():
            yield OutputPanel("[dim]Select an agent or task.[/]", id="output")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "agentos"
        for selector in ("#agents", "#tasks", "#changes", "#messages"):
            self.query_one(selector).border_title = selector[1:]
        self.refresh_snapshot()
        # Polling is fine here: reads are cheap and WAL means they never block
        # a running scheduler. A change feed would be a lot of machinery for a
        # viewer.
        self.set_interval(REFRESH_SECONDS, self.refresh_snapshot)
        self.refresh_changes()
        self.set_interval(CHANGES_SECONDS, self.refresh_changes)

    def refresh_snapshot(self) -> None:
        try:
            self.snapshot = self.reader.read()
        except Exception as exc:  # a transient read must not kill the UI
            self.query_one("#summary", Static).update(f"[red]read failed: {exc}[/]")

    def refresh_changes(self) -> None:
        # exclusive: a slow scan is replaced rather than queued behind.
        self.run_worker(
            self._scan_changes, thread=True, exclusive=True, group="changes"
        )

    def _scan_changes(self) -> None:
        try:
            found = self.changes_reader.read()
        except Exception as exc:  # git trouble must not kill the UI
            found = exc
        if get_current_worker().is_cancelled:
            return  # the app is closing; its panels may already be gone
        if isinstance(found, Exception):
            self.call_from_thread(self._changes_failed, found)
        else:
            self.call_from_thread(self.apply_changes, found)

    def apply_changes(self, changes: list[AgentChanges]) -> None:
        self.changes = changes
        try:
            panel = self.query_one("#changes", ChangesPanel)
        except NoMatches:  # a scan that finished as the app shut down
            return
        panel.border_subtitle = ""
        if self.snapshot is None:
            return
        panel.show(self.snapshot, changes)
        # Merge readiness comes from the scan, so it refreshes on the scan too.
        self._show_attention()
        if self._selected_changes or self._selected_attention:
            self._refresh_output(self.snapshot)

    def _changes_failed(self, exc: Exception) -> None:
        try:
            panel = self.query_one("#changes", ChangesPanel)
        except NoMatches:
            return
        panel.border_subtitle = f"scan failed: {exc}"[:60]

    def watch_snapshot(self, snapshot: Snapshot | None) -> None:
        if snapshot is None:
            return
        self.query_one("#summary", Static).update(summary_line(snapshot))
        self.query_one("#agents", AgentsPanel).show(snapshot)
        self.query_one("#tasks", TasksPanel).show(snapshot)
        self.query_one("#messages", MessagesPanel).show(snapshot)
        self.query_one("#changes", ChangesPanel).show(snapshot, self.changes)
        self._show_attention()
        self._refresh_output(snapshot)

    @property
    def attention(self) -> list[AttentionItem]:
        """Database items plus branches the latest scan found ready to merge."""
        if self.snapshot is None:
            return []
        return sort_items(self.snapshot.attention + merge_items(self.changes))

    def _show_attention(self) -> None:
        self.query_one("#attention", AttentionPanel).show(self.attention)

    def select_agent(self, name: str) -> None:
        """Show an agent in the output pane.

        Selection is exclusive, so this clears any selected task. Setting the
        fields directly would silently keep whichever was set first, because the
        output pane has to prefer one.
        """
        self._selected_agent, self._selected_task = name, None
        self._selected_changes = self._selected_attention = None
        if self.snapshot is not None:
            self._refresh_output(self.snapshot)

    def select_task(self, key: str) -> None:
        """Show a task in the output pane, clearing any selected agent."""
        self._selected_task, self._selected_agent = key, None
        self._selected_changes = self._selected_attention = None
        if self.snapshot is not None:
            self._refresh_output(self.snapshot)

    def select_changes(self, agent: str) -> None:
        """Show an agent's change breakdown, clearing other selections."""
        self._selected_changes = agent
        self._selected_agent = self._selected_task = None
        self._selected_attention = None
        if self.snapshot is not None:
            self._refresh_output(self.snapshot)

    def select_attention(self, key: str) -> None:
        """Show what an item needs and how to resolve it."""
        self._selected_attention = key
        self._selected_agent = self._selected_task = self._selected_changes = None
        if self.snapshot is not None:
            self._refresh_output(self.snapshot)

    def _refresh_output(self, snapshot: Snapshot) -> None:
        try:
            output = self.query_one("#output", OutputPanel)
        except NoMatches:  # a late highlight event while the app shuts down
            return
        if self._selected_attention:
            output.show_attention(self.attention, self._selected_attention)
        elif self._selected_changes:
            output.show_changes(snapshot, self.changes, self._selected_changes)
        elif self._selected_task:
            output.show_task(snapshot, self._selected_task)
        elif self._selected_agent:
            output.show_agent(snapshot, self._selected_agent)

    def on_data_table_row_highlighted(self, event) -> None:
        # Every refresh rebuilds the tables, which fires highlights of its own;
        # only the table the user is in may move the selection.
        if not event.data_table.has_focus:
            return
        key = event.row_key.value if event.row_key else None
        self._select_from(event.data_table, key)

    def on_descendant_focus(self, event) -> None:
        """Entering a table selects its current row, so the pane follows."""
        table = event.widget
        if not isinstance(table, DataTable) or not table.row_count:
            return
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:  # cursor outside the rows during a rebuild
            return
        self._select_from(table, key)

    def _select_from(self, table: DataTable, key: str | None) -> None:
        if key is None or self.snapshot is None:
            return
        if isinstance(table, AgentsPanel):
            self.select_agent(key)
        elif isinstance(table, ChangesPanel):
            self.select_changes(key)
        elif isinstance(table, AttentionPanel):
            self.select_attention(key)
        else:
            self.select_task(key)

    def action_refresh(self) -> None:
        self.refresh_snapshot()

    def action_focus_agents(self) -> None:
        self.query_one("#agents", AgentsPanel).focus()

    def action_focus_tasks(self) -> None:
        self.query_one("#tasks", TasksPanel).focus()

    def action_focus_changes(self) -> None:
        self.query_one("#changes", ChangesPanel).focus()

    def action_focus_attention(self) -> None:
        panel = self.query_one("#attention", AttentionPanel)
        if panel.display:
            panel.focus()

    # ------------------------------------------------------------ task actions

    def selected_task_key(self) -> str | None:
        """The task the selection points at, from the tasks or needs-you panel."""
        if self._selected_task:
            return self._selected_task
        if self._selected_attention and self._selected_attention.startswith("task:"):
            return self._selected_attention.split(":", 1)[1]
        return None

    def action_unblock(self) -> None:
        self._confirm_task_action(
            "unblock",
            "Clears the blocker and queues it again. Fix what blocked it first, "
            "or the agent will hit the same wall.",
            self.reader.unblock,
        )

    def action_retry(self) -> None:
        self._confirm_task_action(
            "retry",
            "Puts it back in the queue with a fresh attempt.",
            self.reader.retry,
        )

    def _confirm_task_action(self, verb: str, warning: str, apply) -> None:
        key = self.selected_task_key()
        if key is None:
            self.notify(
                f"Select a task to {verb} (tasks panel or needs you).",
                severity="warning",
            )
            return

        def decided(answer: bool | None) -> None:
            if answer:
                self._apply_task_action(verb, key, apply)

        self.push_screen(
            ConfirmScreen(f"[bold]{verb.capitalize()} {key}?[/]\n\n{warning}"),
            decided,
        )

    def _apply_task_action(self, verb: str, key: str, apply) -> None:
        try:
            task = apply(key)
        except (InvalidTaskTransition, TaskNotFound) as exc:
            self.notify(f"Could not {verb} {key}: {exc}", severity="error")
            return
        self.notify(
            f"{key} -> {task.status.value}. Run `agentctl work` to start it.",
            title={"unblock": "Unblocked", "retry": "Retried"}[verb],
        )
        self.refresh_snapshot()

    # --------------------------------------------------------------- provider

    def action_provider(self) -> None:
        try:
            providers = self.reader.providers()
        except Exception as exc:  # an unreadable config must not kill the UI
            self.notify(f"Could not read providers: {exc}", severity="error")
            return

        def chosen(name: str | None) -> None:
            if not name or any(p.active and p.name == name for p in providers):
                return
            status = self.reader.switch_provider(name)
            self.notify(
                f"Planning {status.planning or 'default'}, execution "
                f"{status.execution or 'default'}. Applies to the next "
                "`agentctl work`.",
                title=f"Switched to {status.label}",
            )
            self.refresh_snapshot()

        self.push_screen(ProviderScreen(providers), chosen)

    # ------------------------------------------------------------------ work

    def action_work(self) -> None:
        """Start `agentctl work`, or ask a running one to stop."""
        state = self.reader.work_state()
        if state == "stopping":
            self.notify("Already stopping: running agents are finishing their tasks.")
            return
        if state == "running":
            self._confirm(
                "[bold]Stop work?[/]\n\nNothing new starts. Agents already running "
                "finish their current task, then the scheduler exits.",
                self._stop_work,
            )
            return
        provider = self.snapshot.provider if self.snapshot else None
        on = f" on {provider.label}" if provider else ""
        self._confirm(
            f"[bold]Start work{on}?[/]\n\nRuns every ready task, like `agentctl "
            "work` in a terminal. This uses paid model usage. It keeps running if "
            "you close the dashboard; press w again to stop it.",
            self._start_work,
        )

    def _start_work(self) -> None:
        try:
            log = self.reader.start_work()
        except Exception as exc:
            self.notify(f"Could not start work: {exc}", severity="error")
            return
        self.notify(f"Output: {log}", title="Work started")
        self.refresh_snapshot()

    def _stop_work(self) -> None:
        self.reader.stop_work()
        self.notify("Running agents finish their tasks; nothing new starts.", title="Stopping")
        self.refresh_snapshot()

    # ------------------------------------------------------ block and stop

    def action_block(self) -> None:
        key = self.selected_task_key()
        if key is None:
            self.notify("Select a task to block (tasks panel).", severity="warning")
            return
        self._confirm(
            f"[bold]Block {key}?[/]\n\nIt will not be scheduled until you "
            "unblock it (u). Tasks that depend on it wait too.",
            lambda: self._task_change("block", key, self.reader.hold),
        )

    def action_stop(self) -> None:
        """Pause or resume the selected agent, or cancel the selected task."""
        if self._selected_agent and self.snapshot is not None:
            agent = self.snapshot.agent(self._selected_agent)
            if agent is None:
                return
            if agent.status is AgentStatus.PAUSED:
                self._agent_change("resume", agent.name, self.reader.resume)
                return
            self._confirm(
                f"[bold]Pause {agent.name}?[/]\n\nIt takes no new tasks until you "
                "press s again to resume it.",
                lambda: self._agent_change("pause", agent.name, self.reader.pause),
            )
            return
        key = self.selected_task_key()
        if key is None:
            self.notify(
                "Select an agent to pause, or a task to cancel.", severity="warning"
            )
            return
        self._confirm(
            f"[bold]Cancel {key}?[/]\n\nThis cannot be undone. Tasks that "
            "depend on it will be blocked. To pause it instead, use b.",
            lambda: self._task_change("cancel", key, self.reader.cancel),
        )

    def _task_change(self, verb: str, key: str, apply) -> None:
        try:
            task = apply(key)
        except (InvalidTaskTransition, TaskNotFound) as exc:
            self.notify(f"Could not {verb} {key}: {exc}", severity="error")
            return
        self.notify(f"{key} -> {task.status.value}")
        self.refresh_snapshot()

    def _agent_change(self, verb: str, name: str, apply) -> None:
        try:
            agent = apply(name)
        except (AgentBusy, InvalidTaskTransition) as exc:
            self.notify(f"Could not {verb} {name}: {exc}", severity="error")
            return
        self.notify(f"{name} -> {agent.status.value}")
        self.refresh_snapshot()

    def _confirm(self, question: str, then) -> None:
        def decided(answer: bool | None) -> None:
            if answer:
                then()

        self.push_screen(ConfirmScreen(question), decided)

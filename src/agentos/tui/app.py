"""The Textual dashboard.

Layout, per the spec:

    +---- AGENTS ----+  +---- TASKS ----+
    |                |  |               |
    +----------------+  +---------------+
    +------------- MESSAGES ------------+
    +----------- AGENT OUTPUT ----------+

Widgets only display a `Snapshot` and translate keystrokes into selections. All
state lives in the services; this module contains no orchestration logic and
cannot run an agent -- the reader is wired to a runtime that refuses.
"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Static

from agentos.schemas.enums import AgentStatus, TaskStatus
from agentos.tui.snapshot import Snapshot, SnapshotReader

REFRESH_SECONDS = 2.0

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
    objective = snapshot.active_objective
    if objective is not None:
        parts.append(f"objective #{objective.id} [italic]{objective.status.value}[/]")
    if total:
        parts.append(f"{snapshot.completed}/{total} tasks complete")
    if snapshot.failed:
        parts.append(f"[red]{len(snapshot.failed)} failed[/]")
    if snapshot.blocked:
        parts.append(f"[yellow]{len(snapshot.blocked)} blocked[/]")
    return "   ".join(parts)


class AgentsPanel(DataTable):
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


class TasksPanel(DataTable):
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


class DashboardApp(App):
    """Read-only dashboard over a project."""

    CSS = """
    Screen { layout: vertical; }
    #top { height: 40%; }
    #agents, #tasks { width: 1fr; border: round $accent; }
    #messages { height: 20%; border: round magenta; }
    #output { height: 1fr; border: round $accent; overflow-y: auto; }
    #summary { height: auto; padding: 0 1; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("a", "focus_agents", "Agents"),
        Binding("t", "focus_tasks", "Tasks"),
    ]

    snapshot: reactive[Snapshot | None] = reactive(None)

    def __init__(self, reader: SnapshotReader) -> None:
        super().__init__()
        self.reader = reader
        self._selected_agent: str | None = None
        self._selected_task: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="summary")
        with Horizontal(id="top"):
            yield AgentsPanel(id="agents")
            yield TasksPanel(id="tasks")
        yield MessagesPanel("", id="messages")
        with Vertical():
            yield OutputPanel("[dim]Select an agent or task.[/]", id="output")
        yield Footer()

    def on_mount(self) -> None:
        self.title = "agentos"
        self.refresh_snapshot()
        # Polling is fine here: reads are cheap and WAL means they never block
        # a running scheduler. A change feed would be a lot of machinery for a
        # viewer.
        self.set_interval(REFRESH_SECONDS, self.refresh_snapshot)

    def refresh_snapshot(self) -> None:
        try:
            self.snapshot = self.reader.read()
        except Exception as exc:  # a transient read must not kill the UI
            self.query_one("#summary", Static).update(f"[red]read failed: {exc}[/]")

    def watch_snapshot(self, snapshot: Snapshot | None) -> None:
        if snapshot is None:
            return
        self.query_one("#summary", Static).update(summary_line(snapshot))
        self.query_one("#agents", AgentsPanel).show(snapshot)
        self.query_one("#tasks", TasksPanel).show(snapshot)
        self.query_one("#messages", MessagesPanel).show(snapshot)
        self._refresh_output(snapshot)

    def select_agent(self, name: str) -> None:
        """Show an agent in the output pane.

        Selection is exclusive, so this clears any selected task. Setting the
        fields directly would silently keep whichever was set first, because the
        output pane has to prefer one.
        """
        self._selected_agent, self._selected_task = name, None
        if self.snapshot is not None:
            self._refresh_output(self.snapshot)

    def select_task(self, key: str) -> None:
        """Show a task in the output pane, clearing any selected agent."""
        self._selected_task, self._selected_agent = key, None
        if self.snapshot is not None:
            self._refresh_output(self.snapshot)

    def _refresh_output(self, snapshot: Snapshot) -> None:
        output = self.query_one("#output", OutputPanel)
        if self._selected_task:
            output.show_task(snapshot, self._selected_task)
        elif self._selected_agent:
            output.show_agent(snapshot, self._selected_agent)

    def on_data_table_row_highlighted(self, event) -> None:
        key = event.row_key.value if event.row_key else None
        if key is None or self.snapshot is None:
            return
        if isinstance(event.data_table, AgentsPanel):
            self.select_agent(key)
        else:
            self.select_task(key)

    def action_refresh(self) -> None:
        self.refresh_snapshot()

    def action_focus_agents(self) -> None:
        self.query_one("#agents", AgentsPanel).focus()

    def action_focus_tasks(self) -> None:
        self.query_one("#tasks", TasksPanel).focus()

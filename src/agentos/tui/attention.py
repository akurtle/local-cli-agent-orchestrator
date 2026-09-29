"""What is waiting on a human, for the dashboard's "needs you" panel.

Everything here is derived from state other services already keep: an objective
whose plan is awaiting approval, a task an agent reported blocked, a task that
failed or failed verification, a command held for approval, and an agent branch
ready to merge. Each item carries the exact command that resolves it, because a
panel that says "something is wrong" without saying what to type is not much
use.

Pure functions over a `Snapshot` and plain records, so the whole thing is
testable without a database or Textual.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import StrEnum

from agentos.schemas.dto import ObjectiveView, TaskView
from agentos.schemas.enums import ObjectiveStatus, TaskStatus


class Kind(StrEnum):
    """Why a human is needed, most urgent first."""

    PLAN = "plan"
    BLOCKED = "blocked"
    COMMAND = "command"
    VERIFY = "verify"
    FAILED = "failed"
    MERGE = "merge"


KIND_ORDER = list(Kind)

KIND_LABEL = {
    Kind.PLAN: "approve plan",
    Kind.BLOCKED: "blocked",
    Kind.COMMAND: "approve cmd",
    Kind.VERIFY: "checks failed",
    Kind.FAILED: "failed",
    Kind.MERGE: "ready to merge",
}

KIND_STYLE = {
    Kind.PLAN: "yellow",
    Kind.BLOCKED: "yellow",
    Kind.COMMAND: "yellow",
    Kind.VERIFY: "red",
    Kind.FAILED: "red",
    Kind.MERGE: "green",
}


@dataclass(frozen=True)
class Denial:
    """A tool call the agent's CLI declined because nobody could approve it."""

    tool: str
    detail: str

    @property
    def spelled(self) -> str:
        return f"{self.tool}: {self.detail}" if self.detail else self.tool


@dataclass(frozen=True)
class PendingCommand:
    """A command the orchestrator held for approval (see CommandService)."""

    id: int
    agent: str
    task_key: str | None
    command: str
    rule: str


@dataclass(frozen=True)
class AttentionItem:
    kind: Kind
    key: str
    """Unique within the list; also the row key in the panel."""
    title: str
    reason: str = ""
    agent: str | None = None
    actions: list[str] = field(default_factory=list)
    """Commands that resolve it, in the order to run them."""
    notes: list[str] = field(default_factory=list)
    denials: list[Denial] = field(default_factory=list)
    holding_up: list[str] = field(default_factory=list)
    """Task keys stuck behind this one, which clear once it does."""

    @property
    def label(self) -> str:
        return KIND_LABEL[self.kind]


def parse_denials(stdout: str) -> list[Denial]:
    """Pull `permission_denials` out of a run's stream-json output.

    The CLI reports them on the final `result` line. Only the tail is decoded,
    so a long transcript costs one JSON parse, not thousands.
    """
    for line in reversed((stdout or "").strip().splitlines()[-5:]):
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("type") != "result":
            continue
        found = []
        seen = set()
        for entry in event.get("permission_denials") or []:
            if not isinstance(entry, dict):
                continue
            tool = str(entry.get("tool_name") or "?")
            payload = entry.get("tool_input") or {}
            detail = ""
            if isinstance(payload, dict):
                detail = str(
                    payload.get("command")
                    or payload.get("file_path")
                    or payload.get("url")
                    or ""
                )
            detail = " ".join(detail.split())[:160]
            if (tool, detail) not in seen:
                seen.add((tool, detail))
                found.append(Denial(tool=tool, detail=detail))
        return found
    return []


# ------------------------------------------------------------------- builders


def _plan(objective: ObjectiveView) -> AttentionItem:
    return AttentionItem(
        kind=Kind.PLAN,
        key=f"plan:{objective.id}",
        title=f"objective #{objective.id}: {objective.description[:80]}",
        reason="The manager's plan is waiting for a yes before it becomes tasks.",
        actions=[f"agentctl objective {objective.id}"],
        notes=[
            "The plan is approved at the prompt of `agentctl run`. If that prompt "
            "was closed, re-run the objective, or use `agentctl replan`.",
        ],
    )


def _blocked(task: TaskView, denials: list[Denial]) -> AttentionItem:
    notes = ["Blocked tasks are never retried automatically."]
    if denials:
        notes.append(
            "The agent's CLI declined the calls above because nobody could "
            "approve them. To allow a program, add it to commands.allowed in "
            "agentos.yaml."
        )
    return AttentionItem(
        kind=Kind.BLOCKED,
        key=f"task:{task.key}",
        title=f"{task.key} {task.title}",
        reason=task.error or "The agent reported a blocker.",
        agent=task.assigned_agent,
        actions=[f"agentctl task unblock {task.key}", "agentctl work"],
        notes=notes,
        denials=denials,
    )


def _waiting(task: TaskView, upstream: list[TaskView]) -> AttentionItem:
    """Blocked by a dependency that is not itself on the list."""
    waits = ", ".join(f"{t.key} ({t.status.value})" for t in upstream) or "-"
    return AttentionItem(
        kind=Kind.BLOCKED,
        key=f"task:{task.key}",
        title=f"{task.key} {task.title}",
        reason=f"Waiting on {waits}.",
        agent=task.assigned_agent,
        actions=[f"agentctl task show {task.key}"],
        notes=[
            "A dependency will not finish on its own. Retry or unblock it, or "
            "cancel this task if it is no longer needed.",
        ],
    )


def _failed(task: TaskView, denials: list[Denial]) -> AttentionItem:
    verify = task.status is TaskStatus.FAILED_VERIFICATION
    return AttentionItem(
        kind=Kind.VERIFY if verify else Kind.FAILED,
        key=f"task:{task.key}",
        title=f"{task.key} {task.title}",
        reason=task.error
        or ("The agent claimed success; the checks disagreed." if verify else "Failed."),
        agent=task.assigned_agent,
        actions=[f"agentctl task retry {task.key}", "agentctl work"],
        notes=[
            "Retries are used up, so it waits for you."
            if not verify
            else "Fix what the checks found first; the retry is verified again."
        ],
        denials=denials,
    )


def _command(record: PendingCommand) -> AttentionItem:
    return AttentionItem(
        kind=Kind.COMMAND,
        key=f"command:{record.id}",
        title=record.command[:100],
        reason=record.rule or "Matches commands.require_approval.",
        agent=record.agent,
        actions=["agentctl approvals"],
        notes=[
            f"Asked for by {record.agent}"
            + (f" during {record.task_key}." if record.task_key else "."),
            "It did not run. Approval is per invocation; if the rule is too "
            "strict, change commands.require_approval in agentos.yaml.",
        ],
    )


def merge_items(changes) -> list[AttentionItem]:
    """Agent branches with commits the base does not have yet.

    Takes `AgentChanges` from the changes scan; the shared directory has no
    branch of its own to merge, so it is skipped.
    """
    items = []
    for entry in changes:
        if entry.shared or not entry.commits_ahead:
            continue
        items.append(
            AttentionItem(
                kind=Kind.MERGE,
                key=f"merge:{entry.agent}",
                title=f"{entry.branch or entry.agent} -> {entry.base}",
                reason=(
                    f"{entry.commits_ahead} commit(s), {len(entry.files)} files, "
                    f"+{entry.added} -{entry.removed}"
                ),
                agent=entry.agent,
                actions=[
                    f"agentctl diff {entry.agent}",
                    "agentctl integrate",
                    "agentctl integrate --apply",
                ],
                notes=["`integrate` alone is a dry run; --apply asks before merging."],
            )
        )
    return items


def attention_items(
    objectives: list[ObjectiveView],
    tasks: list[TaskView],
    pending_commands: list[PendingCommand] | None = None,
    denials: dict[str, list[Denial]] | None = None,
) -> list[AttentionItem]:
    """Everything in the database that is waiting on a human, most urgent first.

    A task blocked only because a dependency is stuck is not listed on its own:
    it is named under the item that actually needs the human, and clears when
    that does.
    """
    denials = denials or {}
    by_key = {t.key: t for t in tasks}
    items: list[AttentionItem] = []
    items += [
        _plan(o) for o in objectives if o.status is ObjectiveStatus.AWAITING_APPROVAL
    ]

    roots: dict[str, AttentionItem] = {}
    waiting: list[TaskView] = []
    for task in tasks:
        if task.needs_intervention and not task.is_terminal:
            roots[task.key] = _blocked(task, denials.get(task.key, []))
        elif task.status in {TaskStatus.FAILED, TaskStatus.FAILED_VERIFICATION}:
            roots[task.key] = _failed(task, denials.get(task.key, []))
        elif task.status is TaskStatus.BLOCKED:
            waiting.append(task)

    held: dict[str, list[str]] = {}
    for task in waiting:
        causes = _stuck_ancestors(task, by_key, roots)
        for cause in causes:
            held.setdefault(cause, []).append(task.key)
        if not causes:
            upstream = [by_key[k] for k in task.depends_on if k in by_key]
            items.append(_waiting(task, upstream))

    for key, item in roots.items():
        if key in held:
            item = replace(item, holding_up=held[key])
        items.append(item)
    items += [_command(c) for c in pending_commands or []]
    return sort_items(items)


def _stuck_ancestors(
    task: TaskView, by_key: dict[str, TaskView], roots: dict[str, AttentionItem]
) -> list[str]:
    """The listed tasks somewhere upstream of `task`, nearest first."""
    found: list[str] = []
    seen: set[str] = set()
    frontier = list(task.depends_on)
    while frontier:
        key = frontier.pop(0)
        if key in seen:
            continue
        seen.add(key)
        if key in roots:
            found.append(key)
        elif key in by_key:
            frontier += by_key[key].depends_on
    return found


def sort_items(items: list[AttentionItem]) -> list[AttentionItem]:
    return sorted(items, key=lambda i: KIND_ORDER.index(i.kind))

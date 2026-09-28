"""When to replace an agent's Claude session.

A persistent session is valuable -- the agent remembers what it just did -- but
it accumulates context indefinitely, which costs more per turn and reasons worse
as irrelevant history piles up. Rotation caps that.

The decision is a pure function so the policy is auditable and testable without
a database or a session. What makes rotation *safe* is elsewhere: the context
layers re-inject persisted memory into the new session, so knowledge survives
even though the conversation does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RotationReason(StrEnum):
    TASK_BUDGET = "task_budget"
    """The session has handled its configured number of tasks."""
    OBJECTIVE_CHANGED = "objective_changed"
    """The agent moved to different work, so old context is mostly noise."""
    MANUAL = "manual"
    """An operator asked for a fresh session."""


@dataclass(frozen=True)
class RotationDecision:
    rotate: bool
    reason: RotationReason | None = None
    detail: str = ""

    def __bool__(self) -> bool:
        return self.rotate


NO_ROTATION = RotationDecision(rotate=False)


def should_rotate(
    session_id: str | None,
    session_task_count: int,
    session_objective_id: int | None,
    next_objective_id: int | None,
    max_tasks_per_session: int,
    rotate_on_objective_change: bool,
) -> RotationDecision:
    """Decide whether to start a fresh session before the next task.

    Never rotates when there is no session: there would be nothing to replace,
    and reporting a rotation would be misleading.
    """
    if not session_id:
        return NO_ROTATION

    if session_task_count >= max_tasks_per_session:
        return RotationDecision(
            rotate=True,
            reason=RotationReason.TASK_BUDGET,
            detail=(
                f"{session_task_count} tasks in this session "
                f"(limit {max_tasks_per_session})"
            ),
        )

    # Only rotate on a *change* between two known objectives. Moving from
    # standalone work into an objective, or vice versa, is not a context switch
    # worth discarding a warm session for.
    if (
        rotate_on_objective_change
        and session_objective_id is not None
        and next_objective_id is not None
        and session_objective_id != next_objective_id
    ):
        return RotationDecision(
            rotate=True,
            reason=RotationReason.OBJECTIVE_CHANGED,
            detail=(
                f"objective {session_objective_id} -> {next_objective_id}"
            ),
        )

    return NO_ROTATION


SUMMARY_INSTRUCTION = """\
Your session is being replaced to keep your context small. Before it is, write \
down only what a future version of you would need to know and could not work out \
from the repository.

Do NOT summarise the conversation. Do NOT restate the tasks you did. Reply with \
at most 8 short bullet points covering:

- durable facts about this codebase (frameworks, layout, conventions)
- decisions taken that should not be silently revisited
- traps or gotchas you hit

If there is nothing worth carrying forward, reply with exactly: NOTHING.
"""


def parse_summary(text: str, max_items: int = 8) -> list[str]:
    """Pull bullet points out of a rotation summary reply.

    Deliberately strict: anything that is not a short bullet is ignored, because
    a rambling paragraph is exactly the context we are trying not to carry.
    """
    if not text:
        return []
    if text.strip().upper().startswith("NOTHING"):
        return []

    items: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not line.startswith(("-", "*", "•")):
            continue
        cleaned = line.lstrip("-*• ").strip()
        # Drop the response block and anything implausibly long.
        if not cleaned or cleaned.startswith("<<<") or len(cleaned) > 300:
            continue
        items.append(cleaned)
        if len(items) >= max_items:
            break
    return items

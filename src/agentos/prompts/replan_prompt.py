"""The prompt that asks the manager to repair a plan.

It carries only what the manager needs to judge the situation: the objective, what
went wrong, what has already succeeded, and what is stuck. Not the whole graph and
not anybody's conversation.

The rules it states are the rules Python enforces, so a refusal is explainable.
"""

from __future__ import annotations

from agentos.schemas.dto import TaskView
from agentos.schemas.replan import MAX_OPERATIONS, REPLAN_BEGIN, REPLAN_END
from agentos.schemas.replan import ReplanTrigger

MAX_LISTED_TASKS = 20
MAX_MESSAGES = 8


def replan_format() -> str:
    """The required block, generated from the real delimiters."""
    return f"""{REPLAN_BEGIN}
{{
  "assessment": "One or two sentences on what actually went wrong.",
  "operations": [
    {{
      "type": "create_task",
      "temp_id": "FIX-CALLBACK",
      "agent": "backend",
      "title": "Fix the OAuth callback",
      "description": "The callback rejects valid state tokens.",
      "acceptance_criteria": ["Valid state is accepted"],
      "reason": "The failure is in the callback, not the tests."
    }},
    {{
      "type": "add_dependency",
      "task": "AUTH-QA",
      "depends_on": "FIX-CALLBACK",
      "reason": "Re-test only after the fix lands."
    }}
  ]
}}
{REPLAN_END}"""


def build_replan_prompt(
    objective: str,
    trigger: ReplanTrigger,
    roster: dict[str, str],
    failed: TaskView | None = None,
    failure_summary: str = "",
    completed: list[TaskView] | None = None,
    blocked: list[TaskView] | None = None,
    outstanding: list[TaskView] | None = None,
    messages: list[str] | None = None,
    reason: str = "",
) -> str:
    """Ask the manager for corrective operations."""
    blocks: list[str] = []

    if objective.strip():
        blocks.append(f"## OBJECTIVE\n{objective.strip()}")

    situation = [f"Trigger: {trigger.value}"]
    if reason.strip():
        situation.append(f"Operator note: {reason.strip()}")
    if failed is not None:
        situation.append(f"Failed task: {failed.key} ({failed.assigned_agent}) -- {failed.title}")
        if failed.attempts:
            situation.append(f"Attempts so far: {failed.attempts}")
    if failure_summary.strip():
        situation.append(f"Failure detail:\n{failure_summary.strip()[:2000]}")
    blocks.append("## WHAT WENT WRONG\n" + "\n".join(situation))

    def listing(title: str, items: list[TaskView] | None, note: str = "") -> None:
        if not items:
            return
        lines = [
            f"- {t.key} ({t.assigned_agent or 'unassigned'}) {t.title}"
            for t in items[:MAX_LISTED_TASKS]
        ]
        if len(items) > MAX_LISTED_TASKS:
            lines.append(f"- ...and {len(items) - MAX_LISTED_TASKS} more")
        body = "\n".join(lines)
        blocks.append(f"## {title}\n" + (f"{note}\n" if note else "") + body)

    listing(
        "ALREADY COMPLETED",
        completed,
        "This work is done and cannot be changed. Build on it.",
    )
    listing("BLOCKED", blocked, "These cannot proceed as things stand.")
    listing("STILL TO RUN", outstanding)

    if messages:
        blocks.append(
            "## RELEVANT MESSAGES\n" + "\n\n".join(messages[:MAX_MESSAGES])
        )

    blocks.append(
        "## YOUR JOB\n"
        "Propose the smallest set of changes that gets the objective moving "
        "again. Prefer adding a corrective task over cancelling existing work.\n\n"
        "## RULES\n"
        f"- At most {MAX_OPERATIONS} operations.\n"
        "- Operation types: create_task, add_dependency, remove_dependency, "
        "cancel_task, retry_task, reassign_task.\n"
        "- If you cancel a task, use remove_dependency for anything that still "
        "depends on it, or that work stays blocked forever.\n"
        "- A completed or cancelled task may NOT be modified. Completed history "
        "is immutable and any such operation will be rejected.\n"
        "- A running task may not be cancelled.\n"
        "- Only a failed or blocked task can be retried.\n"
        "- Assign work only to agents in the roster below.\n"
        "- `depends_on` may reference an existing task key or a temp_id you "
        "define earlier in the same plan.\n"
        "- The graph must stay acyclic.\n\n"
        "## AVAILABLE AGENTS\n"
        + "\n".join(f"- `{name}` ({role})" for name, role in sorted(roster.items()))
    )

    blocks.append(
        "## REQUIRED FORMAT\n"
        "Explain your reasoning first if you wish, then end with exactly one "
        "block in this form:\n\n" + replan_format()
    )
    return "\n\n".join(blocks)

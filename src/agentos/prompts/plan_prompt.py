"""The prompt that asks the manager for a plan.

The roster is injected so the manager chooses among agents that actually exist
rather than inventing them. The plan format is generated from the real
delimiters, as elsewhere, so prompt and parser cannot drift.

We deliberately do not paste the repository into the prompt. The manager runs
inside the project with its own tools and can read what it needs.
"""

from __future__ import annotations

from agentos.schemas.plan import MAX_PLANNED_TASKS, PLAN_BEGIN, PLAN_END


def plan_format() -> str:
    """The required plan block, generated from the real delimiters."""
    return f"""{PLAN_BEGIN}
{{
  "objective": "Restate the objective in one line.",
  "tasks": [
    {{
      "temp_id": "AUTH-BACKEND",
      "title": "Implement Google OAuth backend",
      "assigned_agent": "backend",
      "description": "What to build and where. Be specific.",
      "acceptance_criteria": ["OAuth callback works", "Session is created"],
      "depends_on": []
    }},
    {{
      "temp_id": "AUTH-QA",
      "title": "Test Google authentication",
      "assigned_agent": "qa",
      "description": "Cover success and failure paths.",
      "acceptance_criteria": ["Both paths covered"],
      "depends_on": ["AUTH-BACKEND"]
    }}
  ],
  "notes": "Anything the operator should know before approving."
}}
{PLAN_END}"""


def build_plan_prompt(
    objective: str,
    roster: dict[str, str],
    project_name: str = "",
    existing_tasks: list[str] | None = None,
) -> str:
    """Ask the manager to decompose one objective."""
    roster_lines = "\n".join(
        f"- `{name}` (role: {role})" for name, role in sorted(roster.items())
    )

    blocks: list[str] = [
        "## OBJECTIVE",
        objective.strip(),
        "## YOUR JOB",
        (
            "Break this objective into tasks and assign each to an agent. Inspect "
            "the repository first: read the files that matter rather than guessing. "
            "Do NOT implement anything yourself and do not edit any files."
        ),
        "## AVAILABLE AGENTS",
        (
            "Assign every task to one of these exact names. You cannot create new "
            "agents, and a plan naming anything else will be rejected.\n"
            + roster_lines
        ),
        "## RULES",
        (
            f"- At most {MAX_PLANNED_TASKS} tasks. Prefer few substantial tasks "
            "over many trivial ones.\n"
            "- `temp_id` is yours to choose and must be unique within this plan.\n"
            "- `depends_on` may only reference temp_ids defined in this same plan.\n"
            "- Only add a dependency where one genuinely exists. Tasks with no\n"
            "  dependency between them will run in parallel, which is desirable.\n"
            "- Testing and review normally depend on the work they cover.\n"
            "- Acceptance criteria must be things another agent could verify.\n"
            "- The graph must be acyclic."
        ),
    ]

    if existing_tasks:
        blocks += [
            "## EXISTING TASKS",
            (
                "These already exist in this project. Do not duplicate them.\n"
                + "\n".join(f"- {key}" for key in existing_tasks)
            ),
        ]

    blocks += [
        "## REQUIRED PLAN FORMAT",
        (
            "Explain your reasoning first if you wish, then end your reply with "
            "exactly one plan block in this form:\n\n" + plan_format()
        ),
    ]
    return "\n\n".join(blocks)

"""Role prompt assembly.

A role prompt is built from two parts:

  1. A role brief (responsibilities / boundaries / guidance) -- a markdown file,
     overridable by the user.
  2. The response contract -- GENERATED from the delimiters and field names in
     `schemas.responses` so the instructions given to the agent can never drift
     away from what the parser actually accepts.

Part 2 is deliberately not stored as prose in the markdown files. If the parser
changes, the prompt changes with it automatically.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from agentos.schemas.responses import RESPONSE_BEGIN, RESPONSE_END

BUILTIN_DIR = Path(__file__).parent
GENERIC_TEMPLATE = "generic.md"


class PromptNotFound(RuntimeError):
    """An explicitly configured prompt file does not exist."""


def response_contract() -> str:
    """The machine-readable half of every role prompt.

    Generated from the real delimiters so prompt and parser stay in lockstep.
    """
    return f"""
## Required response format

End every reply with one response block, exactly once, in this form:

{RESPONSE_BEGIN}
{{
  "status": "completed",
  "summary": "One or two sentences on what you actually did.",
  "files_changed": ["path/to/file.py"],
  "messages": [
    {{"to": "frontend", "message": "Login endpoint is POST /api/auth/login."}}
  ],
  "requested_tasks": [
    {{"agent_role": "qa", "title": "Test login endpoint",
     "description": "Verify valid and invalid credentials."}}
  ],
  "blockers": [],
  "interfaces": ["POST /api/auth/google"],
  "decisions": ["Reused the existing JWT session model."],
  "warnings": []
}}
{RESPONSE_END}

Rules for the block:

- `status` must be one of: completed, failed, blocked, needs_review, in_progress.
- Use `blocked` when you cannot proceed, and put the reason in `blockers`.
- `summary` describes what you actually did. Do not claim work you did not do.
- `files_changed` lists only files you really modified. It is checked against git.
- `messages` is how you talk to another agent. You cannot call agents directly;
  the orchestrator delivers these. Address them by agent name.
- `requested_tasks` is how you ask for follow-up work by someone else. The
  orchestrator validates each one and may reject it.
- `interfaces` are contracts other agents will consume. List anything they
  must call exactly right.
- `decisions` are choices that should not be silently revisited. These are
  remembered and shown to other agents later, so keep them short and factual.
- `warnings` are traps the next agent should know about.
- Emit the block last. Write any explanation before it, not inside it.
- The JSON must be valid. No comments, no trailing commas.
""".strip()


@lru_cache(maxsize=64)
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _builtin_brief(role: str) -> str:
    candidate = BUILTIN_DIR / f"{role}.md"
    if candidate.is_file():
        return _read(candidate)
    # Unknown role: fall back to a neutral brief so user-defined roles work
    # without shipping a file for every possible name.
    return _read(BUILTIN_DIR / GENERIC_TEMPLATE).replace("{role}", role)


def resolve_brief(
    role: str,
    project_root: Path | None = None,
    explicit_path: str | None = None,
) -> str:
    """Find the role brief, honouring user overrides.

    Resolution order:
      1. `prompt:` path from the agent's config entry
      2. `<project>/prompts/<role>.md`
      3. the built-in brief for that role
      4. the built-in generic brief
    """
    if explicit_path:
        path = Path(explicit_path)
        if not path.is_absolute() and project_root is not None:
            path = project_root / path
        if not path.is_file():
            raise PromptNotFound(f"configured prompt file not found: {path}")
        return _read(path)

    if project_root is not None:
        override = project_root / "prompts" / f"{role}.md"
        if override.is_file():
            return _read(override)

    return _builtin_brief(role)


def build_system_prompt(
    agent_name: str,
    role: str,
    description: str = "",
    project_name: str = "",
    roster: dict[str, str] | None = None,
    project_root: Path | None = None,
    explicit_path: str | None = None,
    capabilities: str = "",
) -> str:
    """Assemble the full system prompt for one agent.

    `roster` maps agent name -> role. It is included so an agent knows who it can
    address, which is what stops it inventing recipients.
    """
    parts: list[str] = [
        f"You are the agent named `{agent_name}` in a multi-agent team"
        + (f" working on the project \"{project_name}\"." if project_name else "."),
    ]
    if description:
        parts.append(f"Your remit: {description}")

    parts.append(resolve_brief(role, project_root, explicit_path))

    if roster:
        lines = "\n".join(
            f"- `{name}` ({r})" for name, r in sorted(roster.items()) if name != agent_name
        )
        if lines:
            parts.append(
                "## Other agents you may address\n"
                "These are the only valid recipients. Do not invent others.\n" + lines
            )

    if capabilities:
        parts.append("## CAPABILITIES\n" + capabilities)

    parts.append(response_contract())
    return "\n\n".join(parts)

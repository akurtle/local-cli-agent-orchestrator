"""What an agent is allowed to do.

Prompts are guidance, not enforcement. A reviewer told "do not edit files" may
still edit files, so capabilities are enforced in three independent layers:

  1. **Preventive** -- the missing capability's tools are passed to the CLI as
     `--disallowedTools`, so the agent cannot perform the action at all.
  2. **Validating** -- actions an agent asks for in its response block (messages,
     requested tasks) are rejected if it lacks the capability.
  3. **Detective** -- git is compared against what the agent was permitted to do,
     so an edit that slipped through is still caught and recorded.

A denylist rather than an allowlist for layer 1: enumerating everything an agent
may use risks omitting something benign and breaking it, while denying the
specific write and execute tools is precise. If a mapping is ever incomplete,
layer 3 still catches the result.
"""

from __future__ import annotations

from enum import StrEnum


class Capability(StrEnum):
    READ_FILES = "read_files"
    EDIT_FILES = "edit_files"
    RUN_COMMAND = "run_command"
    RUN_TESTS = "run_tests"

    CREATE_TASK = "create_task"
    UPDATE_TASK = "update_task"
    REQUEST_TASK = "request_task"

    MESSAGE_AGENT = "message_agent"

    GIT_DIFF = "git_diff"
    GIT_COMMIT = "git_commit"
    GIT_MERGE = "git_merge"

    INSPECT_REPO = "inspect_repo"


# Tools the Claude CLI must be denied when a capability is absent. Verified
# against `claude --disallowedTools`, which accepts tool names and `Bash(...)`
# scoping.
DENIED_TOOLS_WITHOUT: dict[Capability, tuple[str, ...]] = {
    Capability.EDIT_FILES: ("Edit", "Write", "NotebookEdit"),
    Capability.READ_FILES: ("Read", "Grep", "Glob"),
    Capability.RUN_COMMAND: ("Bash", "PowerShell"),
}

# Roles that do not list capabilities get these. Chosen so an existing project
# keeps working, while a reviewer still does not silently gain edit rights.
ROLE_DEFAULTS: dict[str, frozenset[Capability]] = {
    "manager": frozenset(
        {
            Capability.READ_FILES,
            Capability.INSPECT_REPO,
            Capability.CREATE_TASK,
            Capability.UPDATE_TASK,
            Capability.REQUEST_TASK,
            Capability.MESSAGE_AGENT,
            Capability.GIT_DIFF,
        }
    ),
    "backend": frozenset(
        {
            Capability.READ_FILES,
            Capability.EDIT_FILES,
            Capability.RUN_COMMAND,
            Capability.RUN_TESTS,
            Capability.INSPECT_REPO,
            Capability.GIT_DIFF,
            Capability.GIT_COMMIT,
            Capability.MESSAGE_AGENT,
            Capability.REQUEST_TASK,
        }
    ),
    "qa": frozenset(
        {
            Capability.READ_FILES,
            Capability.RUN_COMMAND,
            Capability.RUN_TESTS,
            Capability.INSPECT_REPO,
            Capability.GIT_DIFF,
            Capability.MESSAGE_AGENT,
            Capability.REQUEST_TASK,
        }
    ),
    "reviewer": frozenset(
        {
            Capability.READ_FILES,
            Capability.RUN_TESTS,
            Capability.RUN_COMMAND,
            Capability.INSPECT_REPO,
            Capability.GIT_DIFF,
            Capability.MESSAGE_AGENT,
            Capability.REQUEST_TASK,
        }
    ),
}

# `frontend` and any other implementation role behave like backend: they exist to
# change code.
IMPLEMENTATION_DEFAULTS = ROLE_DEFAULTS["backend"]

# A role nobody has described. Read-only plus talking, because guessing that an
# unknown role may write to the repository is the wrong way to be wrong.
GENERIC_DEFAULTS = frozenset(
    {
        Capability.READ_FILES,
        Capability.INSPECT_REPO,
        Capability.GIT_DIFF,
        Capability.MESSAGE_AGENT,
        Capability.REQUEST_TASK,
    }
)

WRITE_ROLES = frozenset({"backend", "frontend", "mobile", "database", "integrator"})

# Human-readable phrasing for the prompt, so an agent is told the same rules the
# code enforces.
DESCRIPTIONS: dict[Capability, str] = {
    Capability.READ_FILES: "read files in your working directory",
    Capability.EDIT_FILES: "create and modify source files",
    Capability.RUN_COMMAND: "run development commands",
    Capability.RUN_TESTS: "run the test suite",
    Capability.CREATE_TASK: "create tasks directly",
    Capability.UPDATE_TASK: "change existing tasks",
    Capability.REQUEST_TASK: "request follow-up work from another agent",
    Capability.MESSAGE_AGENT: "send messages to other agents",
    Capability.GIT_DIFF: "inspect the git diff",
    Capability.GIT_COMMIT: "commit changes",
    Capability.GIT_MERGE: "merge branches",
    Capability.INSPECT_REPO: "explore the repository",
}


def defaults_for_role(role: str) -> frozenset[Capability]:
    """Capabilities for a role that did not declare any."""
    normalised = (role or "").strip().lower()
    if normalised in ROLE_DEFAULTS:
        return ROLE_DEFAULTS[normalised]
    if normalised in WRITE_ROLES:
        return IMPLEMENTATION_DEFAULTS
    return GENERIC_DEFAULTS


def parse_capabilities(values: list[str]) -> tuple[frozenset[Capability], list[str]]:
    """Turn configured strings into capabilities.

    Returns (recognised, unknown). Unknown names are reported rather than
    ignored: a typo in a permission list must not silently grant or withhold.
    """
    recognised: set[Capability] = set()
    unknown: list[str] = []
    for value in values:
        name = (value or "").strip().lower()
        if not name:
            continue
        try:
            recognised.add(Capability(name))
        except ValueError:
            unknown.append(value)
    return frozenset(recognised), unknown


def denied_tools(granted: frozenset[Capability]) -> list[str]:
    """CLI tools to deny, given what the agent has.

    Deterministically ordered so a command line is reproducible and diffable.
    """
    denied: set[str] = set()
    for capability, tools in DENIED_TOOLS_WITHOUT.items():
        if capability not in granted:
            denied.update(tools)
    return sorted(denied)


def describe(granted: frozenset[Capability]) -> tuple[list[str], list[str]]:
    """(may, may not) phrases for the prompt, in a stable order."""
    may: list[str] = []
    may_not: list[str] = []
    for capability in Capability:
        phrase = DESCRIPTIONS.get(capability, capability.value)
        if capability in granted:
            may.append(phrase)
        else:
            may_not.append(phrase)
    return may, may_not

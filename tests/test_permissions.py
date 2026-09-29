"""Phase 14: capabilities enforced by code, not by prompts.

The claim being tested is that a reviewer without edit_files genuinely cannot
edit, through three independent layers, and that every refusal leaves a record.
"""

from __future__ import annotations

import pytest

from agentos.config import Config
from agentos.db.session import Database
from agentos.schemas.capabilities import (
    Capability,
    GENERIC_DEFAULTS,
    ROLE_DEFAULTS,
    defaults_for_role,
    denied_tools,
    describe,
    parse_capabilities,
)
from agentos.schemas.dto import AgentView
from agentos.schemas.runtime import RunRequest
from agentos.services.agents import AgentService
from agentos.services.permissions import PermissionService
from tests.test_agents import StubRuntime

CONFIG = {
    "project": {"name": "Perms"},
    "agents": {
        "manager": {"role": "manager"},
        "backend": {"role": "backend"},
        "frontend": {"role": "frontend"},
        "qa": {"role": "qa"},
        "reviewer": {"role": "reviewer"},
    },
}


@pytest.fixture
def config() -> Config:
    return Config.model_validate(CONFIG)


@pytest.fixture
def permissions(db: Database, config: Config) -> PermissionService:
    return PermissionService(db, config)


def agent(name: str, role: str) -> AgentView:
    return AgentView(id=1, name=name, role=role)


# -------------------------------------------------------------- pure defaults


def test_reviewer_cannot_edit_by_default() -> None:
    """The headline rule: review does not imply write access."""
    assert Capability.EDIT_FILES not in defaults_for_role("reviewer")
    assert Capability.READ_FILES in defaults_for_role("reviewer")
    assert Capability.GIT_DIFF in defaults_for_role("reviewer")


def test_backend_can_edit_by_default() -> None:
    granted = defaults_for_role("backend")
    assert Capability.EDIT_FILES in granted
    assert Capability.RUN_TESTS in granted


def test_frontend_is_an_implementation_role() -> None:
    assert Capability.EDIT_FILES in defaults_for_role("frontend")


def test_manager_plans_and_does_not_implement() -> None:
    granted = defaults_for_role("manager")
    assert Capability.CREATE_TASK in granted
    assert Capability.EDIT_FILES not in granted


def test_qa_runs_tests_but_does_not_fix() -> None:
    granted = defaults_for_role("qa")
    assert Capability.RUN_TESTS in granted
    assert Capability.EDIT_FILES not in granted


def test_unknown_role_gets_read_only_defaults() -> None:
    """Guessing that an unknown role may write is the wrong way to be wrong."""
    granted = defaults_for_role("astrologer")
    assert granted == GENERIC_DEFAULTS
    assert Capability.EDIT_FILES not in granted
    assert Capability.RUN_COMMAND not in granted


def test_role_lookup_is_case_insensitive() -> None:
    assert defaults_for_role("Backend") == ROLE_DEFAULTS["backend"]


def test_nobody_gets_merge_by_default() -> None:
    """Merging is an integration decision, not an agent's to take."""
    for role in ("manager", "backend", "frontend", "qa", "reviewer", "unknown"):
        assert Capability.GIT_MERGE not in defaults_for_role(role)


# ------------------------------------------------------------- parsing config


def test_parse_recognises_valid_names() -> None:
    granted, unknown = parse_capabilities(["read_files", "git_diff"])
    assert granted == {Capability.READ_FILES, Capability.GIT_DIFF}
    assert unknown == []


def test_parse_reports_unknown_names() -> None:
    """A typo must not silently grant or withhold."""
    granted, unknown = parse_capabilities(["read_files", "edit_fils"])
    assert granted == {Capability.READ_FILES}
    assert unknown == ["edit_fils"]


def test_parse_ignores_blanks_and_normalises_case() -> None:
    granted, unknown = parse_capabilities([" READ_FILES ", "", "  "])
    assert granted == {Capability.READ_FILES}
    assert unknown == []


# ------------------------------------------------------- layer 1: denied tools


def test_missing_edit_denies_the_write_tools() -> None:
    denied = denied_tools(defaults_for_role("reviewer"))
    assert "Edit" in denied
    assert "Write" in denied
    assert "NotebookEdit" in denied


def test_agent_with_edit_is_denied_nothing_for_editing() -> None:
    assert denied_tools(defaults_for_role("backend")) == []


def test_missing_run_command_denies_shells() -> None:
    denied = denied_tools(GENERIC_DEFAULTS)
    assert "Bash" in denied
    assert "PowerShell" in denied


def test_denied_tools_are_deterministic() -> None:
    """A reproducible command line is a diffable one."""
    first = denied_tools(GENERIC_DEFAULTS)
    assert first == sorted(first)
    assert first == denied_tools(GENERIC_DEFAULTS)


def test_denied_tools_reach_the_command_line() -> None:
    from agentos.runtime.claude_cli import ClaudeRunner

    class FakeRunner(ClaudeRunner):
        @property
        def executable(self) -> str:
            return "claude"

    argv, _ = FakeRunner().build_command(
        RunRequest(prompt="x", disallowed_tools=["Edit", "Write"])
    )
    assert "--disallowedTools" in argv
    index = argv.index("--disallowedTools")
    assert argv[index + 1 : index + 3] == ["Edit", "Write"]


def test_no_disallowed_flag_when_nothing_is_denied() -> None:
    from agentos.runtime.claude_cli import ClaudeRunner

    class FakeRunner(ClaudeRunner):
        @property
        def executable(self) -> str:
            return "claude"

    argv, _ = FakeRunner().build_command(RunRequest(prompt="x"))
    assert "--disallowedTools" not in argv


async def test_reviewer_run_denies_edit_tools(db, config, tmp_path) -> None:
    """End to end: the reviewer's actual invocation forbids editing."""
    runtime = StubRuntime()
    service = AgentService(db, config, runtime, tmp_path)
    service.sync_from_config()
    await service.run_agent("reviewer", "review it")
    assert "Edit" in runtime.requests[0].disallowed_tools

    await service.run_agent("backend", "build it")
    assert runtime.requests[1].disallowed_tools == []


# ---------------------------------------------------------------- grants logic


def test_config_capabilities_override_role_defaults(db: Database) -> None:
    config = Config.model_validate(
        {"agents": {"reviewer": {"role": "reviewer", "capabilities": ["edit_files"]}}}
    )
    permissions = PermissionService(db, config)
    grants = permissions.grants_for(agent("reviewer", "reviewer"))
    assert grants.has(Capability.EDIT_FILES)
    # An explicit list is exhaustive, not additive.
    assert not grants.has(Capability.READ_FILES)
    assert grants.source == "config"


def test_empty_capability_list_grants_nothing(db: Database) -> None:
    """Distinct from omitting the key, which means "use role defaults"."""
    config = Config.model_validate(
        {"agents": {"locked": {"role": "backend", "capabilities": []}}}
    )
    grants = PermissionService(db, config).grants_for(agent("locked", "backend"))
    assert grants.capabilities == frozenset()


def test_omitted_capabilities_use_role_defaults(permissions: PermissionService) -> None:
    grants = permissions.grants_for(agent("backend", "backend"))
    assert grants.has(Capability.EDIT_FILES)
    assert "defaults for role backend" in grants.source


def test_runtime_grant_and_revoke(permissions: PermissionService) -> None:
    reviewer = agent("reviewer", "reviewer")
    assert not permissions.allows(reviewer, Capability.EDIT_FILES)

    permissions.grant("reviewer", Capability.EDIT_FILES)
    assert permissions.allows(reviewer, Capability.EDIT_FILES)
    assert "runtime override" in permissions.grants_for(reviewer).source

    permissions.revoke("reviewer", Capability.EDIT_FILES)
    assert not permissions.allows(reviewer, Capability.EDIT_FILES)


def test_revoking_a_default_capability(permissions: PermissionService) -> None:
    backend = agent("backend", "backend")
    permissions.revoke("backend", Capability.EDIT_FILES)
    assert not permissions.allows(backend, Capability.EDIT_FILES)
    assert "Edit" in permissions.grants_for(backend).denied_tools


def test_unknown_names_are_surfaced_on_the_grants(db: Database) -> None:
    config = Config.model_validate(
        {"agents": {"a": {"role": "backend", "capabilities": ["read_files", "nope"]}}}
    )
    grants = PermissionService(db, config).grants_for(agent("a", "backend"))
    assert grants.unknown == ["nope"]


# -------------------------------------------------------------------- denials


def test_check_allows_and_records_nothing(permissions: PermissionService) -> None:
    assert permissions.check("backend", Capability.EDIT_FILES, "edit_files")
    assert permissions.denials() == []


def test_check_refuses_and_records(permissions: PermissionService) -> None:
    allowed = permissions.check(
        "reviewer", Capability.EDIT_FILES, "edit_files", "touched app.py", "REV-1"
    )
    assert not allowed

    records = permissions.denials()
    assert len(records) == 1
    assert records[0].agent == "reviewer"
    assert records[0].capability == "edit_files"
    assert records[0].task_key == "REV-1"
    assert "app.py" in records[0].detail


def test_denials_can_be_filtered_by_agent(permissions: PermissionService) -> None:
    permissions.deny("reviewer", Capability.EDIT_FILES, "edit_files")
    permissions.deny("qa", Capability.EDIT_FILES, "edit_files")
    assert len(permissions.denials(agent="reviewer")) == 1
    assert len(permissions.denials()) == 2


def test_denial_detail_is_capped(permissions: PermissionService) -> None:
    permissions.deny("reviewer", Capability.EDIT_FILES, "edit_files", "x" * 9000)
    assert len(permissions.denials()[0].detail) <= 2000


def test_denials_survive_reopen(tmp_path, config: Config) -> None:
    path = tmp_path / "denials.db"
    first = Database(path)
    first.create_all()
    PermissionService(first, config).deny(
        "reviewer", Capability.EDIT_FILES, "edit_files", "tried it"
    )
    first.dispose()

    second = Database(path)
    second.create_all()
    assert len(PermissionService(second, config).denials()) == 1
    second.dispose()


# -------------------------------------------------------------- prompt wording


def test_capability_prompt_states_both_sides(permissions: PermissionService) -> None:
    text = permissions.capability_prompt(agent("reviewer", "reviewer"))
    assert "You MAY:" in text
    assert "You MAY NOT:" in text
    assert "create and modify source files" in text
    assert "enforced by the orchestrator, not by trust" in text


def test_capability_prompt_for_an_agent_with_everything(db: Database) -> None:
    config = Config.model_validate(
        {
            "agents": {
                "god": {
                    "role": "backend",
                    "capabilities": [c.value for c in Capability],
                }
            }
        }
    )
    text = PermissionService(db, config).capability_prompt(agent("god", "backend"))
    assert "You MAY NOT:" not in text


def test_capability_prompt_appears_in_the_system_prompt(
    db, config: Config, tmp_path
) -> None:
    service = AgentService(db, config, StubRuntime(), tmp_path)
    service.sync_from_config()
    prompt = service.system_prompt_for(service.get_agent("reviewer"))
    assert "## CAPABILITIES" in prompt
    assert "You MAY NOT:" in prompt


def test_describe_is_stably_ordered() -> None:
    first_may, first_not = describe(defaults_for_role("reviewer"))
    second_may, second_not = describe(defaults_for_role("reviewer"))
    assert first_may == second_may
    assert first_not == second_not


def test_permission_mode_reaches_the_command_line() -> None:
    from agentos.runtime.claude_cli import ClaudeRunner

    class FakeRunner(ClaudeRunner):
        @property
        def executable(self) -> str:
            return "claude"

    argv, _ = FakeRunner().build_command(
        RunRequest(prompt="x", permission_mode="acceptEdits")
    )
    index = argv.index("--permission-mode")
    assert argv[index + 1] == "acceptEdits"

    argv, _ = FakeRunner().build_command(RunRequest(prompt="x"))
    assert "--permission-mode" not in argv


def test_configured_permission_mode_wins() -> None:
    from agentos.runtime.claude_cli import ClaudeRunner

    class FakeRunner(ClaudeRunner):
        @property
        def executable(self) -> str:
            return "claude"

    runner = FakeRunner(extra_args=["--permission-mode", "plan"])
    argv, _ = runner.build_command(
        RunRequest(prompt="x", permission_mode="acceptEdits")
    )
    assert argv.count("--permission-mode") == 1
    assert argv[argv.index("--permission-mode") + 1] == "plan"


async def test_only_editors_get_accept_edits(db, config, tmp_path) -> None:
    """Print mode cannot prompt, so an editor must be pre-approved to edit."""
    runtime = StubRuntime()
    service = AgentService(db, config, runtime, tmp_path)
    service.sync_from_config()
    await service.run_agent("backend", "build it")
    assert runtime.requests[0].permission_mode == "acceptEdits"

    await service.run_agent("reviewer", "review it")
    assert runtime.requests[1].permission_mode is None

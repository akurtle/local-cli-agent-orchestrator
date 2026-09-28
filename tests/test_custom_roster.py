"""Phase 9: arbitrary, user-defined agent rosters.

Nothing in the orchestrator may assume the five starter roles exist.
"""

from __future__ import annotations

import json

import pytest

from agentos.config import Config, ConfigError, load_config
from agentos.db.session import Database
from agentos.prompts.loader import resolve_brief
from agentos.schemas.enums import AgentStatus, RunStatus
from agentos.schemas.plan import PLAN_BEGIN, PLAN_END
from agentos.schemas.runtime import RunResult
from agentos.services.agents import AgentService
from agentos.services.objectives import ObjectiveService
from agentos.services.tasks import TaskService
from tests.test_agents import StubRuntime

# A roster that shares no names with the defaults.
CUSTOM = {
    "project": {"name": "Custom"},
    "orchestrator": {"manager_role": "architect"},
    "agents": {
        "chief": {"role": "architect", "description": "Plans the system."},
        "api": {"role": "backend", "description": "HTTP surface."},
        "database": {"role": "database", "description": "Schema and migrations."},
        "mobile": {"role": "mobile"},
        "security": {"role": "security"},
    },
}


@pytest.fixture
def config() -> Config:
    return Config.model_validate(CUSTOM)


@pytest.fixture
def service(db: Database, config: Config, tmp_path):
    svc = AgentService(db, config, StubRuntime(), tmp_path)
    svc.sync_from_config()
    return svc


# ----------------------------------------------------------------- the roster


def test_custom_roles_are_registered(service: AgentService) -> None:
    roles = {a.role for a in service.list_agents()}
    assert roles == {"architect", "backend", "database", "mobile", "security"}


def test_no_default_agents_are_invented(service: AgentService) -> None:
    names = {a.name for a in service.list_agents()}
    assert names == {"chief", "api", "database", "mobile", "security"}
    assert "manager" not in names
    assert "frontend" not in names


def test_unknown_roles_get_a_neutral_brief() -> None:
    for role in ("database", "mobile", "security", "sre", "anything"):
        brief = resolve_brief(role)
        assert role in brief
        assert "{role}" not in brief


def test_prompt_names_the_custom_role(service: AgentService) -> None:
    agent = service.get_agent("security")
    prompt = service.system_prompt_for(agent)
    assert "security" in prompt
    # It is told who else exists, and not itself.
    assert "`api`" in prompt
    assert "- `security`" not in prompt


def test_per_agent_prompt_file_override(db: Database, tmp_path) -> None:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "custom-brief.md").write_text(
        "# Role: Architect\nYou design.", encoding="utf-8"
    )
    config = Config.model_validate(
        {
            "agents": {
                "chief": {"role": "architect", "prompt": "prompts/custom-brief.md"}
            }
        }
    )
    service = AgentService(db, config, StubRuntime(), tmp_path)
    service.sync_from_config()
    assert "You design." in service.system_prompt_for(service.get_agent("chief"))


# -------------------------------------------------------------- manager role


async def test_configured_manager_role_is_used(db, config, tmp_path) -> None:
    """The planning role is configurable, so `architect` can plan."""
    plan = {
        "objective": "Ship it",
        "tasks": [
            {
                "temp_id": "A",
                "title": "Design the schema",
                "assigned_agent": "database",
                "description": "x",
                "acceptance_criteria": ["y"],
                "depends_on": [],
            }
        ],
    }
    reply = "\n".join([PLAN_BEGIN, json.dumps(plan), PLAN_END])

    class Scripted(StubRuntime):
        async def run(self, request, on_event=None):
            self.requests.append(request)
            return RunResult(
                status=RunStatus.SUCCEEDED, session_id="s", exit_code=0, text=reply
            )

    agents = AgentService(db, config, Scripted(), tmp_path)
    agents.sync_from_config()
    objectives = ObjectiveService(db, config, agents, TaskService(db, config))

    assert objectives.manager_name() == "chief"
    proposal = await objectives.propose("Ship it")
    assert proposal.ok
    created = objectives.approve(proposal)
    assert created[0].assigned_agent == "database"


def test_missing_manager_role_names_the_configured_roles(db, tmp_path) -> None:
    config = Config.model_validate(
        {
            "orchestrator": {"manager_role": "lead"},
            "agents": {"api": {"role": "backend"}},
        }
    )
    agents = AgentService(db, config, StubRuntime(), tmp_path)
    agents.sync_from_config()
    objectives = ObjectiveService(db, config, agents, TaskService(db, config))
    with pytest.raises(ValueError, match="no agent has the 'lead' role"):
        objectives.manager_name()


# ------------------------------------------------------------ config handling


def test_runtime_scalar_shorthand() -> None:
    assert Config.model_validate({"runtime": "claude"}).runtime.name == "claude"


def test_runtime_mapping_still_works() -> None:
    config = Config.model_validate({"runtime": {"name": "claude", "model": "m"}})
    assert config.runtime.model == "m"


def test_agents_with_role_helper() -> None:
    config = Config.model_validate(
        {"agents": {"a": {"role": "backend"}, "b": {"role": "backend"}}}
    )
    assert config.agents_with_role("backend") == ["a", "b"]
    assert config.agents_with_role("nope") == []


def test_yaml_roundtrip_of_a_custom_roster(tmp_path) -> None:
    path = tmp_path / "agentos.yaml"
    path.write_text(
        "runtime: claude\n"
        "orchestrator:\n"
        "  manager_role: architect\n"
        "agents:\n"
        "  chief:\n"
        "    role: architect\n"
        "  mobile:\n"
        "    role: mobile\n"
        "    prompt: prompts/mobile.md\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.runtime.name == "claude"
    assert config.orchestrator.manager_role == "architect"
    assert config.agents["mobile"].prompt == "prompts/mobile.md"


def test_unknown_agent_key_is_still_rejected(tmp_path) -> None:
    """Flexibility about roles must not mean flexibility about typos."""
    path = tmp_path / "agentos.yaml"
    path.write_text("agents:\n  api:\n    rol: backend\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


# ------------------------------------------------------- retiring old agents


def test_agent_removed_from_config_goes_offline(db, config, tmp_path) -> None:
    """Its history is worth keeping, but it must stop taking work."""
    service = AgentService(db, config, StubRuntime(), tmp_path)
    service.sync_from_config()
    service.agents.set_session_id("mobile", "precious-session")

    del config.agents["mobile"]
    service.sync_from_config()

    retired = service.get_agent("mobile")
    assert retired.status is AgentStatus.OFFLINE
    # Not deleted: the session and its run history survive.
    assert retired.session_id == "precious-session"


def test_offline_agent_is_not_dispatchable() -> None:
    from agentos.services.scheduler import AVAILABLE_AGENT_STATUSES

    assert AgentStatus.OFFLINE not in AVAILABLE_AGENT_STATUSES


def test_returning_agent_comes_back_online(db, config, tmp_path) -> None:
    service = AgentService(db, config, StubRuntime(), tmp_path)
    service.sync_from_config()

    removed = config.agents.pop("mobile")
    service.sync_from_config()
    assert service.get_agent("mobile").status is AgentStatus.OFFLINE

    config.agents["mobile"] = removed
    service.sync_from_config()
    assert service.get_agent("mobile").status is AgentStatus.IDLE

"""Phase 2: agent persistence, sessions, prompts and state transitions.

These tests use a stub runtime rather than the real CLI, so they are fast,
offline and free. Session *continuity* against the real CLI is verified by the
manual demonstration, not here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentos.config import Config
from agentos.db.session import Database
from agentos.prompts.loader import (
    PromptNotFound,
    build_system_prompt,
    resolve_brief,
    response_contract,
)
from agentos.repositories.agents import AgentNotFound, AgentRepository
from agentos.schemas.enums import AgentStatus, RunStatus
from agentos.schemas.responses import RESPONSE_BEGIN, RESPONSE_END
from agentos.schemas.runtime import RunRequest, RunResult
from agentos.services.agents import (
    AgentBusy,
    AgentPaused,
    AgentService,
    InvalidTransition,
)

CONFIG_DICT = {
    "project": {"name": "Test Project"},
    "agents": {
        "manager": {"role": "manager", "description": "Plans work."},
        "backend": {"role": "backend", "description": "Owns the server."},
        "frontend": {"role": "frontend"},
    },
}


class StubRuntime:
    """Records requests and returns scripted results."""

    name = "claude"

    def __init__(self, results: list[RunResult] | None = None) -> None:
        self.requests: list[RunRequest] = []
        self.results = results or []
        self._default_session = "session-fresh"

    def preflight(self) -> dict[str, str]:
        return {"runtime": self.name}

    async def run(self, request: RunRequest, on_event=None) -> RunResult:
        self.requests.append(request)
        if self.results:
            return self.results.pop(0)
        return RunResult(
            status=RunStatus.SUCCEEDED,
            session_id=request.session_id or self._default_session,
            exit_code=0,
            text="ok",
        )

    async def resume(self, session_id, prompt, on_event=None, **overrides):
        return await self.run(
            RunRequest(prompt=prompt, session_id=session_id, resume=True, **overrides)
        )

    @staticmethod
    def is_stale_session(result: RunResult) -> bool:
        return "no conversation found" in (result.text or "").lower()


def make_task(db: Database, key: str = "T-1") -> int:
    """Create a real task row.

    agents.current_task_id is a real foreign key, so tests must reference a task
    that exists rather than inventing an id.
    """
    from agentos.db.models import Task

    with db.session() as session:
        task = Task(key=key, title=key)
        session.add(task)
        session.flush()
        return task.id


@pytest.fixture
def config() -> Config:
    return Config.model_validate(CONFIG_DICT)


@pytest.fixture
def runtime() -> StubRuntime:
    return StubRuntime()


@pytest.fixture
def service(db: Database, config: Config, runtime: StubRuntime, tmp_path: Path):
    return AgentService(db=db, config=config, runtime=runtime, project_root=tmp_path)


# ------------------------------------------------------- loading from config


def test_sync_creates_configured_agents(service: AgentService) -> None:
    views, created = service.sync_from_config()
    assert {v.name for v in views} == {"manager", "backend", "frontend"}
    assert set(created) == {"manager", "backend", "frontend"}


def test_sync_is_idempotent(service: AgentService) -> None:
    service.sync_from_config()
    views, created = service.sync_from_config()
    assert created == []
    assert len(views) == 3


def test_sync_refreshes_definition_but_keeps_state(
    service: AgentService, config: Config
) -> None:
    """Config owns definitions; the database owns session and status."""
    service.sync_from_config()
    service.agents.set_session_id("backend", "keep-me")
    service.transition("backend", AgentStatus.WORKING)

    config.agents["backend"].description = "New remit."
    service.sync_from_config()

    agent = service.get_agent("backend")
    assert agent.description == "New remit."
    assert agent.session_id == "keep-me"
    assert agent.status is AgentStatus.WORKING


def test_agent_defaults(service: AgentService) -> None:
    service.sync_from_config()
    agent = service.get_agent("frontend")
    assert agent.status is AgentStatus.IDLE
    assert agent.session_id is None
    assert agent.runtime == "claude"
    assert agent.current_task_id is None


def test_get_agent_lazily_syncs(service: AgentService) -> None:
    """Asking for a configured agent works without an explicit sync first."""
    agent = service.get_agent("manager")
    assert agent.role == "manager"


def test_unknown_agent_raises_with_roster(service: AgentService) -> None:
    with pytest.raises(AgentNotFound, match="Configured agents: backend, frontend"):
        service.get_agent("nope")


def test_arbitrary_roles_are_supported(db: Database, tmp_path: Path) -> None:
    """Nothing may hardcode the five default roles."""
    config = Config.model_validate(
        {"agents": {"security": {"role": "security"}, "mobile": {"role": "mobile"}}}
    )
    service = AgentService(db, config, StubRuntime(), tmp_path)
    views, _ = service.sync_from_config()
    assert {v.role for v in views} == {"security", "mobile"}


# -------------------------------------------------------------- transitions


def test_legal_transition(service: AgentService, db: Database) -> None:
    service.sync_from_config()
    task_id = make_task(db)
    agent = service.transition("backend", AgentStatus.WORKING, current_task_id=task_id)
    assert agent.status is AgentStatus.WORKING
    assert agent.current_task_id == task_id


def test_illegal_transition_is_refused(service: AgentService) -> None:
    service.sync_from_config()
    with pytest.raises(InvalidTransition, match="cannot go from idle to waiting"):
        service.transition("backend", AgentStatus.WAITING)


def test_transition_to_same_status_is_a_noop(service: AgentService) -> None:
    service.sync_from_config()
    agent = service.transition("backend", AgentStatus.IDLE)
    assert agent.status is AgentStatus.IDLE


def test_clear_task_on_transition(service: AgentService, db: Database) -> None:
    service.sync_from_config()
    service.transition("backend", AgentStatus.WORKING, current_task_id=make_task(db))
    agent = service.transition("backend", AgentStatus.IDLE, clear_task=True)
    assert agent.current_task_id is None


def test_pause_and_unpause(service: AgentService) -> None:
    service.sync_from_config()
    assert service.pause("backend").status is AgentStatus.PAUSED
    assert service.unpause("backend").status is AgentStatus.IDLE


def test_cannot_pause_a_working_agent(service: AgentService) -> None:
    service.sync_from_config()
    service.transition("backend", AgentStatus.WORKING)
    with pytest.raises(AgentBusy):
        service.pause("backend")


# ---------------------------------------------------------------- execution


async def test_first_run_starts_and_persists_session(
    service: AgentService, runtime: StubRuntime
) -> None:
    outcome = await service.run_agent("backend", "do the thing")
    assert outcome.ok
    assert outcome.resumed is False
    assert runtime.requests[0].resume is False
    # Session captured from the run and written to the database.
    assert service.get_agent("backend").session_id == "session-fresh"


async def test_second_run_resumes_stored_session(
    service: AgentService, runtime: StubRuntime
) -> None:
    await service.run_agent("backend", "first")
    outcome = await service.run_agent("backend", "second")
    assert outcome.resumed is True
    assert runtime.requests[1].resume is True
    assert runtime.requests[1].session_id == "session-fresh"


async def test_sessions_are_independent_per_agent(db: Database, tmp_path: Path) -> None:
    config = Config.model_validate(CONFIG_DICT)

    class PerAgentRuntime(StubRuntime):
        def __init__(self) -> None:
            super().__init__()
            self.counter = 0

        async def run(self, request: RunRequest, on_event=None) -> RunResult:
            self.requests.append(request)
            if request.resume:
                session = request.session_id
            else:
                self.counter += 1
                session = f"session-{self.counter}"
            return RunResult(
                status=RunStatus.SUCCEEDED, session_id=session, exit_code=0, text="ok"
            )

    service = AgentService(db, config, PerAgentRuntime(), tmp_path)
    await service.run_agent("backend", "x")
    await service.run_agent("frontend", "y")

    backend = service.get_agent("backend").session_id
    frontend = service.get_agent("frontend").session_id
    assert backend and frontend and backend != frontend


async def test_agent_returns_to_idle_after_success(service: AgentService) -> None:
    await service.run_agent("backend", "x")
    assert service.get_agent("backend").status is AgentStatus.IDLE


async def test_failed_run_marks_agent_failed(db: Database, tmp_path: Path) -> None:
    runtime = StubRuntime(
        [RunResult(status=RunStatus.FAILED, exit_code=1, error="boom", text="")]
    )
    service = AgentService(db, Config.model_validate(CONFIG_DICT), runtime, tmp_path)
    outcome = await service.run_agent("backend", "x")
    assert not outcome.ok
    assert service.get_agent("backend").status is AgentStatus.FAILED


async def test_failed_agent_can_run_again(db: Database, tmp_path: Path) -> None:
    """A failed agent must not be permanently stuck."""
    runtime = StubRuntime([RunResult(status=RunStatus.FAILED, exit_code=1)])
    service = AgentService(db, Config.model_validate(CONFIG_DICT), runtime, tmp_path)
    await service.run_agent("backend", "x")
    outcome = await service.run_agent("backend", "retry")
    assert outcome.ok
    assert service.get_agent("backend").status is AgentStatus.IDLE


async def test_stale_session_falls_back_to_fresh(db: Database, tmp_path: Path) -> None:
    """A vanished session is recoverable, not a hard failure."""
    runtime = StubRuntime(
        [
            RunResult(status=RunStatus.SUCCEEDED, session_id="old", exit_code=0),
            RunResult(
                status=RunStatus.FAILED,
                exit_code=1,
                text="No conversation found with session ID: old",
            ),
            RunResult(status=RunStatus.SUCCEEDED, session_id="new", exit_code=0),
        ]
    )
    service = AgentService(db, Config.model_validate(CONFIG_DICT), runtime, tmp_path)
    await service.run_agent("backend", "first")

    outcome = await service.run_agent("backend", "second")
    assert outcome.ok
    assert outcome.session_restarted is True
    assert outcome.resumed is False
    assert service.get_agent("backend").session_id == "new"
    # Third request must have started a new session, not resumed the dead one.
    assert runtime.requests[2].resume is False


async def test_genuine_failure_is_not_retried(db: Database, tmp_path: Path) -> None:
    """Stale-session detection must stay narrow."""
    runtime = StubRuntime(
        [
            RunResult(status=RunStatus.SUCCEEDED, session_id="old", exit_code=0),
            RunResult(status=RunStatus.FAILED, exit_code=1, text="syntax error"),
        ]
    )
    service = AgentService(db, Config.model_validate(CONFIG_DICT), runtime, tmp_path)
    await service.run_agent("backend", "first")
    outcome = await service.run_agent("backend", "second")
    assert not outcome.ok
    assert outcome.session_restarted is False
    assert len(runtime.requests) == 2  # no third attempt


async def test_reset_session_forces_fresh_start(
    service: AgentService, runtime: StubRuntime
) -> None:
    await service.run_agent("backend", "first")
    service.reset_session("backend")
    assert service.get_agent("backend").session_id is None
    await service.run_agent("backend", "second")
    assert runtime.requests[1].resume is False


async def test_run_persists_a_run_row(service: AgentService, db: Database) -> None:
    outcome = await service.run_agent("backend", "x")
    from agentos.db.models import Run

    with db.session() as session:
        row = session.get(Run, outcome.run_id)
        assert row is not None
        assert row.agent_id == service.get_agent("backend").id
        assert row.status == "succeeded"


async def test_paused_agent_refuses_work(service: AgentService) -> None:
    service.sync_from_config()
    service.pause("backend")
    with pytest.raises(AgentPaused):
        await service.run_agent("backend", "x")


async def test_busy_agent_refuses_work(service: AgentService, db: Database) -> None:
    service.sync_from_config()
    task_id = make_task(db)
    service.transition("backend", AgentStatus.WORKING, current_task_id=task_id)
    with pytest.raises(AgentBusy, match=f"task {task_id}"):
        await service.run_agent("backend", "x")


async def test_runtime_crash_does_not_leave_agent_working(
    db: Database, tmp_path: Path
) -> None:
    class ExplodingRuntime(StubRuntime):
        async def run(self, request: RunRequest, on_event=None) -> RunResult:
            raise OSError("process died")

    service = AgentService(
        db, Config.model_validate(CONFIG_DICT), ExplodingRuntime(), tmp_path
    )
    with pytest.raises(OSError):
        await service.run_agent("backend", "x")
    assert service.get_agent("backend").status is AgentStatus.FAILED


async def test_system_prompt_is_sent_on_every_run(
    service: AgentService, runtime: StubRuntime
) -> None:
    await service.run_agent("backend", "x")
    await service.run_agent("backend", "y")
    assert all("Role: Backend" in (r.system_prompt or "") for r in runtime.requests)


# ------------------------------------------------------------------ prompts


def test_contract_matches_the_real_delimiters() -> None:
    contract = response_contract()
    assert RESPONSE_BEGIN in contract
    assert RESPONSE_END in contract


def test_builtin_briefs_exist_for_default_roles() -> None:
    for role in ("manager", "backend", "frontend", "qa", "reviewer"):
        assert resolve_brief(role).lower().startswith("# role:")


def test_unknown_role_gets_generic_brief() -> None:
    brief = resolve_brief("security")
    assert "security" in brief
    assert "{role}" not in brief


def test_project_prompt_overrides_builtin(tmp_path: Path) -> None:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "backend.md").write_text("CUSTOM BRIEF", encoding="utf-8")
    assert resolve_brief("backend", project_root=tmp_path) == "CUSTOM BRIEF"


def test_explicit_prompt_path_wins(tmp_path: Path) -> None:
    custom = tmp_path / "my-brief.md"
    custom.write_text("EXPLICIT", encoding="utf-8")
    assert (
        resolve_brief("backend", project_root=tmp_path, explicit_path="my-brief.md")
        == "EXPLICIT"
    )


def test_missing_explicit_prompt_raises(tmp_path: Path) -> None:
    with pytest.raises(PromptNotFound, match="not found"):
        resolve_brief("backend", project_root=tmp_path, explicit_path="nope.md")


def test_system_prompt_includes_roster_but_not_self() -> None:
    prompt = build_system_prompt(
        agent_name="backend",
        role="backend",
        roster={"backend": "backend", "frontend": "frontend", "qa": "qa"},
    )
    assert "`frontend`" in prompt
    assert "`qa`" in prompt
    # An agent should not be told it may message itself.
    assert "- `backend`" not in prompt


def test_system_prompt_contains_identity_and_contract() -> None:
    prompt = build_system_prompt(
        agent_name="api", role="backend", description="Owns HTTP.", project_name="Demo"
    )
    assert "`api`" in prompt
    assert "Demo" in prompt
    assert "Owns HTTP." in prompt
    assert RESPONSE_BEGIN in prompt


def test_service_builds_prompt_with_configured_override(
    service: AgentService, tmp_path: Path, config: Config
) -> None:
    config.agents["backend"].prompt = "custom.md"
    (tmp_path / "custom.md").write_text("OVERRIDDEN", encoding="utf-8")
    agent = service.get_agent("backend")
    assert "OVERRIDDEN" in service.system_prompt_for(agent)


# --------------------------------------------------------------- repository


def test_repository_returns_dtos_not_orm(db: Database) -> None:
    """ORM instances must not escape the repository layer."""
    repo = AgentRepository(db)
    repo.upsert("solo", "backend")
    view = repo.get("solo")
    assert not hasattr(view, "_sa_instance_state")
    # Frozen DTO: callers cannot accidentally mutate persisted state.
    with pytest.raises(Exception):
        view.status = AgentStatus.FAILED


def test_repository_get_missing_raises(db: Database) -> None:
    with pytest.raises(AgentNotFound):
        AgentRepository(db).get("ghost")


def test_repository_find_missing_returns_none(db: Database) -> None:
    assert AgentRepository(db).find("ghost") is None


def test_repository_find_by_role(db: Database) -> None:
    repo = AgentRepository(db)
    repo.upsert("api", "backend")
    repo.upsert("worker", "backend")
    repo.upsert("ui", "frontend")
    assert {v.name for v in repo.find_by_role("backend")} == {"api", "worker"}


def test_repository_survives_reopen(tmp_path: Path) -> None:
    """Session IDs must outlive the process."""
    db_path = tmp_path / "state.db"
    first = Database(db_path)
    first.create_all()
    AgentRepository(first).upsert("backend", "backend")
    AgentRepository(first).set_session_id("backend", "persisted-session")
    first.dispose()

    second = Database(db_path)
    second.create_all()
    assert AgentRepository(second).get("backend").session_id == "persisted-session"
    second.dispose()


async def test_agents_run_concurrently(db: Database, tmp_path: Path) -> None:
    """Independent agents must be able to work at the same time.

    Phase 3 depends on this; it also guards the thread-safety of the database
    layer, since each run writes state from a worker thread.
    """
    import asyncio

    class SlowRuntime(StubRuntime):
        async def run(self, request: RunRequest, on_event=None) -> RunResult:
            self.requests.append(request)
            await asyncio.sleep(0.2)
            return RunResult(
                status=RunStatus.SUCCEEDED,
                session_id=f"s-{len(self.requests)}",
                exit_code=0,
                text="ok",
            )

    service = AgentService(
        db, Config.model_validate(CONFIG_DICT), SlowRuntime(), tmp_path
    )
    service.sync_from_config()

    loop = asyncio.get_running_loop()
    started = loop.time()
    outcomes = await asyncio.gather(
        service.run_agent("backend", "a"),
        service.run_agent("frontend", "b"),
        service.run_agent("manager", "c"),
    )
    elapsed = loop.time() - started

    assert all(o.ok for o in outcomes)
    # Serial execution would take >=0.6s; concurrent should be well under that.
    assert elapsed < 0.5, f"agents did not run concurrently (took {elapsed:.2f}s)"

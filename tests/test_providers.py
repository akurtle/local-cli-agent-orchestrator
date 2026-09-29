"""Model providers: the Codex runtime, model tiers, and switching providers.

The Codex runner is driven against a stub CLI that emits the events the real
codex-cli 0.156 was observed to emit, so everything except the model is the
real implementation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agentos.config import Config
from agentos.paths import ProjectPaths
from agentos.providers import (
    EXECUTION,
    PLANNING,
    PROVIDERS,
    apply_override,
    provider_statuses,
    read_override,
    resolve_model,
    tier_for,
    tier_model,
    write_override,
)
from agentos.runtime.codex_cli import CodexRunner
from agentos.runtime.registry import build_runtime, known_runtimes
from agentos.schemas.enums import RunStatus
from agentos.schemas.runtime import RunRequest, RunResult

FAKE = Path(__file__).parent / "fixtures" / "fake_codex.py"


def config(**extra) -> Config:
    data = {
        "project": {"name": "p"},
        "agents": {
            "manager": {"role": "manager"},
            "backend": {"role": "backend"},
            "reviewer": {"role": "reviewer"},
        },
    }
    data.update(extra)
    return Config.model_validate(data)


# ------------------------------------------------------------------------ tiers


def test_the_manager_plans_and_everyone_else_executes() -> None:
    c = config()
    assert tier_for(c, "manager") == PLANNING
    assert tier_for(c, "backend") == EXECUTION
    assert tier_for(c, "reviewer") == EXECUTION


def test_an_agent_can_choose_its_tier() -> None:
    c = config(agents={"reviewer": {"role": "reviewer", "tier": "planning"}})
    assert tier_for(c, "reviewer") == PLANNING


def test_a_renamed_manager_role_still_plans() -> None:
    c = Config.model_validate(
        {
            "orchestrator": {"manager_role": "architect"},
            "agents": {"lead": {"role": "architect"}, "dev": {"role": "backend"}},
        }
    )
    assert tier_for(c, "lead") == PLANNING
    assert tier_for(c, "dev") == EXECUTION


def test_defaults_use_a_strong_planner_and_a_fast_executor() -> None:
    c = config()
    assert resolve_model(c, "manager") == PROVIDERS["claude"].planning
    assert resolve_model(c, "backend") == PROVIDERS["claude"].execution
    assert PROVIDERS["claude"].planning != PROVIDERS["claude"].execution


def test_model_precedence() -> None:
    """Agent model > provider tier > runtime.model > built-in default."""
    pinned = config(runtime={"name": "claude", "model": "flat-model"})
    # The older single-model setting keeps working for every agent...
    assert resolve_model(pinned, "manager") == "flat-model"
    assert resolve_model(pinned, "backend") == "flat-model"

    tiered = config(
        runtime={"name": "claude", "model": "flat-model"},
        providers={"claude": {"planning": "big", "execution": "small"}},
    )
    # ...until tiers are configured, which win over it.
    assert resolve_model(tiered, "manager") == "big"
    assert resolve_model(tiered, "backend") == "small"

    own = config(
        providers={"claude": {"planning": "big", "execution": "small"}},
        agents={"backend": {"role": "backend", "model": "special"}},
    )
    assert resolve_model(own, "backend") == "special"


# ------------------------------------------------------------ switching provider


def test_override_drops_the_other_providers_models_and_paths() -> None:
    c = config(
        runtime={
            "name": "claude",
            "model": "claude-x",
            "executable": "C:/claude.exe",
            "extra_args": ["--flag"],
        },
        agents={"backend": {"role": "backend", "model": "claude-y"}},
    )
    switched = apply_override(c, "codex")
    assert switched.runtime.name == "codex"
    assert switched.runtime.executable is None
    assert switched.runtime.extra_args == []
    assert resolve_model(switched, "backend") == PROVIDERS["codex"].execution
    # The same provider changes nothing.
    assert apply_override(c, "claude") is c
    assert apply_override(c, None) is c


def test_codex_tiers_come_from_its_own_section() -> None:
    c = config(providers={"codex": {"planning": "cx-big", "execution": "cx-small"}})
    switched = apply_override(c, "codex")
    assert resolve_model(switched, "manager") == "cx-big"
    assert resolve_model(switched, "backend") == "cx-small"


def test_override_file_round_trip(tmp_path: Path) -> None:
    paths = ProjectPaths(root=tmp_path)
    assert read_override(paths) is None
    write_override(paths, "codex")
    assert read_override(paths) == "codex"
    write_override(paths, None)
    assert read_override(paths) is None


def test_unknown_override_is_ignored_or_refused(tmp_path: Path) -> None:
    paths = ProjectPaths(root=tmp_path)
    with pytest.raises(ValueError):
        write_override(paths, "gemini")
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    (paths.state_dir / "provider").write_text("nonsense", encoding="utf-8")
    assert read_override(paths) is None


def test_statuses_describe_each_provider(tmp_path: Path) -> None:
    c = config(providers={"codex": {"executable": sys.executable}})
    statuses = {s.name: s for s in provider_statuses(c, "codex")}
    assert statuses["codex"].active and not statuses["claude"].active
    assert statuses["codex"].installed
    assert statuses["codex"].planning == PROVIDERS["codex"].planning
    assert tier_model(c, EXECUTION, "codex") == PROVIDERS["codex"].execution


# ---------------------------------------------------------------------- registry


def test_registry_knows_both_providers() -> None:
    assert set(known_runtimes()) >= {"claude", "codex"}
    runner = build_runtime(
        apply_override(config(providers={"codex": {"executable": "x.exe"}}), "codex")
    )
    assert isinstance(runner, CodexRunner)
    assert runner._explicit_executable == "x.exe"


# ------------------------------------------------------------------ the runner


class FakeCodex(CodexRunner):
    """The real CodexRunner, launching the stub through this interpreter."""

    def build_command(self, request):
        argv, session_id = super().build_command(request)
        return [sys.executable, str(FAKE), *argv[1:]], session_id


@pytest.fixture
def codex() -> FakeCodex:
    return FakeCodex(executable=sys.executable, default_timeout=30.0)


def _echo(result: RunResult) -> dict:
    return json.loads(result.text.split("\n\n", 1)[1])


def test_fresh_command_shape(codex: FakeCodex) -> None:
    argv, session_id = CodexRunner.build_command(
        codex, RunRequest(prompt="hi", model="m1", permission_mode="acceptEdits")
    )
    assert session_id is None
    assert argv[1:3] == ["exec", "--json"]
    assert argv[argv.index("-m") + 1] == "m1"
    assert 'sandbox_mode="workspace-write"' in argv
    assert 'approval_policy="never"' in argv
    assert argv[-1] == "-"
    assert "hi" not in argv  # prompts travel over stdin


def test_resume_command_shape(codex: FakeCodex) -> None:
    argv, session_id = CodexRunner.build_command(
        codex, RunRequest(prompt="hi", session_id="t-1", resume=True)
    )
    assert argv[1:3] == ["exec", "resume"]
    assert argv[-2:] == ["t-1", "-"]
    assert session_id == "t-1"
    # `resume` has no --sandbox flag; the -c override works for both.
    assert "-s" not in argv and "--sandbox" not in argv


def test_only_editors_can_write() -> None:
    assert CodexRunner.sandbox_for(RunRequest(prompt="x")) == "read-only"
    assert (
        CodexRunner.sandbox_for(RunRequest(prompt="x", permission_mode="acceptEdits"))
        == "workspace-write"
    )
    denied = RunRequest(prompt="x", permission_mode="acceptEdits", disallowed_tools=["Edit"])
    assert CodexRunner.sandbox_for(denied) == "read-only"


async def test_run_reads_session_text_and_usage(codex: FakeCodex, tmp_path: Path) -> None:
    result = await codex.run(
        RunRequest(prompt="do it", system_prompt="You are qa.", cwd=str(tmp_path))
    )
    assert result.ok, result.error
    assert result.session_id and len(result.session_id) == 36
    assert result.text.startswith("first message\n\n")
    assert result.usage == {"input_tokens": 10, "output_tokens": 2}
    assert result.num_turns == 1
    # The role brief leads the prompt, since Codex has no system-prompt flag.
    stdin = _echo(result)["stdin"]
    assert stdin.startswith("You are qa.") and stdin.endswith("do it")


async def test_resume_keeps_the_session(codex: FakeCodex, tmp_path: Path) -> None:
    first = await codex.run(RunRequest(prompt="one", cwd=str(tmp_path)))
    second = await codex.resume(first.session_id, "two", cwd=str(tmp_path))
    assert second.ok
    assert second.session_id == first.session_id


async def test_failed_turn_is_a_failure(codex: FakeCodex, tmp_path: Path) -> None:
    result = await codex.run(RunRequest(prompt="FAIL_TURN", cwd=str(tmp_path)))
    assert result.status is RunStatus.FAILED
    assert "model overloaded" in (result.error or "")


async def test_warning_items_are_not_failures(codex: FakeCodex, tmp_path: Path) -> None:
    result = await codex.run(RunRequest(prompt="fine", cwd=str(tmp_path)))
    assert result.ok


async def test_nonzero_exit_names_codex(codex: FakeCodex, tmp_path: Path) -> None:
    result = await codex.run(RunRequest(prompt="EXIT_BAD", cwd=str(tmp_path)))
    assert result.status is RunStatus.FAILED
    assert result.error == "codex exited with code 3"


async def test_unknown_session_is_stale(codex: FakeCodex, tmp_path: Path) -> None:
    result = await codex.resume("missing-thread", "hi", cwd=str(tmp_path))
    assert result.status is RunStatus.FAILED
    assert CodexRunner.is_stale_session(result)
    assert not CodexRunner.is_stale_session(
        await codex.run(RunRequest(prompt="EXIT_BAD", cwd=str(tmp_path)))
    )


async def test_timeout_kills_codex(tmp_path: Path) -> None:
    runner = FakeCodex(executable=sys.executable, default_timeout=2.0)
    result = await runner.run(RunRequest(prompt="SLEEP", cwd=str(tmp_path)))
    assert result.status is RunStatus.TIMEOUT


# ----------------------------------------------------- agents across a switch


async def test_a_switch_starts_a_fresh_session(db, tmp_path: Path) -> None:
    """A Claude session cannot be resumed by Codex, so it is not tried."""
    from agentos.services.agents import AgentService
    from tests.test_agents import StubRuntime

    c = config()
    claude_side = StubRuntime()
    claude_side.name = "claude"
    service = AgentService(db, c, claude_side, tmp_path)
    service.sync_from_config()
    await service.run_agent("backend", "first")
    claude_session = service.get_agent("backend").session_id
    assert claude_session

    codex_side = StubRuntime()
    codex_side.name = "codex"
    codex_side._default_session = "codex-thread"
    switched = AgentService(db, apply_override(c, "codex"), codex_side, tmp_path)
    outcome = await switched.run_agent("backend", "second")
    request = codex_side.requests[0]
    assert not request.resume and request.session_id is None
    assert request.model == PROVIDERS["codex"].execution
    assert outcome.session_id != claude_session

    # Back on the same provider, the new session resumes normally.
    await switched.run_agent("backend", "third")
    assert codex_side.requests[1].resume


async def test_model_comes_from_config_not_the_database(db, tmp_path: Path) -> None:
    """A dashboard sync on another provider must not change this run's model."""
    from agentos.services.agents import AgentService
    from tests.test_agents import StubRuntime

    runtime = StubRuntime()
    service = AgentService(db, config(), runtime, tmp_path)
    service.sync_from_config()
    # Another process, on Codex, synced the same database.
    AgentService(db, apply_override(config(), "codex"), StubRuntime(), tmp_path).sync_from_config()

    await service.run_agent("manager", "plan")
    assert runtime.requests[0].model == PROVIDERS["claude"].planning


# ---------------------------------------------------------------- the dashboard


pytest.importorskip("textual")
from tests.test_tui import project  # noqa: E402,F401  (shared fixture)


async def test_p_switches_the_provider(project) -> None:
    from agentos.tui.app import DashboardApp, ProviderScreen, summary_line

    reader, _tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "Claude" in summary_line(app.snapshot)

        await pilot.press("p")
        await pilot.pause()
        assert isinstance(app.screen, ProviderScreen)
        await pilot.press("down", "enter")
        await pilot.pause()

        assert read_override(reader.paths) == "codex"
        assert "Codex" in summary_line(app.snapshot)
        assert PROVIDERS["codex"].planning in summary_line(app.snapshot)


async def test_escape_leaves_the_provider_alone(project) -> None:
    from agentos.tui.app import DashboardApp

    reader, _tasks, _db = project
    app = DashboardApp(reader)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("p")
        await pilot.pause()
        await pilot.press("down", "escape")
        await pilot.pause()
        assert read_override(reader.paths) is None

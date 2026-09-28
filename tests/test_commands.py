"""Phase 15: safe command execution.

The policy is pure, so every rule is tested directly. Execution is tested against
real subprocesses, because the failures worth catching -- path escape, timeouts,
shell metacharacters having no effect -- only happen for real.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentos.config import Config
from agentos.db.session import Database
from agentos.schemas.capabilities import Capability
from agentos.services.command_service import (
    CommandService,
    required_capability,
)
from agentos.services.commands import (
    CommandPolicy,
    CommandRunner,
    Verdict,
    WorkingDirectoryError,
    normalise_program,
    resolve_within,
)
from agentos.services.permissions import PermissionService

POLICY = CommandPolicy(
    allowed=["pytest", "python", "npm", "git", "ruff", "mypy"],
    denied=["powershell", "cmd", "bash", "ssh", "rm"],
    require_approval=["git push", "git reset", "git clean"],
)


# ------------------------------------------------------------- name normalising


@pytest.mark.parametrize(
    "given",
    ["pytest", "pytest.exe", "PYTEST", r"C:\Python\Scripts\pytest.exe", "/usr/bin/pytest"],
)
def test_program_names_normalise(given: str) -> None:
    """Otherwise the allowlist is trivially bypassed on Windows."""
    assert normalise_program(given) == "pytest"


def test_quoted_and_padded_names_normalise() -> None:
    assert normalise_program('  "git"  ') == "git"


def test_script_suffixes_are_stripped() -> None:
    assert normalise_program("npm.cmd") == "npm"
    assert normalise_program("evil.ps1") == "evil"


# --------------------------------------------------------------- policy verdicts


def test_allowed_command() -> None:
    decision = POLICY.decide(["pytest", "tests/"])
    assert decision.allowed
    assert decision.verdict == Verdict.ALLOWED


def test_git_status_is_allowed() -> None:
    assert POLICY.decide(["git", "status"]).allowed


def test_unlisted_command_is_denied() -> None:
    decision = POLICY.decide(["whoami"])
    assert decision.verdict == Verdict.DENIED
    assert "not on the allowed list" in decision.reason


def test_denied_shell_is_denied() -> None:
    decision = POLICY.decide(["bash", "-c", "rm -rf /"])
    assert decision.verdict == Verdict.DENIED
    assert "denied list" in decision.reason


def test_denied_wins_over_allowed() -> None:
    """Listing something in both is a mistake; refusing is the safe reading."""
    policy = CommandPolicy(allowed=["git"], denied=["git"])
    assert policy.decide(["git", "status"]).verdict == Verdict.DENIED


def test_risky_git_needs_approval() -> None:
    decision = POLICY.decide(["git", "reset", "--hard", "HEAD~3"])
    assert decision.needs_approval
    assert "git reset" in decision.reason


def test_approval_matches_on_the_prefix_not_the_whole_command() -> None:
    assert POLICY.decide(["git", "push", "--force", "origin", "main"]).needs_approval
    assert POLICY.decide(["git", "clean", "-fd"]).needs_approval


def test_approval_rule_does_not_match_a_longer_word() -> None:
    """`git pushover` is not `git push`."""
    policy = CommandPolicy(allowed=["git"], require_approval=["git push"])
    assert policy.decide(["git", "pushover"]).allowed


def test_benign_git_is_not_gated() -> None:
    assert POLICY.decide(["git", "diff"]).allowed
    assert POLICY.decide(["git", "log", "--oneline"]).allowed


def test_empty_command_is_denied() -> None:
    assert POLICY.decide([]).verdict == Verdict.DENIED
    assert POLICY.decide(["   "]).verdict == Verdict.DENIED


def test_approval_matching_is_case_insensitive() -> None:
    assert POLICY.decide(["GIT", "RESET", "--hard"]).needs_approval


def test_absolute_path_still_matches_policy() -> None:
    decision = POLICY.decide([r"C:\Program Files\Git\bin\git.exe", "reset", "--hard"])
    assert decision.needs_approval


# ------------------------------------------------------- directory containment


def test_resolve_within_accepts_the_boundary_itself(tmp_path: Path) -> None:
    assert resolve_within(tmp_path, tmp_path) == tmp_path.resolve()


def test_resolve_within_accepts_a_child(tmp_path: Path) -> None:
    child = tmp_path / "src"
    child.mkdir()
    assert resolve_within(child, tmp_path) == child.resolve()


def test_traversal_escape_is_refused(tmp_path: Path) -> None:
    """The attack this exists to stop."""
    inner = tmp_path / "worktrees" / "backend"
    inner.mkdir(parents=True)
    with pytest.raises(WorkingDirectoryError, match="outside"):
        resolve_within(inner / ".." / ".." / "..", inner)


def test_sibling_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    with pytest.raises(WorkingDirectoryError):
        resolve_within(tmp_path / "frontend", tmp_path / "backend")


def test_missing_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(WorkingDirectoryError, match="not a directory"):
        resolve_within(tmp_path / "nope", tmp_path)


def test_a_file_is_not_a_working_directory(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(WorkingDirectoryError):
        resolve_within(target, tmp_path)


# ------------------------------------------------------------------- execution


@pytest.fixture
def runner() -> CommandRunner:
    return CommandRunner(
        CommandPolicy(allowed=["python"], denied=["bash"], require_approval=["python -c"]),
        default_timeout=30.0,
    )


async def test_allowed_command_runs(tmp_path: Path) -> None:
    runner = CommandRunner(CommandPolicy(allowed=["python"]))
    result = await runner.execute(
        [sys.executable, "-c", "print('hello')"], cwd=tmp_path
    )
    assert result.ok
    assert result.exit_code == 0
    assert "hello" in result.stdout


async def test_nonzero_exit_is_reported_not_hidden(tmp_path: Path) -> None:
    runner = CommandRunner(CommandPolicy(allowed=["python"]))
    result = await runner.execute(
        [sys.executable, "-c", "import sys; sys.exit(3)"], cwd=tmp_path
    )
    assert result.ran
    assert not result.ok
    assert result.exit_code == 3


async def test_stderr_is_captured(tmp_path: Path) -> None:
    runner = CommandRunner(CommandPolicy(allowed=["python"]))
    result = await runner.execute(
        [sys.executable, "-c", "import sys; sys.stderr.write('bad things')"],
        cwd=tmp_path,
    )
    assert "bad things" in result.stderr


async def test_denied_command_never_runs(tmp_path: Path) -> None:
    runner = CommandRunner(CommandPolicy(allowed=["python"], denied=["bash"]))
    marker = tmp_path / "should-not-exist.txt"
    result = await runner.execute(
        ["bash", "-c", f"touch {marker}"], cwd=tmp_path
    )
    assert result.verdict == Verdict.DENIED
    assert not result.ran
    assert not marker.exists()


async def test_approval_needed_command_does_not_run(tmp_path: Path) -> None:
    runner = CommandRunner(
        CommandPolicy(allowed=["python"], require_approval=["python -c"])
    )
    result = await runner.execute([sys.executable, "-c", "print(1)"], cwd=tmp_path)
    assert result.verdict == Verdict.NEEDS_APPROVAL
    assert not result.ran


async def test_approval_supplied_lets_it_run(tmp_path: Path) -> None:
    runner = CommandRunner(
        CommandPolicy(allowed=["python"], require_approval=["python -c"])
    )
    result = await runner.execute(
        [sys.executable, "-c", "print(1)"], cwd=tmp_path, approved_by="operator"
    )
    assert result.ok
    assert result.approved_by == "operator"


async def test_shell_metacharacters_are_inert(tmp_path: Path) -> None:
    """No shell=True, so `;` and `&&` are just characters."""
    runner = CommandRunner(CommandPolicy(allowed=["python"]))
    marker = tmp_path / "pwned.txt"
    result = await runner.execute(
        [sys.executable, "-c", f"print('safe'); # && touch {marker}"],
        cwd=tmp_path,
    )
    assert result.ok
    assert "safe" in result.stdout
    assert not marker.exists()


async def test_escape_attempt_is_refused_before_running(tmp_path: Path) -> None:
    runner = CommandRunner(CommandPolicy(allowed=["python"]))
    boundary = tmp_path / "worktrees" / "backend"
    boundary.mkdir(parents=True)
    result = await runner.execute(
        [sys.executable, "-c", "print(1)"],
        cwd=boundary / ".." / "..",
        boundary=boundary,
    )
    assert result.verdict == Verdict.DENIED
    assert "outside" in result.denied_reason
    assert not result.ran


async def test_boundary_defaults_to_cwd(tmp_path: Path) -> None:
    """A caller that forgets a boundary is still constrained."""
    runner = CommandRunner(CommandPolicy(allowed=["python"]))
    result = await runner.execute([sys.executable, "-c", "print(1)"], cwd=tmp_path)
    assert result.ok


async def test_timeout_kills_the_command(tmp_path: Path) -> None:
    runner = CommandRunner(CommandPolicy(allowed=["python"]))
    result = await runner.execute(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        timeout=2.0,
    )
    assert result.timed_out
    assert "timed out" in result.denied_reason


async def test_unknown_executable_is_reported(tmp_path: Path) -> None:
    runner = CommandRunner(CommandPolicy(allowed=["definitely-not-real-xyz"]))
    result = await runner.execute(["definitely-not-real-xyz"], cwd=tmp_path)
    assert result.verdict == Verdict.DENIED
    assert "not found on PATH" in result.denied_reason


# --------------------------------------------------- capability for a command


def test_test_runners_need_only_run_tests() -> None:
    """run_tests is a narrower privilege than run_command."""
    assert required_capability(["pytest"]) is Capability.RUN_TESTS
    assert required_capability(["pytest", "tests/backend"]) is Capability.RUN_TESTS


def test_npm_test_needs_only_run_tests() -> None:
    assert required_capability(["npm", "test"]) is Capability.RUN_TESTS


def test_other_commands_need_run_command() -> None:
    assert required_capability(["ruff", "check", "."]) is Capability.RUN_COMMAND
    assert required_capability(["npm", "run", "build"]) is Capability.RUN_COMMAND
    assert required_capability(["git", "status"]) is Capability.RUN_COMMAND


def test_empty_command_needs_run_command() -> None:
    assert required_capability([]) is Capability.RUN_COMMAND


# ------------------------------------------------------------------- service


@pytest.fixture
def config() -> Config:
    return Config.model_validate(
        {
            "agents": {
                "backend": {"role": "backend"},
                "reviewer": {"role": "reviewer"},
                "watcher": {"role": "watcher", "capabilities": ["read_files"]},
            },
            "commands": {
                "allowed": ["python", "pytest", "git"],
                "denied": ["bash"],
                "require_approval": ["git reset"],
            },
        }
    )


@pytest.fixture
def service(db: Database, config: Config) -> CommandService:
    return CommandService(db, config, PermissionService(db, config))


async def test_service_runs_and_records(
    service: CommandService, tmp_path: Path
) -> None:
    result = await service.run(
        "backend", [sys.executable, "-c", "print('ok')"], cwd=tmp_path, task_key="T-1"
    )
    assert result.ok

    history = service.history()
    assert len(history) == 1
    assert history[0].agent == "backend"
    assert history[0].task_key == "T-1"
    assert history[0].verdict == Verdict.ALLOWED
    assert history[0].exit_code == 0


async def test_service_records_a_denial(
    service: CommandService, tmp_path: Path
) -> None:
    """Knowing what an agent tried is as useful as knowing what it did."""
    result = await service.run("backend", ["bash", "-c", "echo hi"], cwd=tmp_path)
    assert result.verdict == Verdict.DENIED
    assert service.history()[0].verdict == Verdict.DENIED


async def test_agent_without_run_command_is_refused(
    service: CommandService, tmp_path: Path
) -> None:
    result = await service.run(
        "watcher", [sys.executable, "-c", "print(1)"], cwd=tmp_path
    )
    assert result.verdict == Verdict.DENIED
    assert "lacks run_command" in result.denied_reason
    # And it lands in the capability denial trail too.
    assert [d.capability for d in service.permissions.denials()] == ["run_command"]


async def test_reviewer_may_run_tests(service: CommandService, tmp_path: Path) -> None:
    """Reviewer has run_tests but not edit_files, and tests are not edits."""
    result = await service.run("reviewer", ["pytest", "--version"], cwd=tmp_path)
    # Either it ran or pytest is absent; the point is it was not refused.
    assert result.verdict != Verdict.DENIED or "not found" in result.denied_reason


async def test_pending_approvals_are_listed(
    service: CommandService, tmp_path: Path
) -> None:
    await service.run("backend", ["git", "reset", "--hard"], cwd=tmp_path)
    pending = service.pending_approvals()
    assert len(pending) == 1
    assert pending[0].approval_required
    assert pending[0].spelled.startswith("git reset")


async def test_history_can_be_filtered(service: CommandService, tmp_path: Path) -> None:
    await service.run("backend", [sys.executable, "-c", "print(1)"], cwd=tmp_path)
    await service.run("backend", ["bash", "-c", "x"], cwd=tmp_path)
    assert len(service.history(verdict=Verdict.DENIED)) == 1
    assert len(service.history(agent="backend")) == 2
    assert service.history(agent="reviewer") == []


async def test_command_history_survives_reopen(tmp_path, config: Config) -> None:
    path = tmp_path / "cmd.db"
    first = Database(path)
    first.create_all()
    await CommandService(first, config).run(
        "backend", ["bash", "-c", "nope"], cwd=tmp_path
    )
    first.dispose()

    second = Database(path)
    second.create_all()
    assert len(CommandService(second, config).history()) == 1
    second.dispose()


def test_decide_does_not_run_anything(service: CommandService) -> None:
    decision = service.decide(["bash", "-c", "echo"])
    assert decision.verdict == Verdict.DENIED
    assert service.history() == []

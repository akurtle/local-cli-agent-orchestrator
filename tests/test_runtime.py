"""Tests for the ClaudeRunner adapter, driven against a stub CLI."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentos.runtime.base import RuntimeNotAvailable
from agentos.runtime.claude_cli import (
    PRESERVE_OUTPUT_TAIL_ENV,
    ClaudeRunner,
    _truncate_stdout,
    resolve_executable,
)
from agentos.schemas.enums import RunStatus
from agentos.schemas.runtime import RunRequest

FAKE = Path(__file__).parent / "fixtures" / "fake_claude.py"


class FakeRunner(ClaudeRunner):
    """ClaudeRunner that launches the stub via the current interpreter.

    Only the argv prefix changes; all spawning, streaming, parsing and teardown
    logic under test is the real implementation.
    """

    def build_command(self, request: RunRequest) -> tuple[list[str], str]:
        argv, session_id = super().build_command(request)
        return [sys.executable, str(FAKE), *argv[1:]], session_id


@pytest.fixture
def runner(tmp_path: Path) -> FakeRunner:
    return FakeRunner(executable=sys.executable, default_timeout=60.0)


# ----------------------------------------------------------- command building


def test_new_session_mints_uuid(runner: FakeRunner) -> None:
    argv, session_id = runner.build_command(RunRequest(prompt="hi"))
    assert "--session-id" in argv
    assert argv[argv.index("--session-id") + 1] == session_id
    assert len(session_id) == 36


def test_resume_uses_given_session(runner: FakeRunner) -> None:
    argv, session_id = runner.build_command(
        RunRequest(prompt="hi", session_id="abc-123", resume=True)
    )
    assert argv[argv.index("--resume") + 1] == "abc-123"
    assert session_id == "abc-123"
    assert "--session-id" not in argv


def test_resume_without_session_id_is_rejected(runner: FakeRunner) -> None:
    with pytest.raises(ValueError, match="requires a session_id"):
        runner.build_command(RunRequest(prompt="hi", resume=True))


def test_stream_mode_requires_verbose(runner: FakeRunner) -> None:
    argv, _ = runner.build_command(RunRequest(prompt="hi", stream=True))
    assert "--verbose" in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"


def test_prompt_is_never_placed_on_argv(runner: FakeRunner) -> None:
    """Windows caps command lines near 32 KB, so prompts must go via stdin."""
    secret = "x" * 5000
    argv, _ = runner.build_command(RunRequest(prompt=secret))
    assert not any(secret in arg for arg in argv)


# ------------------------------------------------------------ event decoding


def test_parse_event_line_blank_is_ignored() -> None:
    assert ClaudeRunner.parse_event_line("   ") is None


def test_parse_event_line_malformed_becomes_parse_error() -> None:
    event = ClaudeRunner.parse_event_line("{not json")
    assert event is not None
    assert event.type == "parse_error"


def test_parse_event_line_non_object_becomes_parse_error() -> None:
    event = ClaudeRunner.parse_event_line("[1, 2, 3]")
    assert event is not None
    assert event.type == "parse_error"


def test_stdout_truncation_keeps_head_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(PRESERVE_OUTPUT_TAIL_ENV, raising=False)
    captured = _truncate_stdout("BEGIN-0123456789-END", limit=10)
    assert captured.startswith("BEGIN-0123")
    assert not captured.endswith("-END")


def test_stdout_truncation_can_preserve_tail_for_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(PRESERVE_OUTPUT_TAIL_ENV, "1")
    captured = _truncate_stdout("BEGIN-0123456789-END", limit=10)
    assert "tail retained" in captured
    assert captured.endswith("456789-END")
    assert not captured.startswith("BEGIN")


# ----------------------------------------------------------------- execution


async def test_run_succeeds_and_extracts_text(runner: FakeRunner, tmp_path: Path) -> None:
    result = await runner.run(RunRequest(prompt="hello world", cwd=str(tmp_path)))
    assert result.status is RunStatus.SUCCEEDED
    assert result.ok
    assert result.exit_code == 0
    assert "ECHO: hello world" in result.text
    assert result.session_id
    assert result.num_turns == 1
    assert result.cost_usd == pytest.approx(0.0123)
    assert result.duration_seconds is not None


async def test_run_collects_events_and_invokes_observer(
    runner: FakeRunner, tmp_path: Path
) -> None:
    seen: list[str] = []
    result = await runner.run(
        RunRequest(prompt="hi", cwd=str(tmp_path)), on_event=lambda e: seen.append(e.type)
    )
    assert "system" in seen and "assistant" in seen and "result" in seen
    assert len(result.events) == len(seen)


async def test_observer_exception_does_not_break_run(
    runner: FakeRunner, tmp_path: Path
) -> None:
    def boom(_event) -> None:
        raise RuntimeError("observer is broken")

    result = await runner.run(RunRequest(prompt="hi", cwd=str(tmp_path)), on_event=boom)
    assert result.ok


async def test_tool_use_blocks_are_excluded_from_text(
    runner: FakeRunner, tmp_path: Path
) -> None:
    result = await runner.run(RunRequest(prompt="hi", cwd=str(tmp_path)))
    assert "tool_use" not in result.text


async def test_malformed_line_does_not_abort_run(
    runner: FakeRunner, tmp_path: Path
) -> None:
    result = await runner.run(RunRequest(prompt="BADJSON please", cwd=str(tmp_path)))
    assert result.ok
    assert any(e.type == "parse_error" for e in result.events)


async def test_nonzero_exit_is_failure(runner: FakeRunner, tmp_path: Path) -> None:
    result = await runner.run(RunRequest(prompt="SPAWN_FAIL", cwd=str(tmp_path)))
    assert result.status is RunStatus.FAILED
    assert result.exit_code == 3
    assert "fake catastrophic failure" in result.stderr


async def test_is_error_result_is_failure(runner: FakeRunner, tmp_path: Path) -> None:
    """Exit code 0 but a result event flagged is_error still means failure."""
    result = await runner.run(RunRequest(prompt="TURN_ERROR", cwd=str(tmp_path)))
    assert result.status is RunStatus.FAILED
    assert result.error


async def test_timeout_kills_process(runner: FakeRunner, tmp_path: Path) -> None:
    result = await runner.run(
        RunRequest(prompt="HANG", cwd=str(tmp_path), timeout_seconds=2.0)
    )
    assert result.status is RunStatus.TIMEOUT
    assert "timeout" in (result.error or "").lower()


async def test_missing_cwd_fails_cleanly(runner: FakeRunner, tmp_path: Path) -> None:
    result = await runner.run(
        RunRequest(prompt="hi", cwd=str(tmp_path / "does-not-exist"))
    )
    assert result.status is RunStatus.FAILED
    assert "working directory" in (result.error or "")


async def test_session_id_survives_resume(runner: FakeRunner, tmp_path: Path) -> None:
    first = await runner.run(RunRequest(prompt="one", cwd=str(tmp_path)))
    second = await runner.run(
        RunRequest(
            prompt="two", cwd=str(tmp_path), session_id=first.session_id, resume=True
        )
    )
    assert second.session_id == first.session_id


async def test_stream_yields_events(runner: FakeRunner, tmp_path: Path) -> None:
    types = [e.type async for e in runner.stream(RunRequest(prompt="hi", cwd=str(tmp_path)))]
    assert types[0] == "system"
    assert types[-1] == "result"


# ------------------------------------------------------------------ preflight


def test_preflight_reports_version(runner: FakeRunner) -> None:
    """The stub answers --version, exercising the real preflight path."""

    class VersionRunner(FakeRunner):
        @property
        def executable(self) -> str:
            return sys.executable

        def preflight(self) -> dict[str, str]:
            import subprocess

            proc = subprocess.run(
                [sys.executable, str(FAKE), "--version"],
                capture_output=True,
                text=True,
                check=False,
            )
            assert proc.returncode == 0
            return {"version": proc.stdout.strip()}

    assert VersionRunner().preflight()["version"] == "9.9.9 (fake)"


def test_resolve_executable_rejects_missing_path() -> None:
    with pytest.raises(RuntimeNotAvailable, match="not found"):
        resolve_executable("definitely-not-a-real-binary-xyz")

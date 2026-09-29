"""Adapter around the Claude Code CLI.

This is the ONLY module in the project allowed to spawn the agent process.
Everything else goes through RunRequest/RunResult.

Key decisions, all verified against claude 2.1.x on Windows:

  * Prompts go over **stdin**, never argv. Windows caps a command line near
    32 KB and our prompts carry role + inbox + task text.
  * Session IDs are **minted by us** via `--session-id <uuid>` and resumed with
    `--resume <uuid>`. We never scrape an ID out of prose and hope.
  * No `shell=True`, ever. argv is passed as a list to create_subprocess_exec.
  * Timeout/cancel kills the whole **process tree**; `claude` spawns children
    and Windows has no SIGTERM, so we escalate to `taskkill /T /F`.
  * Authentication is inherited from the existing Claude Code login. We never
    read or require ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from pathlib import Path

from agentos.runtime.base import RuntimeNotAvailable
from agentos.schemas.enums import RunStatus
from agentos.schemas.runtime import RunRequest, RunResult, StreamEvent

# Cap retained output so one chatty agent cannot bloat the database.
MAX_CAPTURED_CHARS = 2_000_000

# Emitted by the CLI when --resume names a conversation it cannot find. Verified
# against claude 2.1.x, which prints this as plain text and exits 1 without any
# JSON, so it cannot be detected structurally.
STALE_SESSION_MARKERS = (
    "no conversation found with session id",
    "no conversation found",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _truncate(text: str, limit: int = MAX_CAPTURED_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


def resolve_executable(explicit: str | None = None) -> str:
    """Locate the claude CLI.

    On Windows the install may be claude.exe, claude.cmd or a bare shim, so we
    let shutil.which consult PATHEXT rather than guessing a suffix.
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(explicit)
        if found:
            return found
        raise RuntimeNotAvailable(f"configured claude executable not found: {explicit}")

    found = shutil.which("claude")
    if found:
        return found

    # Fall back to the documented per-user install location.
    for guess in (
        Path.home() / ".local" / "bin" / "claude.exe",
        Path.home() / ".local" / "bin" / "claude",
    ):
        if guess.is_file():
            return str(guess)

    raise RuntimeNotAvailable(
        "could not find the 'claude' CLI on PATH. Install Claude Code, or set "
        "runtime.executable in your config file."
    )


class _NullTimeout:
    """Stand-in for asyncio.timeout(None) so the `async with` reads cleanly."""

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class ClaudeRunner:
    """Launches and supervises `claude -p` processes."""

    name = "claude"

    def __init__(
        self,
        executable: str | None = None,
        default_model: str | None = None,
        extra_args: list[str] | None = None,
        default_timeout: float | None = 900.0,
    ) -> None:
        self._explicit_executable = executable
        self._resolved: str | None = None
        self.default_model = default_model
        self.extra_args = list(extra_args or [])
        self.default_timeout = default_timeout

    # ---------------------------------------------------------------- preflight

    @property
    def executable(self) -> str:
        if self._resolved is None:
            self._resolved = resolve_executable(self._explicit_executable)
        return self._resolved

    def preflight(self) -> dict[str, str]:
        """Confirm the CLI exists and responds. Raises RuntimeNotAvailable."""
        exe = self.executable
        try:
            proc = subprocess.run(
                [exe, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                check=False,
            )
        except OSError as exc:
            raise RuntimeNotAvailable(f"failed to execute {exe}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeNotAvailable(f"{exe} --version timed out") from exc

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:400]
            raise RuntimeNotAvailable(
                f"{exe} --version exited {proc.returncode}: {detail}"
            )

        return {
            "runtime": self.name,
            "executable": exe,
            "version": (proc.stdout or "").strip(),
            "auth": "inherited from Claude Code login (no API key used)",
        }

    # ------------------------------------------------------------ command build

    def build_command(self, request: RunRequest) -> tuple[list[str], str]:
        """Return (argv, session_id).

        The session id is returned separately because when starting fresh we
        mint it here and the caller must persist it.
        """
        argv: list[str] = [self.executable, "-p"]

        if request.resume:
            if not request.session_id:
                raise ValueError("resume=True requires a session_id")
            session_id = request.session_id
            argv += ["--resume", session_id]
        else:
            session_id = request.session_id or str(uuid.uuid4())
            argv += ["--session-id", session_id]

        if request.stream:
            # stream-json in print mode requires --verbose to emit events.
            argv += ["--output-format", "stream-json", "--verbose"]
        else:
            argv += ["--output-format", "json"]

        model = request.model or self.default_model
        if model:
            argv += ["--model", model]

        if request.system_prompt:
            argv += ["--append-system-prompt", request.system_prompt]

        if request.allowed_tools:
            argv += ["--allowedTools", *request.allowed_tools]

        # Capability enforcement: the agent cannot use a tool it was denied, so a
        # missing permission is prevented rather than merely discouraged.
        if request.disallowed_tools:
            argv += ["--disallowedTools", *request.disallowed_tools]

        # An explicit --permission-mode in extra_args is the user's override.
        if request.permission_mode and "--permission-mode" not in self.extra_args:
            argv += ["--permission-mode", request.permission_mode]

        argv += self.extra_args
        return argv, session_id

    def _child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Force UTF-8 on the pipe; the Windows console default is cp1252.
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    # ------------------------------------------------------------------- events

    @staticmethod
    def parse_event_line(line: str) -> StreamEvent | None:
        """Decode one stream-json line. Returns None for blank lines.

        Malformed lines become a `parse_error` event rather than raising, because
        a single bad line must not abort an otherwise good run.
        """
        text = line.strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return StreamEvent(type="parse_error", raw={"line": text[:2000]})
        if not isinstance(payload, dict):
            return StreamEvent(type="parse_error", raw={"line": text[:2000]})
        return StreamEvent(
            type=str(payload.get("type") or "unknown"),
            subtype=payload.get("subtype"),
            session_id=payload.get("session_id"),
            raw=payload,
        )

    @staticmethod
    def _assistant_text(event: StreamEvent) -> str:
        """Pull plain text out of an assistant event, ignoring tool-use blocks."""
        message = event.raw.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts)

    # ---------------------------------------------------------------- execution

    async def run(
        self,
        request: RunRequest,
        on_event: Callable[[StreamEvent], None] | None = None,
    ) -> RunResult:
        """Execute one turn to completion and return a persistable result."""
        argv, session_id = self.build_command(request)
        started = _utcnow()
        timeout = request.timeout_seconds or self.default_timeout

        cwd = request.cwd or os.getcwd()
        if not Path(cwd).is_dir():
            return RunResult(
                status=RunStatus.FAILED,
                session_id=session_id,
                command=argv,
                started_at=started,
                finished_at=_utcnow(),
                error=f"working directory does not exist: {cwd}",
            )

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=self._child_env(),
            )
        except OSError as exc:
            return RunResult(
                status=RunStatus.FAILED,
                session_id=session_id,
                command=argv,
                started_at=started,
                finished_at=_utcnow(),
                error=f"failed to spawn {argv[0]}: {exc}",
            )

        events: list[StreamEvent] = []
        stdout_chunks: list[str] = []
        assistant_text: list[str] = []
        collected = {
            "result_text": "",
            "usage": {},
            "cost": None,
            "num_turns": None,
        }

        async def feed_stdin() -> None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(request.prompt.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass

        async def pump_stdout() -> None:
            assert proc.stdout is not None
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace")
                stdout_chunks.append(line)
                event = self.parse_event_line(line)
                if event is None:
                    continue
                events.append(event)
                if on_event is not None:
                    try:
                        on_event(event)
                    except Exception:
                        # A misbehaving observer must not kill the run.
                        pass
                if event.type == "assistant":
                    assistant_text.append(self._assistant_text(event))
                elif event.type == "result":
                    self._absorb_result(event, collected)

        async def pump_stderr() -> str:
            assert proc.stderr is not None
            data = await proc.stderr.read()
            return data.decode("utf-8", errors="replace")

        stderr_text = ""
        status = RunStatus.SUCCEEDED
        error: str | None = None

        try:
            limiter = asyncio.timeout(timeout) if timeout else _NullTimeout()
            async with limiter:
                _, stderr_text, _ = await asyncio.gather(
                    feed_stdin(), pump_stderr(), pump_stdout()
                )
                await proc.wait()
        except TimeoutError:
            await self._kill_tree(proc)
            status = RunStatus.TIMEOUT
            error = f"run exceeded timeout of {timeout}s"
        except asyncio.CancelledError:
            await self._kill_tree(proc)
            raise
        finally:
            if proc.returncode is None:
                await self._kill_tree(proc)

        exit_code = proc.returncode

        # stream-json carries the authoritative session id; prefer it over ours
        # in case the CLI forked the session.
        for event in reversed(events):
            if event.session_id:
                session_id = event.session_id
                break

        if status is RunStatus.SUCCEEDED and exit_code != 0:
            status = RunStatus.FAILED
            error = f"claude exited with code {exit_code}"

        # A result event flagged is_error means the CLI ran but the turn failed.
        for event in events:
            if event.type == "result" and event.raw.get("is_error"):
                status = RunStatus.FAILED
                error = error or str(
                    event.raw.get("result") or "claude reported is_error=true"
                )

        text = collected["result_text"] or "".join(assistant_text)

        return RunResult(
            status=status,
            session_id=session_id,
            exit_code=exit_code,
            text=_truncate(str(text)),
            stdout=_truncate("".join(stdout_chunks)),
            stderr=_truncate(stderr_text),
            command=argv,
            events=events,
            started_at=started,
            finished_at=_utcnow(),
            error=error,
            usage=collected["usage"] or {},
            cost_usd=collected["cost"],
            num_turns=collected["num_turns"],
        )

    @staticmethod
    def _absorb_result(event: StreamEvent, collected: dict) -> None:
        """Copy the fields we care about out of a terminal result event."""
        value = event.raw.get("result")
        if isinstance(value, str):
            collected["result_text"] = value
        if isinstance(event.raw.get("usage"), dict):
            collected["usage"] = event.raw["usage"]
        cost = event.raw.get("total_cost_usd")
        if isinstance(cost, (int, float)):
            collected["cost"] = float(cost)
        turns = event.raw.get("num_turns")
        if isinstance(turns, int):
            collected["num_turns"] = turns

    async def stream(self, request: RunRequest) -> AsyncIterator[StreamEvent]:
        """Yield events as they arrive.

        Built on top of `run` with a queue so exactly one place knows how to
        spawn and reap the process.
        """
        queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()

        def emit(event: StreamEvent) -> None:
            queue.put_nowait(event)

        task = asyncio.create_task(self.run(request, on_event=emit))
        task.add_done_callback(lambda _: queue.put_nowait(None))

        while True:
            event = await queue.get()
            if event is None:
                break
            yield event
        await task

    async def resume(
        self,
        session_id: str,
        prompt: str,
        on_event: Callable[[StreamEvent], None] | None = None,
        **overrides: object,
    ) -> RunResult:
        """Continue an existing session.

        Thin wrapper over `run` so there is still exactly one execution path.
        """
        request = RunRequest(
            prompt=prompt, session_id=session_id, resume=True, **overrides
        )
        return await self.run(request, on_event=on_event)

    @staticmethod
    def is_stale_session(result: RunResult) -> bool:
        """True when a resume failed because the session no longer exists.

        The CLI reports this as plain text on stdout with exit 1 and emits no
        JSON, so we match on its message. Deliberately narrow: a genuine failure
        must not be mistaken for a stale session and silently retried.
        """
        if result.status is RunStatus.SUCCEEDED:
            return False
        haystack = " ".join(
            (result.text, result.stdout, result.stderr)
        ).lower()
        return any(marker in haystack for marker in STALE_SESSION_MARKERS)

    # ----------------------------------------------------------------- teardown

    @staticmethod
    async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
        """Terminate the process and any children.

        Windows has no SIGTERM and `claude` spawns child processes, so a plain
        terminate() can leave orphans holding a worktree open.
        """
        if proc.returncode is not None:
            return

        if sys.platform == "win32" and proc.pid:
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(proc.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(killer.wait(), timeout=10)
            except (OSError, TimeoutError):
                pass

        if proc.returncode is None:
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (TimeoutError, asyncio.TimeoutError):
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass

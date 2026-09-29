"""Adapter around the OpenAI Codex CLI (`codex exec`).

Shares ClaudeRunner's process handling -- stdin prompts, streaming, timeouts,
killing the process tree -- and differs only where the CLIs differ. Verified
against codex-cli 0.156 on Windows:

  * `codex exec --json -` reads the prompt from stdin and prints JSONL events:
    `thread.started` (carrying the session id), `item.completed` (with
    `agent_message` text or `command_execution` results), and `turn.completed`
    with token usage.
  * Codex mints its own session ids, so a fresh run cannot be given one; the
    id is read from `thread.started`. `codex exec resume <id> -` continues it.
  * There is no per-tool allowlist. Capabilities map onto Codex's sandbox:
    an agent that may edit gets `workspace-write` (writes inside its working
    directory, no network), any other agent `read-only`. Approval prompts are
    turned off (`approval_policy="never"`) because nobody can answer them in a
    headless run; anything outside the sandbox is refused, not asked about.
    The per-program `commands.allowed` list therefore cannot be enforced up
    front here -- the git comparison and verification after a run still are.
  * There is no system-prompt flag, so the role brief leads the stdin prompt.
  * Authentication is inherited from the existing `codex login`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from agentos.runtime.base import RuntimeNotAvailable
from agentos.runtime.claude_cli import ClaudeRunner
from agentos.schemas.runtime import RunRequest, StreamEvent

# `codex exec resume` with an id it has no record of exits 1 with this.
CODEX_STALE_MARKERS = ("no rollout found for thread id",)


def resolve_codex(explicit: str | None = None) -> str:
    if explicit:
        candidate = Path(explicit).expanduser()
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(explicit)
        if found:
            return found
        raise RuntimeNotAvailable(f"configured codex executable not found: {explicit}")
    # npm installs a .cmd shim on Windows; which() finds it through PATHEXT.
    found = shutil.which("codex")
    if found:
        return found
    raise RuntimeNotAvailable(
        "could not find the 'codex' CLI on PATH. Install it with "
        "`npm install -g @openai/codex`, or set providers.codex.executable."
    )


class CodexRunner(ClaudeRunner):
    """Launches and supervises `codex exec` processes."""

    name = "codex"
    auth_note = "inherited from Codex login (no API key used)"
    stale_markers = CODEX_STALE_MARKERS

    @staticmethod
    def _resolve_executable(explicit: str | None) -> str:
        return resolve_codex(explicit)

    def build_command(self, request: RunRequest) -> tuple[list[str], str | None]:
        """Return (argv, session_id). A fresh run has no id until Codex reports one."""
        argv: list[str] = [self.executable, "exec"]
        session_id: str | None = None
        if request.resume:
            if not request.session_id:
                raise ValueError("resume=True requires a session_id")
            session_id = request.session_id
            argv.append("resume")

        # A git repository is not required: a shared project directory or an
        # agent worktree both are one, but the check adds nothing here.
        argv += ["--json", "--skip-git-repo-check"]

        model = request.model or self.default_model
        if model:
            argv += ["-m", model]

        # `resume` does not accept --sandbox, so both paths use -c overrides.
        argv += [
            "-c", f'sandbox_mode="{self.sandbox_for(request)}"',
            "-c", 'approval_policy="never"',
        ]
        argv += self.extra_args

        if session_id:
            argv.append(session_id)
        argv.append("-")  # prompt on stdin
        return argv, session_id

    @staticmethod
    def sandbox_for(request: RunRequest) -> str:
        """Edit permission is the line between writing and looking."""
        may_edit = request.permission_mode == "acceptEdits" and not {
            "Edit",
            "Write",
        } & set(request.disallowed_tools)
        return "workspace-write" if may_edit else "read-only"

    def stdin_text(self, request: RunRequest) -> str:
        if not request.system_prompt:
            return request.prompt
        return (
            f"{request.system_prompt}\n\n"
            "---\n\n"
            f"{request.prompt}"
        )

    # ------------------------------------------------------------------- events

    @staticmethod
    def parse_event_line(line: str) -> StreamEvent | None:
        event = ClaudeRunner.parse_event_line(line)
        if event is not None and event.type == "thread.started":
            thread = event.raw.get("thread_id")
            if isinstance(thread, str) and thread:
                event = event.model_copy(update={"session_id": thread})
        return event

    def absorb_event(
        self, event: StreamEvent, texts: list[str], collected: dict
    ) -> None:
        if event.type == "item.completed":
            item = event.raw.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    texts.append(text)
        elif event.type == "turn.completed":
            usage = event.raw.get("usage")
            if isinstance(usage, dict):
                collected["usage"] = usage
            collected["num_turns"] = (collected["num_turns"] or 0) + 1

    @staticmethod
    def join_text(texts: list[str]) -> str:
        # Separate messages, not one stream of deltas as Claude's are.
        return "\n\n".join(t for t in texts if t)

    @staticmethod
    def turn_error(events: list[StreamEvent]) -> str | None:
        for event in events:
            if event.type == "turn.failed":
                error = event.raw.get("error")
                if isinstance(error, dict):
                    return str(error.get("message") or "codex reported turn.failed")
                return "codex reported turn.failed"
            # A top-level error event is fatal; an `error` *item* is only a
            # warning (Codex uses those for things like unknown config keys).
            if event.type == "error":
                return str(event.raw.get("message") or "codex reported an error")
        return None

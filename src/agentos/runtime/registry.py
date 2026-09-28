"""Runtime factory.

Config says `runtime: claude`; this maps that name to an implementation. Adding
a codex/gemini/ollama runtime later means adding one entry here and one module,
with no changes to the scheduler.
"""

from __future__ import annotations

from agentos.config import Config
from agentos.runtime.base import AgentRuntime, RuntimeNotAvailable
from agentos.runtime.claude_cli import ClaudeRunner

_PLANNED = {"codex", "gemini", "ollama"}


def known_runtimes() -> list[str]:
    return ["claude"]


def build_runtime(config: Config) -> AgentRuntime:
    name = config.runtime.name.strip().lower()
    if name == "claude":
        return ClaudeRunner(
            executable=config.runtime.executable,
            default_model=config.runtime.model,
            extra_args=config.runtime.extra_args,
            default_timeout=config.orchestrator.default_timeout_seconds,
        )
    if name in _PLANNED:
        raise RuntimeNotAvailable(
            f"runtime {name!r} is planned but not implemented yet; use 'claude'"
        )
    raise RuntimeNotAvailable(
        f"unknown runtime {name!r}; known runtimes: {known_runtimes()}"
    )

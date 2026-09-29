"""Runtime factory.

Config names a provider (`runtime.name`, possibly overridden per project from
the dashboard); this maps that name to an implementation. Adding another CLI
means one module and one entry here, with no changes to the scheduler.
"""

from __future__ import annotations

from agentos.config import Config
from agentos.runtime.base import AgentRuntime, RuntimeNotAvailable
from agentos.runtime.claude_cli import ClaudeRunner
from agentos.runtime.codex_cli import CodexRunner

_RUNNERS = {"claude": ClaudeRunner, "codex": CodexRunner}
_PLANNED = {"gemini", "ollama"}


def known_runtimes() -> list[str]:
    return list(_RUNNERS)


def build_runtime(config: Config) -> AgentRuntime:
    name = config.runtime.name.strip().lower()
    runner = _RUNNERS.get(name)
    if runner is not None:
        section = config.providers.get(name)
        return runner(
            executable=(section and section.executable) or config.runtime.executable,
            default_model=config.runtime.model,
            extra_args=(section.extra_args if section and section.extra_args else None)
            or config.runtime.extra_args,
            default_timeout=config.orchestrator.default_timeout_seconds,
        )
    if name in _PLANNED:
        raise RuntimeNotAvailable(
            f"runtime {name!r} is planned but not implemented yet; "
            f"use one of {known_runtimes()}"
        )
    raise RuntimeNotAvailable(
        f"unknown runtime {name!r}; known runtimes: {known_runtimes()}"
    )

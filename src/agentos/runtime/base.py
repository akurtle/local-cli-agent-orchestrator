"""The contract every agent runtime implements.

Keeping this abstract is what prevents Claude from being welded into the
scheduler. A runtime is just "something that can take a prompt plus an optional
session id and give back a RunResult".
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Protocol, runtime_checkable

from agentos.schemas.runtime import RunRequest, RunResult, StreamEvent


class RuntimeNotAvailable(RuntimeError):
    """The backing CLI could not be found or is not usable."""


@runtime_checkable
class AgentRuntime(Protocol):
    """Protocol for an external agent process."""

    name: str

    def preflight(self) -> dict[str, str]:
        """Verify the runtime is usable. Raises RuntimeNotAvailable if not.

        Returns a dict of diagnostic details for `agentctl doctor`.
        """
        ...

    async def run(
        self,
        request: RunRequest,
        on_event: Callable[[StreamEvent], None] | None = None,
    ) -> RunResult:
        """Execute one turn to completion."""
        ...

    def stream(self, request: RunRequest) -> AsyncIterator[StreamEvent]:
        """Yield events as they arrive."""
        ...

"""A runtime that does not launch anything.

Lets you exercise scheduling, dependency resolution and concurrency without
spending model usage: `agentctl work --dry-run`. It satisfies the AgentRuntime
protocol, so the scheduler cannot tell the difference and no scheduling code
needs a test-only branch.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone

from agentos.schemas.enums import RunStatus
from agentos.schemas.responses import RESPONSE_BEGIN, RESPONSE_END
from agentos.schemas.runtime import RunRequest, RunResult, StreamEvent


class DryRunRuntime:
    """Reports success immediately without starting a process."""

    name = "dry-run"

    def __init__(self, delay: float = 0.0, fail_keys: set[str] | None = None) -> None:
        self.delay = delay
        # Task keys that should report failure, so failure paths can be
        # rehearsed without a real agent.
        self.fail_keys = fail_keys or set()
        self.requests: list[RunRequest] = []

    def preflight(self) -> dict[str, str]:
        return {
            "runtime": self.name,
            "executable": "(none: dry run)",
            "version": "n/a",
            "auth": "not required (no process is launched)",
        }

    def _should_fail(self, prompt: str) -> str | None:
        for key in self.fail_keys:
            if key in prompt:
                return key
        return None

    async def run(
        self,
        request: RunRequest,
        on_event: Callable[[StreamEvent], None] | None = None,
    ) -> RunResult:
        self.requests.append(request)
        started = datetime.now(timezone.utc)
        if self.delay:
            await asyncio.sleep(self.delay)

        session_id = request.session_id or f"dry-{len(self.requests)}"
        failing = self._should_fail(request.prompt)
        status = "failed" if failing else "completed"
        summary = (
            f"DRY RUN: simulated failure for {failing}."
            if failing
            else "DRY RUN: no work was performed."
        )
        text = (
            f"{summary}\n\n{RESPONSE_BEGIN}\n"
            f'{{"status": "{status}", "summary": "{summary}", '
            '"files_changed": [], "messages": [], "requested_tasks": [], '
            f'"blockers": {["simulated"] if failing else []}}}\n'
            f"{RESPONSE_END}"
        ).replace("'", '"')

        if on_event is not None:
            for event in (
                StreamEvent(type="system", subtype="init", session_id=session_id),
                StreamEvent(type="result", subtype=status, session_id=session_id),
            ):
                on_event(event)

        return RunResult(
            status=RunStatus.FAILED if failing else RunStatus.SUCCEEDED,
            session_id=session_id,
            exit_code=1 if failing else 0,
            text=text,
            command=["(dry-run)"],
            started_at=started,
            finished_at=datetime.now(timezone.utc),
            error=f"dry-run simulated failure for {failing}" if failing else None,
            cost_usd=0.0,
        )

    async def resume(
        self,
        session_id: str,
        prompt: str,
        on_event: Callable[[StreamEvent], None] | None = None,
        **overrides: object,
    ) -> RunResult:
        return await self.run(
            RunRequest(prompt=prompt, session_id=session_id, resume=True, **overrides),
            on_event=on_event,
        )

    async def stream(self, request: RunRequest) -> AsyncIterator[StreamEvent]:
        result = await self.run(request)
        for event in result.events:
            yield event

    @staticmethod
    def is_stale_session(result: RunResult) -> bool:
        return False

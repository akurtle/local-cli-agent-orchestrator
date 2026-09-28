"""A runtime that does not launch anything.

Lets you exercise scheduling, dependency resolution and concurrency without
spending model usage: `agentctl work --dry-run`. It satisfies the AgentRuntime
protocol, so the scheduler cannot tell the difference and no scheduling code
needs a test-only branch.

Simulated failures are faithful to how a real agent fails. A task failing is NOT
the process failing: `claude` exits 0 and the agent says so in its response
block. Only `crash_keys` makes the process itself fail, which is what the
orchestrator classifies as an infrastructure problem.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone

from agentos.schemas.enums import RunStatus
from agentos.schemas.responses import RESPONSE_BEGIN, RESPONSE_END
from agentos.schemas.runtime import RunRequest, RunResult, StreamEvent


def response_block(
    status: str,
    summary: str,
    blockers: list[str] | None = None,
) -> str:
    payload = {
        "status": status,
        "summary": summary,
        "files_changed": [],
        "messages": [],
        "requested_tasks": [],
        "blockers": blockers or [],
    }
    return "\n".join([RESPONSE_BEGIN, json.dumps(payload), RESPONSE_END])


class DryRunRuntime:
    """Reports success immediately without starting a process."""

    name = "dry-run"

    def __init__(
        self,
        delay: float = 0.0,
        fail_keys: set[str] | None = None,
        block_keys: set[str] | None = None,
        crash_keys: set[str] | None = None,
    ) -> None:
        self.delay = delay
        # Task keys whose agent reports `failed`: the process ran fine.
        self.fail_keys = fail_keys or set()
        # Task keys whose agent reports a blocker: needs intervention.
        self.block_keys = block_keys or set()
        # Task keys where the process itself fails: an infrastructure problem.
        self.crash_keys = crash_keys or set()
        self.requests: list[RunRequest] = []

    def preflight(self) -> dict[str, str]:
        return {
            "runtime": self.name,
            "executable": "(none: dry run)",
            "version": "n/a",
            "auth": "not required (no process is launched)",
        }

    @staticmethod
    def _match(prompt: str, keys: set[str]) -> str | None:
        for key in keys:
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

        crashing = self._match(request.prompt, self.crash_keys)
        blocking = self._match(request.prompt, self.block_keys)
        failing = self._match(request.prompt, self.fail_keys)

        if crashing:
            # The process never produced a result: an infrastructure failure.
            return RunResult(
                status=RunStatus.FAILED,
                session_id=session_id,
                exit_code=1,
                text="",
                stderr=f"DRY RUN: simulated process crash for {crashing}",
                command=["(dry-run)"],
                started_at=started,
                finished_at=datetime.now(timezone.utc),
                error=f"failed to spawn (simulated) for {crashing}",
                cost_usd=0.0,
            )

        if blocking:
            status, summary = "blocked", f"DRY RUN: simulated blocker for {blocking}"
            blockers = [f"simulated blocker for {blocking}"]
        elif failing:
            status, summary = "failed", f"DRY RUN: simulated failure for {failing}"
            blockers = []
        else:
            status, summary = "completed", "DRY RUN: no work was performed."
            blockers = []

        text = summary + "\n\n" + response_block(status, summary, blockers)

        if on_event is not None:
            for event in (
                StreamEvent(type="system", subtype="init", session_id=session_id),
                StreamEvent(type="result", subtype="success", session_id=session_id),
            ):
                on_event(event)

        # The process succeeded even when the agent reports failure: exit code 0
        # is what a real `claude` returns after a turn it considers unsuccessful.
        return RunResult(
            status=RunStatus.SUCCEEDED,
            session_id=session_id,
            exit_code=0,
            text=text,
            command=["(dry-run)"],
            started_at=started,
            finished_at=datetime.now(timezone.utc),
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

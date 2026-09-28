"""Persistence for agent invocations.

Every runtime invocation becomes exactly one Run row. That is what makes
`agentctl logs <agent>` possible without an event-sourcing layer.
"""

from __future__ import annotations

import json

from agentos.db.models import Run
from agentos.db.session import Database
from agentos.schemas.runtime import RunResult


def record_run(
    db: Database,
    result: RunResult,
    *,
    agent_id: int | None = None,
    task_id: int | None = None,
    runtime: str = "claude",
) -> int:
    """Store a finished run and return its row id."""
    with db.session() as session:
        run = Run(
            agent_id=agent_id,
            task_id=task_id,
            session_id=result.session_id,
            runtime=runtime,
            # argv is stored purely for auditability; it is never re-executed.
            command=json.dumps(result.command),
            status=result.status.value,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            result_text=result.text,
            error=result.error,
            cost_usd=result.cost_usd,
            num_turns=result.num_turns,
            started_at=result.started_at,
            finished_at=result.finished_at,
        )
        session.add(run)
        session.flush()
        return run.id

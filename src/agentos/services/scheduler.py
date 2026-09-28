"""The dependency-aware scheduler.

Responsibilities: decide what may run now, launch it, record what happened, and
stop when nothing can progress. Every decision comes from the pure functions in
`services/dag.py`; this module only loads rows, runs agents and persists results.

It does not busy-loop. Each pass launches what it can and then blocks on
`asyncio.wait(FIRST_COMPLETED)` until a running task finishes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from agentos.config import Config
from agentos.db.session import Database
from agentos.prompts.task_prompt import build_task_prompt
from agentos.schemas.dto import SchedulerReport, TaskView
from agentos.schemas.enums import AgentStatus, TaskStatus
from agentos.services import dag
from agentos.services.agents import AgentBusy, AgentPaused, AgentService
from agentos.services.tasks import TaskService

# Agent statuses that can accept a new task.
AVAILABLE_AGENT_STATUSES = frozenset({AgentStatus.IDLE, AgentStatus.FAILED})


class SchedulerEvent:
    """Names for the progress callback, so the CLI can render without guessing."""

    DISPATCH = "dispatch"
    COMPLETED = "completed"
    FAILED = "failed"
    RETRY = "retry"
    BLOCKED = "blocked"
    PASS = "pass"
    STOP = "stop"


class Scheduler:
    def __init__(
        self,
        db: Database,
        config: Config,
        agent_service: AgentService,
        task_service: TaskService,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.agents = agent_service
        self.tasks = task_service
        self.on_progress = on_progress
        self._stopping = False

    # ------------------------------------------------------------------ helpers

    def _notify(self, event: str, detail: str) -> None:
        if self.on_progress is not None:
            try:
                self.on_progress(event, detail)
            except Exception:
                # A broken renderer must not abort an orchestration run.
                pass

    def request_stop(self) -> None:
        """Stop launching new work; let running tasks finish."""
        self._stopping = True

    def available_agents(self) -> set[str]:
        """Agents that could take a task right now."""
        return {
            view.name
            for view in self.agents.list_agents()
            if view.status in AVAILABLE_AGENT_STATUSES
        }

    def recover_stale_running(self) -> list[str]:
        """Reset tasks left `running` by a crashed process.

        Nothing is running when the scheduler starts, so a `running` row is
        always stale. We return it to the queue rather than failing it, because
        the work may not have started; its retry count is untouched so a genuine
        crash loop still hits the retry ceiling.
        """
        recovered: list[str] = []
        for task in self.tasks.list_tasks({TaskStatus.RUNNING}):
            self.tasks.tasks.set_status(task.id, TaskStatus.READY)
            recovered.append(task.key)
        for agent in self.agents.list_agents():
            if agent.status is AgentStatus.WORKING:
                self.agents.agents.set_status(
                    agent.name, AgentStatus.IDLE, clear_task=True
                )
        return recovered

    # ---------------------------------------------------------------- execution

    def _build_prompt(self, task: TaskView) -> str:
        return build_task_prompt(
            task=task,
            dependencies=self.tasks.completed_dependencies(task),
            retry_of=task.error if task.attempts else None,
        )

    async def _run_task(
        self, task: TaskView
    ) -> tuple[TaskView, bool, str | None, str]:
        """Execute one task. Returns (task, ok, error, agent output text)."""
        prompt = self._build_prompt(task)
        agent_name = task.assigned_agent
        assert agent_name is not None  # selection guarantees this

        try:
            outcome = await self.agents.run_agent(
                agent_name, prompt, task_id=task.id
            )
        except (AgentBusy, AgentPaused) as exc:
            # Lost a race for the agent: return the task to the queue untouched.
            await asyncio.to_thread(
                self.tasks.tasks.set_status, task.id, TaskStatus.READY
            )
            return task, False, f"agent unavailable: {exc}", ""
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.tasks.tasks.set_status, task.id, TaskStatus.READY
            )
            raise
        except Exception as exc:
            return task, False, f"{type(exc).__name__}: {exc}", ""

        error = outcome.error or (None if outcome.ok else "agent failed")
        return task, outcome.ok, error, outcome.text

    async def _settle(self, task: TaskView, ok: bool, error: str | None, text: str = "") -> str:
        """Record the outcome of a finished task and return the event name."""
        if ok:
            await asyncio.to_thread(
                self.tasks.transition, task.id, TaskStatus.COMPLETED, text or "", None
            )
            self._notify(SchedulerEvent.COMPLETED, task.key)
            return SchedulerEvent.COMPLETED

        attempts = await asyncio.to_thread(self.tasks.tasks.increment_attempts, task.id)
        max_retries = self.config.orchestrator.max_task_retries
        if attempts <= max_retries:
            # Return to the queue for another attempt; the retry prompt will
            # include the previous error.
            await asyncio.to_thread(
                self.tasks.tasks.set_status,
                task.id,
                TaskStatus.READY,
                None,
                error or "unknown failure",
            )
            self._notify(
                SchedulerEvent.RETRY, f"{task.key} (attempt {attempts}/{max_retries})"
            )
            return SchedulerEvent.RETRY

        await asyncio.to_thread(
            self.tasks.transition, task.id, TaskStatus.FAILED, None, error or "failed"
        )
        self._notify(SchedulerEvent.FAILED, f"{task.key}: {error}")
        return SchedulerEvent.FAILED

    async def run(self, max_passes: int = 1000) -> SchedulerReport:
        """Drive tasks to completion.

        Stops when nothing can progress, when interrupted, or when the pass
        ceiling is hit (a safety valve, not an expected exit).
        """
        await asyncio.to_thread(self.tasks.validate_graph)

        recovered = await asyncio.to_thread(self.recover_stale_running)
        for key in recovered:
            self._notify(SchedulerEvent.RETRY, f"{key} recovered from stale running")

        completed: list[str] = []
        failed: list[str] = []
        skipped: list[str] = []
        dispatched = 0
        passes = 0
        stop_reason = "nothing left to do"
        interrupted = False

        # task id -> asyncio task
        running: dict[int, asyncio.Task] = {}
        capacity = self.config.orchestrator.max_concurrent_agents

        try:
            while passes < max_passes:
                passes += 1
                await asyncio.to_thread(self.tasks.refresh_readiness)
                nodes = await asyncio.to_thread(self.tasks.tasks.nodes)

                if not self._stopping:
                    free = capacity - len(running)
                    available = await asyncio.to_thread(self.available_agents)
                    # An agent running a task is not free, even if its row has
                    # not been observed as `working` yet.
                    for task_id in running:
                        node = nodes.get(task_id)
                        if node and node.agent_name:
                            available.discard(node.agent_name)

                    chosen = dag.selectable(nodes, available, free)
                    for node in chosen:
                        task = await asyncio.to_thread(self.tasks.get_task, node.id)
                        await asyncio.to_thread(
                            self.tasks.transition, node.id, TaskStatus.RUNNING
                        )
                        refreshed = await asyncio.to_thread(
                            self.tasks.get_task, node.id
                        )
                        running[node.id] = asyncio.create_task(
                            self._run_task(refreshed), name=f"task-{task.key}"
                        )
                        dispatched += 1
                        self._notify(
                            SchedulerEvent.DISPATCH,
                            f"{task.key} -> {task.assigned_agent}",
                        )

                if not running:
                    if self._stopping:
                        stop_reason = "interrupted"
                        interrupted = True
                        break
                    ready_now = [
                        n for n in nodes.values() if n.status is TaskStatus.READY
                    ]
                    if ready_now:
                        # Ready work exists but no agent can take it. Without
                        # this guard the loop would spin.
                        skipped = sorted(n.key for n in ready_now)
                        stop_reason = (
                            "ready tasks have no available agent: "
                            + ", ".join(skipped)
                        )
                    elif dag.is_settled(nodes):
                        stop_reason = "nothing left to do"
                    else:
                        stop_reason = "no runnable tasks"
                    break

                done, _pending = await asyncio.wait(
                    running.values(), return_when=asyncio.FIRST_COMPLETED
                )
                for finished in done:
                    task_id = next(
                        tid for tid, handle in running.items() if handle is finished
                    )
                    del running[task_id]
                    task, ok, error, text = await finished
                    event = await self._settle(task, ok, error, text)
                    if event == SchedulerEvent.COMPLETED:
                        completed.append(task.key)
                    elif event == SchedulerEvent.FAILED:
                        failed.append(task.key)
            else:
                stop_reason = f"reached the {max_passes}-pass ceiling"

        except asyncio.CancelledError:
            interrupted = True
            stop_reason = "cancelled"
            for handle in running.values():
                handle.cancel()
            if running:
                await asyncio.gather(*running.values(), return_exceptions=True)
            raise
        finally:
            await asyncio.to_thread(self.tasks.refresh_readiness)

        final_nodes = await asyncio.to_thread(self.tasks.tasks.nodes)
        blocked = sorted(
            n.key for n in final_nodes.values() if n.status is TaskStatus.BLOCKED
        )

        self._notify(SchedulerEvent.STOP, stop_reason)
        return SchedulerReport(
            passes=passes,
            dispatched=dispatched,
            completed=completed,
            failed=failed,
            blocked=blocked,
            skipped=skipped,
            stop_reason=stop_reason,
            interrupted=interrupted,
        )

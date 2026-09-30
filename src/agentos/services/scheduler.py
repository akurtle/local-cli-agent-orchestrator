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
from pathlib import Path

from agentos.config import Config
from agentos.db.session import Database
from agentos.prompts.task_prompt import build_task_prompt
from agentos.schemas.dto import SchedulerReport, TaskView
from agentos.schemas.enums import AgentStatus, FailureKind, TaskStatus
from agentos.services import dag
from agentos.services.agents import AgentBusy, AgentPaused, AgentService
from agentos.services.context_service import ContextService
from agentos.services.events import EventBus, EventType
from agentos.services.memory import MemoryService
from agentos.schemas.capabilities import Capability
from agentos.services.messages import MessageService
from agentos.services.permissions import PermissionService
from agentos.services.results import ResultProcessor, build_repair_prompt
from agentos.services.tasks import TaskService
from agentos.services.verification import VerificationService
from agentos.services.workspaces import WorkspaceService

# Agent statuses that can accept a new task.
AVAILABLE_AGENT_STATUSES = frozenset({AgentStatus.IDLE, AgentStatus.FAILED})


class SchedulerEvent:
    """Names for the progress callback, so the CLI can render without guessing."""

    DISPATCH = "dispatch"
    DENIED = "denied"
    COMPLETED = "completed"
    MESSAGE = "message"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    WORKSPACE = "workspace"
    ROTATED = "rotated"
    REMEMBERED = "remembered"
    HANDOFF = "handoff"
    CHANGES = "changes"
    SPAWNED = "spawned"
    REPAIR = "repair"
    REJECTED = "rejected"
    FAILED = "failed"
    RETRY = "retry"
    BLOCKED = "blocked"
    PASS = "pass"
    STOP = "stop"


TIMEOUT_MARKERS = ("timeout", "timed out")
LAUNCH_MARKERS = ("failed to spawn", "could not find", "no such file")


def classify_run_failure(outcome) -> FailureKind:
    """Classify a failure where the process itself did not succeed.

    Pure and string-based because the CLI reports these as prose, not
    structurally. Anything unrecognised is treated as a launch problem, which is
    retryable -- the conservative choice, since the alternative would silently
    give up on a transient error.
    """
    haystack = f"{outcome.error or ''} {outcome.text or ''}".lower()
    if any(marker in haystack for marker in TIMEOUT_MARKERS):
        return FailureKind.TIMEOUT
    if any(marker in haystack for marker in LAUNCH_MARKERS):
        return FailureKind.LAUNCH
    return FailureKind.LAUNCH


def classify_result(result) -> FailureKind:
    """Classify a failure where the agent ran but the turn did not succeed."""
    if not result.parsed:
        return FailureKind.UNPARSEABLE
    if result.status == "blocked" or result.blockers:
        return FailureKind.BLOCKED
    return FailureKind.AGENT_FAILED


class Scheduler:
    def __init__(
        self,
        db: Database,
        config: Config,
        agent_service: AgentService,
        task_service: TaskService,
        message_service: MessageService | None = None,
        result_processor: ResultProcessor | None = None,
        workspace_service: WorkspaceService | None = None,
        context_service: ContextService | None = None,
        memory_service: MemoryService | None = None,
        event_bus: EventBus | None = None,
        verification_service: VerificationService | None = None,
        on_progress: Callable[[str, str], None] | None = None,
        stop_check: Callable[[], bool] | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.agents = agent_service
        self.tasks = task_service
        self.messages = message_service or MessageService(db, config)
        self.results = result_processor or ResultProcessor(
            db, config, task_service, self.messages
        )
        self.workspaces = workspace_service
        self.memory = memory_service or MemoryService(db, config)
        self.permissions = PermissionService(db, config)
        self.events = event_bus or EventBus(db)
        self.verification = verification_service or VerificationService(db, config)
        self.context = context_service or ContextService(
            db, config, task_service, self.memory
        )
        self.on_progress = on_progress
        # Polled before each pass, for a stop asked for from another process
        # (the dashboard). Same effect as request_stop().
        self.stop_check = stop_check
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

    def _stop_asked(self) -> bool:
        if self.stop_check is None:
            return False
        try:
            return bool(self.stop_check())
        except Exception:  # a broken check must not stop real work
            return False

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

    def _build_context(self, agent, task: TaskView, inbox):
        """Assemble the layered context for one task.

        Replaces flat prompt concatenation: the orchestrator now decides, layer
        by layer, what the agent sees, and each layer has a budget.
        """
        return self.context.assemble(agent=agent, task=task, inbox=inbox.items)

    async def _run_task(
        self, task: TaskView
    ) -> tuple[TaskView, bool, str | None, str, FailureKind | None]:
        """Execute one task.

        Returns (task, ok, error, output, failure kind). The kind is what lets the
        retry policy tell "the process would not start" apart from "the agent says
        it is blocked".
        """
        agent_name = task.assigned_agent
        assert agent_name is not None  # selection guarantees this

        # Claim unread messages. They are marked delivered now and only
        # confirmed read once this run finishes, so a crash cannot lose them.
        inbox = await asyncio.to_thread(self.messages.take_inbox, agent_name)

        # Give the agent an isolated worktree if it is configured for one, so two
        # coding agents never edit the same checkout at once. Done before context
        # assembly so the identity layer can state which branch it is on.
        workspace = None
        agent_view = await asyncio.to_thread(self.agents.get_agent, agent_name)
        if self.workspaces is not None:
            workspace = await self.workspaces.prepare(agent_view)
            if workspace.warning:
                self._notify(
                    SchedulerEvent.WORKSPACE, f"{agent_name}: {workspace.warning}"
                )
            elif workspace.isolated:
                await asyncio.to_thread(
                    self.agents.agents.set_worktree,
                    agent_name,
                    str(workspace.path),
                    workspace.branch,
                )
                self._notify(
                    SchedulerEvent.WORKSPACE,
                    f"{agent_name} -> {workspace.branch}",
                )
                await self.events.emit_async(
                    EventType.GIT_WORKTREE_CREATED,
                    summary=str(workspace.branch),
                    agent=agent_name,
                    task_key=task.key,
                    path=str(workspace.path),
                )
                agent_view = await asyncio.to_thread(self.agents.get_agent, agent_name)

        # Rotate first: rotation writes what the old session knew into memory,
        # and the prompt below must be able to recall it.
        rotated = await self.agents.maybe_rotate(agent_name, task.objective_id)
        if rotated:
            self._notify(SchedulerEvent.ROTATED, f"{agent_name}: {rotated}")
            await self.events.emit_async(
                EventType.AGENT_ROTATED,
                summary=rotated,
                agent=agent_name,
                task_key=task.key,
            )
            agent_view = await asyncio.to_thread(self.agents.get_agent, agent_name)

        assembled = await asyncio.to_thread(
            self._build_context, agent_view, task, inbox
        )
        prompt = assembled.prompt

        try:
            outcome = await self.agents.run_agent(
                agent_name,
                prompt,
                task_id=task.id,
                cwd=str(workspace.path) if workspace else None,
                objective_id=task.objective_id,
            )
        except (AgentBusy, AgentPaused) as exc:
            # Lost a race for the agent: return the task to the queue untouched.
            await asyncio.to_thread(self.messages.release, inbox)
            await asyncio.to_thread(
                self.tasks.tasks.set_status, task.id, TaskStatus.READY
            )
            return (
                task,
                False,
                f"agent unavailable: {exc}",
                "",
                FailureKind.UNAVAILABLE,
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(self.messages.release, inbox)
            await asyncio.to_thread(
                self.tasks.tasks.set_status, task.id, TaskStatus.READY
            )
            raise
        except Exception as exc:
            # The runtime raised rather than returning: the process never ran.
            await asyncio.to_thread(self.messages.release, inbox)
            return (
                task,
                False,
                f"{type(exc).__name__}: {exc}",
                "",
                FailureKind.LAUNCH,
            )

        if not outcome.ok:
            # The process itself failed, so the agent never really read anything.
            await asyncio.to_thread(self.messages.release, inbox)
            error = outcome.error or "agent failed"
            return task, False, error, outcome.text, classify_run_failure(outcome)

        result = await self._apply_result(task, agent_name, outcome.text)

        if result.treat_as_failure:
            await asyncio.to_thread(self.messages.release, inbox)
            reason = result.parse_error or (
                "; ".join(result.blockers) or f"agent reported {result.status}"
            )
            return task, False, reason, outcome.text, classify_result(result)

        # The run completed and its response was understood: the messages and
        # handoffs that went into this prompt are genuinely consumed.
        await asyncio.to_thread(self.messages.confirm_read, inbox)
        await asyncio.to_thread(self.memory.confirm_handoffs, assembled.handoffs)

        summary = result.summary or outcome.text

        # The agent has *claimed* success. Record that, then check it: a claim is
        # not a fact, and only the checks decide whether the task completes.
        await asyncio.to_thread(
            self.tasks.transition, task.id, TaskStatus.AGENT_DONE, None, None
        )

        # Only report changes for an isolated worktree. In a shared project
        # directory, concurrent agents and the orchestrator's own state files all
        # show up as changes, so attributing any of them to this task would be
        # wrong rather than merely noisy.
        if (
            workspace is not None
            and workspace.isolated
            and self.workspaces is not None
        ):
            report = await self.workspaces.capture(
                workspace, claimed_files=result.files_changed
            )
            if not report.is_empty:
                self._notify(
                    SchedulerEvent.CHANGES,
                    f"{task.key}: {len(report.files_changed)} file(s) "
                    f"{report.diff_summary}",
                )
            if report.unverified_claims:
                self._notify(
                    SchedulerEvent.REJECTED,
                    f"{task.key}: claimed but unchanged: "
                    + ", ".join(report.unverified_claims[:5]),
                )

            # Detective layer: git shows what really happened, so an edit that
            # slipped past the tool denial is still caught and recorded.
            if report.files_changed and not await asyncio.to_thread(
                self.permissions.check,
                agent_name,
                Capability.EDIT_FILES,
                "edit_files",
                f"{len(report.files_changed)} file(s) changed without permission: "
                + ", ".join(report.files_changed[:10]),
                task.key,
            ):
                self._notify(
                    SchedulerEvent.DENIED,
                    f"{agent_name} changed {len(report.files_changed)} file(s) "
                    f"without edit_files ({task.key})",
                )
                await self.events.emit_async(
                    EventType.CAPABILITY_DENIED,
                    summary=f"edit_files: {len(report.files_changed)} file(s)",
                    agent=agent_name,
                    task_key=task.key,
                    capability="edit_files",
                    files=report.files_changed[:20],
                )
            summary = summary + "\n\n" + report.render()

        await self._record_knowledge(task, agent_name, result)

        verdict = await self._verify(task, agent_name, workspace)
        if verdict is not None:
            summary = summary + "\n\n" + verdict.render()
            if not verdict.passed:
                reasons = "; ".join(
                    f"{c.short} {c.detail}".strip() for c in verdict.failures
                )
                return (
                    task,
                    False,
                    f"verification failed: {reasons}",
                    summary,
                    FailureKind.VERIFICATION_FAILED,
                )

        return task, True, None, summary, None

    async def _verify(self, task: TaskView, agent_name: str, workspace):
        """Run the configured checks against the agent's claim.

        Returns None when verification is switched off, so the caller can tell
        "nothing to check" apart from "checked and passed".
        """
        if not self.verification.is_enabled():
            return None

        agent_view = await asyncio.to_thread(self.agents.get_agent, agent_name)
        checks, _review, _manual = await asyncio.to_thread(
            self.verification.plan, task, agent_view.role
        )
        if not checks:
            return None

        await asyncio.to_thread(
            self.tasks.transition, task.id, TaskStatus.VERIFYING, None, None
        )
        self._notify(
            SchedulerEvent.VERIFYING, f"{task.key}: {len(checks)} check(s)"
        )

        # Verify where the work happened. Without a worktree that is the project
        # directory, which is also the boundary the checks may not escape.
        directory = str(workspace.path) if workspace else str(
            self.agents.project_root or Path.cwd()
        )
        verdict = await self.verification.verify(
            task=task,
            cwd=directory,
            boundary=directory,
            agent_role=agent_view.role,
        )

        if verdict.passed:
            self._notify(SchedulerEvent.VERIFIED, task.key)
            await self.events.emit_async(
                EventType.TASK_VERIFIED,
                summary=f"{len(verdict.checks)} check(s) passed",
                task_key=task.key,
                agent=agent_name,
                objective_id=task.objective_id,
            )
        else:
            detail = "; ".join(c.short for c in verdict.failures)
            self._notify(SchedulerEvent.UNVERIFIED, f"{task.key}: {detail}")
            await self.events.emit_async(
                EventType.TASK_VERIFICATION_FAILED,
                summary=detail[:200],
                task_key=task.key,
                agent=agent_name,
                objective_id=task.objective_id,
            )
        return verdict

    async def _record_knowledge(self, task: TaskView, agent_name: str, result) -> None:
        """Persist what should outlive this run, and hand off to dependents.

        Only the durable parts: decisions and warnings change what a later agent
        should do, so they become memories. The summary describes one task and is
        already stored on it.
        """
        stored = await asyncio.to_thread(
            self.memory.record_from_response,
            agent_name,
            task,
            result.decisions,
            result.warnings,
            task.objective_id,
        )
        if stored:
            self._notify(
                SchedulerEvent.REMEMBERED,
                f"{agent_name}: {len(stored)} fact(s) from {task.key}",
            )
            await self.events.emit_async(
                EventType.MEMORY_RECORDED,
                summary=f"{len(stored)} fact(s)",
                agent=agent_name,
                task_key=task.key,
                objective_id=task.objective_id,
                count=len(stored),
            )

        # A handoff is only useful if somebody is waiting on this work.
        recipients = await asyncio.to_thread(self._dependent_agents, task)
        for recipient in recipients:
            await asyncio.to_thread(
                self.memory.create_handoff,
                agent_name,
                task,
                result.summary,
                recipient,
                result.files_changed,
                result.interfaces,
                result.decisions,
                result.warnings,
            )
            self._notify(
                SchedulerEvent.HANDOFF, f"{agent_name} -> {recipient} ({task.key})"
            )
            await self.events.emit_async(
                EventType.HANDOFF_CREATED,
                summary=f"{agent_name} -> {recipient}",
                agent=agent_name,
                task_key=task.key,
                objective_id=task.objective_id,
                recipient=recipient,
            )

    def _dependent_agents(self, task: TaskView) -> list[str]:
        """Agents assigned to tasks that depend on this one.

        The receiving agent gets a small packet instead of needing this agent's
        conversation, which is what makes independent sessions workable.
        """
        recipients: list[str] = []
        for candidate in self.tasks.list_tasks():
            if candidate.id == task.id or candidate.status.is_terminal:
                continue
            if task.key not in candidate.depends_on:
                continue
            owner = candidate.assigned_agent
            if owner and owner != task.assigned_agent and owner not in recipients:
                recipients.append(owner)
        return recipients

    async def _apply_result(self, task: TaskView, agent_name: str, text: str):
        """Validate the agent response and apply what it legitimately asks for.

        One repair attempt is allowed for an unparseable reply; the repair prompt
        asks only for the response block, never for more work.
        """
        result = await asyncio.to_thread(
            self.results.process, text, task, agent_name
        )

        if not result.parsed:
            self._notify(
                SchedulerEvent.REPAIR, f"{task.key}: {result.parse_error}"
            )
            try:
                repair = await self.agents.run_agent(
                    agent_name,
                    build_repair_prompt(text, result.parse_error or "unknown"),
                    task_id=task.id,
                )
            except Exception:
                return result
            if repair.ok:
                result = await asyncio.to_thread(
                    self.results.process, repair.text, task, agent_name, True
                )

        for key in result.tasks_created:
            self._notify(SchedulerEvent.SPAWNED, f"{agent_name} requested {key}")
        if result.messages_sent:
            self._notify(
                SchedulerEvent.MESSAGE,
                f"{agent_name} sent {result.messages_sent} message(s)",
            )
        for reason in result.rejected:
            self._notify(SchedulerEvent.REJECTED, f"{task.key}: {reason}")
        return result

    async def _settle(
        self,
        task: TaskView,
        ok: bool,
        error: str | None,
        text: str = "",
        kind: FailureKind | None = None,
    ) -> str:
        """Record the outcome of a finished task and return the event name.

        The retry policy depends on why the attempt failed:
          * a reported blocker is never retried -- it needs intervention, and
            another attempt would hit the same wall and spend more usage
          * losing a race for a busy agent is not the task's fault, so it returns
            to the queue without consuming an attempt
          * everything else retries up to max_task_retries
        """
        if ok:
            await asyncio.to_thread(
                self.tasks.transition, task.id, TaskStatus.COMPLETED, text or "", None
            )
            self._notify(SchedulerEvent.COMPLETED, task.key)
            await self.events.emit_async(
                EventType.TASK_COMPLETED,
                summary=(text or "")[:200],
                task_key=task.key,
                agent=task.assigned_agent,
                objective_id=task.objective_id,
            )
            return SchedulerEvent.COMPLETED

        if kind is FailureKind.UNAVAILABLE:
            # _run_task already returned it to READY; do not penalise the task.
            self._notify(SchedulerEvent.RETRY, f"{task.key}: {error}")
            return SchedulerEvent.RETRY

        if kind is FailureKind.BLOCKED:
            await asyncio.to_thread(
                self.tasks.transition,
                task.id,
                TaskStatus.BLOCKED,
                None,
                error or "blocked",
                True,
            )
            self._notify(SchedulerEvent.BLOCKED, f"{task.key}: {error}")
            await self.events.emit_async(
                EventType.TASK_BLOCKED,
                summary=error or "blocked",
                task_key=task.key,
                agent=task.assigned_agent,
                objective_id=task.objective_id,
                needs_intervention=True,
            )
            return SchedulerEvent.BLOCKED

        attempts = await asyncio.to_thread(self.tasks.tasks.increment_attempts, task.id)
        max_retries = self.config.orchestrator.max_task_retries
        if attempts <= max_retries:
            # Return to the queue for another attempt; the retry prompt includes
            # the previous error.
            await asyncio.to_thread(
                self.tasks.tasks.set_status,
                task.id,
                TaskStatus.READY,
                None,
                error or "unknown failure",
            )
            label = kind.value if kind else "failure"
            self._notify(
                SchedulerEvent.RETRY,
                f"{task.key} ({label}, attempt {attempts}/{max_retries})",
            )
            await self.events.emit_async(
                EventType.TASK_RETRIED,
                summary=f"{label}, attempt {attempts}/{max_retries}",
                task_key=task.key,
                agent=task.assigned_agent,
                objective_id=task.objective_id,
                failure_kind=label,
                attempt=attempts,
            )
            return SchedulerEvent.RETRY

        final = (
            TaskStatus.FAILED_VERIFICATION
            if kind is FailureKind.VERIFICATION_FAILED
            else TaskStatus.FAILED
        )
        await asyncio.to_thread(
            self.tasks.transition, task.id, final, None, error or "failed"
        )
        suffix = f" [{kind.value}]" if kind else ""
        self._notify(SchedulerEvent.FAILED, f"{task.key}: {error}{suffix}")
        await self.events.emit_async(
            EventType.TASK_FAILED,
            summary=(error or "failed")[:200],
            task_key=task.key,
            agent=task.assigned_agent,
            objective_id=task.objective_id,
            failure_kind=kind.value if kind else None,
            attempts=attempts,
        )
        return SchedulerEvent.FAILED

    async def run(self, max_passes: int = 1000) -> SchedulerReport:
        """Drive tasks to completion.

        Stops when nothing can progress, when interrupted, or when the pass
        ceiling is hit (a safety valve, not an expected exit).
        """
        await self.events.emit_async(
            EventType.SCHEDULER_STARTED,
            summary=f"concurrency {self.config.orchestrator.max_concurrent_agents}",
        )
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
                if not self._stopping and self._stop_asked():
                    self.request_stop()
                    self._notify(
                        SchedulerEvent.STOP,
                        "stop requested: finishing running tasks, starting nothing new",
                    )
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
                        await self.events.emit_async(
                            EventType.TASK_STARTED,
                            summary=f"dispatched to {task.assigned_agent}",
                            task_key=task.key,
                            agent=task.assigned_agent,
                            objective_id=task.objective_id,
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
                    task, ok, error, text, kind = await finished
                    event = await self._settle(task, ok, error, text, kind)
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
        await self.events.emit_async(
            EventType.SCHEDULER_STOPPED,
            summary=stop_reason,
            completed=len(completed),
            failed=len(failed),
            dispatched=dispatched,
        )
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

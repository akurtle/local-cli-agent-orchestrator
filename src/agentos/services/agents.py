"""Agent lifecycle: config sync, prompt assembly, invocation, state transitions.

This is the only place that decides how an agent moves between statuses and when
a Claude session is started versus resumed. The CLI asks this service for things;
it never drives the runtime or the database itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

from agentos.config import Config
from agentos.db.session import Database
from agentos.prompts.loader import build_system_prompt
from agentos.repositories.agents import AgentNotFound, AgentRepository
from agentos.runtime.base import AgentRuntime
from agentos.schemas.dto import AgentRunOutcome, AgentView
from agentos.schemas.enums import AgentStatus, MemoryCategory, MemoryScope
from agentos.schemas.runtime import RunRequest, RunResult, StreamEvent
from agentos.services.memory import MemoryService
from agentos.services.permissions import PermissionService
from agentos.services.rotation import (
    SUMMARY_INSTRUCTION,
    RotationDecision,
    RotationReason,
    parse_summary,
    should_rotate,
)
from agentos.services.runs import record_run

# Statuses an agent may move to from a given status. Kept as data so the rules
# are auditable in one place instead of implied by scattered assignments.
ALLOWED_TRANSITIONS: dict[AgentStatus, frozenset[AgentStatus]] = {
    AgentStatus.IDLE: frozenset(
        {AgentStatus.WORKING, AgentStatus.PAUSED, AgentStatus.OFFLINE}
    ),
    AgentStatus.WORKING: frozenset(
        {
            AgentStatus.IDLE,
            AgentStatus.WAITING,
            AgentStatus.BLOCKED,
            AgentStatus.FAILED,
            AgentStatus.OFFLINE,
        }
    ),
    AgentStatus.WAITING: frozenset(
        {AgentStatus.WORKING, AgentStatus.IDLE, AgentStatus.BLOCKED, AgentStatus.OFFLINE}
    ),
    AgentStatus.BLOCKED: frozenset(
        {AgentStatus.IDLE, AgentStatus.WORKING, AgentStatus.OFFLINE}
    ),
    AgentStatus.FAILED: frozenset(
        {AgentStatus.IDLE, AgentStatus.WORKING, AgentStatus.OFFLINE}
    ),
    AgentStatus.PAUSED: frozenset({AgentStatus.IDLE, AgentStatus.OFFLINE}),
    AgentStatus.OFFLINE: frozenset({AgentStatus.IDLE}),
}


class InvalidTransition(RuntimeError):
    """Refused an illegal agent status change."""


class AgentBusy(RuntimeError):
    """The agent is already working on something."""


class AgentPaused(RuntimeError):
    """The agent is paused and will not accept work."""


class AgentService:
    def __init__(
        self,
        db: Database,
        config: Config,
        runtime: AgentRuntime,
        project_root: Path | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.runtime = runtime
        self.project_root = project_root
        self.agents = AgentRepository(db)
        # Rotation needs somewhere to put what the old session knew.
        self.memory = MemoryService(db, config)
        self.permissions = PermissionService(db, config)

    # ------------------------------------------------------------ config -> db

    def sync_from_config(self) -> tuple[list[AgentView], list[str]]:
        """Make the database match the configured agent roster.

        Returns (all agents, names newly created). Config owns *definitions*;
        the database owns *state*, so an existing agent keeps its session and
        status across a sync.

        An agent that has been removed from the config is marked OFFLINE rather
        than deleted: its runs and session are history worth keeping, but it must
        stop being eligible for work. Removing it outright would orphan its run
        records and its worktree.
        """
        created: list[str] = []
        for name, section in self.config.agents.items():
            _view, was_created = self.agents.upsert(
                name=name,
                role=section.role,
                description=section.description,
                runtime=self.config.runtime.name,
                model=section.model or self.config.runtime.model,
            )
            if was_created:
                created.append(name)

        self._retire_unconfigured()
        return self.agents.list(), created

    def _retire_unconfigured(self) -> list[str]:
        """Take agents no longer in the config out of circulation."""
        retired: list[str] = []
        for view in self.agents.list():
            if view.name in self.config.agents:
                # A returning agent becomes available again.
                if view.status is AgentStatus.OFFLINE:
                    self.agents.set_status(view.name, AgentStatus.IDLE)
                continue
            if view.status is not AgentStatus.OFFLINE:
                self.agents.set_status(
                    view.name, AgentStatus.OFFLINE, clear_task=True
                )
                retired.append(view.name)
        return retired

    def list_agents(self) -> list[AgentView]:
        return self.agents.list()

    def get_agent(self, name: str) -> AgentView:
        """Fetch an agent, syncing from config first if it is not in the db yet."""
        existing = self.agents.find(name)
        if existing is not None:
            return existing
        if name in self.config.agents:
            self.sync_from_config()
            return self.agents.get(name)
        known = ", ".join(sorted(self.config.agents)) or "(none configured)"
        raise AgentNotFound(
            f"unknown agent {name!r}. Configured agents: {known}"
        )

    # ------------------------------------------------------------------ prompts

    def roster(self) -> dict[str, str]:
        """agent name -> role, for telling an agent who it may address."""
        return {name: section.role for name, section in self.config.agents.items()}

    def system_prompt_for(self, agent: AgentView) -> str:
        section = self.config.agents.get(agent.name)
        return build_system_prompt(
            agent_name=agent.name,
            role=agent.role,
            description=agent.description,
            project_name=self.config.project.name,
            roster=self.roster(),
            project_root=self.project_root,
            explicit_path=section.prompt if section else None,
            capabilities=self.permissions.capability_prompt(agent),
        )

    # ------------------------------------------------------------- transitions

    def transition(
        self,
        name: str,
        to: AgentStatus,
        current_task_id: int | None = None,
        clear_task: bool = False,
    ) -> AgentView:
        """Move an agent to a new status, refusing illegal moves."""
        agent = self.agents.get(name)
        if agent.status == to:
            return agent
        allowed = ALLOWED_TRANSITIONS.get(agent.status, frozenset())
        if to not in allowed:
            raise InvalidTransition(
                f"{name}: cannot go from {agent.status.value} to {to.value}"
            )
        return self.agents.set_status(
            name, to, current_task_id=current_task_id, clear_task=clear_task
        )

    def pause(self, name: str) -> AgentView:
        agent = self.get_agent(name)
        if agent.status is AgentStatus.WORKING:
            raise AgentBusy(f"{name} is working on task {agent.current_task_id}")
        return self.transition(name, AgentStatus.PAUSED)

    def unpause(self, name: str) -> AgentView:
        self.get_agent(name)
        return self.transition(name, AgentStatus.IDLE)

    def reset_session(self, name: str) -> AgentView:
        """Forget an agent's Claude session so the next run starts fresh."""
        self.get_agent(name)
        return self.agents.set_session_id(name, None)

    # ----------------------------------------------------------------- rotation

    def rotation_decision(
        self, agent: AgentView, objective_id: int | None = None
    ) -> RotationDecision:
        """Whether this agent's session should be replaced before its next task."""
        context = self.config.context
        return should_rotate(
            session_id=agent.session_id,
            session_task_count=agent.session_task_count,
            session_objective_id=agent.session_objective_id,
            next_objective_id=objective_id,
            max_tasks_per_session=context.max_tasks_per_session,
            rotate_on_objective_change=context.rotate_on_objective_change,
        )

    async def maybe_rotate(
        self, name: str, objective_id: int | None = None
    ) -> str | None:
        """Rotate if policy says so. Returns a description, or None.

        Callers that build a prompt must invoke this *first*: rotation harvests
        knowledge into memory, and that memory has to be available when the
        prompt is assembled. Rotating afterwards would drop the carried facts on
        the very first task of the new session -- the one that needs them most.
        """
        agent = self.get_agent(name)
        decision = self.rotation_decision(agent, objective_id)
        if not decision.rotate or decision.reason is None:
            return None
        await self.rotate_session(name, decision.reason, objective_id)
        return f"{decision.reason.value}: {decision.detail}"

    async def rotate_session(
        self,
        name: str,
        reason: RotationReason = RotationReason.MANUAL,
        objective_id: int | None = None,
    ) -> list[str]:
        """Replace an agent's session, carrying knowledge across as memory.

        Order matters: the summary is taken and persisted *before* the session is
        discarded, so a failure in between loses nothing that was not already
        saved. Returns the facts carried forward.
        """
        agent = self.get_agent(name)
        if not agent.session_id:
            return []

        carried: list[str] = []
        if self.config.context.summarise_on_rotation:
            carried = await self._harvest_session_knowledge(agent)

        # Discard the conversation. The next run starts fresh and is given the
        # persisted memory instead.
        await asyncio.to_thread(
            self.agents.set_session,
            name,
            None,
            0,
            objective_id,
        )
        return carried

    async def _harvest_session_knowledge(self, agent: AgentView) -> list[str]:
        """Ask the agent what is worth keeping, then persist it.

        A failure here is not fatal: losing a summary is worse than losing the
        run, but not worth refusing to rotate over, since the alternative is an
        ever-growing session.
        """
        try:
            result = await self._invoke(
                agent=agent,
                prompt=SUMMARY_INSTRUCTION,
                system_prompt=self.system_prompt_for(agent),
                working_dir=str(self.project_root or Path.cwd()),
                timeout=self.config.orchestrator.default_timeout_seconds,
                on_event=None,
            )
        except Exception:
            return []

        if not result.ok:
            return []

        facts = parse_summary(result.text)
        for fact in facts:
            await asyncio.to_thread(
                self.memory.remember,
                MemoryScope.AGENT,
                fact,
                agent.name,
                MemoryCategory.SESSION_SUMMARY,
                None,
                agent.name,
                None,
            )
        return facts

    # ---------------------------------------------------------------- execution

    async def run_agent(
        self,
        name: str,
        prompt: str,
        task_id: int | None = None,
        cwd: str | None = None,
        timeout_seconds: float | None = None,
        on_event: Callable[[StreamEvent], None] | None = None,
        objective_id: int | None = None,
        system_prompt: str | None = None,
    ) -> AgentRunOutcome:
        """Invoke one agent turn, persisting the run and its session.

        Starts a new Claude session the first time, resumes it afterwards, and
        falls back to a fresh session if the stored one has disappeared.
        """
        agent = self.get_agent(name)

        if agent.status is AgentStatus.PAUSED:
            raise AgentPaused(f"{name} is paused; run `agentctl resume {name}` first")
        if agent.status is AgentStatus.WORKING:
            raise AgentBusy(
                f"{name} is already working on task {agent.current_task_id}"
            )

        # A caller may supply a fully layered prompt; otherwise fall back to the
        # role prompt alone, which keeps older callers working unchanged.
        system_prompt = system_prompt or self.system_prompt_for(agent)
        working_dir = cwd or agent.worktree_path or str(self.project_root or Path.cwd())

        # Safety net for callers that did not rotate first (e.g. `agent run`).
        # The scheduler calls maybe_rotate before assembling its prompt, so this
        # is normally a no-op there.
        rotated_reason = await self.maybe_rotate(name, objective_id)
        if rotated_reason:
            agent = self.get_agent(name)
        timeout = timeout_seconds or self.config.orchestrator.default_timeout_seconds

        agent = await asyncio.to_thread(
            self.transition, name, AgentStatus.WORKING, task_id
        )

        resumed = bool(agent.session_id)
        session_restarted = False
        try:
            result = await self._invoke(
                agent=agent,
                prompt=prompt,
                system_prompt=system_prompt,
                working_dir=working_dir,
                timeout=timeout,
                on_event=on_event,
            )

            # A stored session can vanish (cleared history, different machine).
            # That is recoverable: start a fresh session once, and say so.
            if resumed and self.runtime.is_stale_session(result):
                await asyncio.to_thread(self.agents.set_session_id, name, None)
                session_restarted = True
                resumed = False
                agent = self.agents.get(name)
                result = await self._invoke(
                    agent=agent,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    working_dir=working_dir,
                    timeout=timeout,
                    on_event=on_event,
                )
        except BaseException:
            # Never leave an agent stuck in `working` because of a crash or Ctrl+C.
            await asyncio.to_thread(
                self._safe_transition, name, AgentStatus.FAILED, True
            )
            raise

        run_id = await asyncio.to_thread(
            record_run,
            self.db,
            result,
            agent_id=agent.id,
            task_id=task_id,
            runtime=self.runtime.name,
        )

        # Persist the session id before anything else can fail, so the next run
        # can resume even if the caller crashes here.
        if result.session_id:
            await asyncio.to_thread(self.agents.set_session_id, name, result.session_id)

        # A completed turn counts against the session's task budget.
        if result.ok:
            await asyncio.to_thread(
                self.agents.increment_session_tasks, name, objective_id
            )

        final_status = AgentStatus.IDLE if result.ok else AgentStatus.FAILED
        agent = await asyncio.to_thread(
            self._safe_transition, name, final_status, True
        )

        return AgentRunOutcome(
            agent=agent,
            run_id=run_id,
            ok=result.ok,
            text=result.text,
            error=result.error,
            session_id=result.session_id,
            resumed=resumed,
            session_restarted=session_restarted,
            rotated=rotated_reason,
            cost_usd=result.cost_usd,
            duration_seconds=result.duration_seconds,
        )

    async def _invoke(
        self,
        agent: AgentView,
        prompt: str,
        system_prompt: str,
        working_dir: str,
        timeout: float,
        on_event: Callable[[StreamEvent], None] | None,
    ) -> RunResult:
        request = RunRequest(
            prompt=prompt,
            system_prompt=system_prompt,
            session_id=agent.session_id,
            resume=bool(agent.session_id),
            cwd=working_dir,
            model=agent.model,
            timeout_seconds=timeout,
            disallowed_tools=self.permissions.grants_for(agent).denied_tools,
            stream=True,
        )
        return await self.runtime.run(request, on_event=on_event)

    def _safe_transition(
        self, name: str, to: AgentStatus, clear_task: bool = False
    ) -> AgentView:
        """Transition, falling back to a direct write if the move is illegal.

        Used on teardown paths: refusing to record a terminal status would be
        worse than bending the state machine, because the agent would stay stuck
        in `working` forever.
        """
        try:
            return self.transition(name, to, clear_task=clear_task)
        except InvalidTransition:
            return self.agents.set_status(name, to, clear_task=clear_task)

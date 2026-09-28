"""Integration of agent branches.

The rule this module exists to enforce: **never silently destroy an agent's
work**. So it is built around detection, not resolution.

    inspect branches
          |
    can it merge cleanly?
       /            \\
     yes             no
      |               |
    merge          report + create a resolution task

Merging happens on a dedicated integration branch, never on whatever the operator
has checked out, and a conflict aborts back to a clean state rather than leaving
half-merged files behind.

Conflict *resolution* is deliberately not automated. A conflict means two agents
disagreed about the same lines, and picking a winner mechanically is how work gets
lost. The orchestrator reports it and queues a task for somebody to decide.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agentos.config import Config
from agentos.db.session import Database
from agentos.paths import ProjectPaths
from agentos.schemas.dto import TaskView
from agentos.services.tasks import TaskService
from agentos.vcs.manager import GitError, GitManager, branch_for

INTEGRATION_PREFIX = "integration"


@dataclass(frozen=True)
class BranchState:
    """What one agent branch contains relative to the base."""

    agent: str
    branch: str
    exists: bool = False
    commits: int = 0
    files: list[str] = field(default_factory=list)
    merges_cleanly: bool = False
    error: str | None = None

    @property
    def has_work(self) -> bool:
        return self.exists and self.commits > 0


@dataclass(frozen=True)
class Overlap:
    """Two branches that changed the same file."""

    path: str
    agents: list[str]

    def describe(self) -> str:
        return f"{self.path} ({', '.join(self.agents)})"


@dataclass(frozen=True)
class IntegrationPlan:
    """What integration would do, computed before anything is changed."""

    base: str
    integration_branch: str
    branches: list[BranchState] = field(default_factory=list)
    overlaps: list[Overlap] = field(default_factory=list)

    @property
    def mergeable(self) -> list[BranchState]:
        return [b for b in self.branches if b.has_work and b.merges_cleanly]

    @property
    def conflicted(self) -> list[BranchState]:
        return [b for b in self.branches if b.has_work and not b.merges_cleanly]

    @property
    def empty(self) -> list[BranchState]:
        return [b for b in self.branches if b.exists and not b.has_work]

    @property
    def is_clean(self) -> bool:
        return not self.conflicted

    @property
    def sequential_risk(self) -> bool:
        """True when branches overlap, so merging one may break another.

        Each branch is checked against the base independently, so `merges_cleanly`
        cannot see conflicts between two branches. An overlap is the signal that
        the per-branch verdict may not survive integration.
        """
        return bool(self.overlaps) and len(self.mergeable) > 1

    @property
    def has_anything_to_do(self) -> bool:
        return bool(self.mergeable or self.conflicted)


@dataclass(frozen=True)
class IntegrationResult:
    """What integration actually did."""

    plan: IntegrationPlan
    merged: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    resolution_tasks: list[str] = field(default_factory=list)
    integration_branch: str | None = None

    @property
    def ok(self) -> bool:
        return not self.failed


def compute_overlaps(branches: list[BranchState]) -> list[Overlap]:
    """Find files touched by more than one branch.

    Pure, so it is testable without git. An overlap is not necessarily a
    conflict -- two agents can edit different parts of one file -- but it is what
    an operator wants flagged before integrating.
    """
    owners: dict[str, list[str]] = {}
    for branch in branches:
        if not branch.has_work:
            continue
        for path in branch.files:
            owners.setdefault(path, []).append(branch.agent)
    return [
        Overlap(path=path, agents=sorted(agents))
        for path, agents in sorted(owners.items())
        if len(agents) > 1
    ]


class IntegrationService:
    def __init__(
        self,
        db: Database,
        config: Config,
        paths: ProjectPaths,
        task_service: TaskService,
        git: GitManager | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.paths = paths
        self.tasks = task_service
        self.git = git or GitManager(
            root=paths.root, worktrees_dir=paths.worktrees_dir
        )

    # ------------------------------------------------------------------ planning

    async def inspect_branch(self, agent: str, base: str) -> BranchState:
        """Describe one agent branch without modifying anything."""
        branch = branch_for(agent)
        if not await self.git.branch_exists(branch):
            return BranchState(agent=agent, branch=branch, exists=False)

        try:
            log = await self.git._run(
                "rev-list", "--count", f"{base}..{branch}", check=False
            )
            commits = int(log.text or 0) if log.ok and log.text.isdigit() else 0

            names = await self.git._run(
                "diff", "--name-only", f"{base}...{branch}", check=False
            )
            files = [line.strip() for line in names.stdout.splitlines() if line.strip()]

            clean = await self.git.can_merge_cleanly(branch, into=base)
        except GitError as exc:
            return BranchState(
                agent=agent, branch=branch, exists=True, error=str(exc)
            )

        return BranchState(
            agent=agent,
            branch=branch,
            exists=True,
            commits=commits,
            files=files,
            merges_cleanly=clean,
        )

    async def plan(
        self, agents: list[str] | None = None, base: str | None = None
    ) -> IntegrationPlan:
        """Work out what integration would do. Changes nothing."""
        await self.git.ensure_repository()
        base_branch = base or await self.git.current_branch() or "HEAD"
        names = agents or sorted(self.config.agents)

        branches = [await self.inspect_branch(name, base_branch) for name in names]
        return IntegrationPlan(
            base=base_branch,
            integration_branch=self.integration_branch_name(),
            branches=branches,
            overlaps=compute_overlaps(branches),
        )

    def integration_branch_name(self, objective_id: int | None = None) -> str:
        if objective_id is not None:
            return f"{INTEGRATION_PREFIX}/objective-{objective_id}"
        return f"{INTEGRATION_PREFIX}/current"

    # ---------------------------------------------------------------- integrating

    async def integrate(
        self,
        plan: IntegrationPlan,
        create_resolution_tasks: bool = True,
        resolver: str | None = None,
    ) -> IntegrationResult:
        """Merge what can merge; report what cannot.

        Work happens on a fresh integration branch built from the base, so the
        operator's checkout is never touched and abandoning the attempt costs
        nothing. A conflicting merge is aborted, leaving no half-merged files.
        """
        merged: list[str] = []
        failed: list[str] = []
        skipped = [b.agent for b in plan.empty]
        resolution_tasks: list[str] = []

        if not plan.has_anything_to_do:
            return IntegrationResult(
                plan=plan, skipped=skipped, integration_branch=None
            )

        branch = plan.integration_branch
        await self._prepare_integration_branch(branch, plan.base)

        # Branches are checked against the base, so two that each merge cleanly
        # on their own can still conflict with each other once the first lands.
        # Merging one at a time and re-checking catches that.
        conflicted = list(plan.conflicted)

        for state in plan.mergeable:
            result = await self.git.merge(
                state.branch,
                cwd=self.paths.root,
                message=f"integrate {state.branch}",
            )
            if result.ok:
                merged.append(state.agent)
                continue

            # Abort rather than leave half-merged files behind.
            await self.git._run("merge", "--abort", check=False)
            failed.append(state.agent)
            # It conflicts with work already integrated, not with the base, which
            # still needs somebody to resolve it.
            conflicted.append(state)

        for state in conflicted:
            if state.agent not in failed:
                failed.append(state.agent)
            if create_resolution_tasks:
                task = self._create_resolution_task(state, plan, resolver)
                if task is not None:
                    resolution_tasks.append(task.key)

        return IntegrationResult(
            plan=plan,
            merged=merged,
            failed=failed,
            skipped=skipped,
            resolution_tasks=resolution_tasks,
            integration_branch=branch,
        )

    async def _prepare_integration_branch(self, branch: str, base: str) -> None:
        """Check out a fresh integration branch at the base commit."""
        if await self.git.branch_exists(branch):
            await self.git._run("checkout", branch)
            # Start from the base again so a re-run is not built on a stale
            # half-finished attempt.
            await self.git._run("reset", "--hard", base)
        else:
            await self.git._run("checkout", "-b", branch, base)

    def _create_resolution_task(
        self,
        state: BranchState,
        plan: IntegrationPlan,
        resolver: str | None,
    ) -> TaskView | None:
        """Queue a task for a human or agent to resolve a conflict."""
        owner = resolver or state.agent
        if self.tasks.agents.find(owner) is None:
            owner = state.agent
        if self.tasks.agents.find(owner) is None:
            return None

        overlapping = [o.describe() for o in plan.overlaps if state.agent in o.agents]
        detail = "\n".join(f"- {item}" for item in overlapping) or "- (not determined)"

        try:
            return self.tasks.create_task(
                title=f"Resolve merge conflict on {state.branch}",
                description=(
                    f"`{state.branch}` does not merge cleanly into `{plan.base}`.\n\n"
                    f"Files also changed by another branch:\n{detail}\n\n"
                    "Rebase or adjust your branch so it merges cleanly. Do not "
                    "discard the other branch's changes."
                ),
                agent=owner,
                acceptance_criteria=[
                    f"{state.branch} merges into {plan.base} without conflicts",
                    "No changes from other agents were discarded",
                ],
                prefix="MERGE",
                created_by=None,
            )
        except Exception:
            # A failure to queue the task must not hide the conflict itself.
            return None

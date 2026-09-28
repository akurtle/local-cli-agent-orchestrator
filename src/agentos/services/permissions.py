"""Capability enforcement.

The single place that answers "may this agent do this?", and the single place
that records a refusal. Callers ask; they never decide for themselves.

Grants come from config, with role defaults for agents that declare none, plus
optional per-agent overrides stored in the database so an operator can grant or
revoke without editing a file mid-run.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from agentos.config import Config
from agentos.db.models import Denial
from agentos.db.session import Database
from agentos.schemas.capabilities import (
    Capability,
    defaults_for_role,
    denied_tools,
    describe,
    parse_capabilities,
)
from agentos.schemas.dto import AgentView


@dataclass(frozen=True)
class DenialRecord:
    id: int
    agent: str
    capability: str
    action: str
    detail: str = ""
    task_key: str | None = None
    created_at: object = None


@dataclass
class Grants:
    """What one agent may do, and where that came from."""

    agent: str
    role: str
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    source: str = "role defaults"
    unknown: list[str] = field(default_factory=list)
    """Configured names that are not real capabilities."""

    def has(self, capability: Capability) -> bool:
        return capability in self.capabilities

    @property
    def denied_tools(self) -> list[str]:
        return denied_tools(self.capabilities)

    def describe(self) -> tuple[list[str], list[str]]:
        return describe(self.capabilities)


class PermissionService:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config
        # Runtime overrides, keyed by agent name. Deliberately in-memory plus a
        # persisted denial trail rather than a mutable permissions table: config
        # stays the source of truth, and `permission grant` is an explicit,
        # visible act for the current session.
        self._overrides: dict[str, set[Capability]] = {}
        self._revocations: dict[str, set[Capability]] = {}

    # -------------------------------------------------------------------- grants

    def grants_for(self, agent: AgentView | str, role: str | None = None) -> Grants:
        """Everything this agent may do."""
        name = agent if isinstance(agent, str) else agent.name
        agent_role = role or (agent.role if not isinstance(agent, str) else "")

        section = self.config.agents.get(name)
        if section is not None and not agent_role:
            agent_role = section.role

        unknown: list[str] = []
        if section is not None and section.capabilities is not None:
            capabilities, unknown = parse_capabilities(section.capabilities)
            source = "config"
        else:
            capabilities = defaults_for_role(agent_role)
            source = f"defaults for role {agent_role or 'unknown'}"

        granted = set(capabilities) | self._overrides.get(name, set())
        granted -= self._revocations.get(name, set())

        if self._overrides.get(name) or self._revocations.get(name):
            source += " + runtime override"

        return Grants(
            agent=name,
            role=agent_role,
            capabilities=frozenset(granted),
            source=source,
            unknown=unknown,
        )

    def allows(self, agent: AgentView | str, capability: Capability) -> bool:
        return self.grants_for(agent).has(capability)

    def grant(self, agent: str, capability: Capability) -> Grants:
        self._overrides.setdefault(agent, set()).add(capability)
        self._revocations.get(agent, set()).discard(capability)
        return self.grants_for(agent)

    def revoke(self, agent: str, capability: Capability) -> Grants:
        self._revocations.setdefault(agent, set()).add(capability)
        self._overrides.get(agent, set()).discard(capability)
        return self.grants_for(agent)

    # ------------------------------------------------------------------ denials

    def deny(
        self,
        agent: str,
        capability: Capability,
        action: str,
        detail: str = "",
        task_key: str | None = None,
        run_id: int | None = None,
    ) -> DenialRecord:
        """Record a refused action.

        Always persisted: a refusal that leaves no trace is indistinguishable
        from the action never having been attempted, which is exactly what an
        operator needs to tell apart.
        """
        with self.db.session() as session:
            row = Denial(
                agent=agent,
                capability=capability.value,
                action=action,
                detail=detail[:2000],
                task_key=task_key,
                run_id=run_id,
            )
            session.add(row)
            session.flush()
            return DenialRecord(
                id=row.id,
                agent=row.agent,
                capability=row.capability,
                action=row.action,
                detail=row.detail,
                task_key=row.task_key,
                created_at=row.created_at,
            )

    def check(
        self,
        agent: str,
        capability: Capability,
        action: str,
        detail: str = "",
        task_key: str | None = None,
        run_id: int | None = None,
    ) -> bool:
        """Permit or refuse, recording the refusal. Returns True if allowed."""
        if self.allows(agent, capability):
            return True
        self.deny(agent, capability, action, detail, task_key, run_id)
        return False

    def denials(
        self, agent: str | None = None, limit: int | None = None
    ) -> list[DenialRecord]:
        with self.db.session() as session:
            stmt = select(Denial).order_by(Denial.id.desc())
            if agent:
                stmt = stmt.where(Denial.agent == agent)
            if limit:
                stmt = stmt.limit(limit)
            rows = list(session.scalars(stmt).all())
            rows.reverse()
            return [
                DenialRecord(
                    id=row.id,
                    agent=row.agent,
                    capability=row.capability,
                    action=row.action,
                    detail=row.detail,
                    task_key=row.task_key,
                    created_at=row.created_at,
                )
                for row in rows
            ]

    # -------------------------------------------------------------------- prompt

    def capability_prompt(self, agent: AgentView | str) -> str:
        """The CAPABILITIES section.

        The agent is told the same rules the code enforces, so a refusal is never
        a surprise -- but the prompt is a courtesy, not the control.
        """
        grants = self.grants_for(agent)
        may, may_not = grants.describe()
        lines = ["You MAY:"]
        lines += [f"- {phrase}" for phrase in may] or ["- (nothing)"]
        if may_not:
            lines.append("")
            lines.append("You MAY NOT:")
            lines += [f"- {phrase}" for phrase in may_not]
            lines.append("")
            lines.append(
                "These limits are enforced by the orchestrator, not by trust. "
                "Attempting a prohibited action will be refused and recorded."
            )
        return "\n".join(lines)

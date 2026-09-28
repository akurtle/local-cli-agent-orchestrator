"""Memory and handoffs: what the orchestrator carries forward.

Two jobs:

  * recall the right facts for one agent run, scoped rather than everything
  * build handoff packets so a receiving agent never needs the sending agent's
    conversation

Memory is written by Python from validated agent output, never by an agent
writing to the database. An agent can state a decision in its response block;
this service decides whether to keep it, at what importance, and in what scope.
"""

from __future__ import annotations

from agentos.config import Config
from agentos.db.session import Database
from agentos.repositories.memories import MemoryRepository
from agentos.schemas.dto import HandoffView, MemoryView, TaskView
from agentos.schemas.enums import MemoryCategory, MemoryScope

# How many of each scope to recall. The context builder trims further by
# characters; this keeps the query cheap and the ordering meaningful.
RECALL_LIMITS = {
    MemoryScope.PROJECT: 20,
    MemoryScope.AGENT: 12,
    MemoryScope.OBJECTIVE: 15,
    MemoryScope.TASK: 5,
}

# A fact an agent volunteers is worth less than one a human wrote down.
AGENT_IMPORTANCE = 45
HUMAN_IMPORTANCE = 70
DECISION_IMPORTANCE = 65
WARNING_IMPORTANCE = 60
SESSION_SUMMARY_IMPORTANCE = 55

MAX_MEMORY_CHARS = 1000


class MemoryService:
    def __init__(self, db: Database, config: Config) -> None:
        self.db = db
        self.config = config
        self.memories = MemoryRepository(db)

    # ---------------------------------------------------------------- recording

    def remember(
        self,
        scope: MemoryScope,
        content: str,
        scope_id: str | None = None,
        category: MemoryCategory = MemoryCategory.FACT,
        importance: int | None = None,
        created_by: str = "human",
        task_key: str | None = None,
    ) -> MemoryView | None:
        """Store a fact, de-duplicating within its scope.

        Returns None when the content is unusable. A repeat of an existing fact
        refreshes it rather than adding a second copy, because a prompt full of
        the same sentence is worse than useless.
        """
        text = (content or "").strip()
        if not text:
            return None
        if len(text) > MAX_MEMORY_CHARS:
            text = text[:MAX_MEMORY_CHARS].rstrip() + " ...[trimmed]"

        weight = importance if importance is not None else self._default_importance(
            category, created_by
        )

        existing = self.memories.find_duplicate(scope, scope_id, text)
        if existing is not None:
            return self.memories.touch(existing.id, importance=weight)

        return self.memories.add(
            scope=scope,
            scope_id=scope_id,
            content=text,
            category=category,
            importance=weight,
            created_by=created_by,
            task_key=task_key,
        )

    @staticmethod
    def _default_importance(category: MemoryCategory, created_by: str) -> int:
        if created_by == "human":
            return HUMAN_IMPORTANCE
        return {
            MemoryCategory.DECISION: DECISION_IMPORTANCE,
            MemoryCategory.WARNING: WARNING_IMPORTANCE,
            MemoryCategory.SESSION_SUMMARY: SESSION_SUMMARY_IMPORTANCE,
        }.get(category, AGENT_IMPORTANCE)

    def record_from_response(
        self,
        agent: str,
        task: TaskView | None,
        decisions: list[str],
        warnings: list[str],
        objective_id: int | None = None,
    ) -> list[MemoryView]:
        """Persist the durable parts of an agent's response.

        Decisions and warnings are kept because they change what a later agent
        should do. A summary is not: it describes one task and is already stored
        on that task.
        """
        stored: list[MemoryView] = []
        task_key = task.key if task else None

        # A decision about an objective belongs to the objective, so every agent
        # working on it sees it. Without one, it is the agent's own lesson.
        scope = MemoryScope.OBJECTIVE if objective_id else MemoryScope.AGENT
        scope_id = str(objective_id) if objective_id else agent

        for decision in decisions:
            memory = self.remember(
                scope=scope,
                scope_id=scope_id,
                content=decision,
                category=MemoryCategory.DECISION,
                created_by=agent,
                task_key=task_key,
            )
            if memory:
                stored.append(memory)

        for warning in warnings:
            memory = self.remember(
                scope=scope,
                scope_id=scope_id,
                content=warning,
                category=MemoryCategory.WARNING,
                created_by=agent,
                task_key=task_key,
            )
            if memory:
                stored.append(memory)

        return stored

    # ------------------------------------------------------------------- recall

    def recall_project(self) -> list[MemoryView]:
        return self.memories.list(
            scope=MemoryScope.PROJECT, limit=RECALL_LIMITS[MemoryScope.PROJECT]
        )

    def recall_agent(self, agent: str) -> list[MemoryView]:
        return self.memories.list(
            scope=MemoryScope.AGENT,
            scope_id=agent,
            limit=RECALL_LIMITS[MemoryScope.AGENT],
        )

    def recall_objective(self, objective_id: int | None) -> list[MemoryView]:
        if objective_id is None:
            return []
        return self.memories.list(
            scope=MemoryScope.OBJECTIVE,
            scope_id=str(objective_id),
            limit=RECALL_LIMITS[MemoryScope.OBJECTIVE],
        )

    def recall_task(self, task_key: str | None) -> list[MemoryView]:
        if not task_key:
            return []
        return self.memories.list(
            scope=MemoryScope.TASK,
            scope_id=task_key,
            limit=RECALL_LIMITS[MemoryScope.TASK],
        )

    def list_all(self, limit: int | None = None) -> list[MemoryView]:
        return self.memories.list(limit=limit)

    def forget(self, memory_id: int) -> None:
        self.memories.delete(memory_id)

    # ----------------------------------------------------------------- handoffs

    def create_handoff(
        self,
        from_agent: str,
        task: TaskView,
        summary: str,
        to_agent: str | None = None,
        files: list[str] | None = None,
        interfaces: list[str] | None = None,
        decisions: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> HandoffView:
        """Record what the next agent needs to know.

        Assembled by Python from the completed task plus whatever structured
        extras the agent supplied, so it is small and factual by construction.
        """
        return self.memories.add_handoff(
            from_agent=from_agent,
            to_agent=to_agent,
            task_id=task.id,
            task_key=task.key,
            objective_id=task.objective_id,
            summary=summary,
            important_files=files or [],
            interfaces=interfaces or [],
            decisions=decisions or [],
            warnings=warnings or [],
        )

    def take_handoffs(self, agent: str) -> list[HandoffView]:
        """Unconsumed handoffs addressed to this agent.

        Unlike messages, a handoff is marked consumed only by `confirm_handoffs`
        after a successful run, for the same reason: a crash must not lose it.
        """
        return self.memories.handoffs_for(agent, unconsumed_only=True, limit=10)

    def confirm_handoffs(self, handoffs: list[HandoffView]) -> None:
        self.memories.mark_handoffs_consumed([h.id for h in handoffs])

    def list_handoffs(self, limit: int | None = None) -> list[HandoffView]:
        return self.memories.list_handoffs(limit=limit)

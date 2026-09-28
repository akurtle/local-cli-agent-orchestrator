"""Data access for memories and handoffs."""

from __future__ import annotations

from sqlalchemy import select

from agentos.db.models import Handoff, Memory
from agentos.db.session import Database
from agentos.schemas.dto import HandoffView, MemoryView
from agentos.schemas.enums import MemoryCategory, MemoryScope


def _lines(text: str | None) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _text(items: list[str] | None) -> str:
    return "\n".join(i.strip() for i in (items or []) if i.strip())


class MemoryNotFound(LookupError):
    """No memory with that id."""


class MemoryRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ memories

    @staticmethod
    def _view(row: Memory) -> MemoryView:
        return MemoryView(
            id=row.id,
            scope=MemoryScope(row.scope),
            scope_id=row.scope_id,
            category=MemoryCategory(row.category),
            content=row.content,
            importance=row.importance,
            created_by=row.created_by,
            task_key=row.task_key,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    def add(
        self,
        scope: MemoryScope,
        content: str,
        scope_id: str | None = None,
        category: MemoryCategory = MemoryCategory.FACT,
        importance: int = 50,
        created_by: str = "human",
        task_key: str | None = None,
    ) -> MemoryView:
        with self.db.session() as session:
            row = Memory(
                scope=scope.value,
                scope_id=scope_id,
                category=category.value,
                content=content.strip(),
                importance=max(0, min(100, importance)),
                created_by=created_by,
                task_key=task_key,
            )
            session.add(row)
            session.flush()
            return self._view(row)

    def find_duplicate(
        self, scope: MemoryScope, scope_id: str | None, content: str
    ) -> MemoryView | None:
        """Exact-content match within a scope.

        Cheap de-duplication: agents restate the same fact across turns, and a
        prompt full of the same sentence is worse than useless.
        """
        with self.db.session() as session:
            row = session.scalar(
                select(Memory).where(
                    Memory.scope == scope.value,
                    Memory.scope_id == scope_id,
                    Memory.content == content.strip(),
                )
            )
            return self._view(row) if row else None

    def touch(self, memory_id: int, importance: int | None = None) -> MemoryView:
        """Refresh a memory that was seen again, optionally raising importance."""
        with self.db.session() as session:
            row = session.get(Memory, memory_id)
            if row is None:
                raise MemoryNotFound(str(memory_id))
            if importance is not None:
                row.importance = max(row.importance, max(0, min(100, importance)))
            session.flush()
            return self._view(row)

    def list(
        self,
        scope: MemoryScope | None = None,
        scope_id: str | None = None,
        categories: set[MemoryCategory] | None = None,
        limit: int | None = None,
    ) -> list[MemoryView]:
        """Most important first, then most recent."""
        with self.db.session() as session:
            stmt = select(Memory)
            if scope is not None:
                stmt = stmt.where(Memory.scope == scope.value)
            if scope_id is not None:
                stmt = stmt.where(Memory.scope_id == scope_id)
            if categories:
                stmt = stmt.where(
                    Memory.category.in_([c.value for c in categories])
                )
            stmt = stmt.order_by(Memory.importance.desc(), Memory.id.desc())
            if limit:
                stmt = stmt.limit(limit)
            return [self._view(row) for row in session.scalars(stmt).all()]

    def get(self, memory_id: int) -> MemoryView:
        with self.db.session() as session:
            row = session.get(Memory, memory_id)
            if row is None:
                raise MemoryNotFound(str(memory_id))
            return self._view(row)

    def delete(self, memory_id: int) -> None:
        with self.db.session() as session:
            row = session.get(Memory, memory_id)
            if row is None:
                raise MemoryNotFound(str(memory_id))
            session.delete(row)

    def count(self) -> int:
        with self.db.session() as session:
            return len(session.scalars(select(Memory.id)).all())

    # ------------------------------------------------------------------ handoffs

    @staticmethod
    def _handoff_view(row: Handoff) -> HandoffView:
        return HandoffView(
            id=row.id,
            from_agent=row.from_agent,
            to_agent=row.to_agent,
            task_key=row.task_key,
            objective_id=row.objective_id,
            summary=row.summary,
            important_files=_lines(row.important_files),
            interfaces=_lines(row.interfaces),
            decisions=_lines(row.decisions),
            warnings=_lines(row.warnings),
            consumed=row.consumed,
            created_at=row.created_at,
        )

    def add_handoff(
        self,
        from_agent: str,
        summary: str,
        to_agent: str | None = None,
        task_id: int | None = None,
        task_key: str = "",
        objective_id: int | None = None,
        important_files: list[str] | None = None,
        interfaces: list[str] | None = None,
        decisions: list[str] | None = None,
        warnings: list[str] | None = None,
    ) -> HandoffView:
        with self.db.session() as session:
            row = Handoff(
                from_agent=from_agent,
                to_agent=to_agent,
                task_id=task_id,
                task_key=task_key,
                objective_id=objective_id,
                summary=summary.strip(),
                important_files=_text(important_files),
                interfaces=_text(interfaces),
                decisions=_text(decisions),
                warnings=_text(warnings),
            )
            session.add(row)
            session.flush()
            return self._handoff_view(row)

    def handoffs_for(
        self, agent: str, unconsumed_only: bool = True, limit: int | None = None
    ) -> list[HandoffView]:
        with self.db.session() as session:
            stmt = select(Handoff).where(Handoff.to_agent == agent)
            if unconsumed_only:
                stmt = stmt.where(Handoff.consumed.is_(False))
            stmt = stmt.order_by(Handoff.id)
            if limit:
                stmt = stmt.limit(limit)
            return [self._handoff_view(row) for row in session.scalars(stmt).all()]

    def list_handoffs(self, limit: int | None = None) -> list[HandoffView]:
        with self.db.session() as session:
            stmt = select(Handoff).order_by(Handoff.id.desc())
            if limit:
                stmt = stmt.limit(limit)
            rows = list(session.scalars(stmt).all())
            rows.reverse()
            return [self._handoff_view(row) for row in rows]

    def mark_handoffs_consumed(self, handoff_ids: list[int]) -> None:
        if not handoff_ids:
            return
        with self.db.session() as session:
            for row in session.scalars(
                select(Handoff).where(Handoff.id.in_(handoff_ids))
            ).all():
                row.consumed = True

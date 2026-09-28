"""Data access for objectives."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from agentos.db.models import Objective, Task
from agentos.db.session import Database
from agentos.schemas.dto import ObjectiveView
from agentos.schemas.enums import ObjectiveStatus


class ObjectiveNotFound(LookupError):
    """No objective with that id."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ObjectiveRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def _view(self, session, row: Objective) -> ObjectiveView:
        keys = list(
            session.scalars(
                select(Task.key).where(Task.objective_id == row.id).order_by(Task.id)
            ).all()
        )
        return ObjectiveView(
            id=row.id,
            description=row.description,
            status=ObjectiveStatus(row.status),
            task_keys=keys,
            created_at=row.created_at,
            completed_at=row.completed_at,
        )

    def _require(self, session, objective_id: int) -> Objective:
        row = session.get(Objective, objective_id)
        if row is None:
            raise ObjectiveNotFound(str(objective_id))
        return row

    # -------------------------------------------------------------------- reads

    def list(self, statuses: set[ObjectiveStatus] | None = None) -> list[ObjectiveView]:
        with self.db.session() as session:
            stmt = select(Objective).order_by(Objective.id)
            if statuses:
                stmt = stmt.where(Objective.status.in_([s.value for s in statuses]))
            return [self._view(session, row) for row in session.scalars(stmt).all()]

    def get(self, objective_id: int) -> ObjectiveView:
        with self.db.session() as session:
            return self._view(session, self._require(session, objective_id))

    def latest(self) -> ObjectiveView | None:
        with self.db.session() as session:
            row = session.scalar(select(Objective).order_by(Objective.id.desc()))
            return self._view(session, row) if row else None

    def active(self) -> list[ObjectiveView]:
        return self.list(
            {ObjectiveStatus.ACTIVE, ObjectiveStatus.AWAITING_APPROVAL}
        )

    # ------------------------------------------------------------------- writes

    def create(self, description: str) -> ObjectiveView:
        with self.db.session() as session:
            row = Objective(
                description=description, status=ObjectiveStatus.PLANNING.value
            )
            session.add(row)
            session.flush()
            return self._view(session, row)

    def set_status(self, objective_id: int, status: ObjectiveStatus) -> ObjectiveView:
        with self.db.session() as session:
            row = self._require(session, objective_id)
            row.status = status.value
            if status.is_terminal:
                row.completed_at = _utcnow()
            session.flush()
            return self._view(session, row)

    def set_plan(self, objective_id: int, plan_json: str) -> None:
        with self.db.session() as session:
            self._require(session, objective_id).plan_json = plan_json

    def delete(self, objective_id: int) -> None:
        with self.db.session() as session:
            session.delete(self._require(session, objective_id))

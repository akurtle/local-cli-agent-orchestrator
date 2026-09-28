"""Database engine and session handling.

Deliberately synchronous. Our concurrency problem is subprocesses, not disk
I/O, so an async driver would add failure modes for no benefit. Async callers
wrap short DB blocks in `asyncio.to_thread`.

WAL mode is enabled so a reader (`agentctl status`) never blocks the writer
(the orchestrator).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from agentos.db.models import Base


def _configure_sqlite(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class Database:
    """Owns the engine and hands out sessions."""

    def __init__(self, db_path: Path | str, echo: bool = False) -> None:
        self.db_path = str(db_path)
        url = "sqlite://" if self.db_path == ":memory:" else f"sqlite:///{self.db_path}"
        self.engine: Engine = create_engine(url, echo=echo, future=True)
        event.listen(self.engine, "connect", _configure_sqlite)
        self._sessionmaker = sessionmaker(
            bind=self.engine, expire_on_commit=False, future=True
        )

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope: commits on success, rolls back on error."""
        session = self._sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()

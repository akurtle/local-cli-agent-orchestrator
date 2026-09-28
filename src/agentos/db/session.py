"""Database engine and session handling.

Deliberately synchronous. Our concurrency problem is subprocesses, not disk
I/O, so an async driver would add failure modes for no benefit. Async callers
wrap short DB blocks in `asyncio.to_thread`.

WAL mode is enabled so a reader (`agentctl status`) never blocks the writer
(the orchestrator).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agentos.db import migrations
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
        in_memory = self.db_path == ":memory:"
        url = "sqlite://" if in_memory else f"sqlite:///{self.db_path}"

        # Connections are handed between threads because async callers run DB
        # work via asyncio.to_thread. SQLite's default same-thread check would
        # reject that, so we disable it and rely on WAL plus busy_timeout for
        # concurrency (see _configure_sqlite).
        kwargs: dict = {
            "echo": echo,
            "future": True,
            "connect_args": {"check_same_thread": False},
        }
        if in_memory:
            # An in-memory database lives inside one connection, so every thread
            # must share that connection or it would see an empty schema.
            kwargs["poolclass"] = StaticPool

        self.engine: Engine = create_engine(url, **kwargs)

        # A shared single connection tolerates use from different threads but not
        # simultaneous use, so serialize it. File-backed databases get a real
        # pool (one connection per thread) and need no lock.
        self._lock: threading.RLock | None = threading.RLock() if in_memory else None
        event.listen(self.engine, "connect", _configure_sqlite)
        self._sessionmaker = sessionmaker(
            bind=self.engine, expire_on_commit=False, future=True
        )

    def create_all(self) -> list[str]:
        """Create missing tables, then add any columns introduced later.

        Migration runs first so an older database gains its missing columns
        before anything queries it. Returns the migration changes applied.
        """
        applied = migrations.apply(self.engine)
        Base.metadata.create_all(self.engine)
        return applied

    @property
    def schema_version(self) -> int:
        return migrations.current_version(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope: commits on success, rolls back on error."""
        if self._lock is not None:
            with self._lock:
                yield from self._session_scope()
        else:
            yield from self._session_scope()

    def _session_scope(self) -> Iterator[Session]:
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

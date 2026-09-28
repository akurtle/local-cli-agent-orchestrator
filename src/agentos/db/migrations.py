"""Minimal additive schema migration.

`create_all()` creates missing *tables* but never alters existing ones, so a
database created by an earlier phase lacks columns added later. Deleting the
database is not acceptable -- it holds session IDs and run history.

Full Alembic is more machinery than this project needs, so we handle the only
case that actually occurs in practice: adding a column. Each entry is checked
against `PRAGMA table_info` and applied only if missing, which makes this
idempotent and safe on both fresh and old databases.

Adding and dropping a column are handled. Renaming, changing a type and adding
a constraint are NOT. If those become necessary, add Alembic rather than growing
this file into a half-migration-tool.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, text

SCHEMA_VERSION = 7


@dataclass(frozen=True)
class AddColumn:
    table: str
    column: str
    ddl: str
    """SQL type plus default, e.g. "VARCHAR(32) DEFAULT 'claude'"."""

    @property
    def statement(self) -> str:
        # Table and column names here are developer-authored constants, never
        # user or model input, so there is nothing to inject.
        return f"ALTER TABLE {self.table} ADD COLUMN {self.column} {self.ddl}"


@dataclass(frozen=True)
class DropColumn:
    """Remove a column that is no longer part of the model.

    Requires SQLite 3.35+. Only used to retire a column whose meaning has been
    replaced, so no data worth keeping is lost.
    """

    table: str
    column: str

    @property
    def statement(self) -> str:
        return f"ALTER TABLE {self.table} DROP COLUMN {self.column}"


# Schema changes after the initial version, oldest first.
ADDITIVE_COLUMNS: tuple[AddColumn, ...] = (
    # Phase 2: per-agent runtime, so different agents could use different CLIs.
    AddColumn("agents", "runtime", "VARCHAR(32) DEFAULT 'claude'"),
    # Phase 4: a three-state delivery lifecycle replaces the read boolean, so a
    # crash between injecting a message and finishing the run cannot lose it.
    AddColumn("messages", "status", "VARCHAR(16) DEFAULT 'pending'"),
    AddColumn("messages", "read_at", "DATETIME"),
    # Phase 5: tasks belong to an objective.
    AddColumn("tasks", "objective_id", "INTEGER REFERENCES objectives(id)"),
    # Phase 7: a task blocked by an agent must not be un-blocked by readiness.
    AddColumn("tasks", "needs_intervention", "BOOLEAN DEFAULT 0"),
    # Phase 13: session rotation needs to know how much a session has done.
    AddColumn("agents", "session_task_count", "INTEGER DEFAULT 0"),
    AddColumn("agents", "session_objective_id", "INTEGER"),
)

DROPPED_COLUMNS: tuple[DropColumn, ...] = (
    # Phase 4: superseded by messages.status.
    DropColumn("messages", "read"),
)


def existing_columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


def indexes_on_column(engine: Engine, table: str, column: str) -> list[str]:
    """Index names on `table` that reference `column`.

    SQLite refuses to drop a column an index depends on, so these must go first.
    """
    found: list[str] = []
    with engine.connect() as conn:
        names = conn.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name=:t AND name NOT LIKE 'sqlite_%'"
            ),
            {"t": table},
        ).fetchall()
        for (name,) in names:
            info = conn.execute(text(f"PRAGMA index_info({name})")).fetchall()
            if any(row[2] == column for row in info):
                found.append(name)
    return found


def table_exists(engine: Engine, table: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:t"),
            {"t": table},
        ).fetchone()
    return row is not None


def apply(engine: Engine) -> list[str]:
    """Bring an existing database up to date. Returns the changes applied."""
    applied: list[str] = []
    for change in ADDITIVE_COLUMNS:
        if not table_exists(engine, change.table):
            continue  # create_all will build it with the column already present
        if change.column in existing_columns(engine, change.table):
            continue
        with engine.begin() as conn:
            conn.execute(text(change.statement))
        applied.append(f"+{change.table}.{change.column}")

    for removal in DROPPED_COLUMNS:
        if not table_exists(engine, removal.table):
            continue
        if removal.column not in existing_columns(engine, removal.table):
            continue
        # Drop dependent indexes first, or SQLite rejects the column drop.
        for index_name in indexes_on_column(engine, removal.table, removal.column):
            with engine.begin() as conn:
                conn.execute(text(f"DROP INDEX IF EXISTS {index_name}"))
            applied.append(f"-index {index_name}")
        with engine.begin() as conn:
            conn.execute(text(removal.statement))
        applied.append(f"-{removal.table}.{removal.column}")

    with engine.begin() as conn:
        conn.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))
    return applied


def current_version(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("PRAGMA user_version")).scalar() or 0)

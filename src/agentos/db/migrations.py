"""Minimal additive schema migration.

`create_all()` creates missing *tables* but never alters existing ones, so a
database created by an earlier phase lacks columns added later. Deleting the
database is not acceptable -- it holds session IDs and run history.

Full Alembic is more machinery than this project needs, so we handle the only
case that actually occurs in practice: adding a column. Each entry is checked
against `PRAGMA table_info` and applied only if missing, which makes this
idempotent and safe on both fresh and old databases.

Anything beyond adding a column (dropping, renaming, changing a type, adding a
constraint) is NOT handled here. If that becomes necessary, add Alembic rather
than growing this file into a half-migration-tool.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine, text

SCHEMA_VERSION = 2


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


# Columns added after the initial schema, oldest first.
ADDITIVE_COLUMNS: tuple[AddColumn, ...] = (
    # Phase 2: per-agent runtime, so different agents could use different CLIs.
    AddColumn("agents", "runtime", "VARCHAR(32) DEFAULT 'claude'"),
)


def existing_columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {row[1] for row in rows}


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
        applied.append(f"{change.table}.{change.column}")

    with engine.begin() as conn:
        conn.execute(text(f"PRAGMA user_version = {SCHEMA_VERSION}"))
    return applied


def current_version(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("PRAGMA user_version")).scalar() or 0)

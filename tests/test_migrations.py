"""Schema evolution must not destroy a database that holds session IDs."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text

from agentos.db import migrations
from agentos.db.session import Database


def test_fresh_database_is_stamped(tmp_path: Path) -> None:
    db = Database(tmp_path / "fresh.db")
    db.create_all()
    assert db.schema_version == migrations.SCHEMA_VERSION
    db.dispose()


def test_create_all_is_idempotent(tmp_path: Path) -> None:
    db = Database(tmp_path / "x.db")
    assert db.create_all() == []  # nothing to migrate on a brand new file
    assert db.create_all() == []
    db.dispose()


def test_missing_column_is_added_to_an_existing_database(tmp_path: Path) -> None:
    """Simulates a Phase 1 database meeting the Phase 2 schema.

    Regression: create_all() creates missing tables but never alters existing
    ones, so `agents.runtime` was absent and every query failed.
    """
    path = tmp_path / "old.db"
    db = Database(path)
    db.create_all()

    # Roll the schema back to before the column existed.
    with db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE agents DROP COLUMN runtime"))
        conn.execute(text("PRAGMA user_version = 0"))
    assert "runtime" not in migrations.existing_columns(db.engine, "agents")
    db.dispose()

    reopened = Database(path)
    applied = reopened.create_all()
    assert "+agents.runtime" in applied
    assert "runtime" in migrations.existing_columns(reopened.engine, "agents")
    assert reopened.schema_version == migrations.SCHEMA_VERSION
    reopened.dispose()


def test_existing_rows_survive_migration(tmp_path: Path) -> None:
    from agentos.db.models import Agent

    path = tmp_path / "keep.db"
    db = Database(path)
    db.create_all()
    with db.session() as session:
        session.add(Agent(name="backend", role="backend", session_id="precious"))
    with db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE agents DROP COLUMN runtime"))
        conn.execute(text("PRAGMA user_version = 0"))
    db.dispose()

    reopened = Database(path)
    reopened.create_all()
    with reopened.session() as session:
        row = session.query(Agent).one()
        assert row.session_id == "precious"
        # The added column takes its declared default.
        assert row.runtime == "claude"
    reopened.dispose()


def test_migration_skips_tables_that_do_not_exist_yet(tmp_path: Path) -> None:
    """An empty file must not error; create_all builds the tables complete."""
    db = Database(tmp_path / "empty.db")
    assert migrations.apply(db.engine) == []
    db.dispose()


def test_add_column_statement_shape() -> None:
    change = migrations.AddColumn("agents", "runtime", "VARCHAR(32) DEFAULT 'claude'")
    assert (
        change.statement
        == "ALTER TABLE agents ADD COLUMN runtime VARCHAR(32) DEFAULT 'claude'"
    )


def test_indexed_column_can_be_dropped(tmp_path: Path) -> None:
    """Regression: SQLite refuses to drop a column an index depends on.

    The Phase 1 `messages.read` column was indexed, so the Phase 4 drop failed on
    real databases with `error in index ix_messages_read after drop column`.
    Test databases are built fresh from the current model and never had it.
    """
    path = tmp_path / "indexed.db"
    db = Database(path)
    db.create_all()

    # Recreate the old shape: the dropped column, plus its index.
    with db.engine.begin() as conn:
        conn.execute(text("ALTER TABLE messages ADD COLUMN read BOOLEAN DEFAULT 0"))
        conn.execute(text("CREATE INDEX ix_messages_read ON messages (read)"))
        conn.execute(text("PRAGMA user_version = 0"))
    assert "read" in migrations.existing_columns(db.engine, "messages")
    assert migrations.indexes_on_column(db.engine, "messages", "read") == [
        "ix_messages_read"
    ]
    db.dispose()

    reopened = Database(path)
    applied = reopened.create_all()
    assert "-index ix_messages_read" in applied
    assert "-messages.read" in applied
    assert "read" not in migrations.existing_columns(reopened.engine, "messages")
    reopened.dispose()


def test_indexes_on_column_ignores_unrelated_indexes(tmp_path: Path) -> None:
    db = Database(tmp_path / "idx.db")
    db.create_all()
    # messages.status is indexed by the model; the recipient index is separate.
    assert "ix_messages_status" in migrations.indexes_on_column(
        db.engine, "messages", "status"
    )
    assert migrations.indexes_on_column(db.engine, "messages", "body") == []
    db.dispose()

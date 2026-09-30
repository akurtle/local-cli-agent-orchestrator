from __future__ import annotations

import sqlite3
from pathlib import Path


class Database:
    def __init__(self, path: str | Path, migrations: str | Path) -> None:
        self.path = Path(path)
        self.migrations = Path(migrations)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            applied = {
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for migration in sorted(self.migrations.glob("*.sql")):
                if migration.name in applied:
                    continue
                connection.executescript(migration.read_text(encoding="utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)",
                    (migration.name,),
                )

    def list_projects(self) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, description, created_at FROM projects ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_project(self, name: str, description: str) -> dict:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO projects(name, description) VALUES (?, ?)",
                (name, description),
            )
            row = connection.execute(
                "SELECT id, name, description, created_at FROM projects WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        assert row is not None
        return dict(row)

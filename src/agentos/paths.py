"""Filesystem layout for a project workspace.

A "project" is just a directory containing a config file. All mutable state
lives in a single state directory beneath it so it is trivial to inspect or
delete.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentos.branding import CONFIG_FILENAME, DB_FILENAME, STATE_DIRNAME


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @property
    def config_file(self) -> Path:
        return self.root / CONFIG_FILENAME

    @property
    def state_dir(self) -> Path:
        return self.root / STATE_DIRNAME

    @property
    def db_file(self) -> Path:
        return self.state_dir / DB_FILENAME

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def worktrees_dir(self) -> Path:
        return self.root / "worktrees"

    def ensure(self) -> None:
        """Create the state directories. Safe to call repeatedly."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)


def find_project_root(start: Path | None = None) -> Path | None:
    """Walk upward looking for a config file, like git does for .git."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / CONFIG_FILENAME).is_file():
            return candidate
    return None

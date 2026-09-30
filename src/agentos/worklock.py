"""One scheduler per project, and a way to ask it to stop from outside.

Two schedulers on one database would both see a task as READY and dispatch it
twice. `agentctl work` therefore holds an OS-level exclusive lock on
`.agentos/work.lock` for as long as it runs. The OS drops the lock when the
process exits, however it exits, so a crash never leaves a stale lock behind.

The stop request is a file rather than a signal because signals do not cross
processes portably on Windows. The scheduler checks for it before each pass
and, if present, stops launching new work while running agents finish -- the
same graceful stop as a first Ctrl+C.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from agentos.paths import ProjectPaths

LOCK_FILENAME = "work.lock"
STOP_FILENAME = "work.stop"


def lock_path(paths: ProjectPaths) -> Path:
    return paths.state_dir / LOCK_FILENAME


def stop_path(paths: ProjectPaths) -> Path:
    return paths.state_dir / STOP_FILENAME


class WorkLock:
    """An exclusive, non-blocking lock held for the life of a scheduler."""

    def __init__(self, paths: ProjectPaths) -> None:
        self.path = lock_path(paths)
        self._handle = None

    def acquire(self) -> bool:
        """True if this process now holds the lock; False if another does."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            _lock(handle)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            _unlock(self._handle)
        except OSError:
            pass
        self._handle.close()
        self._handle = None

    def __enter__(self) -> "WorkLock":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def is_running(paths: ProjectPaths) -> bool:
    """Whether some process holds the work lock right now."""
    if not lock_path(paths).exists():
        return False
    probe = WorkLock(paths)
    if probe.acquire():
        probe.release()
        return False
    return True


def request_stop(paths: ProjectPaths) -> None:
    stop_path(paths).parent.mkdir(parents=True, exist_ok=True)
    stop_path(paths).write_text(str(os.getpid()), encoding="utf-8")


def stop_requested(paths: ProjectPaths) -> bool:
    return stop_path(paths).exists()


def clear_stop(paths: ProjectPaths) -> None:
    stop_path(paths).unlink(missing_ok=True)


if sys.platform == "win32":
    import msvcrt

    def _lock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

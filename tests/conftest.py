from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agentos.db.session import Database

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db() -> Database:
    database = Database(":memory:")
    database.create_all()
    yield database
    database.dispose()


@pytest.fixture
def fake_claude_argv() -> list[str]:
    """argv prefix that runs our stub CLI instead of the real claude.

    Lets us test spawning, streaming, timeouts and failure handling
    deterministically and for free.
    """
    return [sys.executable, str(FIXTURES / "fake_claude.py")]

"""The code view's data: git's unified diff parsed into files, hunks and lines.

The parser is tested on literal diff text for every shape git produces, then
the endpoint is run against real repositories, because what counts as "the
change" (merge base, untracked files, the orchestrator's own files) is git's
behaviour, not ours.
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from agentos.gui.review import (
    MAX_FILE_LINES,
    new_file_diff,
    parse_commits,
    parse_unified_diff,
)

MODIFIED = """\
diff --git a/src/app.py b/src/app.py
index 1111111..2222222 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,4 +1,5 @@ def main():
 import os
-print("old")
+print("new")
+print("extra")
 x = 1
 y = 2
@@ -20,2 +21,2 @@ class Thing:
-    a = 1
+    a = 2
     b = 3
\\ No newline at end of file
"""


def test_modified_file_lines_are_numbered_on_both_sides() -> None:
    [diff] = parse_unified_diff(MODIFIED)
    assert diff.path == "src/app.py" and diff.status == "modified"
    assert (diff.added, diff.removed) == (3, 2)
    first, second = diff.hunks
    assert (first.old_start, first.new_start, first.header) == (1, 1, "def main():")
    kinds = [(l.kind, l.old, l.new) for l in first.lines]
    assert kinds == [
        ("context", 1, 1),
        ("del", 2, None),
        ("add", None, 2),
        ("add", None, 3),
        ("context", 3, 4),
        ("context", 4, 5),
    ]
    # The "no newline" marker is not a line of code.
    assert [l.text for l in second.lines] == ["    a = 1", "    a = 2", "    b = 3"]
    assert second.header == "class Thing:"


def test_added_deleted_and_binary_files() -> None:
    text = """\
diff --git a/new.txt b/new.txt
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/new.txt
@@ -0,0 +1,2 @@
+one
+two
diff --git a/old.txt b/old.txt
deleted file mode 100644
index 4444444..0000000
--- a/old.txt
+++ /dev/null
@@ -1 +0,0 @@
-gone
diff --git a/logo.png b/logo.png
index 5555555..6666666 100644
Binary files a/logo.png and b/logo.png differ
"""
    new, old, logo = parse_unified_diff(text)
    assert (new.path, new.status, new.added) == ("new.txt", "added", 2)
    assert (old.path, old.status, old.removed) == ("old.txt", "deleted", 1)
    assert logo.binary and logo.path == "logo.png" and not logo.hunks


def test_renames_keep_both_names() -> None:
    text = """\
diff --git a/src/old_name.py b/src/new_name.py
similarity index 90%
rename from src/old_name.py
rename to src/new_name.py
index 7777777..8888888 100644
--- a/src/old_name.py
+++ b/src/new_name.py
@@ -1 +1 @@
-x = 1
+x = 2
"""
    [diff] = parse_unified_diff(text)
    assert diff.status == "renamed"
    assert (diff.old_path, diff.path) == ("src/old_name.py", "src/new_name.py")
    assert (diff.added, diff.removed) == (1, 1)


def test_pure_rename_has_no_hunks() -> None:
    text = """\
diff --git a/a.txt b/b.txt
similarity index 100%
rename from a.txt
rename to b.txt
"""
    [diff] = parse_unified_diff(text)
    assert diff.status == "renamed" and diff.hunks == []


def test_lines_that_look_like_headers_stay_code() -> None:
    """A removed line starting '--' or an added one starting '++' is content."""
    text = """\
diff --git a/notes.md b/notes.md
--- a/notes.md
+++ b/notes.md
@@ -1,2 +1,2 @@
--- a divider
+++ a plus row
 keep
"""
    [diff] = parse_unified_diff(text)
    assert [(l.kind, l.text) for l in diff.hunks[0].lines] == [
        ("del", "-- a divider"),
        ("add", "++ a plus row"),
        ("context", "keep"),
    ]


def test_huge_files_are_summarised_not_drawn() -> None:
    body = "".join(f"+line {i}\n" for i in range(MAX_FILE_LINES + 1))
    text = f"diff --git a/big.txt b/big.txt\n--- a/big.txt\n+++ b/big.txt\n@@ -0,0 +1,{MAX_FILE_LINES + 1} @@\n{body}"
    [diff] = parse_unified_diff(text)
    assert diff.too_large and diff.hunks == []
    assert diff.added == MAX_FILE_LINES + 1  # the count still tells the story


def test_empty_diff() -> None:
    assert parse_unified_diff("") == []


def test_new_file_is_all_additions(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    diff = new_file_diff(tmp_path, "a.py")
    assert diff.status == "added" and diff.added == 2
    assert [(l.kind, l.new, l.text) for l in diff.hunks[0].lines] == [
        ("add", 1, "x = 1"),
        ("add", 2, "y = 2"),
    ]
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02")
    assert new_file_diff(tmp_path, "b.bin").binary


def test_parse_commits() -> None:
    assert parse_commits("abc1234\tAdd login\nfff0000\tFix: tabs\tinside\n\n") == [
        {"sha": "abc1234", "subject": "Add login"},
        {"sha": "fff0000", "subject": "Fix: tabs\tinside"},
    ]


# ----------------------------------------------------------- real repositories


pytestmark_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytestmark_git
def test_review_of_a_worktree_and_the_shared_folder(project, tmp_path) -> None:
    from agentos.gui.server import GuiState
    from agentos.paths import ProjectPaths
    from agentos.tui.changes import SHARED, ChangesReader
    from agentos.tui.snapshot import SnapshotReader
    from tests.test_vcs import init_repo

    reader, _tasks, db = project
    root = tmp_path / "repo"
    git = asyncio.run(init_repo(root))
    (root / "app.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
    asyncio.run(git._run("add", "app.py"))
    asyncio.run(git._run("commit", "-m", "app"))

    tree = asyncio.run(git.create_worktree("backend"))
    (tree.path / "app.py").write_text("a = 1\nb = 3\n", encoding="utf-8")
    asyncio.run(git._run("commit", "-am", "change b", cwd=tree.path))
    (tree.path / "notes.txt").write_text("new\n", encoding="utf-8")

    # The shared folder: an agent's edit, plus the orchestrator's own files.
    (root / "README.md").write_text("hello\nmore\n", encoding="utf-8")
    (root / "agentos.yaml").write_text("project: {}\n", encoding="utf-8")
    (root / ".agentos").mkdir(exist_ok=True)
    (root / ".agentos" / "state.db").write_text("x", encoding="utf-8")

    paths = ProjectPaths(root=root)
    reader.paths = paths
    state = GuiState(reader, ChangesReader(paths, reader.config))
    state.scan_once()

    backend = state.review("backend")
    assert backend["base"] == "main"
    assert [c["subject"] for c in backend["commits"]] == ["change b"]
    files = {f["path"]: f for f in backend["files"]}
    assert set(files) == {"app.py", "notes.txt"}
    assert files["app.py"]["status"] == "modified"
    assert files["notes.txt"]["status"] == "added"

    shared = state.review(SHARED)
    paths_seen = {f["path"] for f in shared["files"]}
    assert "README.md" in paths_seen
    # Our config, state and the agents' worktrees are never "the change".
    assert not any(
        p == "agentos.yaml" or p.startswith((".agentos/", "worktrees/")) for p in paths_seen
    )
    assert shared["commits"] == []

    with pytest.raises(LookupError):
        state.review("..")
    with pytest.raises(LookupError):
        state.review("nobody")


# The TUI fixture, for a reader with agents and tasks.
from tests.test_tui import project  # noqa: E402,F401

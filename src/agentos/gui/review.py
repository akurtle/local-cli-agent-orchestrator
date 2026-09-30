"""Structured diffs for the web dashboard's code review view.

`git diff` output is parsed here into files, hunks and numbered lines, so the
browser only has to draw them. Pure functions over text, so every quirk of the
format -- renames, new and deleted files, binaries, "no newline at end of file"
-- is testable without a repository.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")

# Past these, a file is summarised rather than drawn: a generated lockfile or
# bundle would freeze the page and nobody reviews it line by line anyway.
MAX_FILE_LINES = 3000
MAX_NEW_FILE_BYTES = 400_000


@dataclass
class Line:
    kind: str
    """"context", "add" or "del"."""
    text: str
    old: int | None = None
    new: int | None = None


@dataclass
class Hunk:
    old_start: int
    new_start: int
    header: str
    """The function or section git names after the @@ line, if any."""
    lines: list[Line] = field(default_factory=list)


@dataclass
class FileDiff:
    path: str
    status: str = "modified"
    """"added", "deleted", "modified" or "renamed"."""
    old_path: str | None = None
    binary: bool = False
    too_large: bool = False
    added: int = 0
    removed: int = 0
    hunks: list[Hunk] = field(default_factory=list)


def _unquote(path: str) -> str:
    """git quotes paths with unusual characters; strip the a/ b/ prefixes."""
    path = path.strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path


def parse_unified_diff(text: str) -> list[FileDiff]:
    """Parse `git diff` (unified, no colour) into files."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0

    for raw in (text or "").splitlines():
        if raw.startswith("diff --git "):
            # "diff --git a/x b/y": the b/ side is the path after the change.
            parts = raw[len("diff --git "):].split(" b/", 1)
            path = _unquote("b/" + parts[1]) if len(parts) == 2 else _unquote(parts[0])
            current = FileDiff(path=path)
            files.append(current)
            hunk = None
            continue
        if current is None:
            continue

        if hunk is None or not raw or raw[0] not in " +-\\":
            if raw.startswith("new file mode"):
                current.status = "added"
            elif raw.startswith("deleted file mode"):
                current.status = "deleted"
            elif raw.startswith("rename from "):
                current.status = "renamed"
                current.old_path = raw[len("rename from "):].strip()
            elif raw.startswith("rename to "):
                current.path = raw[len("rename to "):].strip()
            elif raw.startswith("Binary files ") or raw.startswith("GIT binary patch"):
                current.binary = True
            elif raw.startswith("+++ ") and current.status != "deleted":
                target = raw[4:].strip()
                if target != "/dev/null":
                    current.path = _unquote(target)
            elif match := HUNK.match(raw):
                old_no, new_no = int(match.group(1)), int(match.group(3))
                hunk = Hunk(old_start=old_no, new_start=new_no, header=match.group(5).strip())
                current.hunks.append(hunk)
            continue

        if raw.startswith("\\"):
            continue  # "\ No newline at end of file"
        if match := HUNK.match(raw):
            old_no, new_no = int(match.group(1)), int(match.group(3))
            hunk = Hunk(old_start=old_no, new_start=new_no, header=match.group(5).strip())
            current.hunks.append(hunk)
            continue

        kind, body = raw[0], raw[1:]
        if kind == "+":
            hunk.lines.append(Line("add", body, None, new_no))
            new_no += 1
            current.added += 1
        elif kind == "-":
            hunk.lines.append(Line("del", body, old_no, None))
            old_no += 1
            current.removed += 1
        else:
            hunk.lines.append(Line("context", body, old_no, new_no))
            old_no += 1
            new_no += 1

    for diff in files:
        if sum(len(h.lines) for h in diff.hunks) > MAX_FILE_LINES:
            diff.too_large = True
            diff.hunks = []
    return files


def new_file_diff(root: Path, relative: str) -> FileDiff:
    """An untracked file, shown as entirely added; `git diff` can't see it."""
    path = root / relative
    diff = FileDiff(path=relative.replace("\\", "/"), status="added")
    try:
        size = path.stat().st_size
        if size > MAX_NEW_FILE_BYTES:
            diff.too_large = True
            return diff
        data = path.read_bytes()
    except OSError:
        return diff
    if b"\0" in data[:8000]:
        diff.binary = True
        return diff
    lines = data.decode("utf-8", errors="replace").splitlines()
    diff.added = len(lines)
    if len(lines) > MAX_FILE_LINES:
        diff.too_large = True
        return diff
    diff.hunks = [
        Hunk(
            old_start=0,
            new_start=1,
            header="",
            lines=[Line("add", text, None, i + 1) for i, text in enumerate(lines)],
        )
    ]
    return diff


def parse_commits(log: str) -> list[dict]:
    """`git log --format=%h%x09%s` into [{sha, subject}]."""
    commits = []
    for line in (log or "").splitlines():
        sha, _, subject = line.partition("\t")
        if sha.strip():
            commits.append({"sha": sha.strip(), "subject": subject.strip()})
    return commits

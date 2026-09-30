#!/usr/bin/env python3
"""Create the local Git repository used by the example benchmark."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TEMPLATE = ROOT / "fixtures" / "project-labels-template"
DEFAULT_DESTINATION = ROOT / "work" / "project-labels"


def run(*args: str, cwd: Path) -> None:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--destination",
        type=Path,
        default=DEFAULT_DESTINATION,
        help=f"Where to create the fixture (default: {DEFAULT_DESTINATION})",
    )
    args = parser.parse_args()
    destination = args.destination.expanduser().resolve()
    if destination.exists():
        raise SystemExit(
            f"Destination already exists: {destination}\n"
            "Choose another --destination or remove the existing fixture yourself."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE, destination)
    run("git", "init", "--initial-branch=main", cwd=destination)
    run("git", "config", "user.name", "agentos benchmark", cwd=destination)
    run("git", "config", "user.email", "benchmark@localhost", cwd=destination)
    run("git", "add", ".", cwd=destination)
    run("git", "commit", "-m", "Create benchmark starting state", cwd=destination)
    run("git", "tag", "benchmark-start", cwd=destination)

    print(f"Created benchmark fixture: {destination}")
    print("Starting ref: benchmark-start")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

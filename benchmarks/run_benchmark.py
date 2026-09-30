#!/usr/bin/env python3
"""Repeatable benchmark runner for agentos and direct coding-agent CLIs.

The runner creates a clean clone for every trial, randomizes workflow order,
runs the configured implementation and evaluation steps, and writes one JSON
record per trial plus a flat CSV and Markdown summary.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TOKEN_KEYS = {
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "cache_read_input_tokens": "cache_read_tokens",
    "cached_input_tokens": "cache_read_tokens",
    "cache_creation_input_tokens": "cache_creation_tokens",
}
PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass
class StepResult:
    name: str
    command: list[str]
    exit_code: int | None
    duration_seconds: float
    timed_out: bool = False
    log_file: str = ""


@dataclass
class TrialResult:
    run_id: str
    task: str
    workflow: str
    attempt: int
    base_ref: str
    base_commit: str
    started_at: str
    duration_seconds: float
    workflow_succeeded: bool
    evaluation_succeeded: bool
    timed_out: bool
    workflow_steps: list[dict[str, Any]] = field(default_factory=list)
    evaluation_steps: list[dict[str, Any]] = field(default_factory=list)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    model_invocations: int | None = None
    retries: int | None = None
    agentos_tasks: dict[str, int] = field(default_factory=dict)
    cost_usd: float | None = None
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    changed_paths: list[str] = field(default_factory=list)
    workspace: str = ""
    interventions: int | None = None
    review_score: float | None = None
    notes: str = ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    for key in ("tasks", "workflows"):
        if not isinstance(config.get(key), list) or not config[key]:
            raise ValueError(f"config requires a non-empty {key!r} list")
    task_ids = [str(task.get("id", "")) for task in config["tasks"]]
    workflow_names = [str(item.get("name", "")) for item in config["workflows"]]
    if any(not value for value in task_ids + workflow_names):
        raise ValueError("every task needs an id and every workflow needs a name")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("task ids must be unique")
    if len(workflow_names) != len(set(workflow_names)):
        raise ValueError("workflow names must be unique")
    return config


def expand(value: str, variables: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise ValueError(f"unknown placeholder {key!r} in {value!r}")
        return variables[key]

    # Replace named harness placeholders while leaving JSON, Python dicts, and
    # other literal braces untouched.
    return PLACEHOLDER.sub(replace, value)


def expand_command(command: list[Any], variables: dict[str, str]) -> list[str]:
    if not isinstance(command, list) or not command:
        raise ValueError("every command must be a non-empty JSON array")
    return [expand(str(part), variables) for part in command]


def stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def run_step(
    step: dict[str, Any],
    variables: dict[str, str],
    cwd: Path,
    log_dir: Path,
    default_timeout: float,
    index: int,
    phase: str,
) -> StepResult:
    command = expand_command(step["command"], variables)
    name = str(step.get("name") or Path(command[0]).name)
    timeout = float(step.get("timeout_seconds", default_timeout))
    step_cwd = Path(expand(str(step.get("cwd", "{workspace}")), variables))
    if not step_cwd.is_absolute():
        step_cwd = cwd / step_cwd
    log_path = log_dir / f"{phase}-{index:02d}-{safe_name(name)}.log"
    environment = os.environ.copy()
    environment.update(
        {str(key): expand(str(value), variables) for key, value in step.get("env", {}).items()}
    )
    stdin_text = step.get("stdin")
    if stdin_text is not None:
        stdin_text = expand(str(stdin_text), variables)

    print(f"    {phase}: {name}", flush=True)
    started = time.monotonic()
    flags: dict[str, Any] = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        flags["start_new_session"] = True

    with log_path.open("w", encoding="utf-8", errors="replace") as log:
        log.write("COMMAND: " + json.dumps(command) + "\n\n")
        log.flush()
        process = subprocess.Popen(
            command,
            cwd=step_cwd,
            env=environment,
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            **flags,
        )
        try:
            process.communicate(input=stdin_text, timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_process(process)
            process.wait()

    return StepResult(
        name=name,
        command=command,
        exit_code=process.returncode,
        duration_seconds=round(time.monotonic() - started, 3),
        timed_out=timed_out,
        log_file=str(log_path),
    )


def safe_name(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "-" for char in value)


def git(workspace: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=workspace, capture_output=True, text=True, check=False
    )
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def prepare_workspace(source: str, base_ref: str, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    clone = subprocess.run(
        ["git", "clone", "--quiet", "--no-hardlinks", source, str(destination)],
        capture_output=True,
        text=True,
        check=False,
    )
    if clone.returncode:
        raise RuntimeError(clone.stderr.strip() or "git clone failed")
    git(destination, "checkout", "--quiet", base_ref)
    git(destination, "reset", "--hard", base_ref)
    git(destination, "clean", "-fdx")
    return git(destination, "rev-parse", "HEAD")


def git_change_metrics(workspace: Path, base_commit: str) -> dict[str, Any]:
    paths = set(
        line for line in git(workspace, "diff", "--name-only", base_commit, check=False).splitlines()
        if line
    )
    paths.update(
        line for line in git(workspace, "ls-files", "--others", "--exclude-standard", check=False).splitlines()
        if line
    )
    insertions = deletions = 0
    for line in git(workspace, "diff", "--numstat", base_commit, check=False).splitlines():
        columns = line.split("\t")
        if len(columns) >= 2:
            insertions += int(columns[0]) if columns[0].isdigit() else 0
            deletions += int(columns[1]) if columns[1].isdigit() else 0
    return {
        "files_changed": len(paths),
        "insertions": insertions,
        "deletions": deletions,
        "changed_paths": sorted(paths),
    }


def json_objects(text: str) -> list[Any]:
    objects: list[Any] = []
    try:
        objects.append(json.loads(text))
    except (json.JSONDecodeError, ValueError):
        pass
    for line in text.splitlines():
        try:
            objects.append(json.loads(line))
        except (json.JSONDecodeError, ValueError):
            continue
    return objects


def usage_candidates(value: Any) -> list[dict[str, int]]:
    found: list[dict[str, int]] = []
    if isinstance(value, dict):
        normalized: dict[str, int] = {}
        for key, target in TOKEN_KEYS.items():
            raw = value.get(key)
            if isinstance(raw, (int, float)):
                normalized[target] = normalized.get(target, 0) + int(raw)
        if normalized:
            found.append(normalized)
        for nested in value.values():
            found.extend(usage_candidates(nested))
    elif isinstance(value, list):
        for nested in value:
            found.extend(usage_candidates(nested))
    return found


def usage_from_text(text: str) -> dict[str, int]:
    candidates: list[dict[str, int]] = []
    for value in json_objects(text):
        candidates.extend(usage_candidates(value))
    if not candidates:
        return {}
    # Provider streams often repeat cumulative totals. Keep the largest total
    # within one process instead of summing the same tokens repeatedly.
    return max(candidates, key=lambda item: sum(item.values()))


def agentos_metrics(workspace: Path) -> tuple[dict[str, Any], list[str]]:
    db_path = workspace / ".agentos" / "agentos.db"
    if not db_path.exists():
        return {}, []
    metrics: dict[str, Any] = {}
    outputs: list[str] = []
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "runs" in tables:
            rows = list(
                connection.execute(
                    "SELECT status, stdout, cost_usd, num_turns FROM runs"
                )
            )
            metrics["model_invocations"] = len(rows)
            costs = [float(row["cost_usd"]) for row in rows if row["cost_usd"] is not None]
            metrics["cost_usd"] = round(sum(costs), 6) if costs else None
            outputs.extend(str(row["stdout"] or "") for row in rows)
        if "tasks" in tables:
            metrics["agentos_tasks"] = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT status, COUNT(*) FROM tasks GROUP BY status"
                )
            }
            attempts = connection.execute(
                "SELECT COALESCE(SUM(attempts), 0) FROM tasks"
            ).fetchone()[0]
            metrics["retries"] = int(attempts or 0)
    return metrics, outputs


def collect_usage(log_paths: list[Path], extra_outputs: list[str]) -> dict[str, int | None]:
    totals: dict[str, int] = defaultdict(int)
    sources = [path.read_text(encoding="utf-8", errors="replace") for path in log_paths]
    sources.extend(extra_outputs)
    any_usage = False
    for text in sources:
        usage = usage_from_text(text)
        if usage:
            any_usage = True
        for key, value in usage.items():
            totals[key] += value
    return {
        key: totals.get(key) if any_usage else None
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
        )
    }


def execute_steps(
    steps: list[dict[str, Any]],
    variables: dict[str, str],
    workspace: Path,
    log_dir: Path,
    timeout: float,
    phase: str,
) -> list[StepResult]:
    results: list[StepResult] = []
    for index, step in enumerate(steps, start=1):
        result = run_step(step, variables, workspace, log_dir, timeout, index, phase)
        results.append(result)
        if result.exit_code != 0 and not step.get("continue_on_error", False):
            break
    return results


def run_trial(
    task: dict[str, Any],
    workflow: dict[str, Any],
    attempt: int,
    config_path: Path,
    output_root: Path,
    default_timeout: float,
    keep_workspaces: bool,
) -> TrialResult:
    task_id = safe_name(str(task["id"]))
    workflow_name = safe_name(str(workflow["name"]))
    run_id = f"{task_id}__{workflow_name}__{attempt:02d}"
    run_dir = output_root / "runs" / run_id
    workspace = run_dir / "workspace"
    logs = run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)

    prompt_path = (config_path.parent / str(task["prompt_file"])).resolve()
    prompt = prompt_path.read_text(encoding="utf-8")
    source = expand(
        str(task["repository"]),
        {"config_dir": str(config_path.parent), "benchmark_root": str(config_path.parent)},
    )
    if "://" not in source and not source.startswith("git@"):
        source_path = Path(source).expanduser()
        if not source_path.is_absolute():
            source_path = (config_path.parent / source_path).resolve()
        source = str(source_path)
    base_ref = str(task.get("base_ref", "HEAD"))

    print(f"\n[{run_id}] preparing clean clone", flush=True)
    base_commit = prepare_workspace(source, base_ref, workspace)
    variables = {
        "attempt": str(attempt),
        "base_commit": base_commit,
        "base_ref": base_ref,
        "benchmark_root": str(config_path.parent),
        "config_dir": str(config_path.parent),
        "prompt": prompt,
        "prompt_file": str(prompt_path),
        "run_dir": str(run_dir),
        "task_id": task_id,
        "workspace": str(workspace),
        "workflow": workflow_name,
    }

    started_at = utc_now()
    started = time.monotonic()
    setup_steps = list(task.get("setup_steps", []))
    implementation_steps = setup_steps + list(workflow.get("steps", []))
    workflow_results = execute_steps(
        implementation_steps, variables, workspace, logs, default_timeout, "workflow"
    )
    workflow_ok = bool(workflow_results) and all(
        result.exit_code == 0 for result in workflow_results
    )
    # Capture the implementation diff before held-out tests or coverage tools
    # add evaluator-owned files to the checkout.
    change_metrics = git_change_metrics(workspace, base_commit)

    evaluation_results: list[StepResult] = []
    if workflow_ok or bool(task.get("evaluate_after_failure", True)):
        evaluation_steps = list(task.get("evaluation_setup_steps", [])) + list(
            task.get("evaluation_steps", [])
        )
        evaluation_results = execute_steps(
            evaluation_steps,
            variables,
            workspace,
            logs,
            float(task.get("evaluation_timeout_seconds", default_timeout)),
            "evaluation",
        )
    evaluation_ok = bool(evaluation_results) and all(
        result.exit_code == 0 for result in evaluation_results
    )

    agent_metrics, run_outputs = agentos_metrics(workspace)
    if "model_invocations" not in agent_metrics:
        agent_metrics["model_invocations"] = sum(
            bool(step.get("model_invocation", False))
            for step in implementation_steps[: len(workflow_results)]
        ) or None
    log_paths = [Path(result.log_file) for result in workflow_results]
    usage = collect_usage(log_paths, run_outputs)
    timed_out = any(result.timed_out for result in workflow_results + evaluation_results)

    record = TrialResult(
        run_id=run_id,
        task=task_id,
        workflow=workflow_name,
        attempt=attempt,
        base_ref=base_ref,
        base_commit=base_commit,
        started_at=started_at,
        duration_seconds=round(time.monotonic() - started, 3),
        workflow_succeeded=workflow_ok,
        evaluation_succeeded=evaluation_ok,
        timed_out=timed_out,
        workflow_steps=[asdict(result) for result in workflow_results],
        evaluation_steps=[asdict(result) for result in evaluation_results],
        workspace=str(workspace) if keep_workspaces else "",
        **usage,
        **agent_metrics,
        **change_metrics,
    )
    record_path = run_dir / "result.json"
    record_path.write_text(json.dumps(asdict(record), indent=2), encoding="utf-8")
    print(
        f"[{run_id}] workflow={'PASS' if workflow_ok else 'FAIL'} "
        f"evaluation={'PASS' if evaluation_ok else 'FAIL'} "
        f"time={record.duration_seconds:.1f}s",
        flush=True,
    )
    if not keep_workspaces:
        shutil.rmtree(workspace, ignore_errors=True)
    return record


def flatten(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
        for key, value in record.items()
        if key not in {"workflow_steps", "evaluation_steps"}
    }


def read_records(output_root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((output_root / "runs").glob("*/result.json")):
        records.append(json.loads(path.read_text(encoding="utf-8")))
    return records


def write_outputs(output_root: Path) -> None:
    records = read_records(output_root)
    if not records:
        return
    flat = [flatten(record) for record in records]
    fieldnames = sorted({key for record in flat for key in record})
    with (output_root / "results.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(flat)

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[record["workflow"]].append(record)
    lines = [
        "# Benchmark summary",
        "",
        "| Workflow | Runs | Evaluation success | Median time | Median input tokens | Median output tokens |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for workflow, items in sorted(groups.items()):
        successes = sum(bool(item["evaluation_succeeded"]) for item in items)
        duration = statistics.median(float(item["duration_seconds"]) for item in items)
        input_tokens = [int(item["input_tokens"]) for item in items if item.get("input_tokens") is not None]
        output_tokens = [int(item["output_tokens"]) for item in items if item.get("output_tokens") is not None]
        lines.append(
            f"| {workflow} | {len(items)} | {successes / len(items):.0%} "
            f"| {duration:.1f}s | {median_or_dash(input_tokens)} | {median_or_dash(output_tokens)} |"
        )
    lines.extend(
        [
            "",
            "## Results by task",
            "",
            "| Task | Workflow | Runs | Evaluation success | Median time |",
            "|---|---|---:|---:|---:|",
        ]
    )
    task_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        task_groups[(record["task"], record["workflow"])].append(record)
    for (task, workflow), items in sorted(task_groups.items()):
        successes = sum(bool(item["evaluation_succeeded"]) for item in items)
        duration = statistics.median(float(item["duration_seconds"]) for item in items)
        lines.append(
            f"| {task} | {workflow} | {len(items)} "
            f"| {successes / len(items):.0%} | {duration:.1f}s |"
        )
    (output_root / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def median_or_dash(values: list[int]) -> str:
    return str(round(statistics.median(values))) if values else "—"


def annotate(output_root: Path, run_id: str, interventions: int | None, review_score: float | None, notes: str | None) -> None:
    path = output_root / "runs" / run_id / "result.json"
    if not path.exists():
        raise FileNotFoundError(f"unknown run id: {run_id}")
    record = json.loads(path.read_text(encoding="utf-8"))
    if interventions is not None:
        record["interventions"] = interventions
    if review_score is not None:
        record["review_score"] = review_score
    if notes is not None:
        record["notes"] = notes
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    write_outputs(output_root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    run_parser = subparsers.add_parser("run", help="Run all configured trials")
    run_parser.add_argument("config", type=Path)
    run_parser.add_argument("--workflow", action="append", help="Run only this workflow; repeatable")
    run_parser.add_argument("--task", action="append", help="Run only this task; repeatable")
    run_parser.add_argument("--attempts", type=int, help="Override repetitions from config")
    run_parser.add_argument("--seed", type=int, help="Override randomization seed")
    run_parser.add_argument("--discard-workspaces", action="store_true")

    summary_parser = subparsers.add_parser("summarize", help="Rebuild CSV and Markdown outputs")
    summary_parser.add_argument("output", type=Path)

    annotate_parser = subparsers.add_parser("annotate", help="Add manual review data to one run")
    annotate_parser.add_argument("output", type=Path)
    annotate_parser.add_argument("run_id")
    annotate_parser.add_argument("--interventions", type=int)
    annotate_parser.add_argument("--review-score", type=float)
    annotate_parser.add_argument("--notes")

    args = parser.parse_args()
    if args.action == "summarize":
        write_outputs(args.output.resolve())
        return 0
    if args.action == "annotate":
        annotate(
            args.output.resolve(), args.run_id, args.interventions, args.review_score, args.notes
        )
        return 0

    config_path = args.config.resolve()
    config = load_config(config_path)
    output_root = Path(config.get("output_directory", "output"))
    if not output_root.is_absolute():
        output_root = (config_path.parent / output_root).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    attempts = args.attempts or int(config.get("repetitions", 5))
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    seed = args.seed if args.seed is not None else int(config.get("seed", 20260930))
    default_timeout = float(config.get("timeout_seconds", 3600))
    selected_tasks = [
        task for task in config["tasks"] if not args.task or str(task["id"]) in args.task
    ]
    selected_workflows = [
        workflow
        for workflow in config["workflows"]
        if not args.workflow or str(workflow["name"]) in args.workflow
    ]
    if not selected_tasks or not selected_workflows:
        raise ValueError("task/workflow filters selected nothing")

    schedule = [
        (task, workflow, attempt)
        for task in selected_tasks
        for attempt in range(1, attempts + 1)
        for workflow in selected_workflows
    ]
    random.Random(seed).shuffle(schedule)
    (output_root / "schedule.json").write_text(
        json.dumps(
            [
                {"task": item[0]["id"], "workflow": item[1]["name"], "attempt": item[2]}
                for item in schedule
            ],
            indent=2,
        ),
        encoding="utf-8",
    )

    failures = 0
    for task, workflow, attempt in schedule:
        run_id = f"{safe_name(str(task['id']))}__{safe_name(str(workflow['name']))}__{attempt:02d}"
        result_path = output_root / "runs" / run_id / "result.json"
        if result_path.exists():
            print(f"[{run_id}] already recorded; skipping", flush=True)
            continue
        try:
            result = run_trial(
                task,
                workflow,
                attempt,
                config_path,
                output_root,
                default_timeout,
                keep_workspaces=not args.discard_workspaces,
            )
            failures += 0 if result.evaluation_succeeded else 1
        except KeyboardInterrupt:
            print("\nInterrupted; completed trial records were preserved.", file=sys.stderr)
            write_outputs(output_root)
            return 130
        except Exception as exc:
            failures += 1
            print(f"[{run_id}] harness error: {exc}", file=sys.stderr, flush=True)
        write_outputs(output_root)

    print(f"\nResults: {output_root / 'results.csv'}")
    print(f"Summary: {output_root / 'summary.md'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

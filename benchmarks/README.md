# agentos benchmark harness

`run_benchmark.py` compares direct coding-agent sessions with agentos using clean,
repeatable repository clones. It randomizes trial order, captures command logs and
duration, runs held-out evaluation commands, extracts token usage when the CLI
reports it, and produces JSON, CSV, and Markdown results.

## Configure

Create the ready-made local fixture repository:

```powershell
python benchmarks/create_fixture.py
```

This creates `benchmarks/work/project-labels`, commits the starting application,
and adds the `benchmark-start` tag expected by the example configuration. Then
copy the example configuration:

```powershell
Copy-Item benchmarks/benchmark.example.json benchmarks/benchmark.json
```

The included project-labels benchmark is now ready. To benchmark another
project, update:

- `repository`: a local repository path or Git URL containing a fixed benchmark
  starting point
- `base_ref`: the commit or tag every trial starts from
- `prompt_file`: the identical objective supplied to every workflow
- `evaluation_setup_steps`: commands that copy held-out tests into the clone
- `evaluation_steps`: the authoritative completion checks
- workflow commands and model names

`agentos.benchmark.yaml` pins the agentos planning and execution models to the
same model used by the direct Claude workflow. Change all three model entries
together when testing another model.

The example enables `AGENTOS_PRESERVE_OUTPUT_TAIL=1` only for the AgentOS
execution step. Normal AgentOS runs keep the beginning of oversized provider
output; this opt-in benchmark mode keeps the end instead so Claude's terminal
stream event remains available for token accounting. Enable it manually only
for targeted diagnostic runs that require usage capture.

Commands are JSON argument arrays and are executed without a shell. Available
placeholders include `{workspace}`, `{prompt}`, `{prompt_file}`, `{task_id}`,
`{attempt}`, `{base_ref}`, `{base_commit}`, `{config_dir}`, `{benchmark_root}`,
and `{run_dir}`.

Keep evaluator tests outside the fixture repository so the coding agent cannot
read or modify them during implementation.

## Run

From the agentos repository:

```powershell
python benchmarks/run_benchmark.py run benchmarks/benchmark.json
```

Run a small smoke test first:

```powershell
python benchmarks/run_benchmark.py run benchmarks/benchmark.json `
  --attempts 1 `
  --task project-labels `
  --workflow single-claude
```

Completed run IDs are skipped, so an interrupted benchmark can be resumed with
the same command. Add `--discard-workspaces` when only logs and measurements
should be retained.

The output directory contains:

```text
output/
  schedule.json
  results.csv
  summary.md
  runs/<task>__<workflow>__<attempt>/
    result.json
    logs/
    workspace/
```

## Record human review

After reviewing an anonymized diff, attach intervention and review data:

```powershell
python benchmarks/run_benchmark.py annotate benchmarks/output `
  project-labels__agentos-claude__01 `
  --interventions 1 `
  --review-score 26 `
  --notes "One clarification; all held-out tests passed"
```

Rebuild the aggregate files after manually editing any result records:

```powershell
python benchmarks/run_benchmark.py summarize benchmarks/output
```

`evaluation_succeeded` is the primary success measure. Subscription quotas do
not have a reliable per-run monetary value, so report elapsed time, model
invocations, token usage when available, interventions, and success rate.

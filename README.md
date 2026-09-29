# agentos

**A local, CLI-first orchestrator for teams of coding agents.**

agentos turns a software objective into a dependency-aware task graph, runs the
tasks across Claude Code or OpenAI Codex CLI sessions, verifies the results, and
keeps a human in control of planning and integration.

The orchestrator itself is deterministic Python, not another model. Models make
engineering decisions; agentos owns scheduling, state transitions, permissions,
process lifecycle, verification, and Git isolation.

> [!IMPORTANT]
> agentos is experimental, pre-1.0 software. It can launch paid model sessions
> and allow agents to modify a working tree. Start in a disposable branch or
> repository, review the generated plan, and inspect every diff before merging.

## Why agentos?

- **Bring your existing login.** It drives the installed `claude` or `codex`
  CLI and does not require application code to handle provider API keys.
- **Run specialists concurrently.** Assign arbitrary roles to persistent agents
  and schedule independent tasks in parallel.
- **Keep work isolated.** Coding agents can work on dedicated Git branches and
  worktrees instead of sharing one checkout.
- **Treat model output as untrusted.** Structured responses are validated before
  they can change orchestrator state, and claimed file changes are checked
  against Git.
- **Verify before completing.** Agent success claims pass through configurable
  test and lint commands before dependent tasks can continue.
- **Retain operator control.** Plans, risky commands, and merges can require
  explicit approval. Integration is a report-only operation unless `--apply` is
  provided.
- **Keep an audit trail.** SQLite stores tasks, runs, messages, denials,
  verification results, and events locally under `.agentos/`.

## How it works

```text
objective
    |
    v
manager creates a plan --> human approval
    |
    v
dependency graph --> ready tasks run concurrently
    |                         |
    |                         +--> isolated agent sessions/worktrees
    v
verification checks --> review diffs --> explicit integration
```

The default roster contains manager, backend, frontend, QA, and reviewer roles,
but neither those names nor that team shape are required. Roles, prompts, model
tiers, capabilities, and worktree isolation are configured per project.

## Requirements

- Python 3.12 or newer
- Git, recommended for worktree isolation and integration
- At least one installed and authenticated agent CLI:
  - Claude Code, available as `claude`
  - OpenAI Codex, available as `codex`

The current release is developed and tested primarily on Windows. The runtime
contains POSIX process handling as well, but Linux and macOS support should be
considered best effort until cross-platform CI is in place.

## Install from source

agentos is not currently published as a package, so install it from a clone.

```bash
git clone https://github.com/akurtle/local-cli-agent-orchestrator.git
cd local-cli-agent-orchestrator
python -m venv .venv
```

Activate the environment:

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

```bash
# macOS or Linux
source .venv/bin/activate
```

Then install the CLI. The `tui` extra adds the interactive dashboard; `dev`
adds the dashboard and test tooling.

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[tui]"
```

## Quick start

With the installation environment still active, move into the repository you
want agentos to manage:

```bash
cd /path/to/your/project
agentctl init --name "My Project"
```

`init` creates `agentos.yaml`, initializes `.agentos/agentos.db`, and adds the
agentos state and worktree directories to `.gitignore` when run in a Git
repository.

Choose a provider if the default (`claude`) is not the one you want, then check
the configuration and CLI installation:

```bash
agentctl provider codex       # optional; use `claude` to switch back
agentctl doctor
```

Review `agentos.yaml`, commit or branch your existing work, and start an
objective:

```bash
agentctl run "Add health checks and document the endpoint"
agentctl status
agentctl work
```

`run` asks the manager to propose a plan and prompts before creating tasks.
`work` executes the approved graph. Both operations can invoke paid model
sessions. To plan, approve, and immediately execute in one command:

```bash
agentctl run "Add health checks and document the endpoint" --work
```

Once the tasks settle, inspect what happened before applying anything:

```bash
agentctl status
agentctl git status
agentctl diff backend
agentctl integrate             # report only
agentctl integrate --apply     # prompts before merging
```

For a scheduling-only exercise that launches no agent processes, create tasks
manually and use `agentctl work --dry-run`.

## Configuration

Each managed repository has an `agentos.yaml` at its root. Configuration is
validated strictly, so misspelled or unknown keys fail early.

```yaml
project:
  name: Example Service
  description: API and web client

runtime:
  name: claude                 # claude or codex

orchestrator:
  max_concurrent_agents: 3
  default_timeout_seconds: 900
  max_task_retries: 1
  manager_role: manager

providers:
  claude:
    # planning: <model-name>
    # execution: <model-name>
  codex:
    # planning: <model-name>
    # execution: <model-name>

agents:
  manager:
    role: manager
    description: Plans and coordinates work; does not implement.
    tier: planning

  backend:
    role: backend
    description: Owns server-side code and data models.
    worktree: true

  reviewer:
    role: reviewer
    description: Reviews completed work against acceptance criteria.
    worktree: false

verification:
  backend:
    commands:
      - [pytest, -q]
      - [ruff, check, .]
  default:
    commands: []

approvals:
  manager_plan: true
  merge: true
  dangerous_command: true
  final_completion: false

commands:
  allowed: [pytest, python, npm, git, ruff, mypy]
  denied: [powershell, pwsh, cmd, bash, sh, ssh, curl, wget]
  require_approval: ["git push", "git reset", "git clean"]

context:
  max_tasks_per_session: 8
  rotate_on_objective_change: true
```

### Providers and model tiers

The manager uses the provider's planning tier by default; other agents use its
execution tier. Override a tier under `providers`, or set `model` on an
individual agent. `agentctl provider` shows the effective provider and model
selection, while `agentctl provider claude|codex` changes it for the current
project without rewriting the YAML file.

Authentication is inherited from the selected CLI's existing login. Prompts and
repository context are still sent to that provider through its CLI, subject to
the provider's own terms, privacy controls, and usage charges.

### Agents, roles, and capabilities

Agent names and roles are arbitrary strings. Built-in role briefs exist for the
starter roster; an unknown role receives a neutral brief, and `prompt` can point
to a project-specific Markdown file. An agent may also set:

- `worktree: true` for a dedicated `agent/<name>` branch and worktree
- `tier: planning|execution` to select a provider tier
- `model: <name>` to pin a model
- `capabilities: [...]` to replace the defaults for that role

Use `agentctl permissions` to inspect the effective capabilities and
`agentctl permission denials` to audit refusals.

### Verification

An agent response marked complete first moves to an intermediate state. agentos
then runs the configured verification commands and only marks the task complete
when the verdict allows it. Failed verification blocks dependent tasks.

Commands are argument arrays, not shell strings. Acceptance criteria beginning
with `$`, `cmd:`, `command:`, or `run:` can also become automated checks. Other
criteria remain visible as manual or review items instead of being treated as
automatically satisfied.

## Command guide

Run `agentctl --help` or `agentctl <command> --help` for the complete interface.

| Goal | Command |
|---|---|
| Initialize or diagnose a project | `agentctl init`, `agentctl doctor` |
| Plan an objective | `agentctl run "<objective>"` |
| Execute ready work | `agentctl work` |
| Exercise scheduling without model calls | `agentctl work --dry-run` |
| Inspect progress | `agentctl status`, `agentctl dashboard` |
| Manage the task graph | `agentctl tasks`, `agentctl task ...`, `agentctl replan` |
| Inspect agents and runs | `agentctl agents`, `agentctl agent ...`, `agentctl logs <agent>` |
| Send or inspect messages | `agentctl message <agent> "<text>"`, `agentctl messages` |
| Inspect code changes | `agentctl git status`, `agentctl diff <agent>` |
| Preview or apply integration | `agentctl integrate`, `agentctl integrate --apply` |
| Inspect permissions and commands | `agentctl permissions`, `agentctl commands` |
| Inspect context and memory | `agentctl context <agent>`, `agentctl memories` |
| Follow events and metrics | `agentctl timeline`, `agentctl watch`, `agentctl stats` |
| Show or switch providers | `agentctl provider [claude|codex]` |

The Textual dashboard is an operational view over the same services used by the
CLI. It shows agents, tasks, messages, attention items, recent runs, and change
summaries. It does not run the scheduler on its own.

## Safety model

agentos is designed to reduce accidental actions and make them observable. It
is **not a security sandbox** for hostile code or models.

- Model responses are parsed into Pydantic schemas before state changes.
- Agent text is never interpolated into a shell command.
- Development commands use argument arrays, a configured executable allowlist,
  working-directory boundaries, timeouts, and an audit log.
- Claude tool access is restricted from capabilities. Codex maps write access to
  its `workspace-write` or `read-only` sandbox because it has no equivalent
  per-tool allowlist.
- Worktree diffs are compared with an agent's claimed file list, and
  unauthorized edits are recorded.
- Planning and merging require approval by default. Non-interactive runs refuse
  an unanswered gate instead of assuming consent.
- `agentctl integrate` only reports unless `--apply` is supplied. Conflicts are
  not resolved by silently choosing a side.

An allowlist cannot make arbitrary programs safe: `python`, package managers,
test runners, and Git can all execute project-controlled code. Run agentos only
on repositories and machines where that risk is acceptable.

## State and Git behavior

- Mutable state lives under `.agentos/`; the SQLite database is the source of
  truth and uses WAL mode.
- Agent worktrees live under `worktrees/` and use `agent/<name>` branches.
- Removing a worktree keeps its branch unless explicitly told otherwise.
- Integration happens on a dedicated integration branch, leaving the operator's
  current checkout untouched.
- If Git isolation is requested but unavailable, execution falls back to the
  project directory and reports a warning.
- The first interrupt stops new dispatch and lets active runs settle; a second
  interrupt cancels them.

## Architecture

```text
src/agentos/
  cli/             Typer commands and Rich output
  tui/             Textual dashboard
  runtime/         Claude, Codex, and dry-run adapters
  services/        orchestration, scheduling, verification, permissions
  repositories/    persistence interfaces
  db/              SQLAlchemy models, sessions, and additive migrations
  schemas/         validated runtime and model-response contracts
  prompts/         built-in role briefs
  vcs/             Git worktree, diff, commit, and merge operations
tests/              offline unit and integration tests with fake CLIs
```

Several boundaries are deliberate:

- Runtime adapters are the only code that launches model CLIs.
- The VCS manager is the only code that invokes Git.
- Scheduling, graph validation, command policy, and plan validation are kept as
  pure logic where possible.
- Events explain what happened; persisted task and objective rows define what is
  true now.
- Session history is not copied between agents. Scoped memories, messages, and
  handoff packets carry durable context.

## Development

Install the development dependencies and run the offline suite:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

The test suite uses fake Claude and Codex executables; it does not require a
provider login or incur model usage. Useful checks while changing the CLI:

```bash
agentctl --help
agentctl doctor
python -m pytest --collect-only -q
```

Contributions are welcome. Keep orchestration decisions deterministic, preserve
the existing trust boundaries, add tests for behavior changes, and avoid tests
that contact a real provider. Please open an issue before a large architectural
change so the intended behavior can be agreed on first.

## Known limitations

- The project is pre-1.0 and does not yet publish compatibility guarantees.
- Installation is from source; there is no PyPI release yet.
- Cross-platform CI and end-to-end testing against every CLI release are not yet
  in place.
- SQLite migrations are additive and intentionally small; complex schema changes
  would require a dedicated migration framework.
- Command policy is a guardrail, not a containment boundary.
- Provider CLI output formats can change and may require adapter updates.

## License

No open-source license has been selected yet. Until a license file is added,
copyright law reserves the project author's rights; publishing the repository
alone does not grant permission to use, modify, or redistribute the code.

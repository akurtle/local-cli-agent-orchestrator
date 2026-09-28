# agentos

A local, CLI-first multi-agent orchestration system. Multiple Claude Code CLI
processes act as workers; all orchestration logic — scheduling, task state,
dependencies, messaging, process lifecycle, Git isolation — is deterministic
Python.

**The orchestrator is not an LLM.** Agents decide engineering questions; Python
decides everything else.

## Constraints this project honours

- No direct use of the Anthropic API.
- No `ANTHROPIC_API_KEY`. Auth is inherited from your existing Claude Code login.
- `claude` is treated as an external worker process, nothing more.
- Nothing an agent emits is ever passed to a shell.

## Status: Phase 11 complete

The manager decomposes an objective into a validated task graph; agents execute
it concurrently in isolated git worktrees, message each other and request
follow-up work.

| Phase | Scope | State |
|---|---|---|
| 1 | Foundation, runtime adapter, CLI | **done** |
| 2 | Persistent agents, role prompts, sessions | **done** |
| 3 | Tasks, dependencies, scheduler | **done** |
| 4 | Messaging, inboxes, structured responses | **done** |
| 5 | Manager agent planning | **done** |
| 6 | Git worktree isolation | **done** |
| 7 | Orchestration loop hardening | **done** |
| 8 | Rich dashboard | **done** |
| 9 | Configurable agent definitions | **done** |
| 10 | Integrator | **done** |
| 11 | Approval gates | **done** |
| 12 | Textual TUI | next |

## Install

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Verify Phase 1

```bash
.venv\Scripts\python.exe -m pytest -q          # 509 tests, no network, no cost
.venv\Scripts\agentctl.exe init --name "My Project"
.venv\Scripts\agentctl.exe doctor              # detects the claude CLI
.venv\Scripts\agentctl.exe claude-test "Say hello in exactly three words."
.venv\Scripts\agentctl.exe runs
.venv\Scripts\agentctl.exe run-show 1
```

Session continuity (the same Claude session across two separate processes):

```bash
.venv\Scripts\agentctl.exe claude-test "Remember the codeword TITANIUM. Reply OK."
.venv\Scripts\agentctl.exe claude-test "What was the codeword?" --session <id-printed-above>
```

## Architecture notes

### Session IDs are ours, not scraped
The CLI accepts `--session-id <uuid>`, so the orchestrator **mints** a UUID and
owns it from the start, then resumes with `--resume <uuid>`. Parsing output only
*confirms* the ID. Each agent therefore keeps its own independent Claude session.

### Prompts travel over stdin
Windows caps a command line near 32 KB, and prompts will carry role + inbox +
task text. Prompts are never placed on argv; there is a test enforcing this.

### One place spawns processes
`runtime/claude_cli.py` is the only module permitted to spawn the agent.
Everything else speaks `RunRequest` / `RunResult`. `runtime/base.py` defines the
protocol so a future `codex`/`gemini`/`ollama` runtime slots in without touching
the scheduler.

### Windows specifics
- The **Proactor** event loop is required for asyncio subprocesses; we never
  install `WindowsSelectorEventLoopPolicy`.
- There is no `SIGTERM`, and `claude` spawns children, so timeout/cancel
  escalates to `taskkill /T /F` to kill the whole tree rather than orphan it.
- Pipes are decoded as UTF-8 with `errors="replace"` (the console is cp1252).
- The executable is resolved via `shutil.which` so `.exe` / `.cmd` shims work.
- A legacy console is cp1252, where printing `●` raises and kills the
  command, so glyphs degrade to ASCII and agent text is sanitised before render.

### Model output is untrusted
Agents emit prose plus a delimited JSON block
(`<<<AGENT_RESPONSE … AGENT_RESPONSE>>>`) validated by Pydantic before any state
changes. Unknown fields are dropped, unknown statuses rejected, and the *last*
block wins so a model restating the format mid-reasoning cannot hijack the
result. `files_changed` is a claim to verify against `git diff`, not a fact.

### Roles are arbitrary strings
The five starter agents are a convenience, not a requirement. A role is any
string; an unrecognised one gets a neutral brief, so `database`, `mobile` or
`security` work with no code change. Each agent may point at its own prompt file.
Even the planning role is configurable via `orchestrator.manager_role`, so a
roster whose lead is called `architect` needs nothing special.

An agent removed from the config is marked `offline` rather than deleted: its runs
and session are history worth keeping, but it stops being eligible for work.
Putting it back in the config brings it online again.

### One dashboard, derived not stored
`agentctl status` shows objectives, agents, a task progress bar, recent messages
and anything holding a blocker. Every number is computed from services on each
render, so the view cannot drift from the database. `--watch N` re-renders on a
timer; WAL mode means those reads never block a running scheduler.

Rendering never decides state and never queries the database directly, and it is
tested for content rather than exact layout so a column-width change does not
break the suite. There is a test that every task and objective status renders, so
adding one later cannot blow up the dashboard.

### Why a task failed decides what happens next
`FailureKind` separates the cases the spec asks about, and the retry policy
differs for each:

| kind | meaning | policy |
|---|---|---|
| `launch` / `timeout` | the process would not start, or was killed | retry |
| `unparseable` | ran, but produced no usable response block | repair, then retry |
| `agent_failed` | ran, and the agent reported failure | retry |
| `blocked` | the agent cannot proceed | **never retried** |
| `unavailable` | the agent was busy or paused | requeued, no attempt consumed |

A blocker is not retried because another attempt would hit the same wall and
spend more usage. The task goes to `blocked` with `needs_intervention`, which
readiness deliberately leaves alone -- otherwise a task whose dependencies are all
complete would be marked ready again and sent straight back. `agentctl task
unblock <id>` clears it once the operator has dealt with the cause.

### Ctrl+C twice
The first interrupt stops launching new work and lets running agents finish, so
their results are recorded rather than discarded. The second cancels them; the
runtime kills the process tree and the task returns to the queue. Task rows are
written as each task settles, so the database is consistent at any point.

### Agents work in isolated worktrees
An agent with `worktree: true` gets `worktrees/<name>` on branch `agent/<name>`,
so two coding agents never edit the same checkout. Creation is idempotent because
it runs before every task, and removing a worktree keeps the branch by default --
that branch holds the work, and discarding it silently would destroy the thing the
operator still needs to review.

If isolation is configured but git cannot provide it (no repository, no commits),
the task runs in the project directory with a warning rather than stalling the
queue. The operator asked for work to happen.

### `files_changed` is a claim, checked against git
After an isolated task, the orchestrator reads `git status` and records the real
changed files and diff summary. Anything the agent claimed but git does not show
is reported as an unverified claim. For a *shared* directory no attribution is
possible -- concurrent agents and our own state files all appear as changes -- so
capture is skipped entirely rather than crediting one task with another's work.

### A gate that passes unwatched is not a gate
`approvals` in config decides which actions need a human yes. The two that change
the repository or create work (`manager_plan`, `merge`) default to on;
`final_completion` defaults to off, because completion is computed from task state
and a prompt there would be noise.

When a gate is required and nobody can answer, the action is **refused**, not
assumed, naming the flag that would grant it. Automation is still possible, but
only by saying so deliberately: pass `--auto-approve` / `--yes`, or turn the gate
off in config.

`ApprovalService` only answers "is approval required here?" -- it never prompts,
so the same rules hold for the CLI, a TUI or a non-interactive run.

### Integration detects, it does not guess
`agentctl integrate` reports by default and merges only with `--apply`. It works
on a dedicated `integration/...` branch built fresh from the base, so the
operator's checkout is never modified and abandoning the attempt costs nothing. A
conflicting merge is aborted, leaving no half-merged files, and the agent's branch
keeps its work.

Conflict *resolution* is deliberately not automated: a conflict means two agents
disagreed about the same lines, and picking a winner mechanically is how work gets
lost. The orchestrator files a task for somebody to decide, with acceptance
criteria that include not discarding the other branch's changes.

Branches are checked against the base individually, so two that each merge
cleanly can still conflict with each other once the first lands. Overlapping files
are flagged up front, and if that sequential conflict does happen the second
branch gets a resolution task exactly like a predicted one.

### Nothing merges automatically
`GitManager` can detect a conflict with `merge-tree` without touching the working
tree, and `merge()` returns a failed result rather than raising, so a caller can
report the conflict and create a resolution task. The scheduler never calls it.

### Claude proposes, Python applies
`services/results.py` and `services/planner.py` are the trust boundary. An agent
asks for messages and follow-up tasks in its response block; a manager proposes a
plan. Both are validated against the real roster and graph before anything is
written. An agent cannot name a recipient that does not exist, invent a
prerequisite, target an ambiguous role, or exceed a per-turn cap, and the manager
cannot assign work to an agent that is not configured. `validate_plan` is a pure
function, so every rejection rule is tested without a database.

### delivered is not read
Message delivery is three-state: `pending`, `delivered` (injected into a prompt
we actually sent), `read` (the receiving run finished). Marking a message read at
injection time would lose it whenever a Claude process died mid-run. A failed
run, a crash, a lost race for the agent or a cancellation all release the
messages for redelivery.

### One repair attempt, then honesty
An unparseable reply gets exactly one repair turn whose prompt asks only for the
response block and forbids further work. If that also fails to parse, the task
fails -- the orchestrator never records success it cannot verify.

### Objective completion is computed
An objective is complete when all of its tasks completed, and failed when nothing
can progress. No agent is ever asked whether the work is done.

### Blockage propagates, reversibly
A task waiting on a blocked task is itself blocked, so dead work does not look
like work in flight. `refresh_readiness` iterates to a fixed point because one
round only moves blockage a single edge down the chain. It reverses automatically
when the chain clears.

### Scheduling decisions are pure functions
`services/dag.py` holds readiness, cycle detection and dispatch selection as
functions over frozen dataclasses -- no database, no asyncio, no Claude. That is
the part which must be exactly right, so it is testable exhaustively and in
milliseconds (48 tests, 0.06s). `services/scheduler.py` only loads rows, calls
those functions, runs agents and persists what they decide.

### Readiness is derived, never assumed
Statuses are recomputed from the graph on every pass: a dependency that failed or
was cancelled blocks its dependents; all dependencies complete means ready. A
task already marked `ready` regresses to `blocked` if a prerequisite later dies,
and a blocked task recovers if that prerequisite is retried successfully. A
dangling dependency edge blocks rather than letting the task run.

### Cycles are refused at write time
`would_create_cycle` runs before an edge is persisted, so a cycle never reaches
the database. `validate_graph` re-checks the whole graph when the scheduler
starts, as a safety net against a hand-edited database, and raises rather than
spinning. Cycle detection is iterative, so a 3000-deep chain does not blow the
recursion limit.

### The loop does not busy-wait
Each pass dispatches what it can, then blocks on
`asyncio.wait(FIRST_COMPLETED)`. When ready work exists but no agent can take it,
the scheduler stops and says which tasks were skipped instead of polling forever.

### Crash recovery
Nothing is running when the scheduler starts, so any task still marked `running`
is stale by definition. It is returned to the queue rather than failed, since the
work may never have begun, and its retry count is left untouched so a genuine
crash loop still hits the ceiling.

### Dry run costs nothing
`runtime/dry_run.py` satisfies the same `AgentRuntime` protocol and launches no
process, so `agentctl work --dry-run` validates a whole task graph's scheduling
for free. The scheduler contains no test-only branch.

### Agents: config owns definitions, the database owns state
`sync_from_config` refreshes an agent's role, description and model from YAML on
every run, but never touches its `status` or `session_id`. Editing the config
cannot wipe a live session. Nothing hardcodes the five default roles -- an agent
with an unknown role gets a neutral brief, so user-defined roles work today.

### Sessions are recovered, not trusted
A stored session can vanish (cleared history, another machine). The CLI reports
that as plain text on stdout with exit 1 and no JSON, so `is_stale_session`
matches on the message and the service starts one fresh session, once, and says
so. The match is deliberately narrow: a genuine failure is never silently retried.

### Role prompts cannot drift from the parser
The response-format half of every role prompt is generated from the real
delimiters and field rules in `schemas/responses.py`. Change the parser and every
prompt changes with it. The markdown files hold only responsibilities and
boundaries.

### Additive migrations, not Alembic
`create_all()` creates missing tables but never alters existing ones, so Phase 2
adding `agents.runtime` broke every Phase 1 database. `db/migrations.py` checks
`PRAGMA table_info` and adds missing columns, which is idempotent and preserves
session IDs and run history. It handles adding a column and nothing else; if a
drop or type change is ever needed, add Alembic rather than growing that file.

### Synchronous SQLite, on purpose
The concurrency problem here is subprocesses, not disk I/O. WAL mode lets
`agentctl status` read while the orchestrator writes. Async callers wrap short DB
blocks in `asyncio.to_thread`, so connections cross threads: `check_same_thread`
is disabled, a file database gets one pooled connection per thread, and an
in-memory one shares a single connection behind a lock.

## Layout

```
src/agentos/
  branding.py        product name in one place, so renaming is cheap
  config.py          YAML + pydantic validation
  paths.py           project/state layout
  db/models.py       Agent, Task, TaskDependency, Message, Run
  db/session.py      engine, WAL, transactional sessions
  schemas/           enums, response contract, runtime DTOs
  runtime/base.py    AgentRuntime protocol
  runtime/claude_cli.py   the only module that spawns processes
  prompts/           role briefs + generated response contract
  repositories/      all agent SQL; returns DTOs, never ORM rows
  services/agents.py agent lifecycle, sessions, state transitions
  services/dag.py    PURE scheduling logic: readiness, cycles, dispatch order
  services/tasks.py  task creation, validation, status transitions
  services/messages.py   the message bus and its delivery lifecycle
  services/results.py    validates agent responses, applies what is allowed
  services/planner.py    PURE plan validation + temp-id translation
  services/objectives.py manager planning, approval gate, completion
  services/scheduler.py  async dispatch loop over dag.py decisions
  services/runs.py   run persistence
  runtime/dry_run.py no-op runtime for free scheduling dry runs
  vcs/manager.py     the only module that runs git; argv arrays, never a shell
  services/workspaces.py  where each agent works; verifies claims against git
  services/integration.py agent branch inspection and conservative merging
  db/migrations.py   additive column migrations
  cli/main.py        agentctl
  cli/glyphs.py      ASCII fallback for legacy Windows consoles
tests/
  fixtures/fake_claude.py   stub CLI: tests run offline and free
```

## Cost note

Each invocation is metered (~$0.02 for a trivial Haiku turn), so concurrency has
a real bill attached. `max_concurrent_agents` defaults to 3. The test suite never
calls the real CLI.

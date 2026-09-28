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

## Status: Phase 3 complete

Tasks, dependency-aware scheduling and concurrent execution. Messaging and the
manager agent are not built yet, so task graphs are still authored by hand.

| Phase | Scope | State |
|---|---|---|
| 1 | Foundation, runtime adapter, CLI | **done** |
| 2 | Persistent agents, role prompts, sessions | **done** |
| 3 | Tasks, dependencies, scheduler | **done** |
| 4 | Messaging, inboxes, structured responses | next |
| 5 | Manager agent planning | |
| 6 | Git worktree isolation | |
| 7 | Rich dashboard | |
| 8 | Textual TUI | |

## Install

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

## Verify Phase 1

```bash
.venv\Scripts\python.exe -m pytest -q          # 228 tests, no network, no cost
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
  services/scheduler.py  async dispatch loop over dag.py decisions
  services/runs.py   run persistence
  runtime/dry_run.py no-op runtime for free scheduling dry runs
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

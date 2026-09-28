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

## Status: Phase 2 complete

Persistent agents with independent Claude sessions. No scheduler or task system
yet -- agents are invoked one at a time by hand.

| Phase | Scope | State |
|---|---|---|
| 1 | Foundation, runtime adapter, CLI | **done** |
| 2 | Persistent agents, role prompts, sessions | **done** |
| 3 | Tasks, dependencies, scheduler | next |
| 4 | Messaging, inboxes, structured responses | |
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
.venv\Scripts\python.exe -m pytest -q          # 109 tests, no network, no cost
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
  services/runs.py   run persistence
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

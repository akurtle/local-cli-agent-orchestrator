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

## Status: Phase 1 complete

Foundation only: project layout, SQLite models, schemas, config loader,
`ClaudeRunner`, and a basic CLI. No scheduler or agent loop yet.

| Phase | Scope | State |
|---|---|---|
| 1 | Foundation, runtime adapter, CLI | **done** |
| 2 | Single agent, role prompts, persistent sessions | next |
| 3 | Tasks, dependencies, scheduler | |
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
.venv\Scripts\python.exe -m pytest -q          # 54 tests, no network, no cost
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

### Model output is untrusted
Agents emit prose plus a delimited JSON block
(`<<<AGENT_RESPONSE … AGENT_RESPONSE>>>`) validated by Pydantic before any state
changes. Unknown fields are dropped, unknown statuses rejected, and the *last*
block wins so a model restating the format mid-reasoning cannot hijack the
result. `files_changed` is a claim to verify against `git diff`, not a fact.

### Synchronous SQLite, on purpose
The concurrency problem here is subprocesses, not disk I/O. WAL mode lets
`agentctl status` read while the orchestrator writes. Async callers wrap short DB
blocks in `asyncio.to_thread`.

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
  services/runs.py   run persistence
  cli/main.py        agentctl
tests/
  fixtures/fake_claude.py   stub CLI: tests run offline and free
```

## Cost note

Each invocation is metered (~$0.02 for a trivial Haiku turn), so concurrency has
a real bill attached. `max_concurrent_agents` defaults to 3. The test suite never
calls the real CLI.

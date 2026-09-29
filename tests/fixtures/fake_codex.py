"""A stand-in for `codex exec --json`, emitting the events codex-cli 0.156 does.

Behaviour is chosen by words in the prompt, so tests need no flags of their own:
  FAIL_TURN   the turn fails (turn.failed)
  EXIT_BAD    the process exits 3 with nothing useful
  SLEEP       hangs, for timeout tests
Anything else succeeds, replying with two messages. The second echoes the argv
and the stdin prompt, so a test can see exactly what the runner passed.
"""

from __future__ import annotations

import json
import sys
import time
import uuid


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main() -> int:
    args = sys.argv[1:]
    prompt = sys.stdin.read()

    thread = str(uuid.uuid4())
    if "resume" in args:
        thread = args[args.index("-") - 1]
        if thread == "missing-thread":
            sys.stderr.write(
                "Error: thread/resume: thread/resume failed: no rollout found for "
                f"thread id {thread} (code -32600)\n"
            )
            return 1

    if "EXIT_BAD" in prompt:
        return 3

    emit({"type": "thread.started", "thread_id": thread})
    # Codex reports config warnings as error *items*; they are not failures.
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "error",
          "message": "Codex is ignoring 1 unrecognized configuration setting."}})
    emit({"type": "turn.started"})

    if "SLEEP" in prompt:
        time.sleep(60)

    if "FAIL_TURN" in prompt:
        emit({"type": "turn.failed", "error": {"message": "model overloaded"}})
        return 1

    emit({"type": "item.completed", "item": {
        "id": "item_1", "type": "command_execution", "command": "git --version",
        "aggregated_output": "git version 2\n", "exit_code": 0, "status": "completed"}})
    emit({"type": "item.completed", "item": {"id": "item_2", "type": "agent_message",
          "text": "first message"}})
    emit({"type": "item.completed", "item": {"id": "item_3", "type": "agent_message",
          "text": json.dumps({"argv": args, "stdin": prompt})}})
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}})
    return 0


if __name__ == "__main__":
    sys.exit(main())

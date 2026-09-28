"""A stub that mimics `claude -p --output-format stream-json`.

It lets us test process spawning, stream parsing, timeouts and failure paths
deterministically, offline, and for free. Behaviour is driven by keywords in the
prompt it reads from stdin.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("-p", "--print", action="store_true")
    parser.add_argument("--session-id")
    parser.add_argument("--resume")
    parser.add_argument("--output-format", default="text")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--append-system-prompt")
    parser.add_argument("--allowedTools", nargs="*")
    parser.add_argument("--version", action="store_true")
    args, _unknown = parser.parse_known_args()

    if args.version:
        sys.stdout.write("9.9.9 (fake)\n")
        return 0

    prompt = sys.stdin.read()
    session_id = args.resume or args.session_id or str(uuid.uuid4())

    if "HANG" in prompt:
        time.sleep(600)
        return 0

    if "SPAWN_FAIL" in prompt:
        sys.stderr.write("fake catastrophic failure\n")
        return 3

    emit(
        {
            "type": "system",
            "subtype": "init",
            "session_id": session_id,
            "cwd": ".",
        }
    )

    if "BADJSON" in prompt:
        sys.stdout.write("this is not json at all\n")
        sys.stdout.flush()

    reply = "ECHO: " + prompt.strip()

    emit(
        {
            "type": "assistant",
            "session_id": session_id,
            "message": {
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                    {"type": "text", "text": reply},
                ]
            },
        }
    )

    is_error = "TURN_ERROR" in prompt
    emit(
        {
            "type": "result",
            "subtype": "error" if is_error else "success",
            "session_id": session_id,
            "is_error": is_error,
            "result": "turn failed on purpose" if is_error else reply,
            "num_turns": 1,
            "total_cost_usd": 0.0123,
            "usage": {"input_tokens": 5, "output_tokens": 7},
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

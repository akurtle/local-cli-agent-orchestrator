"""The response contract is the trust boundary: nothing here may be lenient."""

from __future__ import annotations

import pytest

from agentos.schemas.responses import (
    RESPONSE_BEGIN,
    RESPONSE_END,
    AgentResponse,
    ResponseParseError,
    extract_response_block,
    parse_agent_response,
)


def wrap(body: str) -> str:
    return f"Some prose first.\n{RESPONSE_BEGIN}\n{body}\n{RESPONSE_END}\nTrailing chatter."


def test_parses_full_response() -> None:
    response = parse_agent_response(
        wrap(
            """
            {
              "status": "completed",
              "summary": "Implemented auth endpoint.",
              "files_changed": ["src/auth.py"],
              "messages": [{"to": "frontend", "message": "POST /api/auth/login"}],
              "requested_tasks": [
                {"agent_role": "qa", "title": "Test login", "description": "both paths"}
              ],
              "blockers": []
            }
            """
        )
    )
    assert response.status == "completed"
    assert response.files_changed == ["src/auth.py"]
    assert response.messages[0].to == "frontend"
    assert response.requested_tasks[0].agent_role == "qa"
    assert not response.is_blocked


def test_minimal_response_is_accepted() -> None:
    response = parse_agent_response(wrap('{"status": "completed"}'))
    assert response.summary == ""
    assert response.messages == []


def test_tolerates_json_code_fence_inside_block() -> None:
    response = parse_agent_response(wrap('```json\n{"status": "failed"}\n```'))
    assert response.status == "failed"


def test_last_block_wins() -> None:
    """A model restating the format earlier must not beat its real answer."""
    text = wrap('{"status": "failed"}') + wrap('{"status": "completed"}')
    assert parse_agent_response(text).status == "completed"


def test_missing_block_raises() -> None:
    with pytest.raises(ResponseParseError, match="no .* block found"):
        parse_agent_response("I did the thing, trust me.")


def test_invalid_json_raises() -> None:
    with pytest.raises(ResponseParseError, match="not valid JSON"):
        parse_agent_response(wrap("{status: completed"))


def test_non_object_json_raises() -> None:
    with pytest.raises(ResponseParseError, match="must be a JSON object"):
        parse_agent_response(wrap('["completed"]'))


def test_unknown_status_is_rejected() -> None:
    with pytest.raises(ResponseParseError, match="validation"):
        parse_agent_response(wrap('{"status": "vibes"}'))


def test_status_is_normalised() -> None:
    assert parse_agent_response(wrap('{"status": "COMPLETED"}')).status == "completed"


def test_unexpected_extra_fields_are_dropped() -> None:
    """Extra keys are ignored rather than fatal, but never reach app state."""
    response = parse_agent_response(
        wrap('{"status": "completed", "run_this_command": "rm -rf /"}')
    )
    assert not hasattr(response, "run_this_command")
    assert response.model_dump().get("run_this_command") is None


def test_blockers_imply_blocked() -> None:
    response = parse_agent_response(
        wrap('{"status": "completed", "blockers": ["needs db creds"]}')
    )
    assert response.is_blocked


def test_message_requires_non_empty_recipient() -> None:
    with pytest.raises(ResponseParseError):
        parse_agent_response(
            wrap('{"status": "completed", "messages": [{"to": "", "message": "hi"}]}')
        )


def test_extract_returns_none_without_block() -> None:
    assert extract_response_block("nothing here") is None
    assert extract_response_block("") is None


def test_default_response_is_valid() -> None:
    assert AgentResponse().status == "completed"

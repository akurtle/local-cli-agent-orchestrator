"""`agentctl gui`: the local web API over the dashboard's reader.

Driven over real HTTP on a loopback port, because the security properties that
matter -- token, Host header, what reaches the filesystem -- live in the
request handling, not in the functions it calls.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from agentos.gui.server import GuiState, create_server, url_for
from agentos.schemas.enums import AgentStatus, TaskStatus
from tests.test_tui import project  # noqa: F401  (shared fixture)

pytest.importorskip("textual")


class FakeChanges:
    def __init__(self) -> None:
        self.git = None

    def read(self):
        return []


@pytest.fixture
def gui(project, tmp_path):
    reader, tasks, db = project
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>agentos</title>", encoding="utf-8")
    (static / "app.js").write_text("console.log(1)", encoding="utf-8")

    state = GuiState(reader, FakeChanges())
    server = create_server(state, 0, static_dir=static)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, server, tasks, static
    server.shutdown()
    server.server_close()


def request(server, method, path, token=None, body=None, host=None):
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": host or f"127.0.0.1:{port}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    response = conn.getresponse()
    raw = response.read()
    conn.close()
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = raw.decode("utf-8", "replace")
    return response.status, payload, dict(response.getheaders())


# -------------------------------------------------------------------- security


def test_binds_to_loopback_only(gui) -> None:
    _state, server, _tasks, _static = gui
    assert server.server_address[0] == "127.0.0.1"


def test_api_needs_the_token(gui) -> None:
    state, server, _tasks, _static = gui
    assert request(server, "GET", "/api/snapshot")[0] == 401
    assert request(server, "GET", "/api/snapshot", token="wrong")[0] == 401
    assert request(server, "POST", "/api/work/start", token="wrong")[0] == 401
    assert request(server, "GET", "/api/snapshot", token=state.token)[0] == 200


def test_foreign_host_is_refused(gui) -> None:
    """DNS rebinding: a hostile name pointed at 127.0.0.1 gets nothing."""
    state, server, _tasks, _static = gui
    status, _body, _h = request(
        server, "GET", "/api/snapshot", token=state.token, host="evil.example:80"
    )
    assert status == 403
    assert request(server, "GET", "/", host="evil.example")[0] == 403


def test_token_travels_in_the_fragment(gui) -> None:
    """A fragment is never sent to a server, so it never lands in a log."""
    state, server, _tasks, _static = gui
    url = url_for(server, state)
    assert url.startswith("http://127.0.0.1:")
    assert url.endswith(f"/#token={state.token}")


def test_responses_carry_protective_headers(gui) -> None:
    state, server, _tasks, _static = gui
    _status, _body, headers = request(server, "GET", "/api/snapshot", token=state.token)
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"
    # No CORS headers: other origins can't read responses or send the token.
    assert "Access-Control-Allow-Origin" not in headers


# ------------------------------------------------------------------ static


def test_serves_the_app_and_falls_back_to_the_shell(gui) -> None:
    _state, server, _tasks, _static = gui
    status, body, headers = request(server, "GET", "/")
    assert status == 200 and "agentos" in body
    assert request(server, "GET", "/app.js")[0] == 200
    # Unknown paths get the app shell, and traversal can't escape the folder.
    assert "agentos" in request(server, "GET", "/some/route")[1]
    status, body, _h = request(server, "GET", "/../../pyproject.toml")
    assert status == 200 and "agentos" in body and "[project]" not in body


def test_explains_a_missing_build(project, tmp_path) -> None:
    reader, _tasks, _db = project
    state = GuiState(reader, FakeChanges())
    server = create_server(state, 0, static_dir=tmp_path / "nowhere")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, body, _h = request(server, "GET", "/")
        assert status == 503 and "npm run build" in body
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- reading


def test_snapshot_has_what_the_page_needs(gui) -> None:
    state, server, _tasks, _static = gui
    status, data, _h = request(server, "GET", "/api/snapshot", token=state.token)
    assert status == 200
    for key in ("project", "agents", "tasks", "attention", "changes", "providers", "counts", "work"):
        assert key in data
    assert data["counts"]["need_you"] == 1
    blocked = next(i for i in data["attention"] if i["key"] == "task:API-2")
    assert blocked["label"] == "blocked"
    assert {t["status"] for t in data["tasks"]} >= {"completed", "blocked"}


def test_diff_refuses_paths_outside_worktrees(gui) -> None:
    state, server, _tasks, _static = gui
    for name in ("..", "..%2F..", "nobody"):
        status, _body, _h = request(server, "GET", f"/api/diff/{name}", token=state.token)
        assert status == 404


def test_work_log_when_nothing_ran(gui) -> None:
    state, server, _tasks, _static = gui
    status, data, _h = request(server, "GET", "/api/work/log", token=state.token)
    assert status == 200 and data["path"] is None


# ---------------------------------------------------------------- actions


def test_unblock_and_hold_a_task(gui) -> None:
    state, server, tasks, _static = gui
    status, data, _h = request(server, "POST", "/api/tasks/API-2/unblock", token=state.token)
    assert status == 200 and data["ok"]
    assert not tasks.get_task("API-2").needs_intervention

    status, _data, _h = request(server, "POST", "/api/tasks/API-2/hold", token=state.token)
    assert status == 200
    assert tasks.get_task("API-2").needs_intervention


def test_refused_actions_say_why(gui) -> None:
    state, server, _tasks, _static = gui
    status, data, _h = request(server, "POST", "/api/tasks/API-1/retry", token=state.token)
    assert status == 409 and "only failed tasks" in data["error"]
    status, data, _h = request(server, "POST", "/api/tasks/NOPE-1/cancel", token=state.token)
    assert status == 404
    status, _data, _h = request(server, "POST", "/api/tasks/API-1/delete", token=state.token)
    assert status == 404


def test_pause_and_resume_an_agent(gui) -> None:
    state, server, _tasks, _static = gui
    assert request(server, "POST", "/api/agents/frontend/pause", token=state.token)[0] == 200
    assert state.reader.agents.get_agent("frontend").status is AgentStatus.PAUSED
    assert request(server, "POST", "/api/agents/frontend/resume", token=state.token)[0] == 200
    assert state.reader.agents.get_agent("frontend").status is AgentStatus.IDLE


def test_switch_provider(gui) -> None:
    from agentos.providers import read_override

    state, server, _tasks, _static = gui
    status, data, _h = request(
        server, "POST", "/api/provider", token=state.token, body={"name": "codex"}
    )
    assert status == 200 and data["provider"]["name"] == "codex"
    assert read_override(state.reader.paths) == "codex"
    status, _data, _h = request(
        server, "POST", "/api/provider", token=state.token, body={"name": "gemini"}
    )
    assert status == 409


def test_start_and_stop_work(gui) -> None:
    from tests.test_tui_controls import holder_script, wait_until

    state, server, _tasks, _static = gui
    state.reader.work_command = holder_script(state.reader.paths.root)

    status, data, _h = request(server, "POST", "/api/work/start", token=state.token)
    assert status == 200 and Path(data["log"]).name.startswith("work-")
    assert wait_until(lambda: state.reader.work_state() == "running")
    # A second scheduler is refused, not started.
    assert request(server, "POST", "/api/work/start", token=state.token)[0] == 409

    assert request(server, "POST", "/api/work/stop", token=state.token)[0] == 200
    assert wait_until(lambda: state.reader.work_state() is None)
    state.reader.work_process.wait(timeout=10)


def test_bad_json_is_a_400(gui) -> None:
    state, server, _tasks, _static = gui
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request(
        "POST",
        "/api/provider",
        body=b"{not json",
        headers={
            "Host": f"127.0.0.1:{port}",
            "Authorization": f"Bearer {state.token}",
            "Content-Type": "application/json",
        },
    )
    assert conn.getresponse().status == 400
    conn.close()


def test_running_task_is_protected(gui) -> None:
    state, server, tasks, _static = gui
    busy = tasks.create_task("Busy", agent="frontend", prefix="API")
    tasks.transition(busy.key, TaskStatus.RUNNING)
    status, data, _h = request(server, "POST", f"/api/tasks/{busy.key}/cancel", token=state.token)
    assert status == 409 and "is running" in data["error"]

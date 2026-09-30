"""The web dashboard's server: a JSON API over the same reader the TUI uses.

Like the TUI, this is a view. Every action is a `SnapshotReader` method the
terminal dashboard already calls, so the two cannot drift apart, and agents are
never run in this process -- `start_work` launches the ordinary `agentctl work`.

It can start paid work and change tasks, so it is locked down as a local tool:

  * **Loopback only.** It binds 127.0.0.1; nothing else on the network sees it.
  * **A per-launch token.** Every /api call needs `Authorization: Bearer
    <token>`. The token reaches the browser once, in the URL fragment
    `agentctl gui` opens (fragments are never sent to a server or logged). A web page on another site cannot read it, and a cross-site request
    cannot add that header without a CORS preflight this server never answers.
  * **Host check.** Requests must name localhost/127.0.0.1, which defeats DNS
    rebinding (a hostile domain re-pointed at 127.0.0.1).

Deliberately the standard library: a dozen endpoints do not justify a web
framework as a dependency.
"""

from __future__ import annotations

import dataclasses
import json
import mimetypes
import secrets
import threading
import time
from datetime import date, datetime
from enum import Enum
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pydantic import BaseModel

from agentos.repositories.tasks import TaskNotFound
from agentos.services.agents import AgentBusy
from agentos.services.tasks import InvalidTaskTransition
from agentos.tui.attention import merge_items, sort_items
from agentos.tui.changes import AgentChanges, ChangesReader
from agentos.tui.snapshot import Snapshot, SnapshotReader

STATIC_DIR = Path(__file__).parent / "static"
CHANGES_SECONDS = 5.0
LOG_TAIL_BYTES = 64_000
MAX_DIFF_CHARS = 400_000


# ---------------------------------------------------------------- serializing


def to_json(value: Any) -> Any:
    """Plain JSON from the dataclasses, pydantic models and enums we hand out."""
    if isinstance(value, BaseModel):
        return to_json(value.model_dump(mode="json"))
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        data = {f.name: to_json(getattr(value, f.name)) for f in dataclasses.fields(value)}
        # Computed properties the UI needs, which asdict() would drop.
        for name in ("added", "removed", "new_files", "shared", "churn", "label", "spelled"):
            attr = getattr(type(value), name, None)
            if isinstance(attr, property):
                data[name] = to_json(getattr(value, name))
        if isinstance(value, AgentChanges):
            data["areas"] = to_json(value.areas)
        return data
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_json(v) for v in value]
    return value


# ------------------------------------------------------------------ the state


class GuiState:
    """Everything the handlers share, behind one lock."""

    def __init__(self, reader: SnapshotReader, changes: ChangesReader | None = None) -> None:
        self.reader = reader
        self.changes_reader = changes or ChangesReader(reader.paths, reader.config)
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.RLock()
        self.changes: list[AgentChanges] = []
        self.changes_error: str | None = None
        self._stop = threading.Event()

    # The git scan is slow next to a database read, so it runs on its own
    # thread and the snapshot serves the latest result, as the TUI does.
    def start_scanning(self) -> None:
        threading.Thread(target=self._scan_loop, name="changes-scan", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def scan_once(self) -> None:
        try:
            found = self.changes_reader.read()
        except Exception as exc:  # git trouble must not take the server down
            self.changes_error = str(exc)
            return
        self.changes, self.changes_error = found, None

    def _scan_loop(self) -> None:
        while not self._stop.is_set():
            self.scan_once()
            self._stop.wait(CHANGES_SECONDS)

    def snapshot(self) -> dict:
        with self.lock:
            snap: Snapshot = self.reader.read()
            providers = self.reader.providers()
        attention = sort_items(snap.attention + merge_items(self.changes))
        data = to_json(snap)
        data["attention"] = to_json(attention)
        data["changes"] = to_json(self.changes)
        data["changes_error"] = self.changes_error
        data["providers"] = to_json(providers)
        data["counts"] = {
            "tasks": len(snap.tasks),
            "completed": snap.completed,
            "failed": len(snap.failed),
            "blocked": len(snap.blocked),
            "need_you": len(attention),
        }
        data["generated_at"] = time.time()
        return data

    def work_log(self) -> dict:
        logs = sorted(self.reader.paths.logs_dir.glob("work-*.log"))
        if not logs:
            return {"path": None, "text": ""}
        latest = logs[-1]
        with open(latest, "rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - LOG_TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
        return {"path": str(latest), "text": text, "truncated": size > LOG_TAIL_BYTES}

    def diff(self, agent: str) -> dict:
        """The full diff behind one row of the changes panel."""
        import asyncio

        from agentos.tui.changes import SHARED

        git = self.changes_reader.git
        if agent == SHARED:
            where = self.reader.paths.root
        else:
            where = (self.reader.paths.worktrees_dir / agent).resolve()
            if not where.is_dir() or where.parent != self.reader.paths.worktrees_dir.resolve():
                raise LookupError(f"{agent} has no worktree")
        entry = next((c for c in self.changes if c.agent == agent), None)
        base = entry.base if entry else "HEAD"

        async def gather() -> str:
            fork = await git._run("merge-base", base, "HEAD", cwd=where, check=False)
            start = fork.text if fork.ok and fork.text else "HEAD"
            result = await git._run("diff", "--no-color", start, cwd=where, check=False)
            return result.stdout

        text = asyncio.run(gather())
        untracked = [f.path for f in (entry.files if entry else []) if f.is_new]
        return {
            "agent": agent,
            "text": text[:MAX_DIFF_CHARS],
            "truncated": len(text) > MAX_DIFF_CHARS,
            "new_files": untracked,
        }

    def review(self, agent: str) -> dict:
        """One change source as structured files, for the code review view."""
        import asyncio

        from agentos.branding import CONFIG_FILENAME
        from agentos.gui.review import new_file_diff, parse_commits, parse_unified_diff
        from agentos.tui.changes import SHARED

        paths = self.reader.paths
        git = self.changes_reader.git
        if agent == SHARED:
            where = paths.root
        else:
            where = (paths.worktrees_dir / agent).resolve()
            if not where.is_dir() or where.parent != paths.worktrees_dir.resolve():
                raise LookupError(f"{agent} has no worktree")
        entry = next((c for c in self.changes if c.agent == agent), None)
        base = entry.base if entry else "HEAD"

        # The orchestrator's own files are not the agents' work.
        ours = tuple(
            f"{p.relative_to(paths.root).as_posix()}/"
            for p in (paths.worktrees_dir, paths.state_dir)
        )

        def mine(path: str) -> bool:
            normal = path.replace("\\", "/")
            return not (agent == SHARED and (normal.startswith(ours) or normal == CONFIG_FILENAME))

        async def gather() -> tuple[str, str, str]:
            fork = await git._run("merge-base", base, "HEAD", cwd=where, check=False)
            start = fork.text if fork.ok and fork.text else "HEAD"
            diff = await git._run(
                "diff", "--no-color", "--find-renames", "-U3", start, cwd=where, check=False
            )
            untracked = await git._run(
                "ls-files", "--others", "--exclude-standard", cwd=where, check=False
            )
            log = ""
            if agent != SHARED:
                commits = await git._run(
                    "log", "--format=%h%x09%s", "-n", "50", f"{base}..HEAD",
                    cwd=where, check=False,
                )
                log = commits.stdout
            return diff.stdout, untracked.stdout, log

        diff_text, untracked_text, log_text = asyncio.run(gather())
        files = [f for f in parse_unified_diff(diff_text) if mine(f.path)]
        files += [
            new_file_diff(where, rel)
            for rel in untracked_text.splitlines()
            if rel.strip() and mine(rel)
        ]
        files.sort(key=lambda f: f.path)
        return {
            "agent": agent,
            "base": base,
            "branch": entry.branch if entry else None,
            "commits": parse_commits(log_text),
            "files": to_json(files),
        }


# ------------------------------------------------------------------- routing


class ApiError(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


TASK_ACTIONS = {
    "unblock": "unblock",
    "retry": "retry",
    "hold": "hold",
    "cancel": "cancel",
}
AGENT_ACTIONS = {"pause": "pause", "resume": "resume"}


def handle_api(state: GuiState, method: str, path: str, body: dict) -> Any:
    parts = [unquote(p) for p in path.strip("/").split("/")][1:]  # drop "api"

    if method == "GET":
        if parts == ["snapshot"]:
            return state.snapshot()
        if parts == ["work", "log"]:
            return state.work_log()
        if len(parts) == 2 and parts[0] in ("diff", "review"):
            try:
                return state.diff(parts[1]) if parts[0] == "diff" else state.review(parts[1])
            except LookupError as exc:
                raise ApiError(HTTPStatus.NOT_FOUND, str(exc)) from exc
        raise ApiError(HTTPStatus.NOT_FOUND, f"no such endpoint: {path}")

    if method != "POST":
        raise ApiError(HTTPStatus.METHOD_NOT_ALLOWED, method)

    reader = state.reader
    try:
        with state.lock:
            if len(parts) == 3 and parts[0] == "tasks" and parts[2] in TASK_ACTIONS:
                task = getattr(reader, TASK_ACTIONS[parts[2]])(parts[1])
                return {"ok": True, "task": to_json(task)}
            if len(parts) == 3 and parts[0] == "agents" and parts[2] in AGENT_ACTIONS:
                agent = getattr(reader, AGENT_ACTIONS[parts[2]])(parts[1])
                return {"ok": True, "agent": to_json(agent)}
            if parts == ["work", "start"]:
                log = reader.start_work()
                return {"ok": True, "log": str(log)}
            if parts == ["work", "stop"]:
                reader.stop_work()
                return {"ok": True}
            if parts == ["provider"]:
                name = str(body.get("name") or "")
                status = reader.switch_provider(name)
                return {"ok": True, "provider": to_json(status)}
    except (InvalidTaskTransition, AgentBusy, ValueError, RuntimeError) as exc:
        raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
    except (TaskNotFound, LookupError) as exc:
        raise ApiError(HTTPStatus.NOT_FOUND, str(exc)) from exc
    raise ApiError(HTTPStatus.NOT_FOUND, f"no such endpoint: {path}")


def make_handler(state: GuiState, static_dir: Path = STATIC_DIR):
    allowed_hosts: set[str] = set()

    class Handler(BaseHTTPRequestHandler):
        server_version = "agentos-gui"

        def log_message(self, *_args) -> None:  # keep the terminal quiet
            pass

        # ------------------------------------------------------------ checks

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            return host in allowed_hosts

        def _token_ok(self) -> bool:
            header = self.headers.get("Authorization") or ""
            supplied = header.removeprefix("Bearer ").strip()
            return bool(supplied) and secrets.compare_digest(supplied, state.token)

        # ------------------------------------------------------------ verbs

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def _dispatch(self, method: str) -> None:
            if not self._host_ok():
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "unexpected Host header"})
                return
            path = urlparse(self.path).path
            if path.startswith("/api/"):
                self._api(method, path)
            elif method == "GET":
                self._static(path)
            else:
                self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": method})

        def _api(self, method: str, path: str) -> None:
            if not self._token_ok():
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "missing or wrong token"})
                return
            body: dict = {}
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    try:
                        body = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
                    except json.JSONDecodeError:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid JSON"})
                        return
            try:
                result = handle_api(state, method, path, body)
            except ApiError as exc:
                self._send_json(exc.status, {"error": str(exc)})
                return
            except Exception as exc:  # a bug in one call must not kill the server
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            self._send_json(HTTPStatus.OK, result)

        def _static(self, path: str) -> None:
            if not (static_dir / "index.html").is_file():
                self._send_text(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "The GUI has not been built. Run `npm install` and `npm run build` "
                    "in the gui/ folder of the agentos repository.",
                )
                return
            relative = path.lstrip("/") or "index.html"
            target = (static_dir / relative).resolve()
            # Anything outside the static folder, or unknown, gets the app shell.
            if static_dir.resolve() not in target.parents or not target.is_file():
                target = static_dir / "index.html"
            data = target.read_bytes()
            kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self._security_headers()
            self.end_headers()
            self.wfile.write(data)

        # ----------------------------------------------------------- output

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")

        def _send_json(self, status: HTTPStatus, payload: Any) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self._security_headers()
            self.end_headers()
            self.wfile.write(data)

        def _send_text(self, status: HTTPStatus, text: str) -> None:
            data = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self._security_headers()
            self.end_headers()
            self.wfile.write(data)

    def allow_port(port: int) -> None:
        allowed_hosts.update({f"127.0.0.1:{port}", f"localhost:{port}"})

    Handler.allow_port = staticmethod(allow_port)  # type: ignore[attr-defined]
    return Handler


def create_server(
    state: GuiState, port: int = 0, static_dir: Path = STATIC_DIR
) -> ThreadingHTTPServer:
    """Bind to loopback. Port 0 picks a free one."""
    handler = make_handler(state, static_dir)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    handler.allow_port(server.server_address[1])
    return server


def url_for(server: ThreadingHTTPServer, state: GuiState) -> str:
    port = server.server_address[1]
    # The token rides in the fragment: never sent to a server, never logged,
    # and the app moves it into sessionStorage and clears it from the address.
    return f"http://127.0.0.1:{port}/#token={state.token}"

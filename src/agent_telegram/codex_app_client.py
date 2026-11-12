"""Minimal client for the Codex Desktop app-tools MCP over its named pipe.

Runs the node and codex-app-tools server.mjs bundled with Codex Desktop against
the app's dedicated codex-app-tools-* pipe (with codex-browser-use-* as a legacy
fallback) and calls the thread tools for one thread,
after checking the thread against Codex's state database. The message is sent
as JSON over stdin, so quoting and command-line length never matter.
"""

from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from agent_telegram.i18n import tr
from agent_telegram import __version__
from agent_telegram.outputs import atomic_write_json, read_json
from agent_telegram.settings import codex_home, var_dir

PIPE_ROOT = "\\\\.\\pipe\\"
# Codex Desktop 26.1013 exposes the app-tools host on a dedicated
# ``codex-app-tools-<uuid>`` pipe. Older builds used the browser-use prefix;
# keep accepting it because the bundled server can still be paired with those
# pipes on older or partially upgraded clients.
_PIPE_RE = re.compile(r"^(?:codex-app-tools|codex-browser-use)-[0-9a-fA-F-]{36}$")
REQUIRED_TOOLS = ("read_thread", "navigate_to_codex_page", "send_message_to_thread")


class AppToolsError(RuntimeError):
    pass


def _is_app_tools_pipe(name: str) -> bool:
    return name.startswith("codex-app-tools-")


def list_pipes() -> list[str]:
    try:
        names = os.listdir(PIPE_ROOT)
    except OSError:
        return []
    # Prefer the dedicated app-tools host. Browser-use pipes commonly answer
    # initialize but reject the host-side tools/list request, which used to
    # make the watcher report a misleading "No handler" failure even when the
    # real app-tools pipe was available.
    return sorted(
        (name for name in names if _PIPE_RE.fullmatch(name)),
        key=lambda name: (not _is_app_tools_pipe(name), name),
    )


def find_node() -> Path | None:
    preferred = Path.home() / ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    if preferred.is_file():
        return preferred
    runtimes = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/runtimes/cua_node"
    candidates = sorted(runtimes.glob("*/bin/node.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def find_server() -> Path | None:
    root = codex_home() / "plugins/cache/openai-bundled/codex-app-tools"
    candidates = sorted(root.glob("*/server.mjs"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def availability() -> dict[str, Any]:
    node, server = find_node(), find_server()
    return {"node": str(node) if node else None, "app_tools_server": str(server) if server else None,
            "pipes": len(list_pipes())}


def tool_text(result: dict[str, Any]) -> str:
    return "\n".join(
        str(part.get("text") or "")
        for part in result.get("content") or []
        if isinstance(part, dict) and part.get("type") == "text"
    )


class AppToolsSession:
    """One JSON-RPC session with a child app-tools server (newline-delimited stdio)."""

    def __init__(self, command: list[str], *, pipe: str, cwd: Path | None = None,
                 env: dict[str, str] | None = None, timeout_s: float = 10.0) -> None:
        self.command = command
        self.pipe = pipe
        self.cwd = cwd
        self.env = env
        self.timeout_s = timeout_s
        self.tool_names: set[str] = set()
        self.thread_probe: dict[str, Any] | None = None  # open_session's read_thread answer
        self._process: subprocess.Popen[bytes] | None = None
        self._lines: queue.Queue[bytes | None] = queue.Queue()
        self._next_id = 0

    def __enter__(self) -> AppToolsSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def start(self) -> AppToolsSession:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._process = subprocess.Popen(
            self.command,
            cwd=str(self.cwd) if self.cwd else None,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        threading.Thread(target=self._read_loop, name="app-tools-reader", daemon=True).start()
        self.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {},
             "clientInfo": {"name": "agent_telegram", "version": __version__}},
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        listed = self.request("tools/list", {})
        self.tool_names = {str(tool.get("name")) for tool in listed.get("tools") or [] if isinstance(tool, dict)}
        return self

    def _read_loop(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        for line in self._process.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _send(self, message: dict[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise AppToolsError("app-tools session is not started")
        try:
            self._process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            self._process.stdin.flush()
        except OSError as exc:
            raise AppToolsError(f"app-tools stdin closed: {exc}") from exc

    def request(self, method: str, params: dict[str, Any], *, timeout_s: float | None = None) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + (timeout_s or self.timeout_s)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AppToolsError(tr('{v0} timed out (pipe {v1})', v0=method, v1=self.pipe))
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if line is None:
                raise AppToolsError(tr('app-tools process exited (pipe {v0})', v0=self.pipe))
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(message, dict) or message.get("id") != request_id:
                continue  # notifications or unrelated responses
            if message.get("error"):
                error = message["error"]
                detail = error.get("message") if isinstance(error, dict) else error
                raise AppToolsError(tr('{v0} failed: {v1}', v0=method, v1=detail))
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def call_tool(self, name: str, arguments: dict[str, Any], *, thread_id: str | None = None,
                  timeout_s: float | None = None) -> dict[str, Any]:
        """``thread_id`` is the executor/caller; targets belong in ``arguments``."""
        params: dict[str, Any] = {"name": name, "arguments": arguments}
        if thread_id:
            params["_meta"] = {
                "openai/threadId": thread_id,
                "x-codex-turn-metadata": {"thread_id": thread_id, "turn_id": f"agent_telegram-{uuid.uuid4()}"},
            }
        result = self.request("tools/call", params, timeout_s=timeout_s)
        if result.get("isError"):
            raise AppToolsError(tr('Codex app tool {v0} returned an error: {v1}', v0=name, v1=tool_text(result)))
        return result

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _cache_path() -> Path:
    return var_dir() / "codex-app-pipe.json"


def _cached_pipe() -> str | None:
    try:
        return str(read_json(_cache_path()).get("pipe") or "") or None
    except (OSError, ValueError, AttributeError):
        return None


def open_session(*, thread_id: str | None = None, timeout_s: float = 30.0,
                 caller_host_id: str | None = None) -> AppToolsSession:
    """Return a started session on the first pipe that exposes the thread tools.

    Most codex-browser-use-* pipes serve browser tools and already fail tools/list;
    the dedicated codex-app-tools-* pipe answers the same request and exposes the
    thread tools. The
    probe avoids list_threads, which can hang for minutes when the app's thread index
    holds a damaged row. Every app tool call requires executor thread metadata, so
    the probe runs only when a thread is known.
    """

    node, server = find_node(), find_server()
    if node is None or server is None:
        raise AppToolsError(
            tr("Cannot find Codex Desktop's bundled node or codex-app-tools server.mjs; make sure Codex Desktop is installed and running.")
        )
    deadline = time.monotonic() + timeout_s
    last_error = tr('No codex-app-tools-* or codex-browser-use-* pipe')
    while True:
        pipes = list_pipes()
        cached = _cached_pipe()
        if cached in pipes:
            pipes.remove(cached)
            # A stale browser-use cache must not delay the dedicated app-tools
            # pipe. Keep cache priority when it is itself the new pipe, or
            # when this client is running against an older Desktop with no
            # dedicated pipe available.
            if _is_app_tools_pipe(cached) or not any(_is_app_tools_pipe(pipe) for pipe in pipes):
                pipes.insert(0, cached)
        for pipe in pipes:
            env = dict(os.environ)
            env["CODEX_APP_TOOLS_PIPE_PATH"] = PIPE_ROOT + pipe
            if caller_host_id is not None:
                env["CODEX_APP_TOOLS_CALLER_HOST_ID"] = caller_host_id
            session = AppToolsSession([str(node), str(server)], pipe=pipe, cwd=server.parent, env=env, timeout_s=8.0)
            try:
                session.start()
                missing = [name for name in REQUIRED_TOOLS if name not in session.tool_names]
                if missing:
                    raise AppToolsError(tr('Missing tools {v0}', v0=missing))
                if thread_id:
                    session.thread_probe = session.call_tool(
                        "read_thread", {"threadId": thread_id, "turnLimit": 1}, thread_id=thread_id, timeout_s=8.0
                    )
            except (AppToolsError, OSError) as exc:
                last_error = f"{pipe}: {exc}"
                session.close()
                continue
            atomic_write_json(_cache_path(), {"pipe": pipe})
            return session
        if time.monotonic() >= deadline:
            raise AppToolsError(tr('No usable Codex app-tools pipe found (is Codex Desktop running?). Last error: {v0}', v0=last_error))
        time.sleep(1.0)


def send_message(
    thread_id: str,
    message: str,
    *,
    model: str | None = None,
    thinking: str | None = None,
    host_id: str = "local",
    timeout_s: float = 30.0,
    navigate: bool = True,
) -> dict[str, Any]:
    """Send one user message to the thread, opening it in Codex Desktop first.

    ``model``/``thinking`` are send_message_to_thread's optional overrides
    (for example ``gpt-6-sol`` and ``high``); None keeps the thread's settings.
    ``navigate=False`` leaves the visible conversation alone; the app delivers
    the message to a background thread just the same.
    """

    arguments: dict[str, Any] = {"threadId": thread_id, "prompt": message, "hostId": host_id}
    if model:
        arguments["model"] = model
    if thinking:
        arguments["thinking"] = thinking
    with open_session(thread_id=thread_id, timeout_s=timeout_s) as session:
        if navigate:
            session.call_tool("navigate_to_codex_page", {"threadId": thread_id}, thread_id=thread_id, timeout_s=20.0)
        sent = session.call_tool("send_message_to_thread", arguments, thread_id=thread_id, timeout_s=60.0)
        return {"pipe": session.pipe, "result_text": tool_text(sent)[:2000]}


_ACTIVE_STATES = frozenset({"active", "running", "inprogress", "in_progress"})


def app_reports_busy(thread_id: str, *, timeout_s: float = 30.0) -> bool | None:
    """Ask Codex Desktop whether the thread is running a turn; None when unknown.

    A turn cut short by an app restart stays open in the rollout forever; the app
    itself reports such a thread as notLoaded/idle with an interrupted turn.
    """

    try:
        with open_session(thread_id=thread_id, timeout_s=timeout_s) as session:
            # open_session already probed the pipe with this very call; one read_thread per check.
            result = session.thread_probe or session.call_tool(
                "read_thread", {"threadId": thread_id, "turnLimit": 1}, thread_id=thread_id, timeout_s=20.0
            )
        data = json.loads(tool_text(result))
    except (AppToolsError, OSError, ValueError):
        return None
    thread = data.get("thread") if isinstance(data.get("thread"), dict) else {}
    status = thread.get("status") if isinstance(thread.get("status"), dict) else {}
    turns = data.get("turns") if isinstance(data.get("turns"), list) else []
    last_turn = turns[0] if turns and isinstance(turns[0], dict) else {}
    states = {str(status.get("type") or "").lower(), str(last_turn.get("status") or "").lower()}
    return bool(states & _ACTIVE_STATES)


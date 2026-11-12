"""Loopback HTTP endpoint for the Claude Code hooks installed by hooks_install.

Routes (POST bodies are the hook input JSON; responses are hook output JSON):
    /claude/event        from hook_report (SessionStart, Stop, StopFailure); answered at once
    /claude/pretooluse   http PreToolUse hook: {"continue": false, ...} while a stop is pending
    /claude/permission   http PermissionRequest hook: may block for hours until the owner answers in
                         Telegram; the 200 and the headers go out at once, then a space every few
                         seconds (Claude Code's HTTP client drops idle connections; it trims the body
                         before parsing it) and finally the JSON answer
    GET /status          counters for install_task.ps1 (no secrets, no token needed)

Only 127.0.0.1 is bound, with SO_EXCLUSIVEADDRUSE so no second process can share the
port. Requests with a foreign Host header (DNS rebinding) or any Origin header (a web
page) are refused, and hook routes need the X-TgBridge-Token header. Bodies are never
logged.
"""

from __future__ import annotations

import json
import math
import secrets
import socket
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
from urllib.parse import urlparse

TOKEN_HEADER = "X-TgBridge-Token"
TIMEOUT_HEADER = "X-TgBridge-Timeout"  # the PermissionRequest hook's own timeout (s), set by hooks_install
KEEPALIVE_S = 5.0
MAX_BODY = 32 * 1024 * 1024  # PreToolUse carries Write/Edit contents
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")


class HookHandler(Protocol):
    def claude_event(self, payload: dict[str, Any]) -> None: ...

    def claude_pretooluse(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def claude_permission(self, payload: dict[str, Any], *, gone: threading.Event | None = None,
                          hook_timeout_s: float | None = None) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...


def _loopback_host(value: str, port: int) -> bool:
    host = value.strip().lower()
    return any(host in (name, f"{name}:{port}") for name in _LOOPBACK_HOSTS)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False
    handler: HookHandler
    token: str
    log: Callable[[str, str], None]

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def handle_error(self, request: Any, client_address: Any) -> None:
        import sys
        import traceback

        if isinstance(sys.exc_info()[1], (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return  # a client (e.g. a /status probe or a timed-out hook) hung up: nothing to report
        self.log(f"hook server error from {client_address}:\n{traceback.format_exc()}", "ERROR")


class _RequestHandler(BaseHTTPRequestHandler):
    server: _Server
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        pass  # the stdlib writes to stderr, which pythonw does not have

    def _reply(self, code: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _allowed(self, *, token: bool) -> bool:
        host = self.headers.get("Host")
        if host is not None and not _loopback_host(host, int(self.server.server_address[1])):
            self._reply(403, {"error": "forbidden_host"})
            return False
        if self.headers.get("Origin") is not None:
            self._reply(403, {"error": "forbidden_origin"})
            return False
        if token:
            given = self.headers.get(TOKEN_HEADER) or ""
            if not secrets.compare_digest(given.encode("utf-8"), self.server.token.encode("utf-8")):
                self._reply(403, {"error": "forbidden_token"})
                return False
        return True

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        if not self._allowed(token=False):
            return
        if urlparse(self.path).path == "/status":
            self._reply(200, self.server.handler.status())
        else:
            self._reply(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib name
        if not self._allowed(token=True):
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self._reply(413, {"error": "body_too_large"})
            self.close_connection = True
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._reply(400, {"error": "bad_json"})
            return
        if not isinstance(payload, dict):
            self._reply(400, {"error": "bad_json"})
            return
        path = urlparse(self.path).path
        handler = self.server.handler
        try:
            if path == "/claude/event":
                handler.claude_event(payload)
                self._reply(200, {})
            elif path == "/claude/pretooluse":
                self._reply(200, handler.claude_pretooluse(payload))
            elif path == "/claude/permission":
                self._permission(handler, payload)
                return
            else:
                self._reply(404, {"error": "not_found"})
        except Exception as exc:  # noqa: BLE001 - an empty answer lets Claude Code continue normally
            self.server.log(f"hook {path} failed: {type(exc).__name__}: {exc}", "ERROR")
            self._reply(200, {})

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
        self.wfile.flush()

    def _permission(self, handler: HookHandler, payload: dict[str, Any]) -> None:
        """Answer a permission request that may take hours, keeping the connection busy meanwhile."""

        try:
            hook_timeout: float | None = float(self.headers.get(TIMEOUT_HEADER) or 0)
        except ValueError:
            hook_timeout = None
        if not hook_timeout or not math.isfinite(hook_timeout) or hook_timeout <= 0:
            hook_timeout = None
        gone = threading.Event()  # Claude Code hung up (the turn was interrupted, the session closed)
        done = threading.Event()
        result: dict[str, Any] = {}

        def run() -> None:
            try:
                result["answer"] = handler.claude_permission(payload, gone=gone, hook_timeout_s=hook_timeout)
            except Exception as exc:  # noqa: BLE001 - an empty answer leaves the request to the dialog
                self.server.log(f"hook /claude/permission failed: {type(exc).__name__}: {exc}", "ERROR")
            finally:
                done.set()

        threading.Thread(target=run, name="tgbridge-permission", daemon=True).start()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.flush()
            while not done.wait(KEEPALIVE_S):
                self._chunk(b" ")
            self._chunk(json.dumps(result.get("answer") or {}, ensure_ascii=False).encode("utf-8"))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except OSError:  # reset, aborted or broken pipe: nobody waits for the answer any more
            gone.set()
            self.close_connection = True


class HookServer:
    def __init__(self, port: int, token: str, handler: HookHandler, *,
                 log: Callable[[str, str], None] = lambda _text, _level: None) -> None:
        self.port = port
        self._server = _Server(("127.0.0.1", port), _RequestHandler, bind_and_activate=False)
        self._server.handler = handler
        self._server.token = token
        self._server.log = log
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Bind (OSError when the port is taken, e.g. by a second bridge) and serve in a daemon thread."""

        try:
            self._server.server_bind()
            self._server.server_activate()
        except OSError:
            self._server.server_close()
            raise
        self._thread = threading.Thread(target=self._server.serve_forever, name="tgbridge-hooks", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
        self._server.server_close()

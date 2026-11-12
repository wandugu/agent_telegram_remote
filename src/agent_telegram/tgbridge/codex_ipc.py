"""Codex Desktop's window-coordination IPC (``\\\\.\\pipe\\codex-ipc``), for writes app-tools lack.

Codex Desktop windows coordinate through a local router: every frame is a 4-byte little-endian
length and UTF-8 JSON with a ``type`` of request, response, broadcast, client-discovery-request
or client-discovery-response. A client registers with an ``initialize`` request; a window
following a thread receives the owner's state snapshot (broadcast thread-stream-state-changed),
and asks the owning window to act through ``thread-follower-*`` requests, which the router
routes to the window that claims the thread. Checked against the Desktop 26.1007 bundle.

The bridge uses read-only snapshots and five writes, all on local threads:

* answering a pending ``request_user_input`` (the questions Codex asks in plan mode): the
  snapshot's ``requests`` hold the app-server request id for the tool call, and
  ``thread-follower-submit-user-input`` answers it as the Desktop's own question panel does;
* "implement this plan": ``thread-follower-update-thread-settings`` switches the thread back to
  Default mode, as the Desktop does before it sends the plan;
* ``thread-follower-steer-turn`` sends only async-question answer envelopes to an active turn;
* ``thread-follower-command-approval-decision`` and ``thread-follower-file-approval-decision``
  answer a pending command or file approval after checking its identity and content.

Methods carry the version the Desktop expects; a newer Desktop that changed one answers
no-client-found or request-version-mismatch, never with a different write. Following a thread
lasts only until its snapshot arrived.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from agent_telegram.i18n import tr
from agent_telegram.codex_threads import question_reply_entries

PIPE_PATH = "\\\\.\\pipe\\codex-ipc"
CLIENT_TYPE = "tgbridge"
LOCAL_HOST = "local"
INITIALIZING = "initializing-client"
MAX_FRAME = 256 * 1024 * 1024  # the router's own limit

USER_INPUT_METHOD = "item/tool/requestUserInput"
PLAN_METHOD = "item/plan/requestImplementation"
SUBMIT_USER_INPUT = "thread-follower-submit-user-input"
UPDATE_THREAD_SETTINGS = "thread-follower-update-thread-settings"
STEER_TURN = "thread-follower-steer-turn"
COMMAND_APPROVAL = "thread-follower-command-approval-decision"
FILE_APPROVAL = "thread-follower-file-approval-decision"
COMMAND_APPROVAL_METHOD = "item/commandExecution/requestApproval"
FILE_APPROVAL_METHOD = "item/fileChange/requestApproval"
PERMISSIONS_APPROVAL_METHOD = "item/permissions/requestApproval"
ELICITATION_METHOD = "mcpServer/elicitation/request"
APPROVAL_METHODS = frozenset({COMMAND_APPROVAL_METHOD, FILE_APPROVAL_METHOD,
                              PERMISSIONS_APPROVAL_METHOD, ELICITATION_METHOD})
FOLLOWING_CHANGED = "thread-stream-following-changed"
STATE_CHANGED = "thread-stream-state-changed"
# Versions from the Desktop's table (a request without hostId uses the plain number).
VERSIONS = {"initialize": 0, SUBMIT_USER_INPUT: 1, UPDATE_THREAD_SETTINGS: 2, FOLLOWING_CHANGED: 1,
            STEER_TURN: 1, COMMAND_APPROVAL: 1, FILE_APPROVAL: 1}

_ERRORS = {
    "no-client-found": 'No Codex Desktop window owns this conversation (it is not open, or an update changed the internal protocol)',
    "request-version-mismatch": "Codex Desktop's internal protocol version does not match (it may have updated)",
    "request-timeout": 'Codex Desktop did not respond in time',
    "client-disconnected": 'The Codex Desktop window disconnected',
    "server-closed": "Codex Desktop's IPC is closed",
    "no-handler-for-request": 'Codex Desktop does not support this operation',
}


def _translated_errors() -> dict[str, str]:
    return {
        'no-client-found': tr('No Codex Desktop window owns this conversation (it is not open, or an update changed the internal protocol)'),
        'request-version-mismatch': tr("Codex Desktop's internal protocol version does not match (it may have updated)"),
        'request-timeout': tr('Codex Desktop did not respond in time'),
        'client-disconnected': tr('The Codex Desktop window disconnected'),
        'server-closed': tr("Codex Desktop's IPC is closed"),
        'no-handler-for-request': tr('Codex Desktop does not support this operation'),
    }

_ERROR_PIPE_BUSY = 231
_BUSY_SLEEP_S = 0.1
_POLL_S = 0.02
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
# The router may identify the bridge's user but never impersonate it.
_SECURITY_SQOS_PRESENT = 0x00100000
_SECURITY_IDENTIFICATION = 0x00010000

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    # Private DLL object: argtypes set here never leak into other modules' ctypes use.
    _kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
        wintypes.HANDLE,
    ]  # fmt: skip
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.PeekNamedPipe.argtypes = [
        wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID, ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]  # fmt: skip
    _kernel32.PeekNamedPipe.restype = wintypes.BOOL
    _kernel32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                                   wintypes.LPVOID]
    _kernel32.ReadFile.restype = wintypes.BOOL
    _kernel32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                                    ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _kernel32.WriteFile.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
else:  # pragma: no cover - the bridge targets Windows
    _kernel32 = None
    _INVALID_HANDLE = None


class CodexIpcError(RuntimeError):
    """The IPC step failed; the message is shown to the Telegram user (Chinese)."""

    def __init__(self, message: str, *, code: Any = None) -> None:
        super().__init__(message)
        self.code = code


class TurnEnded(CodexIpcError):
    """The question no longer belongs to the active turn."""

    def __init__(self, *, code: Any = None) -> None:
        super().__init__(tr('This Codex Desktop turn has ended, or the question is no longer present'), code=code)


class AlreadyAnswered(CodexIpcError):
    """An answer is present/in flight, or the approval is no longer the pending one."""

    def __init__(self, mine: bool) -> None:
        super().__init__(tr('This question or approval has already been handled, or is being handled'))
        self.mine = mine


class OutcomeUnknown(CodexIpcError):
    """A sent write may have completed; callers must reconcile before retrying."""

    def __init__(self, message: str | None = None, *, code: Any = None) -> None:
        super().__init__(message if message is not None else tr('Codex Desktop may have received this; the outcome cannot be confirmed yet'), code=code)


def _unknown_outcome(code: Any) -> bool:
    return isinstance(code, str) and (code.endswith("-timeout") or code == "client-disconnected")


def describe_error(code: Any) -> str:
    text = str(code or tr('Unknown error'))
    if text in _translated_errors():
        return _translated_errors()[text]
    if text.endswith("-timeout"):
        return tr('Codex Desktop did not respond in time; it may have received this operation')
    return text


# --- transport -------------------------------------------------------------------------------


class PipeTransport:
    """Client end of the router's pipe: blocking writes, reads polled with PeekNamedPipe.

    One thread uses it at a time (a synchronous pipe handle serializes its I/O anyway).
    """

    def __init__(self, path: str = PIPE_PATH, *, timeout_s: float = 5.0) -> None:
        if _kernel32 is None:  # pragma: no cover - the bridge targets Windows
            raise CodexIpcError(tr('Codex Desktop IPC is supported only on Windows'))
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            handle = _kernel32.CreateFileW(path, _GENERIC_READ | _GENERIC_WRITE, 0, None, _OPEN_EXISTING,
                                           _SECURITY_SQOS_PRESENT | _SECURITY_IDENTIFICATION, None)
            if handle and handle != _INVALID_HANDLE:
                break
            code = ctypes.get_last_error()
            if code == 2:
                raise CodexIpcError(tr('Cannot find Codex Desktop IPC (is Codex Desktop running?)'))
            if code != _ERROR_PIPE_BUSY or time.monotonic() >= deadline:
                raise CodexIpcError(tr('Cannot connect to Codex Desktop IPC (Windows error {v0})', v0=code))
            time.sleep(_BUSY_SLEEP_S)
        self._handle: Any = handle

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = wintypes.DWORD(0)
            chunk = bytes(view[: 1 << 20])
            if not _kernel32.WriteFile(self._handle, chunk, len(chunk), ctypes.byref(written), None) or not written.value:
                raise CodexIpcError(tr('Failed to write to Codex Desktop IPC (Windows error {v0})', v0=ctypes.get_last_error()))
            view = view[written.value:]

    def read(self, timeout_s: float) -> bytes:
        """What arrived within timeout_s (b"" when nothing did); CodexIpcError when the pipe closed."""

        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            available = wintypes.DWORD(0)
            if not _kernel32.PeekNamedPipe(self._handle, None, 0, None, ctypes.byref(available), None):
                raise CodexIpcError(tr('Codex Desktop closed the IPC connection'), code="client-disconnected")
            if available.value:
                buffer = ctypes.create_string_buffer(available.value)
                got = wintypes.DWORD(0)
                if not _kernel32.ReadFile(self._handle, buffer, available.value, ctypes.byref(got), None):
                    raise CodexIpcError(tr('Failed to read Codex Desktop IPC'), code="client-disconnected")
                return buffer.raw[: got.value]
            if time.monotonic() >= deadline:
                return b""
            time.sleep(_POLL_S)

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle:
            _kernel32.CloseHandle(handle)


# --- client ----------------------------------------------------------------------------------


def encode(message: dict[str, Any]) -> bytes:
    body = json.dumps(message, ensure_ascii=False).encode("utf-8", errors="replace")
    return struct.pack("<I", len(body)) + body


class IpcClient:
    """One short session with the router: initialize, read a thread's state, send requests."""

    def __init__(self, transport: Any, *, client_type: str = CLIENT_TYPE,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.transport = transport
        self.client_type = client_type
        self.clock = clock
        self.client_id = INITIALIZING
        self._buffer = bytearray()
        self._following: set[tuple[str, str]] = set()

    def __enter__(self) -> IpcClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        for conversation_id, host_id in list(self._following):
            try:
                self._follow(conversation_id, host_id, False)
            except Exception:  # noqa: BLE001 - the router drops a closed client's follows anyway
                pass
        try:
            self.transport.close()
        except Exception:  # noqa: BLE001
            pass

    # framing

    def _send(self, message: dict[str, Any]) -> None:
        self.transport.write(encode(message))

    def _take_frame(self) -> dict[str, Any] | None:
        while len(self._buffer) >= 4:
            (size,) = struct.unpack("<I", self._buffer[:4])
            if size == 0 or size > MAX_FRAME:
                raise CodexIpcError(tr('Codex Desktop IPC sent an invalid frame ({v0} bytes)', v0=size))
            if len(self._buffer) < 4 + size:
                return None
            body = bytes(self._buffer[4 : 4 + size])
            del self._buffer[: 4 + size]
            try:
                message = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(message, dict):
                return message
        return None

    def _next(self, deadline: float) -> dict[str, Any] | None:
        while True:
            message = self._take_frame()
            if message is not None:
                return message
            remaining = deadline - self.clock()
            if remaining <= 0:
                return None
            self._buffer += self.transport.read(min(remaining, 1.0))

    def _side_message(self, message: dict[str, Any]) -> bool:
        """Answer what the router asks every client; True when the message is consumed."""

        kind = message.get("type")
        if kind == "client-discovery-request":
            # This client handles nothing: owners of threads answer their own requests.
            self._send({"type": "client-discovery-response", "requestId": message.get("requestId"),
                        "response": {"canHandle": False}})
            return True
        if kind == "request":
            self._send({"type": "response", "requestId": message.get("requestId"), "resultType": "error",
                        "error": "no-handler-for-request"})
            return True
        return False

    def _wait(self, match: Callable[[dict[str, Any]], bool], timeout_s: float, what: str) -> dict[str, Any]:
        deadline = self.clock() + timeout_s
        while True:
            message = self._next(deadline)
            if message is None:
                raise CodexIpcError(tr('{v0} timed out', v0=what), code="response-wait-timeout")
            if self._side_message(message):
                continue
            if match(message):
                return message

    # protocol

    def initialize(self, *, timeout_s: float = 5.0) -> str:
        request_id = str(uuid.uuid4())
        self._send({"type": "request", "requestId": request_id, "sourceClientId": INITIALIZING,
                    "version": VERSIONS["initialize"], "method": "initialize",
                    "params": {"clientType": self.client_type}})
        reply = self._wait(lambda item: item.get("type") == "response" and item.get("requestId") == request_id,
                           timeout_s, tr('Connecting to Codex Desktop IPC '))
        result = reply.get("result") if reply.get("resultType") == "success" else None
        client_id = result.get("clientId") if isinstance(result, dict) else None
        if not isinstance(client_id, str) or not client_id:
            raise CodexIpcError(tr('Codex Desktop IPC refused the connection: {v0}', v0=describe_error(reply.get('error'))))
        self.client_id = client_id
        return client_id

    def _broadcast(self, method: str, params: dict[str, Any]) -> None:
        self._send({"type": "broadcast", "method": method, "sourceClientId": self.client_id,
                    "version": VERSIONS[method], "params": params})

    def _follow(self, conversation_id: str, host_id: str, following: bool) -> None:
        self._broadcast(FOLLOWING_CHANGED, {"conversationId": conversation_id, "hostId": host_id,
                                            "following": following})
        if following:
            self._following.add((conversation_id, host_id))
        else:
            self._following.discard((conversation_id, host_id))

    def snapshot(self, conversation_id: str, *, host_id: str = LOCAL_HOST, timeout_s: float = 10.0) -> dict[str, Any]:
        """The owning window's state of the thread (follows it until the snapshot arrived)."""

        def is_snapshot(message: dict[str, Any]) -> bool:
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            change = params.get("change") if isinstance(params.get("change"), dict) else {}
            return (message.get("type") == "broadcast" and message.get("method") == STATE_CHANGED
                    and params.get("conversationId") == conversation_id and change.get("type") == "snapshot")

        self._follow(conversation_id, host_id, True)
        try:
            message = self._wait(is_snapshot, timeout_s, tr('Waiting for Codex Desktop to send the conversation state'))
        except CodexIpcError as exc:
            raise CodexIpcError(tr('{v0}: no Codex Desktop window has this conversation open, or the internal protocol changed', v0=exc)) from None
        finally:
            self._follow(conversation_id, host_id, False)
        state = message["params"]["change"].get("conversationState")
        if not isinstance(state, dict) or not isinstance(state.get("requests"), list):
            raise CodexIpcError(tr('Unrecognized conversation state from Codex Desktop (it may have updated)'))
        return state

    def request(self, method: str, params: dict[str, Any], *, timeout_s: float = 15.0) -> Any:
        """Send a request to whichever window claims it; its result, or CodexIpcError."""

        request_id = str(uuid.uuid4())
        self._send({"type": "request", "requestId": request_id, "sourceClientId": self.client_id,
                    "version": VERSIONS[method], "method": method, "params": params,
                    "timeoutMs": int(timeout_s * 1000)})
        try:
            reply = self._wait(lambda item: item.get("type") == "response" and item.get("requestId") == request_id,
                               timeout_s + 5.0, tr('Waiting for Codex Desktop to handle the request'))
        except CodexIpcError as exc:
            if _unknown_outcome(exc.code):
                raise OutcomeUnknown(str(exc), code=exc.code) from exc
            raise
        if reply.get("resultType") != "success":
            code = reply.get("error")
            error_type = OutcomeUnknown if _unknown_outcome(code) else CodexIpcError
            raise error_type(describe_error(code), code=code)
        return reply.get("result")


def connect(*, timeout_s: float = 5.0) -> IpcClient:
    """An initialized client on the local router; CodexIpcError when Codex Desktop is not there."""

    client = IpcClient(PipeTransport(timeout_s=timeout_s))
    try:
        client.initialize(timeout_s=timeout_s)
    except BaseException:
        client.close()
        raise
    return client


# --- snapshots and writes --------------------------------------------------------------------


def turns(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Turns in Desktop order, from either the plain list or canonical history islands."""

    history = state.get("turnHistory")
    if isinstance(history, dict) and history.get("kind") == "canonical":
        history = history.get("history")
        if not isinstance(history, dict):
            return []
        entities = history.get("entitiesByKey")
        islands = history.get("islands")
        if not isinstance(entities, dict) or not isinstance(islands, list):
            return []
        result = []
        for island in islands:
            entries = island.get("entries") if isinstance(island, dict) else None
            if not isinstance(entries, list):
                continue
            for entry in entries:
                key = entry.get("value") if isinstance(entry, dict) else None
                turn = entities.get(key) if isinstance(key, (str, int)) else None
                if isinstance(turn, dict):
                    result.append(turn)
        return result
    plain = state.get("turns")
    return [turn for turn in plain if isinstance(turn, dict)] if isinstance(plain, list) else []


def latest_turn(state: dict[str, Any]) -> dict[str, Any] | None:
    """Desktop's latest turn: skip only terminal placeholders without a turn id."""

    return next((turn for turn in reversed(turns(state))
                 if turn.get("turnId") is not None or turn.get("status") not in ("completed", "failed")), None)


def _items(turn: dict[str, Any]) -> list[dict[str, Any]]:
    items = turn.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _submission_texts(value: Any, depth: int = 0) -> Iterator[str]:
    """Inspect only text/input/prompt-like fields and their known containers, with a depth cap.

    Unrecognized unconfirmed-submission shapes are skipped rather than guessed to be answers.
    """

    if depth > 6:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and (key.lower().replace("_", "").endswith(("input", "text", "prompt"))
                                         or key in ("content", "restoreMessage", "context", "message")):
                yield from _submission_texts(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _submission_texts(child, depth + 1)


def _unconfirmed_answers(state: dict[str, Any], call_id: str) -> dict[int, dict[str, Any]]:
    # 26.1007 commonly stores only delivery ids/stage here, with the input in an optimistic
    # steeringUserMessage instead. Metadata-only entries cannot identify a call and are skipped.
    submissions = state.get("unconfirmedTurnSubmissions")
    if not isinstance(submissions, list):
        return {}
    answers: dict[int, dict[str, Any]] = {}
    for submission in submissions:
        if not isinstance(submission, dict):
            continue
        client_id = submission.get("clientUserMessageId", submission.get("clientId"))
        restore = submission.get("restoreMessage")
        if client_id is None and isinstance(restore, dict):
            client_id = restore.get("id")
        for text in _submission_texts(submission):
            for answer_call, index, answer in question_reply_entries(text):
                if answer_call == call_id:
                    answers.setdefault(index, {"answer": answer, "client_id": client_id,
                                               "status": submission.get("status", "pending"),
                                               "kind": "unconfirmedTurnSubmission"})
    return answers


def question_answers(turn: dict[str, Any], call_id: str) -> dict[int, dict[str, Any]]:
    """Answered/in-flight indices in one turn, using only single-text message items.

    Values contain answer, client_id, status (None when absent), and the item's kind. Every
    steering status counts, including pending. Unconfirmed submissions attached to the turn
    are also recognized when their input/text/prompt shape is understood; steer_question_reply
    separately inspects the snapshot's state-level submissions without changing the snapshot.
    """

    answers: dict[int, dict[str, Any]] = {}
    for item in _items(turn):
        kind = item.get("type")
        if kind not in ("userMessage", "steeringUserMessage"):
            continue
        content = item.get("content" if kind == "userMessage" else "input")
        if not isinstance(content, list) or len(content) != 1:
            continue
        part = content[0]
        if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
            continue
        for answer_call, index, answer in question_reply_entries(part["text"]):
            if answer_call == call_id:
                answers.setdefault(index, {"answer": answer,
                                           "client_id": item.get("clientId" if kind == "userMessage"
                                                                 else "clientUserMessageId"),
                                           "status": item.get("status"), "kind": kind})
    for index, answer in _unconfirmed_answers(turn, call_id).items():
        answers.setdefault(index, answer)
    return answers


def steer_question_reply(conversation_id: str, *, turn_id: str, call_id: str, indices: Iterable[int],
                         envelope: str, client_id: str, cwd: str,
                         connect: Callable[[], IpcClient] = connect) -> dict[str, Any]:
    """Steer an active local turn with an async-question answer envelope, never arbitrary input.

    Returns {turn_id: str, other_turn: bool}. Desktop wraps its steer result in ``result``;
    a top-level string turnId is also accepted defensively. An uncertain write is never retried.
    """

    wanted = set(indices)
    entries = question_reply_entries(envelope)
    if (not entries or any(type(index) is not int for index in wanted)
            or any(answer_call != call_id or index not in wanted for answer_call, index, _answer in entries)):
        raise ValueError(tr('Only an answer envelope matching this question and its indices may be sent'))
    with connect() as client:
        state = client.snapshot(conversation_id)
        turn = latest_turn(state)
        if (turn is None or turn.get("turnId") != turn_id or turn.get("status") != "inProgress"
                or not any(item.get("type") == "agentMessage" and item.get("id") == call_id
                           for item in _items(turn))):
            raise TurnEnded()
        existing = question_answers(turn, call_id)
        for index, answer in _unconfirmed_answers(state, call_id).items():
            existing.setdefault(index, answer)
        collisions = [existing[index] for index in wanted if index in existing]
        if collisions:
            raise AlreadyAnswered(mine=all(answer["client_id"] == client_id for answer in collisions))
        try:
            result = client.request(STEER_TURN, {
                "conversationId": conversation_id, "clientUserMessageId": client_id,
                "input": [{"type": "text", "text": envelope, "text_elements": []}],
                "restoreMessage": {
                    "id": client_id, "text": envelope, "createdAt": int(time.time() * 1000), "cwd": cwd,
                    "context": {"prompt": envelope, "turnTrigger": "send_user_message_async_question",
                                "addedFiles": [], "fileAttachments": [], "imageAttachments": [],
                                "commentAttachments": [], "ideContext": None, "workspaceRoots": [cwd],
                                "collaborationMode": state.get("latestCollaborationMode")},
                    "responsesapiClientMetadata": None,
                },
                "serviceTier": None, "attachments": [], "additionalContext": None, "toolOutput": None,
            })
        except CodexIpcError as exc:
            text = str(exc).lower()
            if "active turn already ended" in text or "no active turn to steer" in text:
                raise TurnEnded(code=exc.code) from exc
            if isinstance(exc, OutcomeUnknown) or _unknown_outcome(exc.code):
                raise OutcomeUnknown(str(exc), code=exc.code) from exc
            raise
    steered = None
    if isinstance(result, dict):
        nested = result.get("result")
        if isinstance(nested, dict) and isinstance(nested.get("turnId"), str):
            steered = nested["turnId"]
        elif isinstance(result.get("turnId"), str):
            steered = result["turnId"]
    if steered is None:
        raise CodexIpcError(tr('Codex Desktop did not confirm receiving the answer'))
    return {"turn_id": steered, "other_turn": steered != turn_id}


def find_approval_requests(state: dict[str, Any]) -> list[dict[str, Any]]:
    """Supported pending approvals, preserving each request and its exact JSON-RPC id type."""

    requests = state.get("requests")
    if not isinstance(requests, list):
        return []
    return [request for request in requests if isinstance(request, dict)
            and isinstance(request.get("method"), str) and request["method"] in APPROVAL_METHODS
            and isinstance(request.get("params"), dict)]


def approval_digest(request: dict[str, Any]) -> str:
    """Bind a decision to the method and complete params displayed for this approval."""

    payload = json.dumps({"method": request.get("method"), "params": request.get("params")},
                         sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def approval_details(state: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    """Display fields for an approval; file paths come from the matching fileChange item."""

    params = request.get("params")
    params = params if isinstance(params, dict) else {}
    kinds = {COMMAND_APPROVAL_METHOD: "command", FILE_APPROVAL_METHOD: "file",
             PERMISSIONS_APPROVAL_METHOD: "permissions", ELICITATION_METHOD: "elicitation"}
    method = request.get("method")
    kind = kinds.get(method) if isinstance(method, str) else None
    command = params.get("command")
    command = " ".join(str(arg) for arg in command) if isinstance(command, list) else command
    files = []
    cwd = params.get("cwd")
    for turn in turns(state):
        if turn.get("turnId") != params.get("turnId"):
            continue
        # Desktop 26.1007's approval display uses the matched turn's params.cwd.
        turn_params = turn.get("params")
        if cwd is None and isinstance(turn_params, dict):
            cwd = turn_params.get("cwd")
        if kind == "file":
            for item in _items(turn):
                if item.get("type") != "fileChange" or item.get("id") != params.get("itemId"):
                    continue
                changes = item.get("changes")
                if isinstance(changes, list):
                    files.extend(change["path"] for change in changes
                                 if isinstance(change, dict) and isinstance(change.get("path"), str))
    decisions = params.get("availableDecisions")
    return {"kind": kind, "item_id": params.get("itemId"), "turn_id": params.get("turnId"),
            "approval_id": params.get("approvalId"), "reason": params.get("reason"),
            "command": command if isinstance(command, str) else "", "cwd": cwd,
            "network": params.get("networkApprovalContext") or None,
            "extra_permissions": params.get("additionalPermissions") or None,
            "available_decisions": decisions if isinstance(decisions, list) else None, "files": files}


def decide_approval(conversation_id: str, *, method: str, item_id: str, turn_id: str,
                    approval_id: str | None, digest: str, decision: str,
                    connect: Callable[[], IpcClient] = connect) -> None:
    """Decide one unchanged local command/file approval and verify its removal from the snapshot."""

    if decision not in ("accept", "decline"):
        raise ValueError(tr('An approval decision must be accept or decline'))
    if method not in (COMMAND_APPROVAL_METHOD, FILE_APPROVAL_METHOD):
        raise ValueError(tr('Only command or file approvals can be handled'))
    with connect() as client:
        state = client.snapshot(conversation_id)
        pending = next((request for request in find_approval_requests(state)
                        if request["method"] == method and request["params"].get("itemId") == item_id
                        and request["params"].get("turnId") == turn_id
                        and (approval_id is None or request["params"].get("approvalId") == approval_id)
                        and approval_digest(request) == digest), None)
        if pending is None:
            raise AlreadyAnswered(mine=False)
        decisions = pending["params"].get("availableDecisions")
        if isinstance(decisions, list) and decision not in decisions:
            raise CodexIpcError(tr('Codex Desktop does not allow this approval decision'))
        uncertain = None
        try:
            client.request(COMMAND_APPROVAL if method == COMMAND_APPROVAL_METHOD else FILE_APPROVAL,
                           {"conversationId": conversation_id, "requestId": pending["id"], "decision": decision})
        except CodexIpcError as exc:
            if not isinstance(exc, OutcomeUnknown) and not _unknown_outcome(exc.code):
                raise
            uncertain = OutcomeUnknown(str(exc), code=exc.code)
        try:
            after = client.snapshot(conversation_id)
        except CodexIpcError as exc:
            # The decision went out; only its check failed, so it may well have been taken.
            raise (uncertain or OutcomeUnknown(code=exc.code)) from exc
        if any(type(request.get("id")) is type(pending["id"]) and request.get("id") == pending["id"]
               and request["params"].get("itemId") == item_id for request in find_approval_requests(after)):
            if uncertain is not None:
                raise uncertain
            raise CodexIpcError(tr('Codex Desktop did not accept this decision'))


def _requests(state: dict[str, Any], method: str) -> list[dict[str, Any]]:
    return [item for item in state.get("requests") or []
            if isinstance(item, dict) and item.get("method") == method and isinstance(item.get("params"), dict)]


def find_user_input_request(state: dict[str, Any], *, call_id: str) -> dict[str, Any] | None:
    """The pending request_user_input for the tool call ``call_id``.

    The app-server gives the request the call id as its item id (``item_id: call_id``); nothing
    else identifies it: two calls with the same questions stay apart.
    """

    if not call_id:
        return None
    for item in _requests(state, USER_INPUT_METHOD):
        if str(item["params"].get("itemId") or "") == call_id:
            return item
    return None


def answer_user_input(conversation_id: str, *, call_id: str, answers: dict[str, list[str]],
                      connect: Callable[[], IpcClient] = connect) -> None:
    """Answer the thread's pending request_user_input for ``call_id`` (question id -> answers).

    The answer has the Desktop panel's shape: an option label or the typed text, one per answered
    question; unanswered questions are left out.
    """

    response = {"answers": {qid: {"answers": [str(value) for value in values]}
                            for qid, values in answers.items() if values}}
    with connect() as client:
        state = client.snapshot(conversation_id)
        pending = find_user_input_request(state, call_id=call_id)
        if pending is None:
            raise CodexIpcError(tr('This question is no longer pending in Codex Desktop (it may have been answered on the PC, or the turn has ended)'))
        result = client.request(SUBMIT_USER_INPUT, {"conversationId": conversation_id, "requestId": pending.get("id"),
                                                    "response": response})
    if not (isinstance(result, dict) and result.get("ok") is True):
        raise CodexIpcError(tr('Codex Desktop did not confirm receiving the answer'))


def find_plan_request(state: dict[str, Any], *, turn_id: str) -> dict[str, Any] | None:
    for item in _requests(state, PLAN_METHOD):
        if str(item["params"].get("turnId") or "") == turn_id:
            return item
    return None


def prepare_plan_implementation(conversation_id: str, *, turn_id: str,
                                check: Callable[[str], None] | None = None,
                                connect: Callable[[], IpcClient] = connect) -> str:
    """Switch a thread that offers "implement this plan" for ``turn_id`` to Default mode.

    Returns the plan text the Desktop would send; the caller sends it. Nothing changes when the
    Desktop no longer offers that plan, or when ``check(plan)`` raises (the caller refusing to
    send it): its exception propagates before the switch.
    """

    with connect() as client:
        state = client.snapshot(conversation_id)
        offer = find_plan_request(state, turn_id=turn_id)
        if offer is None:
            raise CodexIpcError(tr('Codex Desktop is not waiting to implement this plan (it may have been handled, or a newer turn has started)'))
        plan = str(offer["params"].get("planContent") or "").strip()
        if not plan:
            raise CodexIpcError(tr('This plan is empty in Codex Desktop'))
        mode = state.get("latestCollaborationMode")
        settings = mode.get("settings") if isinstance(mode, dict) else None
        if not isinstance(settings, dict):
            raise CodexIpcError(tr("Cannot read the conversation's collaboration mode (Codex Desktop may have updated)"))
        if check is not None:
            check(plan)
        if mode.get("mode") != "default":
            target = {"mode": "default", "settings": {**settings, "developer_instructions": None}}
            result = client.request(UPDATE_THREAD_SETTINGS, {"conversationId": conversation_id,
                                                             "threadSettings": {"collaborationMode": target}})
            if not (isinstance(result, dict) and result.get("applied") is True):
                raise CodexIpcError(tr('Codex Desktop did not switch the conversation back to default mode'))
    return plan

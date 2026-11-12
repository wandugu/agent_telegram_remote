"""Deliver a message into a live Claude Code session through its cross-session inbox.

Every live session listens on a named pipe `\\\\.\\pipe\\LOCAL\\cc-msg-<hex>` and accepts
line-delimited JSON: an auth line with the session's own messaging token, then a
user message. An idle session starts a new turn; a busy one reads the message
between tool calls. The token comes from the session's hooks and is a secret:
it never appears in errors or logs.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from typing import Any

from agent_telegram.i18n import tr
PIPE_RE = re.compile(r"^\\\\\.\\pipe\\LOCAL\\cc-msg-[0-9a-f]{16,64}$", re.IGNORECASE)
MAX_TEXT = 100_000
_ERROR_PIPE_BUSY = 231
_BUSY_SLEEP_S = 0.1

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    # Private DLL object: argtypes set here never leak into other modules' ctypes use.
    _kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
        wintypes.HANDLE,
    ]  # fmt: skip
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _INVALID_HANDLE = ctypes.c_void_p(-1).value
else:  # pragma: no cover - the bridge targets Windows
    _kernel32 = None
    _INVALID_HANDLE = None

_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_OPEN_EXISTING = 3
# The pipe server may identify the bridge's user but never impersonate it.
_SECURITY_SQOS_PRESENT = 0x00100000
_SECURITY_IDENTIFICATION = 0x00010000


class InboxError(RuntimeError):
    """Delivery failed; the message is shown to the Telegram user (Chinese)."""


def normalize(socket: str) -> str:
    """The pipe path of a messaging socket value (`uds:` prefix allowed); InboxError when it is not one."""

    value = (socket or "").strip() if isinstance(socket, str) else ""
    if value[:4].lower() == "uds:":
        value = value[4:].strip()
    if not PIPE_RE.fullmatch(value):
        raise InboxError(tr('Invalid session inbox address'))
    return value


# Claude Code's queue priority for the message (read from the user frame, "next" when absent):
# "next" joins a running turn after its current tool calls (steer), "later" waits for the turn to
# end and runs as a new one (queue), "now" interrupts the turn first. An idle session starts a turn.
PRIORITIES = ("next", "later", "now")


def _payload(token: str, text: str, priority: str = "next") -> bytes:
    frame: dict[str, Any] = {"type": "user", "message": {"role": "user", "content": text}}
    if priority != "next":  # "next" is what Claude Code assumes when the field is absent
        frame["priority"] = priority
    lines = (
        json.dumps({"type": "auth", "token": token}, ensure_ascii=False),
        json.dumps(frame, ensure_ascii=False),
    )
    # Lone surrogates (possible in decoded Telegram JSON) cannot be encoded; replace them.
    return "".join(line + "\n" for line in lines).encode("utf-8", errors="replace")


def _open_pipe(path: str, mode: str = "r+b", buffering: int = 0) -> Any:
    """open() for a named-pipe client that keeps the Windows error code.

    The builtin open() goes through the CRT, which reports ERROR_PIPE_BUSY as a bare
    EINVAL (no winerror), so the busy retry could never see it. CreateFileW keeps
    winerror 231 (and 2 -> FileNotFoundError, 5 -> PermissionError).
    """

    if _kernel32 is None:  # pragma: no cover - the bridge targets Windows
        return open(path, mode, buffering=buffering)
    handle = _kernel32.CreateFileW(
        path,
        _GENERIC_READ | _GENERIC_WRITE,
        0,
        None,
        _OPEN_EXISTING,
        _SECURITY_SQOS_PRESENT | _SECURITY_IDENTIFICATION,
        None,
    )
    if not handle or handle == _INVALID_HANDLE:
        code = ctypes.get_last_error()
        raise OSError(0, ctypes.FormatError(code).strip(), path, code)
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDWR)
    except OSError:
        _kernel32.CloseHandle(handle)
        raise
    try:
        return open(fd, mode, buffering=buffering)
    except BaseException:
        os.close(fd)
        raise


def _describe(exc: OSError) -> str:
    code = getattr(exc, "winerror", None) or exc.errno
    return f"{type(exc).__name__} {code}" if code else type(exc).__name__


def _open(path: str, timeout_s: float, opener: Callable[..., Any]) -> Any:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while True:
        try:
            return opener(path, "r+b", buffering=0)
        except FileNotFoundError:
            raise InboxError(tr('The session inbox does not exist (the session may have closed)')) from None
        except OSError as exc:
            if getattr(exc, "winerror", None) != _ERROR_PIPE_BUSY:
                if isinstance(exc, PermissionError):
                    raise InboxError(tr('Cannot open the session inbox (permission denied)')) from exc
                raise InboxError(tr('Cannot open the session inbox ({v0})', v0=_describe(exc))) from exc
            if time.monotonic() >= deadline:
                raise InboxError(tr('The session inbox stayed busy; try again later')) from exc
            time.sleep(_BUSY_SLEEP_S)


def _write_all(pipe: Any, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = pipe.write(view)
        if not written:
            raise OSError("pipe accepted no data")
        view = view[written:]


def post(
    socket: str, token: str, text: str, *, timeout_s: float = 5.0, opener: Callable[..., Any] = _open_pipe,
    priority: str = "next",
) -> None:
    """Write the auth line and one user message to the session inbox, then close the pipe.

    `opener` is called like open(path, "r+b", buffering=0); the default keeps the
    Windows error code so a busy pipe (winerror 231) is retried until timeout_s.
    `priority` is one of PRIORITIES.
    """

    if priority not in PRIORITIES:
        raise InboxError(tr('Unknown delivery mode: {v0}', v0=priority))
    path = normalize(socket)
    if not isinstance(token, str) or not token.strip():
        raise InboxError(tr('Missing session inbox token'))
    if not isinstance(text, str) or not text.strip():
        raise InboxError(tr('Message is empty'))
    if len(text) > MAX_TEXT:
        raise InboxError(tr('Message is too long ({v0} characters, limit {v1})', v0=len(text), v1=MAX_TEXT))
    data = _payload(token, text, priority)
    pipe = _open(path, timeout_s, opener)
    try:
        _write_all(pipe, data)
    except OSError as exc:
        raise InboxError(tr('Failed to write to the session inbox ({v0})', v0=_describe(exc))) from exc
    finally:
        try:
            pipe.close()
        except OSError:
            pass

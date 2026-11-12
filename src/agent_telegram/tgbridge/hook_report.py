"""Claude Code command hook that forwards SessionStart / Stop / StopFailure to the bridge.

Installed by hooks_install as an exec-form async hook:

    <venv>\\Scripts\\pythonw.exe -X utf8 -P -m agent_telegram.tgbridge.hook_report

Claude Code runs it for every session on this machine, so it must stay cheap and
invisible: only the standard library and agent_telegram.paths are imported (no
httpx), nothing is ever written to stdout/stderr, every failure is swallowed and
the exit code is always 0. The hook input JSON arrives on stdin; a trimmed copy
plus the session's inbox socket/token (from the environment) is POSTed to the
bridge's loopback hook server, bypassing any proxy.

Kill switches (exit 0 without doing anything): ``<var>/tgbridge/hooks.disabled``
exists, or ``config.json`` / ``hook_token.txt`` is missing there.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from agent_telegram.paths import gate_path, var_dir

DEFAULT_PORT = 8767
TOKEN_HEADER = "X-TgBridge-Token"
MAX_STDIN = 8 * 1024 * 1024
MAX_MESSAGE_CHARS = 20_000
TIMEOUT_S = 3.0

# Hook input fields forwarded as-is (strings among them are capped at MAX_MESSAGE_CHARS).
_FIELDS = (
    "session_id",
    "transcript_path",
    "cwd",
    "permission_mode",
    "agent_id",
    "agent_type",
    "source",
    "last_assistant_message",
    "error",
    "error_details",
    "stop_hook_active",
    "model",
)

Post = Callable[[str, bytes, dict[str, str], float], Any]


def tg_dir() -> Path:
    return var_dir() / "tgbridge"


def _read_stdin(limit: int = MAX_STDIN) -> bytes:
    """Up to limit+1 bytes of stdin; empty when there is no usable stdin (pythonw)."""

    stream = getattr(sys.stdin, "buffer", None)
    chunks: list[bytes] = []
    size = 0
    while size <= limit:
        want = min(1024 * 1024, limit + 1 - size)
        try:
            chunk = stream.read(want) if stream is not None else os.read(0, want)
        except (OSError, ValueError):
            break
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _hook_port(config_path: Path) -> int:
    try:
        value = json.loads(config_path.read_text(encoding="utf-8-sig")).get("hook_port", DEFAULT_PORT)
    except (OSError, ValueError, AttributeError):
        return DEFAULT_PORT
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 65536:
        return value
    return DEFAULT_PORT


def _capped(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_MESSAGE_CHARS:
        return value[:MAX_MESSAGE_CHARS]
    return value


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry32W)]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                     ctypes.POINTER(wintypes.DWORD)]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _process_parent(pid: int) -> tuple[int, str] | None:
    """(parent pid, full image path) of a live Windows process; None when unknown.

    ctypes only (stdlib), using a module-private WinDLL instance so the argtypes set
    here never leak into other users of ctypes.windll.kernel32 in the same process.
    """
    if sys.platform != "win32":
        return None
    snapshot = _kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return None
    parent: int | None = None
    try:
        entry = _ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(_ProcessEntry32W)
        found = _kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32ProcessID == pid:
                parent = int(entry.th32ParentProcessID)
                break
            found = _kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(snapshot)
    if parent is None:
        return None
    handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not _kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return parent, buffer.value
    finally:
        _kernel32.CloseHandle(handle)


def _same_file(first: str, second: str) -> bool:
    if os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second)):
        return True
    try:
        return os.path.samefile(first, second)
    except (OSError, ValueError):
        return False


def _hook_parent_pid() -> int:
    """PID of the process that started this hook (Claude Code).

    A venv's ``Scripts\\pythonw.exe`` is a redirector that runs the base interpreter as its
    child, so ``os.getppid()`` here is that short-lived redirector. When the parent's image
    is our own ``sys.executable`` (the redirector's path) step over it to its parent. Any
    doubt falls back to ``os.getppid()``.
    """

    ppid = os.getppid()
    if sys.platform != "win32" or sys.prefix == sys.base_prefix:
        return ppid
    try:
        info = _process_parent(ppid)
        if info is not None and info[0] > 0 and _same_file(info[1], sys.executable):
            return info[0]
    except Exception:  # noqa: BLE001 - the literal parent is still a usable answer
        pass
    return ppid


def build_payload(data: dict[str, Any], environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The JSON body for /claude/event from the hook input and the hook's environment.

    ``ppid`` is Claude Code's PID (the venv redirector in between is skipped, see
    ``_hook_parent_pid``), matching ``~/.claude/sessions/<pid>.json``.
    """

    env = os.environ if environ is None else environ
    payload: dict[str, Any] = {"event": data.get("hook_event_name")}
    for name in _FIELDS:
        payload[name] = _capped(data.get(name))
    payload["ppid"] = _hook_parent_pid()
    payload["entrypoint"] = env.get("CLAUDE_CODE_ENTRYPOINT", "")
    payload["headless"] = env.get("AGENT_TELEGRAM_HEADLESS") == "1"
    pipe = env.get("CLAUDE_CODE_MESSAGING_SOCKET", "")
    token = env.get("CLAUDE_CODE_MESSAGING_TOKEN", "")
    if pipe and token:
        payload["inbox"] = {"socket": pipe, "token": token}
    return payload


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # the hook server never redirects; never carry the token elsewhere


def http_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
    """POST without any proxy (ProxyHandler({}) ignores HTTP(S)_PROXY) and without redirects."""

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with opener.open(request, timeout=timeout) as response:
        response.read(64 * 1024)


def _run(stdin: bytes | None, post: Post) -> None:
    if gate_path().exists():
        return
    base = tg_dir()
    if (base / "hooks.disabled").exists():
        return
    config_path = base / "config.json"
    token_path = base / "hook_token.txt"
    if not config_path.is_file() or not token_path.is_file():
        return
    token = token_path.read_text(encoding="utf-8-sig").strip()
    if not token:
        return
    raw = _read_stdin() if stdin is None else stdin
    if not raw or len(raw) > MAX_STDIN:
        return
    data = json.loads(raw.decode("utf-8-sig", errors="replace"))
    if not isinstance(data, dict) or not data.get("hook_event_name"):
        return
    # errors="replace": a lone surrogate (legal as a \ud800 escape in the input JSON) would
    # otherwise make the encode fail and the event be lost.
    body = json.dumps(build_payload(data), ensure_ascii=False).encode("utf-8", errors="replace")
    url = f"http://127.0.0.1:{_hook_port(config_path)}/claude/event"
    headers = {"Content-Type": "application/json; charset=utf-8", TOKEN_HEADER: token}
    post(url, body, headers, TIMEOUT_S)


def main(argv: list[str] | None = None, *, stdin: bytes | None = None, post: Post | None = None) -> int:
    """Hook entry point; argv is ignored. Always returns 0 and never writes to stdout/stderr."""

    try:
        _run(stdin, post or http_post)
    except BaseException:  # noqa: BLE001 - a hook must never fail or print, whatever happens
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

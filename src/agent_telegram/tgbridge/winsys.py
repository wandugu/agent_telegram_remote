"""Windows helpers for the bridge: user presence, job objects and process-tree kills.

Presence decides whether a finished turn needs a Telegram notification (the owner
at the PC sees it locally). Job objects group the `claude -p` runs the bridge
starts so /stop can end the whole tree. They leave processes running when their
handle closes by default, so a bridge restart leaves running work alone;
`kill_on_close=True` opts into ending the whole job when the handle closes.
"""

from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_telegram import jobs

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    # Private DLL objects: argtypes set here never leak into other modules' ctypes use.
    _user32: Any = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _user32.GetLastInputInfo.argtypes = [ctypes.POINTER(_LASTINPUTINFO)]
    _user32.GetLastInputInfo.restype = wintypes.BOOL
    _user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _user32.OpenInputDesktop.restype = wintypes.HANDLE
    _user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    _user32.CloseDesktop.restype = wintypes.BOOL
    _kernel32.GetTickCount.argtypes = []
    _kernel32.GetTickCount.restype = wintypes.DWORD
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
else:  # pragma: no cover - the bridge targets Windows
    ctypes = None  # type: ignore[assignment]
    _user32 = None
    _kernel32 = None

_DESKTOP_SWITCHDESKTOP = 0x0100
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


# --- presence ---------------------------------------------------------------------------


def _idle_ms(tick: int, last_input: int) -> int:
    """Milliseconds between two GetTickCount values; the 32-bit counter wraps every 49.7 days."""

    return (int(tick) - int(last_input)) & 0xFFFFFFFF


def idle_seconds() -> float | None:
    """Seconds since the last keyboard/mouse input in this session, None when unknown."""

    if _user32 is None or _kernel32 is None:
        return None
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not _user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        return _idle_ms(_kernel32.GetTickCount(), info.dwTime) / 1000.0
    except Exception:
        return None


def workstation_locked() -> bool | None:
    """True when the input desktop cannot be opened (lock screen, secure desktop), None when unknown."""

    if _user32 is None:
        return None
    try:
        handle = _user32.OpenInputDesktop(0, False, _DESKTOP_SWITCHDESKTOP)
    except Exception:
        return None
    if not handle:
        return True
    try:
        _user32.CloseDesktop(handle)
    except Exception:
        pass
    return False


class Presence:
    """Whether the owner sits at this PC: unlocked and recent input. Unknown counts as away."""

    def __init__(
        self,
        idle_threshold_s: float,
        *,
        idle: Callable[[], float | None] = idle_seconds,
        locked: Callable[[], bool | None] = workstation_locked,
    ) -> None:
        self.idle_threshold_s = float(idle_threshold_s)
        self._idle = idle
        self._locked = locked

    def at_pc(self) -> bool:
        try:
            locked = self._locked()
            if locked is None or locked:
                return False
            idle = self._idle()
        except Exception:
            return False
        return idle is not None and idle < self.idle_threshold_s


# --- job objects and process trees ------------------------------------------------------


class JobObject:
    """An anonymous Windows job object; terminate() ends every process assigned to it.

    By default, closing the handle (also done when the object is garbage collected)
    leaves the processes running. With kill_on_close=True, closing the handle ends
    every process assigned to the job.
    """

    def __init__(self, *, kill_on_close: bool = False) -> None:
        if _kernel32 is None:
            raise OSError("job objects need Windows")
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if kill_on_close:
            limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not _kernel32.SetInformationJobObject(
                handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                error = ctypes.get_last_error()
                _kernel32.CloseHandle(handle)
                raise ctypes.WinError(error)
        self._handle: int | None = handle
        self._lock = threading.Lock()

    @property
    def closed(self) -> bool:
        return self._handle is None

    def assign(self, pid: int) -> bool:
        with self._lock:
            if self._handle is None or pid <= 0:
                return False
            process = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
            if not process:
                return False
            try:
                return bool(_kernel32.AssignProcessToJobObject(self._handle, process))
            finally:
                _kernel32.CloseHandle(process)

    def terminate(self, code: int = 1) -> bool:
        with self._lock:
            if self._handle is None:
                return False
            return bool(_kernel32.TerminateJobObject(self._handle, code))

    def close(self) -> None:
        with self._lock:
            handle, self._handle = self._handle, None
            if handle:
                _kernel32.CloseHandle(handle)

    def __del__(self) -> None:
        # Finished runs are simply dropped by the bridge; do not leak one kernel handle per run.
        # Closing follows the requested policy: kill_on_close=True intentionally ends its processes.
        try:
            self.close()
        except Exception:
            pass


def _taskkill() -> str:
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR") or r"C:\Windows"
    path = Path(system_root) / "System32" / "taskkill.exe"
    return str(path) if path.is_file() else "taskkill"


def kill_tree(pid: int, created: int | None, *, run: Callable[..., Any] = subprocess.run) -> bool:
    """Force-kill pid and its descendants, only when pid still is the process created at `created`.

    A missing creation time cannot prove the pid was not reused, so it is refused.
    """

    if pid <= 0 or pid == os.getpid() or created is None:
        return False
    if jobs.process_creation_time(pid) != created:
        return False
    try:
        result = run(
            [_taskkill(), "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0

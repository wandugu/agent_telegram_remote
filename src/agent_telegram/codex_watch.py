"""Keep local Codex Desktop tasks going after network errors.

Codex Desktop retries a turn that failed with a model-capacity error only while
that conversation is on screen (a 10/30/120/300 s countdown) and stops after four
consecutive failures, so background tasks stall until someone presses Resume.
This watcher tails the rollouts of recently active local conversations, logs
every failed turn with its time and task name, and continues the ones that
failed with a network-type error (the errors Codex itself offers Resume for, plus
gateway, stream and transport errors it reports as "other") by sending a marked
message through the app-tools pipe, without switching the visible conversation.
Repeated failures back off and continue indefinitely by default (or stop after max_attempts when configured).

    python -m agent_telegram.codex_watch watch [--dry-run]   # keep running
    python -m agent_telegram.codex_watch scan                # read-only snapshot
    python -m agent_telegram.codex_watch report [--limit N]  # recent incidents
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import re
import sqlite3
import sys
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from agent_telegram.i18n import tr
from agent_telegram import codex_app_client, codex_threads
from agent_telegram.codex_threads import AUTO_CONTINUE_MARKER, ThreadInfo, TurnCollector, TurnSummary, carries_user_input
from .settings import codex_watch_log_dir

# Logs of the separately shared codex-watch tool (its default location).
DEFAULT_LOG_DIR = codex_watch_log_dir()
DEFAULT_MESSAGE = f"{AUTO_CONTINUE_MARKER}上一轮因{{reason}}中断，请从中断处继续之前未完成的工作。"  # i18n: data

# Codex Desktop offers Resume for these failures (app 26.924, HUt); a turn the user
# stopped is resumable there too, but the watcher never continues it.
_RESUMABLE = {
    "server_overloaded": 'Model capacity exceeded',
    "internal_server_error": 'Internal server error',
    "http_connection_failed": 'Connection failed',
    "response_stream_connection_failed": 'Response stream connection failed',
    "response_stream_disconnected": 'Response stream disconnected',
    "response_too_many_failed_attempts": 'Request retry limit exceeded',
}


def _translated_resumable() -> dict[str, str]:
    return {
        'server_overloaded': tr('Model capacity exceeded'),
        'internal_server_error': tr('Internal server error'),
        'http_connection_failed': tr('Connection failed'),
        'response_stream_connection_failed': tr('Response stream connection failed'),
        'response_stream_disconnected': tr('Response stream disconnected'),
        'response_too_many_failed_attempts': tr('Request retry limit exceeded'),
    }
_OTHER_RESUMABLE_RE = re.compile(
    r"\bmodel\s+is\s+temporarily\s+unavailable\b"
    r"|^stream\s+disconnected\s+before\s+completion:\s*we['’]re\s+currently\s+processing\s+too\s+many\s+requests\b",
    re.IGNORECASE,
)
_BILLING_RE = re.compile(r"\b(?:usage|quota|rate[\s_-]+limit(?:s|ed|ing)?|billing|credits?)\b", re.IGNORECASE)
# Network failures Codex reports as "other" and offers no Resume for: a proxy's 502/503/504,
# a stream cut (also during remote compaction), transport errors.
_GATEWAY_RE = re.compile(r"\bunexpected\s+status\s+(50[234])\b", re.IGNORECASE)
_STREAM_RE = re.compile(r"\bstream\s+disconnected\b", re.IGNORECASE)
_TRANSPORT_RE = re.compile(
    r"\btransport\s+error\b|\bnetwork\s+error\b|\berror\s+sending\s+request\b"
    r"|\bconnection\s+(?:failed|reset|refused|closed|timed\s+out)\b",
    re.IGNORECASE,
)
_NOT_RESUMABLE = {"usage_limit_exceeded": 'Quota exhausted', "cyber_policy": 'Content safety policy'}


def _translated_not_resumable() -> dict[str, str]:
    return {
        'usage_limit_exceeded': tr('Quota exhausted'),
        'cyber_policy': tr('Content safety policy'),
    }

_CODEX_IMAGE_RE = re.compile(r"\\OpenAI\.Codex_[^\\]+\\app\\ChatGPT\.exe$", re.IGNORECASE)
_KEEP_TURNS = 4
_REMEMBERED_TURNS = 500


def classify(error_info: str, message: str) -> tuple[bool, str]:
    """(resumable, reason) for a failed turn: Codex Desktop's Resume rule plus gateway/transport errors."""

    if error_info in _translated_resumable():
        return True, _translated_resumable()[error_info]
    if error_info == "other" and not _BILLING_RE.search(message):
        if _OTHER_RESUMABLE_RE.search(message):
            return True, tr('Model temporarily unavailable')
        gateway = _GATEWAY_RE.search(message)
        if gateway:
            return True, tr('Gateway error {v0}', v0=gateway.group(1))
        if _STREAM_RE.search(message):
            return True, tr('Response stream disconnected')
        if _TRANSPORT_RE.search(message):
            return True, tr('Network connection error')
    return False, _translated_not_resumable().get(error_info, tr('Other error'))


def continuation_message(template: str, reason: str) -> str:
    text = template.replace("{reason}", reason).strip()
    return text if text.startswith(AUTO_CONTINUE_MARKER) else AUTO_CONTINUE_MARKER + text


def task_name(info: ThreadInfo) -> str:
    title = (info.title.strip().splitlines() or [""])[0][:80]
    return info.name.strip() or title or info.id


def watchable(info: ThreadInfo) -> bool:
    """A local Codex Desktop conversation: `codex exec` probes and CLI sessions are never continued."""

    return bool(info.rollout) and not (info.archived or info.is_subagent or info.is_headless)


# --- time helpers ---------------------------------------------------------------------


def _epoch(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def _local(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _clock(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


def _duration(seconds: float) -> str:
    hours, rest = divmod(int(max(0.0, seconds)), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return tr('{v0} hours {v1} minutes', v0=hours, v1=minutes)
    return tr('{v0} minutes {v1} seconds', v0=minutes, v1=secs) if minutes else tr('{v0} seconds', v0=secs)


# --- Codex Desktop process -------------------------------------------------------------


@functools.cache
def _win32() -> tuple[Any, Any]:
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
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

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    for name in ("Process32FirstW", "Process32NextW"):
        getattr(kernel, name).argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel, ProcessEntry


def _processes() -> list[tuple[int, int, str]]:
    """(pid, parent pid, exe name) for every process, from a Toolhelp snapshot."""

    import ctypes

    kernel, entry_type = _win32()
    snapshot = kernel.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return []
    found = []
    try:
        entry = entry_type()
        entry.dwSize = ctypes.sizeof(entry_type)
        ok = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            found.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), str(entry.szExeFile)))
            ok = kernel.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    return found


def _image_path(pid: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel, _ = _win32()
    handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        return buffer.value if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)) else ""
    finally:
        kernel.CloseHandle(handle)


def codex_main_pid() -> int | None:
    """PID of the Codex Desktop main process: the packaged ChatGPT.exe whose parent is not ChatGPT.exe."""

    if os.name != "nt":
        return None
    chatgpt = {pid: parent for pid, parent, name in _processes() if name.lower() == "chatgpt.exe"}
    mains = sorted(
        pid for pid, parent in chatgpt.items() if parent not in chatgpt and _CODEX_IMAGE_RE.search(_image_path(pid))
    )
    return mains[0] if mains else None


_DEFAULT_PROCESSES = _processes
_DEFAULT_IMAGE_PATH = _image_path


# --- rollout helpers -------------------------------------------------------------------


def read_tail(path: Path, max_bytes: int) -> tuple[list[dict[str, Any]], int]:
    """Complete records in the last ``max_bytes`` of a rollout, and the offset after them."""

    size = path.stat().st_size
    start = max(0, size - max_bytes)
    if start:
        with path.open("rb") as stream:
            stream.seek(start - 1)  # a newline right before start means start begins a line
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return [], size
                newline = chunk.find(b"\n")
                if newline >= 0:
                    start = stream.tell() - len(chunk) + newline + 1
                    break
    return codex_threads.read_records(path, start)


def trailing_failures(turns: list[TurnSummary]) -> tuple[int, float | None]:
    """Consecutive network failures that end the history (a user's new input starts the run)."""

    count, first = 0, None
    for turn in reversed(turns):
        if turn.status != "failed" or not classify(turn.error_info, turn.error)[0]:
            break
        count += 1
        first = _epoch(turn.completed_at) or first
        if carries_user_input(turn):
            break
    return count, first


# --- logs ----------------------------------------------------------------------------------


class EventLog:
    """watch.log (readable, rotated) and incidents.jsonl (one JSON object per event)."""

    def __init__(self, directory: Path, *, echo: bool = False, max_bytes: int = 1024 * 1024, backups: int = 3) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.text_path = directory / "watch.log"
        self.jsonl_path = directory / "incidents.jsonl"
        self.echo = echo
        self.max_bytes = max_bytes
        self.backups = backups

    def line(self, text: str, level: str = "INFO") -> None:
        entry = f"{datetime.now():%Y-%m-%d %H:%M:%S} {level} {text}\n"
        self._rotate()
        with self.text_path.open("a", encoding="utf-8") as stream:
            stream.write(entry)
        if self.echo and sys.stderr is not None:
            sys.stderr.write(entry)
            sys.stderr.flush()

    def incident(self, event: str, text: str, *, at: float, level: str = "INFO", **fields: Any) -> None:
        self.line(text, level)
        record = {"time": _iso(at), "event": event, "text": text}
        record.update({key: value for key, value in fields.items() if value is not None and value != ""})
        with self.jsonl_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _rotate(self) -> None:
        try:
            if self.text_path.stat().st_size < self.max_bytes:
                return
            for index in range(self.backups - 1, 0, -1):
                older = self.directory / f"watch.log.{index}"
                if older.exists():
                    os.replace(older, self.directory / f"watch.log.{index + 1}")
            os.replace(self.text_path, self.directory / "watch.log.1")
        except OSError:
            pass  # missing, or held open by a viewer; rotate next time


def report(log_dir: Path, limit: int = 20) -> list[dict[str, Any]]:
    path = log_dir / "incidents.jsonl"
    if limit <= 0 or not path.is_file():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def default_log_dir() -> Path:
    return Path(os.path.abspath(os.environ.get("CODEX_WATCH_LOG_DIR") or logs_dir() / "codex-watch"))


# Events that leave a continuation planned; their due_at is when the watcher acts on the turn next.
_PLANNED = frozenset({"failure_detected", "send_failed", "no_new_turn", "continued", "continue_deferred"})
# One watcher step past due_at: its poll over all threads, the app's busy check and the send.
_STEP_S = 300.0


def continuation_due(thread_id: str, turn_id: str, *, log_dir: Path | None = None,
                     tail_bytes: int = 256 * 1024) -> float | None:
    """Epoch by which a running watcher's continuation of a failed turn should have started.

    Taken from the newest incident about the turn, so each backoff, failed send or resend
    moves it on. None when the log plans nothing for the turn: no watcher, not seen yet,
    or skipped, stale, ignored or given up.
    """

    path = (log_dir or default_log_dir()) / "incidents.jsonl"
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, stream.seek(0, os.SEEK_END) - tail_bytes))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # the cut first line of the tail, or a line being written
        if not isinstance(record, dict) or record.get("thread_id") != thread_id or record.get("turn_id") != turn_id:
            continue
        due = _epoch(str(record.get("due_at") or "")) if record.get("event") in _PLANNED else None
        return None if due is None else due + _STEP_S
    return None


# --- watcher -------------------------------------------------------------------------------

# Wait after the n-th consecutive failure; kept off Codex's own 10/30/120/300 s retry countdown,
# and capped by DEFAULT_MAX_BACKOFF_S (0 disables the cap). Overridable via CODEX_WATCH_* or CLI.
DEFAULT_BACKOFF_S: tuple[float, ...] = (15.0, 45.0, 60.0, 120.0, 300.0)
DEFAULT_MAX_BACKOFF_S = 30.0
DEFAULT_MAX_ATTEMPTS = 0


def parse_backoff_s(raw: str | None, default: tuple[float, ...] = DEFAULT_BACKOFF_S) -> tuple[float, ...]:
    if not raw or not raw.strip():
        return default
    try:
        values = tuple(float(part.strip()) for part in raw.split(",") if part.strip())
    except ValueError:
        return default
    return values if values and all(value >= 0.0 for value in values) else default


def default_backoff_s() -> tuple[float, ...]:
    return parse_backoff_s(os.environ.get("CODEX_WATCH_BACKOFF_S"), DEFAULT_BACKOFF_S)


def default_max_backoff_s() -> float:
    raw = os.environ.get("CODEX_WATCH_MAX_BACKOFF_S")
    if raw is not None and raw.strip():
        try:
            return max(0.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_MAX_BACKOFF_S


def default_max_attempts() -> int:
    raw = os.environ.get("CODEX_WATCH_MAX_ATTEMPTS")
    if raw is not None and raw.strip():
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_MAX_ATTEMPTS


@dataclass
class WatchConfig:
    poll_s: float = 5.0
    refresh_s: float = 30.0  # re-read the thread list from the state database
    active_window_s: float = 24 * 3600.0  # tail conversations updated this recently
    tail_bytes: int = 1024 * 1024  # rollout tail read when a conversation is first tracked
    # Wait after the n-th consecutive failure; kept off Codex's own 10/30/120/300 s retry countdown.
    backoff_s: tuple[float, ...] = DEFAULT_BACKOFF_S
    max_backoff_s: float = DEFAULT_MAX_BACKOFF_S  # cap on backoff_s (0 = no cap beyond backoff_s)
    max_attempts: int = DEFAULT_MAX_ATTEMPTS  # consecutive failures after which the watcher stops continuing (0 = unlimited)
    max_age_s: float = 3600.0  # never continue a failure older than this
    delivery_timeout_s: float = 180.0  # a continuation should start a new turn within this
    max_sends_per_turn: int = 5  # continuation messages for one failed turn (send errors included)
    message: str = DEFAULT_MESSAGE
    dry_run: bool = False
    # False: only follow the turns (for on_turn_* callbacks); never plan or send continuations.
    continue_failures: bool = True

    def delay(self, attempt: int) -> float:
        wait = self.backoff_s[min(max(attempt, 1), len(self.backoff_s)) - 1]
        return min(wait, self.max_backoff_s) if self.max_backoff_s > 0 else wait


@dataclass
class Pending:
    """A failed turn waiting for its continuation."""

    turn_id: str
    reason: str
    failed_at: float
    due_at: float
    sends: int = 0
    deferred: bool = False  # "Codex is not running" was logged


@dataclass
class ThreadWatch:
    info: ThreadInfo
    path: Path
    offset: int = 0
    started: bool = False  # _start read the tail; until then _read must not read the whole file
    collector: TurnCollector = field(default_factory=TurnCollector)
    known: dict[str, None] = field(default_factory=dict)  # turn ids seen starting
    handled: dict[str, None] = field(default_factory=dict)  # turn ids whose outcome was handled
    failures: int = 0  # consecutive network failures since the last success or user input
    first_failed_at: float | None = None
    continued: int = 0  # continuation messages sent during this run of failures
    pending: Pending | None = None
    sent: Pending | None = None  # continuation sent, waiting for the new turn
    sent_at: float = 0.0

    @property
    def task(self) -> str:
        return task_name(self.info)

    def reset(self) -> None:
        self.failures, self.first_failed_at, self.continued = 0, None, 0
        self.pending = self.sent = None


def _remember(ids: dict[str, None], turn_id: str) -> None:
    ids[turn_id] = None
    if len(ids) > _REMEMBERED_TURNS:
        del ids[next(iter(ids))]


def _send_continuation(thread_id: str, text: str) -> dict[str, Any]:
    return codex_app_client.send_message(thread_id, text, navigate=False)


# Called with the conversation, the turn and the poll time after the watcher's own handling.
TurnCallback = Callable[[ThreadWatch, TurnSummary, float], None]


class Watcher:
    """One poll reads new rollout records of the tracked conversations and acts on due failures.

    on_turn_started / on_turn_finished see every turn that starts or ends after a
    conversation is first tracked (the turns already in its tail at that point are not
    reported). An exception inside a callback is logged and never stops the poll.
    """

    def __init__(
        self,
        config: WatchConfig,
        log: EventLog,
        *,
        clock: Callable[[], float] = time.time,
        send: Callable[[str, str], dict[str, Any]] = _send_continuation,
        busy_check: Callable[[str], bool | None] = codex_app_client.app_reports_busy,
        main_pid: Callable[[], int | None] = codex_main_pid,
        load_threads: Callable[[], list[ThreadInfo]] = codex_threads.load_threads,
        on_turn_started: TurnCallback | None = None,
        on_turn_finished: TurnCallback | None = None,
        collector_factory: Callable[[], TurnCollector] = TurnCollector,
    ) -> None:
        self.config = config
        self.log = log
        self.clock = clock
        self.send = send
        self.busy_check = busy_check
        self.main_pid = main_pid
        self.load_threads = load_threads
        self.on_turn_started = on_turn_started
        self.on_turn_finished = on_turn_finished
        self.collector_factory = collector_factory
        self.watches: dict[str, ThreadWatch] = {}
        self.pid: int | None = None
        self._pid_created: int | None = None
        self._pid_checked = False
        self._next_refresh = 0.0
        self._next_heartbeat = 0.0
        self._load_error: str | None = None

    def poll_once(self) -> None:
        self._check_process()
        now = self.clock()
        if now >= self._next_refresh:
            self._refresh(now)
            self._next_refresh = now + self.config.refresh_s
        for watch in list(self.watches.values()):
            self._read(watch, now)
        for watch in list(self.watches.values()):
            self._act(watch)

    def _event(self, event: str, watch: ThreadWatch, text: str, now: float, *, level: str = "INFO",
               **fields: Any) -> None:
        self.log.incident(event, f"[{watch.task}] {text}", at=now, level=level, thread_id=watch.info.id,
                          task=watch.task, cwd=codex_threads.strip_verbatim(watch.info.cwd), **fields)

    def _check_process(self) -> None:
        if (
            self._pid_checked
            and self.pid is not None
            and self._pid_created is not None
            and self.main_pid is codex_main_pid
            and _processes is _DEFAULT_PROCESSES
            and _image_path is _DEFAULT_IMAGE_PATH
        ):
            from agent_telegram import jobs
            if jobs.process_alive(self.pid, self._pid_created):
                return
        pid = self.main_pid()
        if self._pid_checked and pid == self.pid:
            return
        now = self.clock()
        if pid is not None:
            verb = tr('started') if self._pid_checked else tr('is running')
            self.log.incident("codex_started", tr('Codex {verb} (pid {pid})', verb=verb, pid=pid), at=now, pid=pid)
        elif self._pid_checked:
            self.log.incident("codex_exited", tr('Codex exited (pid {v0})', v0=self.pid), at=now, pid=self.pid)
        else:
            self.log.incident("codex_absent", tr('Codex is not running; waiting for it to start'), at=now)
        self.pid, self._pid_checked = pid, True
        if pid is not None and self.main_pid is codex_main_pid and _processes is _DEFAULT_PROCESSES and _image_path is _DEFAULT_IMAGE_PATH:
            from agent_telegram import jobs
            self._pid_created = jobs.process_creation_time(pid)
        else:
            self._pid_created = None

    # --- tracked conversations ---

    def _recent(self, info: ThreadInfo, now: float) -> bool:
        updated = _epoch(info.updated_at)
        return updated is not None and updated >= now - self.config.active_window_s

    def _refresh(self, now: float) -> None:
        try:
            threads = self.load_threads()
        except (sqlite3.Error, OSError) as exc:
            text = tr('Failed to read Codex conversations: {v0}', v0=exc)
            if text != self._load_error:
                self.log.line(text, "WARNING")
                self._load_error = text
            return
        self._load_error = None
        visible = {info.id: info for info in threads if watchable(info)}
        for thread_id, watch in list(self.watches.items()):
            info = visible.get(thread_id)
            if info is None:
                if watch.pending or watch.sent:
                    self._event("continue_skipped", watch, tr('Conversation archived or deleted; cancelling auto-continue'), now)
                del self.watches[thread_id]
                continue
            watch.info = info
            rollout = info.rollout
            if rollout is not None and rollout != watch.path and rollout.is_file():
                watch.path, watch.offset = rollout, 0  # a new rollout segment starts at byte 0
            elif not (self._recent(info, now) or watch.pending or watch.sent or self._running(watch)):
                del self.watches[thread_id]
        for thread_id, info in visible.items():
            path = info.rollout
            if thread_id in self.watches or path is None or not self._recent(info, now) or not path.is_file():
                continue
            watch = ThreadWatch(info=info, path=path, collector=self.collector_factory())
            self.watches[thread_id] = watch
            self._start(watch, now)
        if now >= self._next_heartbeat:
            first = self._next_heartbeat == 0.0
            state = f"pid {self.pid}" if self.pid else tr('stopped')
            self.log.line(tr('{v0}tracking {v1} recent conversations; Codex {v2}', v0=tr('Started ') if first else tr('Monitoring: '), v1=len(self.watches), v2=state))
            self._next_heartbeat = now + 3600.0

    @staticmethod
    def _running(watch: ThreadWatch) -> bool:
        turns = watch.collector.ordered()
        return bool(turns) and turns[-1].status == "running"

    def _start(self, watch: ThreadWatch, now: float) -> None:
        """Read the tail of a newly tracked conversation and pick up the failure it ends with."""

        try:
            records, watch.offset = read_tail(watch.path, self.config.tail_bytes)
        except OSError:
            return  # _read tries again next poll
        watch.started = True
        watch.collector.feed(records)
        turns = watch.collector.ordered()
        for turn in turns:
            _remember(watch.known, turn.turn_id)
            if turn.status != "running":
                _remember(watch.handled, turn.turn_id)
        watch.failures, watch.first_failed_at = trailing_failures(turns)
        if turns and turns[-1].status == "failed" and self.config.continue_failures:
            self._on_failure(watch, turns[-1], now, startup=True)
        watch.collector.prune(_KEEP_TURNS)

    def _read(self, watch: ThreadWatch, now: float) -> None:
        if not watch.started:
            self._start(watch, now)
            return
        try:
            size = watch.path.stat().st_size
            if size == watch.offset:
                return
            if size < watch.offset:
                # Rewritten: read only the tail again; remembered turn ids prevent repeats.
                records, watch.offset = read_tail(watch.path, self.config.tail_bytes)
            else:
                records, watch.offset = codex_threads.read_records(watch.path, watch.offset)
        except OSError:
            return
        if not records:
            return
        watch.collector.feed(records)
        for turn in watch.collector.ordered():
            if turn.turn_id not in watch.known:
                _remember(watch.known, turn.turn_id)
                self._on_started(watch, turn, now)
                self._notify(self.on_turn_started, watch, turn, now)
            if turn.status != "running" and turn.turn_id not in watch.handled:
                _remember(watch.handled, turn.turn_id)
                self._on_finished(watch, turn, now)
                self._notify(self.on_turn_finished, watch, turn, now)
        watch.collector.prune(_KEEP_TURNS)

    def _notify(self, callback: TurnCallback | None, watch: ThreadWatch, turn: TurnSummary, now: float) -> None:
        if callback is None:
            return
        try:
            callback(watch, turn, now)
        except Exception as exc:  # noqa: BLE001 - a failing callback must not stop the poll
            self.log.line(tr('[{v0}] Turn callback failed: {v1}: {v2}', v0=watch.task, v1=type(exc).__name__, v2=exc), "ERROR")

    # --- turn events ---

    def _on_started(self, watch: ThreadWatch, turn: TurnSummary, now: float) -> None:
        if watch.sent is not None:
            watch.sent = None  # the continuation turn we asked for
        elif watch.pending is not None:
            self._event("continue_skipped", watch, tr('Codex or the user continued the conversation; cancelling auto-continue'), now,
                        turn_id=watch.pending.turn_id)
            watch.pending = None

    def _on_finished(self, watch: ThreadWatch, turn: TurnSummary, now: float) -> None:
        if turn.status == "failed":
            self._on_failure(watch, turn, now)
        elif turn.status == "aborted":
            if watch.pending is not None or watch.sent is not None:
                self._event("continue_skipped", watch, tr('The user stopped this turn; cancelling auto-continue'), now, turn_id=turn.turn_id)
            watch.reset()
        else:
            if watch.failures:
                resumed = _epoch(turn.started_at) or now
                downtime = _duration(resumed - (watch.first_failed_at or resumed))
                self._event("recovered", watch, tr('Recovered: turn completed normally (interrupted for {v0}, auto-continued {v1} times)', v0=downtime, v1=watch.continued),
                            now, turn_id=turn.turn_id, attempt=watch.failures, continued=watch.continued)
            watch.reset()

    def _on_failure(self, watch: ThreadWatch, turn: TurnSummary, now: float, *, startup: bool = False) -> None:
        if not self.config.continue_failures:
            return
        resumable, reason = classify(turn.error_info, turn.error)
        failed_at = _epoch(turn.completed_at) or now
        stale = now - failed_at > self.config.max_age_s
        fields: dict[str, Any] = {"turn_id": turn.turn_id, "error_kind": turn.error_info, "reason": reason,
                                  "error": turn.error, "failed_at": _iso(failed_at)}
        where = tr('Error at {v0}', v0=_local(failed_at))
        brief = " ".join(turn.error.split())[:300]
        if not resumable:
            watch.pending = None
            if not (startup and stale):
                self._event("failure_ignored", watch, tr('Recorded error ({v0}; no auto-continue): {v1} ({v2})', v0=reason, v1=brief, v2=where),
                            now, **fields)
            return
        if not startup:  # _start counted the run of failures already
            if carries_user_input(turn) or not watch.failures:
                watch.failures, watch.first_failed_at, watch.continued = 0, failed_at, 0
            watch.failures += 1
        attempt = fields["attempt"] = watch.failures
        head = tr('Network error detected ({v0}): {v1} ({v2}, consecutive failure {v3})', v0=reason, v1=brief, v2=where, v3=attempt)
        if stale:
            watch.pending = None
            self._event("failure_stale", watch, tr('{v0}; over {v1:.0f} minutes old, no auto-continue', v0=head, v1=self.config.max_age_s / 60),
                        now, **fields)
        elif self.config.max_attempts > 0 and attempt > self.config.max_attempts:
            watch.pending = None
            self._event("gave_up", watch, tr('{v0}; more than {v1} consecutive failures, stopping auto-continue; handle manually', v0=head, v1=self.config.max_attempts),
                        now, level="WARNING", **fields)
        else:
            due = max(now, failed_at + self.config.delay(attempt))
            watch.pending = Pending(turn.turn_id, reason, failed_at, due)
            self._event("failure_detected", watch, tr('{v0}; will auto-continue at {v1}', v0=head, v1=_clock(due)), now, due_at=_iso(due),
                        **fields)

    # --- continuation ---

    def _act(self, watch: ThreadWatch) -> None:
        if not self.config.continue_failures:
            return
        now = self.clock()
        if watch.sent is not None and now - watch.sent_at >= self.config.delivery_timeout_s:
            pending, watch.sent = watch.sent, None
            if pending.sends >= self.config.max_sends_per_turn:
                self._event("gave_up", watch, tr('Sent {v0} continuation messages with no new turn; stopping auto-continue; handle manually', v0=pending.sends),
                            now, level="WARNING", turn_id=pending.turn_id)
                return
            pending.due_at = now + self.config.delay(pending.sends + 1)
            watch.pending = pending
            self._event("no_new_turn", watch, tr('No new turn within {v0:.0f} seconds of the continuation message; will resend at {v1}', v0=self.config.delivery_timeout_s, v1=_clock(pending.due_at)), now, level="WARNING", turn_id=pending.turn_id,
                        due_at=_iso(pending.due_at))
        pending = watch.pending
        if pending is None or now < pending.due_at:
            return
        if now - pending.failed_at > self.config.max_age_s:
            watch.pending = None
            self._event("continue_skipped", watch, tr('Error is over {v0:.0f} minutes old; cancelling auto-continue', v0=self.config.max_age_s / 60),
                        now, turn_id=pending.turn_id)
            return
        if self.pid is None:
            if not pending.deferred:
                pending.deferred = True
                self._event("continue_deferred", watch, tr('Codex is not running; waiting for it to start before auto-continuing'), now, turn_id=pending.turn_id,
                            due_at=_iso(pending.failed_at + self.config.max_age_s))
            return
        if self.busy_check(watch.info.id):
            watch.pending = None
            self._event("continue_skipped", watch, tr('Codex reports this task is running; no continuation needed'), now, turn_id=pending.turn_id)
            return
        self._read(watch, self.clock())  # Codex may have started its own retry while the app was asked
        if watch.pending is not pending:
            return
        text = continuation_message(self.config.message, pending.reason)
        if self.config.dry_run:
            watch.pending = None
            self._event("dry_run", watch, tr('(dry-run) Would send continuation message: {v0}', v0=text), now, turn_id=pending.turn_id)
            return
        pending.sends += 1
        try:
            result = self.send(watch.info.id, text)
        except (codex_app_client.AppToolsError, OSError) as exc:
            if pending.sends >= self.config.max_sends_per_turn:
                watch.pending = None
                self._event("gave_up", watch, tr('Failed to send a continuation message {v0} times ({v1}); stopping auto-continue; handle manually', v0=pending.sends, v1=exc),
                            now, level="WARNING", turn_id=pending.turn_id)
                return
            pending.due_at = now + self.config.delay(pending.sends + 1)
            self._event("send_failed", watch, tr('Failed to send a continuation message: {v0}; will retry at {v1}', v0=exc, v1=_clock(pending.due_at)), now,
                        level="WARNING", turn_id=pending.turn_id, due_at=_iso(pending.due_at))
            return
        watch.pending, watch.sent, watch.sent_at = None, pending, self.clock()
        watch.continued += 1
        self._event("continued", watch, tr('Auto-continued ({v0}, attempt {v1} this turn, pipe {v2})', v0=pending.reason, v1=watch.continued, v2=result.get('pipe')), now, turn_id=pending.turn_id, reason=pending.reason,
                    attempt=watch.failures, due_at=_iso(watch.sent_at + self.config.delivery_timeout_s))


# --- commands -------------------------------------------------------------------------------


def scan(
    *,
    hours: float = 24.0,
    max_age_s: float = WatchConfig.max_age_s,
    now: float | None = None,
    load_threads: Callable[[], list[ThreadInfo]] = codex_threads.load_threads,
    tail_bytes: int = WatchConfig.tail_bytes,
) -> list[dict[str, Any]]:
    """Recently updated local conversations whose last turn failed (read-only)."""

    now = time.time() if now is None else now
    rows = []
    for info in load_threads():
        updated = _epoch(info.updated_at)
        path = info.rollout
        if not watchable(info) or updated is None or updated < now - hours * 3600:
            continue
        if path is None or not path.is_file():
            continue
        collector = TurnCollector()
        collector.feed(read_tail(path, tail_bytes)[0])
        turns = collector.ordered()
        if not turns or turns[-1].status != "failed":
            continue
        last = turns[-1]
        resumable, reason = classify(last.error_info, last.error)
        failed_at = _epoch(last.completed_at) or updated
        if not resumable:
            action = tr('Record only (not a network error)')
        elif now - failed_at > max_age_s:
            action = tr('Over {v0:.0f} minutes old; no auto-continue', v0=max_age_s / 60)
        else:
            action = tr('The watcher will auto-continue')
        rows.append({
            "thread_id": info.id,
            "task": task_name(info),
            "failed_at": _local(failed_at),
            "reason": reason,
            "error_kind": last.error_info,
            "error": last.error,
            "consecutive": trailing_failures(turns)[0],
            "action": action,
        })
    rows.sort(key=lambda row: row["failed_at"], reverse=True)
    return rows


def single_instance_lock(path: Path) -> BinaryIO | None:
    """Lock the first byte of ``path``; None when another process holds the lock.

    The caller keeps the returned handle open for as long as it runs. Windows
    releases the lock when the process exits, including crashes.
    """

    import msvcrt

    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.open("a+b")
    try:
        lock.seek(0)
        try:
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
        except PermissionError:
            lock.close()
            return None  # the byte is locked, so reading it fails: another process holds the lock
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            lock.close()
            return None
    except BaseException:
        lock.close()
        raise
    return lock


def run_watch(config: WatchConfig, log_dir: Path, *, echo: bool = False) -> int:
    from agent_telegram.gate import wait_until_open
    wait_until_open()
    log_dir.mkdir(parents=True, exist_ok=True)
    lock = single_instance_lock(log_dir / "watch.lock")
    if lock is None:
        if sys.stderr is not None:
            print(tr('codex_watch is already running.'), file=sys.stderr)
        return 0
    with lock:
        log = EventLog(log_dir, echo=echo)
        watcher = Watcher(config, log)
        mode = tr(', dry-run: record only, do not send') if config.dry_run else ""
        log.incident("watcher_started", tr('Watcher started (pid {v0}{v1}), log directory {v2}', v0=os.getpid(), v1=mode, v2=log_dir), at=time.time(),
                     pid=os.getpid(), dry_run=config.dry_run)
        last_error = None
        try:
            while True:
                try:
                    watcher.poll_once()
                    last_error = None
                except Exception as exc:  # noqa: BLE001 - one bad poll must not stop the watcher
                    summary = f"{type(exc).__name__}: {exc}"
                    if summary != last_error:
                        log.line(tr('Polling failed: {v0}\n{v1}', v0=summary, v1=traceback.format_exc()), "ERROR")
                        last_error = summary
                time.sleep(config.poll_s)
        except KeyboardInterrupt:
            log.incident("watcher_stopped", tr('Watcher stopped'), at=time.time(), pid=os.getpid())
            return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(
        prog="python -m agent_telegram.codex_watch",
        description=tr('Watch local Codex Desktop conversations, log network errors and automatically send continuation messages.'),
    )
    parser.add_argument("--log-dir", type=Path, help=tr('Log directory (default {v0})', v0=DEFAULT_LOG_DIR))
    commands = parser.add_subparsers(dest="command")
    watch = commands.add_parser("watch", help=tr('Run the watcher (default command)'))
    watch.add_argument("--dry-run", action="store_true", help=tr('Record only; do not send continuation messages'))
    watch.add_argument("--poll", type=float, default=WatchConfig.poll_s, help=tr('Polling interval (seconds)'))
    watch.add_argument("--max-age", type=float, default=WatchConfig.max_age_s / 60, help=tr('Continue only failures within this many minutes'))
    watch.add_argument("--max-attempts", type=int, default=default_max_attempts(), help=tr('Stop continuing after this many consecutive failures (0 means unlimited retries)'))
    watch.add_argument("--max-backoff", type=float, default=default_max_backoff_s(), help=tr('Maximum backoff delay (seconds; 0 means no cap)'))
    watch.add_argument("--backoff", type=parse_backoff_s, default=default_backoff_s(), help=tr('Backoff sequence (seconds, comma-separated)'))
    watch.add_argument("--message", default=DEFAULT_MESSAGE, help=tr('Continuation message; {reason} is replaced by the error reason'))
    scan_parser = commands.add_parser("scan", help=tr('Read only: list conversations whose latest turn failed'))
    scan_parser.add_argument("--hours", type=float, default=24.0, help=tr('Only conversations updated within this many hours'))
    report_parser = commands.add_parser("report", help=tr('List recent events'))
    report_parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)
    if args.command in (None, "watch"):
        pass  # The old-package cutover guard is excluded.
    log_dir = Path(os.path.abspath(args.log_dir)) if args.log_dir else default_log_dir()

    if args.command == "scan":
        pid = codex_main_pid()
        print(tr('Codex main process: {v0}', v0=f'pid {pid}' if pid else tr('stopped')))
        rows = scan(hours=args.hours)
        if not rows:
            print(tr('No conversations are currently stopped on an error.'))
        for row in rows:
            run = tr(' ({v0} consecutive failures)', v0=row['consecutive']) if row["consecutive"] else ""
            print(f"{row['failed_at']}  [{row['task']}]  {row['reason']}{run} → {row['action']}")
            print(f"    {' '.join(row['error'].split())[:240]}  ({row['thread_id']})")
        return 0
    if args.command == "report":
        events = report(log_dir, args.limit)
        if not events:
            print(tr('No events yet ({v0}).', v0=log_dir / 'incidents.jsonl'))
        for item in events:
            print(f"{str(item.get('time', ''))[:19].replace('T', ' ')}  {item.get('text', '')}")
        return 0
    config = WatchConfig(
        poll_s=getattr(args, "poll", WatchConfig.poll_s),
        backoff_s=getattr(args, "backoff", default_backoff_s()),
        max_backoff_s=getattr(args, "max_backoff", default_max_backoff_s()),
        max_age_s=getattr(args, "max_age", WatchConfig.max_age_s / 60) * 60,
        max_attempts=getattr(args, "max_attempts", default_max_attempts()),
        message=getattr(args, "message", DEFAULT_MESSAGE),
        dry_run=getattr(args, "dry_run", False),
    )
    return run_watch(config, log_dir, echo=sys.stderr is not None)


if __name__ == "__main__":
    raise SystemExit(main())

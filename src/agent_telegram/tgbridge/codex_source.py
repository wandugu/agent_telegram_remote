"""Codex Desktop conversations for the Telegram bridge.

Finished turns come from a codex_watch.Watcher of our own in follow-only mode
(continue_failures=False): the separate codex-watch process keeps continuing failed
turns, this watcher only reports them. Messages go out as agent_telegram "codex" jobs in
raw mode, so they exclude agent_telegram's own codex_send on the same conversation and
keep being followed across a bridge restart. New tasks use the app-tools
list_projects / create_thread tools with an anchor conversation as executor metadata.
Threads on SSH remote hosts come from codex_remote.RemoteCodex (view only), merged into
the listing and reading methods here.

Blocking questions (request_user_input, which plan mode uses) and async questions are read
from the rollout and reported as QuestionEvents. Blocking answers and "implement this plan"
go through Codex Desktop's window IPC (codex_ipc); async questions may outlive their turn.
Command/file approvals live only in Desktop snapshots: a lazy source-owned worker reads
them after a rollout stall and rechecks open cards independently of further rollout activity.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_telegram.i18n import tr
from agent_telegram import codex_app_client, codex_threads, codex_watch
from agent_telegram import jobs
from agent_telegram.codex_threads import ThreadInfo, TurnCollector, TurnSummary
from agent_telegram.tgbridge.codex_remote import RemoteCodex
from agent_telegram.tgbridge import codex_ipc, progress, quota
from agent_telegram.tgbridge.model import (CODEX, ApprovalEvent, AttentionEvent, Event, ProgressEvent, Question, QuestionEvent,
                    Quota, SessionView, TurnEvent)

if TYPE_CHECKING:
    from agent_telegram.tgbridge.config import Config

log = logging.getLogger(__name__)

MAX_COMMENTARY = 5  # commentary messages kept per turn
_ITEM_CHARS = 4000  # one commentary message or plan
USER_INPUT_TOOL = "request_user_input"
ASYNC_MAX_AGE_S = 6 * 3600
APPROVAL_STALL_S = 15.0
APPROVAL_POLL_S = 30.0
APPROVAL_RECHECK_S = 15.0
APPROVAL_BACKOFF_MAX_S = 300.0
# Codex Desktop's own text for "implement this plan" (its plan-implementation request chunk).
IMPLEMENT_PLAN_PREFIX = "PLEASE IMPLEMENT THIS PLAN:"
# Polls a change of Codex Desktop's main process (gone, or another pid) must last before its
# questions count as ended: a process snapshot can fail once, and a second ChatGPT.exe handing
# a launch to the running one lives about a second, possibly with a lower pid.
DESKTOP_SETTLE_POLLS = 3
_NOT_SEEN = -1  # no main pid accepted yet (pids are never negative)
SEND_TIMEOUT_S = 4 * 3600
MAX_SEND_CHARS = 100_000
IF_BUSY = ("fail", "wait", "send")
# read_turns / full_text read this much of the rollout tail first and widen up to the maximum.
READ_START_BYTES = 4 * 1024 * 1024
READ_MAX_BYTES = 64 * 1024 * 1024
# A failure codex-watch will continue is pushed quietly with its plan; give that process
# (5 s polls) this long to log the plan before pushing the failure loudly without it.
FAILURE_GRACE_S = 12.0
_REMEMBERED = 2000
_NONCE_RE = re.compile(r"^\S{1,64}$")
_FINISHED = frozenset({"completed", "failed", "aborted"})

_SOURCE_LABEL_AGENT_TG_LEGACY = 'agent_telegram task'
_SOURCE_LABEL_AUTO_CONTINUE = 'Auto-continue'
_SOURCE_LABEL_TELEGRAM = 'From Telegram'
_SOURCE_LABEL_EXCLUDED = 'Content hidden'


def _epoch(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _hhmm(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M")


def _cut_by_restart(began: float | None, app_start: float | None) -> bool:
    """Begun before Codex Desktop's main process started: that restart cut it short."""

    return began is not None and app_start is not None and began + 1.0 < app_start


def _one_line(text: str, limit: int = 60) -> str:
    return " ".join(text.split())[:limit]


def _item_text(item: dict[str, Any]) -> str:
    parts = [
        part["text"]
        for part in item.get("content") or []
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    return "\n".join(parts).strip()


def origin_labels(user_text: str) -> list[str]:
    """Tags for where a turn's input came from."""

    head = user_text.lstrip()
    labels = []
    if head.startswith(("[agent_telegram:", "[agent-telegram:")):
        labels.append(tr('agent_telegram task'))
    if codex_threads.is_auto_continuation(head):
        labels.append(tr('Auto-continue'))
    if head.startswith("[tg:"):
        labels.append(tr('From Telegram'))
    return labels


def thread_target(project_id: str | None) -> dict[str, Any]:
    """create_thread's ``target``.

    From the app's tools/list: anyOf {type: "project", projectId, environment?} |
    {type: "projectless", ...} | {type: "chatgptWorkCloud", ...}. Keep this the only
    place that builds it, so a live schema difference is a one-line fix.
    """

    if project_id:
        return {"type": "project", "projectId": project_id}
    return {"type": "projectless"}


def _json(value: Any) -> Any:
    """A JSON string decoded (None when it is not JSON); anything else as it is."""

    try:
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, RecursionError):
        return None


def parse_questions(arguments: Any) -> list[Question]:
    """The questions of a request_user_input call (its JSON arguments); [] when there are none.

    The model writes these arguments and Codex records the call before it checks them, so any
    shape may appear here.
    """

    data = _json(arguments)
    items = data.get("questions") if isinstance(data, dict) else None
    found: list[Question] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not item.get("id") or not str(item.get("question") or "").strip():
            continue
        found.append(Question(id=str(item["id"]), text=str(item["question"]).strip(),
                              header=str(item.get("header") or "").strip(),
                              options=progress.parse_options(item.get("options"))))
    return found[:progress.MAX_QUESTIONS]


def answer_lines(questions: list[Question], output: Any) -> str:
    """One "header: answer" line per answered question, from a request_user_input output."""

    data = _json(output)
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        return ""
    lines = []
    for question in questions:
        entry = answers.get(question.id)
        values = entry.get("answers") if isinstance(entry, dict) else None
        if isinstance(values, list) and values:
            lines.append(f"{question.header or question.text}: {tr('; ').join(str(value) for value in values)}")
    return "\n".join(lines)


def parse_async_questions(raw: Any) -> list[Question]:
    """Desktop async questions, preserving titles and original positional reply indices."""

    found = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not item["title"]:
            continue
        options = item.get("options")
        found.append(Question(id=str(index), text=item["title"], options=[
            (option, "") for option in options if isinstance(option, str) and option.strip()
        ] if isinstance(options, list) else []))
    return found


@dataclass
class UserInputCall:
    """A blocking or async Codex question request and the answers observed in its rollout."""

    call_id: str
    turn_id: str
    questions: list[Question]
    asked_at: str = ""  # the record's timestamp
    done: bool = False  # answered (all parsed indices for async); blocking also ends with its turn
    answer: str = ""  # answer_lines() of the output, when answered
    kind: str = "blocking"
    answered: dict[int, str] = field(default_factory=dict)
    answer_sources: dict[int, str] = field(default_factory=dict)
    answer_turn_ids: set[str] = field(default_factory=set)
    turn_ended_at: str = ""
    closed_by: str = ""  # blocking: distinguish an output (even empty) from a turn end


class CommentaryCollector(TurnCollector):
    """TurnCollector that also keeps, per turn, the commentary messages (the last
    MAX_COMMENTARY), the text of the last plan and the number of commands run, and the
    blocking and async question requests with their answers."""

    def __init__(self) -> None:
        super().__init__()
        self.commentary: dict[str, list[str]] = {}
        # Mid-turn commentary messages to the user, in order as (kind, text).
        self.progress: dict[str, list[tuple[str, str]]] = {}
        self.progress_total: dict[str, int] = {}  # all of them per turn (the list keeps the last few)
        self.plans: dict[str, str] = {}
        self.commands: dict[str, int] = {}
        self.inputs: dict[str, UserInputCall] = {}  # call id -> call, in order
        # The account's plan limits from the newest token_count event: (epoch seconds, rate_limits).
        self.rate_limits: tuple[float, dict[str, Any]] | None = None
        self.approval_policy: Any = None
        self.approval_policy_seen = False
        self._current = ""  # the turn of the last task_started (response items may lack their turn id)

    def feed(self, records: list[dict[str, Any]]) -> None:
        super().feed(records)
        for record in records:
            payload = record.get("payload")
            if isinstance(payload, dict) and (record.get("type") == "turn_context" or (
                    record.get("type") == "event_msg" and payload.get("type") == "thread_settings_applied")):
                settings = payload.get("thread_settings")
                if "approval_policy" in payload:
                    self.approval_policy, self.approval_policy_seen = payload["approval_policy"], True
                elif isinstance(settings, dict) and "approval_policy" in settings:
                    self.approval_policy, self.approval_policy_seen = settings["approval_policy"], True
            if record.get("type") == "response_item":
                self._response_item(record)
                continue
            if record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "token_count" and isinstance(payload.get("rate_limits"), dict):
                stamp = record.get("timestamp")
                seen = _epoch(stamp) if isinstance(stamp, str) else None
                if seen is not None and (self.rate_limits is None or seen >= self.rate_limits[0]):
                    self.rate_limits = (seen, payload["rate_limits"])
                continue
            if payload.get("type") == "task_started":
                self._current = str(payload.get("turn_id") or "")
            elif payload.get("type") in ("task_complete", "turn_aborted"):
                ended = str(payload.get("turn_id") or "")
                for call in self.inputs.values():
                    if call.turn_id == ended:
                        call.turn_ended_at = str(record.get("timestamp") or "")
                        if call.kind == "blocking":
                            if not call.done:
                                call.closed_by = "turn_ended"
                            call.done = True
            if payload.get("type") != "item_completed":
                continue
            turn_id = str(payload.get("turn_id") or "")
            item = payload.get("item")
            if not turn_id or not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "AgentMessage" and item.get("phase") == "commentary":
                text = _item_text(item)
                if text:
                    notes = self.commentary.setdefault(turn_id, [])
                    notes.append(text[:_ITEM_CHARS])
                    del notes[:-MAX_COMMENTARY]
                    self._progress(turn_id, "commentary", text)
            elif kind == "AgentMessage" and item.get("phase") == "final_answer" and item.get("delivery") == "async":
                call_id = item.get("id")
                if isinstance(call_id, str) and call_id:
                    call = self.inputs.setdefault(call_id, UserInputCall(
                        call_id, turn_id, parse_async_questions(item.get("questions")),
                        asked_at=str(record.get("timestamp") or ""), kind="async"))
                    if call.kind == "async" and not call.questions:
                        call.done = True
            elif kind == "UserMessage":
                parts = item.get("content")
                if (isinstance(parts, list) and len(parts) == 1 and isinstance(parts[0], dict)
                        and parts[0].get("type") == "text" and isinstance(parts[0].get("text"), str)):
                    client_id = item.get("client_id")
                    self._async_reply(parts[0]["text"], turn_id, f"client:{client_id}" if client_id else "")
            elif kind == "FunctionCallOutput" and item.get("name") == "send_message_to_thread":
                output = item.get("output") or ""
                parsed = codex_threads.delegated_reply(output) if isinstance(output, str) else None
                if parsed is not None:
                    text, marker = parsed
                    self._async_reply(text, turn_id, marker)
            elif kind == "Plan":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    self.plans[turn_id] = text.strip()[:_ITEM_CHARS]
            elif kind == "CommandExecution":
                self.commands[turn_id] = self.commands.get(turn_id, 0) + 1

    def _async_reply(self, text: str, turn_id: str, source: str) -> None:
        for call_id, index, answer in codex_threads.question_reply_entries(text):
            call = self.inputs.get(call_id)
            if call is None or call.kind != "async":
                continue
            call.answered[index] = answer
            call.answer_sources[index] = source
            call.answer_turn_ids.add(turn_id)
            call.done = all(int(question.id) in call.answered for question in call.questions)

    def _response_item(self, record: dict[str, Any]) -> None:
        payload = record.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("call_id"), str) or not payload["call_id"]:
            return
        call_id = payload["call_id"]
        kind = payload.get("type")
        if kind == "function_call" and payload.get("name") == USER_INPUT_TOOL:
            meta = payload.get("internal_chat_message_metadata_passthrough")
            turn_id = str(meta.get("turn_id") or "") if isinstance(meta, dict) else ""
            if call_id not in self.inputs:
                # Arguments that do not parse still make Codex wait: reported without questions.
                self.inputs[call_id] = UserInputCall(call_id, turn_id or self._current,
                                                     parse_questions(payload.get("arguments")),
                                                     asked_at=str(record.get("timestamp") or ""))
        elif kind == "function_call_output" and call_id in self.inputs:
            call = self.inputs[call_id]
            if call.kind == "blocking":
                call.done, call.answer = True, answer_lines(call.questions, payload.get("output"))
                call.closed_by = "answered"

    def _progress(self, turn_id: str, kind: str, text: str) -> None:
        items = self.progress.setdefault(turn_id, [])
        items.append((kind, text[:_ITEM_CHARS]))
        del items[:-MAX_COMMENTARY]
        self.progress_total[turn_id] = self.progress_total.get(turn_id, 0) + 1

    def prune(self, keep: int) -> None:
        super().prune(keep)
        for store in (self.commentary, self.progress, self.progress_total, self.plans, self.commands):
            for turn_id in [key for key in store if key not in self.turns]:
                del store[turn_id]
        for call_id in [key for key, call in self.inputs.items() if call.turn_id not in self.turns]:
            del self.inputs[call_id]

    def details(self, turn_id: str) -> dict[str, Any]:
        return {
            "commentary": list(self.commentary.get(turn_id, [])),
            "plan": self.plans.get(turn_id, ""),
            "commands": self.commands.get(turn_id, 0),
        }


class _QuietLog(codex_watch.EventLog):
    """EventLog that writes no files: the bridge's watcher must not touch codex-watch's logs.

    Warnings and errors (for example a failing turn callback) go to the logging module.
    """

    def __init__(self) -> None:  # no directory on purpose
        self.echo = False

    def line(self, text: str, level: str = "INFO") -> None:
        if level in ("WARNING", "ERROR"):
            log.log(logging.ERROR if level == "ERROR" else logging.WARNING, "codex watcher: %s", text)

    def incident(self, event: str, text: str, *, at: float, level: str = "INFO", **fields: Any) -> None:
        self.line(text, level)


def _refuse_send(thread_id: str, text: str) -> dict[str, Any]:
    raise codex_app_client.AppToolsError("the bridge's Codex watcher never sends continuations")


@dataclass
class _Deferred:
    info: ThreadInfo
    turn: TurnSummary
    deadline: float


@dataclass
class _ApprovalWatch:
    """Source-owned scheduling state; touched only while holding CodexSource._lock."""

    signal: tuple[str, int, int] | None
    quiet_since: float
    pid: int | None
    next_poll: float = 0.0
    failures: int = 0
    in_flight: bool = False


def _project_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("projects", "items", "data", "results"):
        if isinstance(data.get(key), list):
            return _project_items(data[key])
    found: list[dict[str, Any]] = []
    for value in data.values():  # grouped listings, e.g. {"local": [...], "remote": [...]}
        if isinstance(value, list):
            found += [item for item in value if isinstance(item, dict) and ("projectId" in item or "id" in item)]
    return found


def _project(item: dict[str, Any]) -> dict[str, Any]:
    path = str(item.get("path") or item.get("rootPath") or item.get("cwd") or item.get("workspaceRoot") or "")
    name = str(item.get("name") or item.get("title") or item.get("displayName") or "")
    git = item.get("isGitRepository", item.get("isGitRepo", item.get("git")))
    return {
        "id": str(item.get("projectId") or item.get("id") or ""),
        "name": name or Path(path).name or path,
        "path": path,
        "kind": str(item.get("type") or item.get("kind") or item.get("source") or ""),
        "git": git if isinstance(git, bool) else None,
        "raw": item,
    }


def _json_or_text(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _created_ids(data: Any) -> tuple[str | None, str | None]:
    """(thread id, host id) from create_thread's answer, wherever the app puts them."""

    if not isinstance(data, dict):
        return None, None
    thread = data.get("thread") if isinstance(data.get("thread"), dict) else {}
    thread_id = data.get("threadId") or data.get("thread_id") or thread.get("id") or thread.get("threadId") or data.get("id")
    host_id = data.get("hostId") or data.get("host_id") or thread.get("hostId")
    return (str(thread_id) if thread_id else None), (str(host_id) if host_id else None)


class CodexSource:
    """Local Codex Desktop conversations: turn events, listing, reading, sending, new tasks.

    Thread-safe: poll() runs on the bridge's poll thread, the other methods on command threads.
    """

    def __init__(
        self,
        cfg: Config,
        emit: Callable[[Event], None],
        *,
        load_threads: Callable[[], list[ThreadInfo]] = codex_threads.load_threads,
        main_pid: Callable[[], int | None] = codex_watch.codex_main_pid,
        busy_check: Callable[[str], bool | None] = codex_app_client.app_reports_busy,
        clock: Callable[[], float] = time.time,
        watch_log_dir: Path | None = None,
        failure_grace_s: float = FAILURE_GRACE_S,
        remote: RemoteCodex | None | bool = True,
        ipc: Any = codex_ipc,
    ) -> None:
        self.cfg = cfg
        self.emit = emit
        self._load_all = load_threads
        self._main_pid = main_pid
        self._busy_check = busy_check
        self.clock = clock
        self.watch_log_dir = watch_log_dir
        self.failure_grace_s = failure_grace_s
        self.ipc = ipc  # injected codex_ipc operations and snapshot connection factory
        self._lock = threading.RLock()
        # The clash check and the job creation of submit_send form one step: two confirmed
        # Telegram sends to the same conversation must not both start a job.
        self._send_lock = threading.RLock()
        self._app_started: tuple[int, float | None] | None = None  # (Codex main pid, its start epoch)
        config = codex_watch.WatchConfig(continue_failures=False, active_window_s=float(cfg.active_window_h) * 3600.0)
        self.watcher = codex_watch.Watcher(
            config,
            _QuietLog(),
            clock=clock,
            send=_refuse_send,
            busy_check=busy_check,
            main_pid=self._pid,
            load_threads=self.load_threads,
            on_turn_finished=self._on_turn_finished,
            collector_factory=CommentaryCollector,
        )
        self._baseline: float | None = None  # first poll; later-tracked conversations report turns since then
        self._tracked: set[str] = set()
        self._emitted: dict[tuple[str, str], None] = {}
        self._deferred: dict[tuple[str, str], _Deferred] = {}
        self._outbox: list[Event] = []
        self._progress_seen: dict[tuple[str, str], int] = {}  # (thread, turn) -> commentary items reported
        # (thread, call id) -> the thread and the blocking/async request reported as waiting
        self._asked: dict[tuple[str, str], tuple[ThreadInfo, UserInputCall]] = {}
        self._desktop_pid: int | None = _NOT_SEEN  # Codex Desktop's main pid as questions see it
        self._desktop_changed = 0  # polls in a row that saw another main pid, or none
        self._approval_states: dict[str, _ApprovalWatch] = {}
        self._approval_jobs: dict[str, _ApprovalWatch] = {}
        # Include the id's type: JSON-RPC integer 7 and string "7" are different requests.
        self._approvals: dict[tuple[str, type, int | str, str], ApprovalEvent] = {}
        self._approval_notices: set[tuple[str, type, int | str, str]] = set()
        self._approval_wake = threading.Event()
        self._approval_stop = threading.Event()
        self._approval_worker: threading.Thread | None = None
        self.last_error: str | None = None
        # remote=True builds the remote-host poller when the config enables it; tests pass one or None.
        if remote is True:
            remote = (RemoteCodex(cfg, emit, anchor=self.anchor_thread, desktop_running=self.desktop_running, clock=clock)
                      if getattr(cfg, "codex_remote_enabled", False) else None)
        self.remote: RemoteCodex | None = remote or None

    # --- conversations ------------------------------------------------------------

    def load_threads(self) -> list[ThreadInfo]:
        """All conversations except automation runs (unless cfg.codex_include_automation)."""

        threads = self._load_all()
        if getattr(self.cfg, "codex_include_automation", False):
            return threads
        return [info for info in threads if info.thread_source != "automation"]

    def _find(self, thread_id: str) -> ThreadInfo | None:
        wanted = thread_id.strip().lower()
        for info in self._load_all():
            if info.id.lower() == wanted and not info.is_subagent:
                return info
        return None

    def _excluded(self, cwd: str) -> bool:
        return bool(cwd) and bool(self.cfg.excluded(cwd))

    def _pid(self) -> int | None:
        try:
            return self._main_pid()
        except OSError:
            return None  # a failed process snapshot must not stop the poll

    def desktop_running(self) -> bool:
        return self._pid() is not None

    def _app_start(self, pid: int | None) -> float | None:
        """Start time (epoch) of the Codex Desktop main process ``pid``; None when unknown."""

        if pid is None:
            return None
        cached = self._app_started
        if cached is None or cached[0] != pid or cached[1] is None:  # a failed lookup is retried
            try:
                created = jobs.process_creation_time(pid)
            except OSError:
                created = None
            # FILETIME: 100 ns ticks since 1601-01-01 UTC.
            started = created / 10_000_000 - 11_644_473_600 if created else None
            cached = self._app_started = (pid, started)
        return cached[1]

    # --- turn events ---------------------------------------------------------------

    def poll(self) -> None:
        """Read new rollout records and emit the turns that finished (emit runs outside the lock).

        A failed watcher poll is stored in ``last_error`` and re-raised after the turns that
        are ready were emitted, so the caller's poll loop logs it (the bridge has no handler
        for the logging module); the next poll starts over.
        """

        failure: Exception | None = None
        with self._lock:
            try:
                self.watcher.poll_once()
                self.last_error = None
            except Exception as exc:  # noqa: BLE001 - reported below, after the ready events
                failure = exc
                self.last_error = f"{type(exc).__name__}: {exc}"
            now = self.clock()
            self._catch_up(now)
            self._flush(now)
            if getattr(self.cfg, "progress_enabled", True):
                self._collect_progress()
            self._collect_questions()
            self._collect_approvals(now)
            ready, self._outbox = self._outbox, []
            approval_pending = bool(self._approval_jobs)
        if approval_pending:
            self._ensure_approval_worker()
        for event in ready:
            try:
                self.emit(event)
            except Exception as exc:  # noqa: BLE001 - the next turns must still be reported
                log.error("Codex turn event not delivered: %s: %s", type(exc).__name__, exc)
        if failure is not None:
            raise failure

    def _catch_up(self, now: float) -> None:
        """Report turns that ended before a conversation got tracked (the watcher reads that tail silently).

        A conversation outside the active window starts being tracked at the watcher's next
        list refresh; a turn it finished in between would otherwise never be pushed.
        """

        started = {thread_id for thread_id, watch in self.watcher.watches.items() if watch.started}
        if self._baseline is None:
            self._baseline, self._tracked = now, started
            return
        for thread_id in started - self._tracked:
            watch = self.watcher.watches[thread_id]
            for turn in watch.collector.ordered():
                done = _epoch(turn.completed_at)
                if turn.status in _FINISHED and done is not None and done >= self._baseline:
                    self._on_turn_finished(watch, turn, now)
        self._tracked = started  # dropped conversations catch up again when tracked anew

    def _collect_progress(self) -> None:
        """Commentary that running turns wrote since the last poll.

        A turn already running at the first poll (or begun before it) reports only newer ones.
        """

        running: set[tuple[str, str]] = set()
        for thread_id, watch in self.watcher.watches.items():
            collector = watch.collector
            if not watch.started or not isinstance(collector, CommentaryCollector):
                continue
            for turn in collector.ordered():
                if turn.status != "running":
                    continue
                key = (thread_id, turn.turn_id)
                running.add(key)
                total = collector.progress_total.get(turn.turn_id, 0)
                seen = self._progress_seen.get(key)
                if seen is None:
                    began = _epoch(turn.started_at) or 0.0
                    seen = total if self._baseline is None or began < self._baseline else 0
                self._progress_seen[key] = total
                if total <= seen:
                    continue
                cwd = codex_threads.strip_verbatim(watch.info.cwd)
                if self._excluded(cwd):
                    continue
                items = collector.progress.get(turn.turn_id, [])[-(total - seen):]
                if items:
                    question = any(kind == "question" for kind, _ in items)
                    self._outbox.append(ProgressEvent(
                        agent=CODEX, session_id=thread_id, title=codex_watch.task_name(watch.info), cwd=cwd,
                        texts=[text for _, text in items], kind="question" if question else "commentary",
                        asks_user=question or any(progress.asks_user(text) for _, text in items)))
        for key in [key for key in self._progress_seen if key not in running]:
            del self._progress_seen[key]

    def _collect_questions(self) -> None:
        """Question requests that started waiting for answers, and reported ones that stopped.

        Blocking calls wait only in the latest running turn. Async calls can outlive their
        turn, until answered, superseded by new user input, expired or cut by a Desktop restart.
        A change of the main process counts only after DESKTOP_SETTLE_POLLS polls, so one
        failed snapshot or a short-lived second instance closes and reopens nothing.
        """

        pid = self.watcher.pid  # refreshed by this poll's watcher pass
        if pid == self._desktop_pid or self._desktop_pid == _NOT_SEEN:
            self._desktop_changed = 0
        else:
            self._desktop_changed += 1
        if self._desktop_changed == 0 or self._desktop_changed >= DESKTOP_SETTLE_POLLS:
            self._desktop_pid, self._desktop_changed = pid, 0
        pid = self._desktop_pid
        app_start = self._app_start(pid)
        now = self.clock()
        waiting: dict[tuple[str, str], tuple[ThreadInfo, UserInputCall]] = {}
        reasons: dict[tuple[str, str], str] = {}
        for thread_id, watch in self.watcher.watches.items():
            collector = watch.collector
            if not watch.started or not isinstance(collector, CommentaryCollector):
                continue
            for call in collector.inputs.values():
                key = (thread_id, call.call_id)
                reason = self._question_close_reason(collector, call, pid=pid, app_start=app_start, now=now)
                if reason:
                    reasons[key] = reason
                else:
                    waiting[key] = (watch.info, call)
        for key, (info, call) in waiting.items():
            if key not in self._asked:
                self._asked[key] = (info, call)
                self._outbox.append(self._question_event(info, call, closed=False))
        for key in [key for key in self._asked if key not in waiting]:
            info, call = self._asked.pop(key)
            watch = self.watcher.watches.get(key[0])
            # A collector pruning the question's turn supersedes it; losing the watch means
            # the thread was archived/deleted or otherwise left the watched set.
            reason = reasons.get(key)
            if reason is None:
                reason = ("superseded" if watch is not None and watch.started
                          and isinstance(watch.collector, CommentaryCollector) else "gone")
            self._outbox.append(self._question_event(info, call, closed=True, by=reason))

    def _question_close_reason(self, collector: CommentaryCollector, call: UserInputCall, *,
                               pid: int | None, app_start: float | None, now: float) -> str:
        """Why a request is no longer waiting; an empty string means it still waits."""

        turn = collector.turns.get(call.turn_id)
        last = collector.order[-1] if collector.order else ""
        if call.done:
            return "answered" if call.kind == "async" else call.closed_by or "turn_ended"
        if turn is None:
            return "superseded"
        if call.kind == "blocking":
            if turn.status != "running":
                return "turn_ended"
            if call.turn_id != last:
                return "superseded"
        if _cut_by_restart(_epoch(call.asked_at), app_start):
            return "restart"
        if pid is None:
            return "desktop_gone"
        if call.kind == "async":
            asked = _epoch(call.asked_at)
            if asked is None or now - asked > ASYNC_MAX_AGE_S:
                return "expired"
            position = collector.order.index(call.turn_id)
            if any(turn_id not in call.answer_turn_ids and codex_threads.carries_user_input(collector.turns[turn_id])
                   for turn_id in collector.order[position + 1:]):
                return "superseded"
        return ""

    def async_question_state(self, thread_id: str, call_id: str) -> dict[str, Any] | None:
        """A watched async request's current turn and reply state, copied under the source lock."""

        with self._lock:
            watch = self.watcher.watches.get(thread_id)
            if watch is None or not watch.started or not isinstance(watch.collector, CommentaryCollector):
                return None
            collector = watch.collector
            call = collector.inputs.get(call_id)
            if call is None or call.kind != "async":
                return None
            turn = collector.turns.get(call.turn_id)
            pid = None if self._desktop_pid == _NOT_SEEN else self._desktop_pid
            reason = self._question_close_reason(collector, call, pid=pid, app_start=self._app_start(pid),
                                                 now=self.clock())
            return {
                "turn_id": call.turn_id,
                "turn_running": bool(turn and turn.status == "running"),
                "turn_last": bool(collector.order and call.turn_id == collector.order[-1]),
                "thread_busy": any(item.status == "running" for item in collector.turns.values()),
                "answered": dict(call.answered),
                "answer_sources": dict(call.answer_sources),
                "waiting": not reason,
            }

    def _question_event(self, info: ThreadInfo, call: UserInputCall, *, closed: bool, by: str = "") -> QuestionEvent:
        cwd = codex_threads.strip_verbatim(info.cwd)
        excluded = self._excluded(cwd)
        questions = list(call.questions)
        answer = call.answer
        if call.kind == "async":
            questions = [question for question in questions if int(question.id) not in call.answered]
            answer = "\n".join(f"{question.text}: {call.answered[int(question.id)]}"
                               for question in call.questions if int(question.id) in call.answered)
        return QuestionEvent(agent=CODEX, session_id=info.id, title=codex_watch.task_name(info), cwd=cwd,
                             request_id=call.call_id, turn_id=call.turn_id,
                             questions=[] if excluded or closed else questions, closed=closed,
                             answer="" if excluded or not closed else answer, excluded=excluded,
                             mode=call.kind, by=by if closed else "",
                             answered={} if excluded else dict(call.answered),
                             answer_sources={} if excluded else dict(call.answer_sources))

    # --- approval snapshots --------------------------------------------------------

    @staticmethod
    def _approval_signal(watch: codex_watch.ThreadWatch) -> tuple[str, int, int] | None:
        """Observe path, consumed byte offset and file size, never Windows' stale mtime.

        Size includes a partially written JSONL record, so even a long tool output
        resets the 15-second quiet interval before the watcher can decode its line.
        """

        try:
            return str(watch.path), watch.offset, watch.path.stat().st_size
        except OSError:
            return None

    def _approval_local(self, thread_id: str, watch: codex_watch.ThreadWatch) -> bool:
        return (watch.started and codex_watch.watchable(watch.info) and not self._remote_owns(thread_id)
                and isinstance(watch.collector, CommentaryCollector))

    def _approval_candidate(self, thread_id: str, state: _ApprovalWatch, now: float) -> bool:
        watch = self.watcher.watches.get(thread_id)
        if (watch is None or not self._approval_local(thread_id, watch) or self._desktop_pid is None
                or self._desktop_pid == _NOT_SEEN or self._desktop_changed or state.pid != self._desktop_pid):
            return False
        collector = watch.collector
        policy = collector.approval_policy if collector.approval_policy_seen else watch.info.approval_mode
        has_open = any(key[0] == thread_id for key in self._approvals)
        if not has_open and isinstance(policy, str) and policy == "never":
            return False
        last = collector.turns.get(collector.order[-1]) if collector.order else None
        # Already displayed approvals need periodic invalidation even while the
        # rollout grows or a settings update changes the policy to never.
        quiet = (state.signal is not None and self._approval_signal(watch) == state.signal
                 and now - state.quiet_since >= APPROVAL_STALL_S)
        return bool(last is not None and last.status == "running" and (has_open or quiet)
                    and not _cut_by_restart(_epoch(last.started_at), self._app_start(state.pid)))

    def _close_approvals(self, thread_id: str, by: str) -> None:
        for key in [key for key in self._approvals if key[0] == thread_id]:
            event = self._approvals.pop(key)
            self._outbox.append(replace(event, closed=True, by=by))
        self._approval_notices = {key for key in self._approval_notices if key[0] != thread_id}

    def _collect_approvals(self, now: float) -> None:
        """Schedule only; poll holds _lock here, and all IPC runs on the dedicated worker."""

        watched = {thread_id for thread_id, watch in self.watcher.watches.items()
                   if self._approval_local(thread_id, watch)}
        for thread_id in list(self._approval_states):
            if thread_id not in watched:
                self._close_approvals(thread_id, "gone")
                self._approval_states.pop(thread_id, None)
                self._approval_jobs.pop(thread_id, None)
        for thread_id in watched:
            watch = self.watcher.watches[thread_id]
            collector = watch.collector
            policy = collector.approval_policy if collector.approval_policy_seen else watch.info.approval_mode
            armed = policy != "never" or any(key[0] == thread_id for key in self._approvals)
            # Threads that can never ask (policy "never", nothing open) cost no stat per poll.
            signal = self._approval_signal(watch) if armed else None
            state = self._approval_states.get(thread_id)
            if state is None:
                state = self._approval_states[thread_id] = _ApprovalWatch(signal, now, self._desktop_pid)
            elif state.pid != self._desktop_pid:
                # _collect_questions has already applied DESKTOP_SETTLE_POLLS to this pid.
                self._close_approvals(thread_id, "desktop_gone")
                self._approval_jobs.pop(thread_id, None)
                state = self._approval_states[thread_id] = _ApprovalWatch(signal, now, self._desktop_pid)
            elif signal != state.signal:
                state.signal, state.quiet_since = signal, now
            last_id = collector.order[-1] if collector.order else ""
            for key, event in list(self._approvals.items()):
                if key[0] != thread_id:
                    continue
                turn = collector.turns.get(event.turn_id)
                if turn is None or turn.status != "running" or event.turn_id != last_id:
                    del self._approvals[key]
                    self._outbox.append(replace(event, closed=True, by="turn_ended"))
            if state.pid is None:
                self._close_approvals(thread_id, "desktop_gone")
            if not self._approval_candidate(thread_id, state, now):
                self._approval_jobs.pop(thread_id, None)
            elif not state.in_flight and now >= state.next_poll:
                self._approval_jobs[thread_id] = state

    def _ensure_approval_worker(self) -> None:
        """Start one lazy read-only daemon; never counted among the bridge's executing writes."""

        with self._lock:
            if self._approval_stop.is_set():
                return
            if self._approval_worker is None:
                self._approval_worker = threading.Thread(target=self._approval_loop,
                                                        name="tgbridge-approvals", daemon=True)
                self._approval_worker.start()
        self._approval_wake.set()

    def stop(self) -> None:
        """Stop approval discovery with the source (an in-flight bounded snapshot may finish first)."""

        self._approval_stop.set()
        self._approval_wake.set()
        worker = self._approval_worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1.0)

    def _approval_loop(self) -> None:
        while not self._approval_stop.is_set():
            self._approval_wake.wait()
            self._approval_wake.clear()
            while not self._approval_stop.is_set() and self._poll_approval_once():
                pass

    def _poll_approval_once(self) -> bool:
        """Consume one queued snapshot; split from the loop for deterministic fake-IPC tests."""

        with self._lock:
            if self._approval_stop.is_set() or not self._approval_jobs:
                return False
            thread_id = next(iter(self._approval_jobs))
            state = self._approval_jobs.pop(thread_id)
            now = self.clock()
            if (self._approval_states.get(thread_id) is not state or state.in_flight
                    or now < state.next_poll or not self._approval_candidate(thread_id, state, now)):
                return True
            state.in_flight, state.next_poll = True, now + APPROVAL_POLL_S
            watch = self.watcher.watches[thread_id]
            turn_id = watch.collector.order[-1]
        # No source lock while connecting, following the thread or waiting for its snapshot.
        error = None
        snapshot = None
        try:
            with self.ipc.connect() as client:
                snapshot = client.snapshot(thread_id)
            if not isinstance(snapshot, dict):
                raise codex_ipc.CodexIpcError(tr("Codex Desktop's approval state is not an object"))
        except Exception as exc:  # noqa: BLE001 - a failed read backs off, never kills discovery
            error = exc
        with self._lock:
            state.in_flight = False
            if self._approval_states.get(thread_id) is not state or self._approval_stop.is_set():
                return True
            now = self.clock()
            if error is not None:
                state.failures += 1
                state.next_poll = now + min(APPROVAL_POLL_S * 2 ** min(state.failures - 1, 4),
                                            APPROVAL_BACKOFF_MAX_S)
                log.warning("Codex approval snapshot failed for %s: %s", thread_id, type(error).__name__)
                return True
            state.failures = 0
            watch = self.watcher.watches.get(thread_id)
            if (watch is None or not self._approval_candidate(thread_id, state, now)
                    or not watch.collector.order or watch.collector.order[-1] != turn_id):
                return True
            self._apply_approval_snapshot(watch.info, snapshot or {}, turn_id)
            has_open = any(key[0] == thread_id for key in self._approvals)
            state.next_poll = now + (APPROVAL_RECHECK_S if has_open else APPROVAL_POLL_S)
        return True

    def _apply_approval_snapshot(self, info: ThreadInfo, snapshot: dict[str, Any], turn_id: str) -> None:
        present: set[tuple[str, type, int | str, str]] = set()
        cwd = codex_threads.strip_verbatim(info.cwd)
        for request in codex_ipc.find_approval_requests(snapshot):
            request_id = request.get("id")
            if type(request_id) not in (int, str):
                continue  # only exact JSON-RPC integer/string identities can be decided
            details = codex_ipc.approval_details(snapshot, request)
            item_id = details.get("item_id")
            key = (info.id, type(request_id), request_id, str(item_id or ""))
            if details["kind"] in ("permissions", "elicitation"):
                present.add(key)
                if key not in self._approval_notices:
                    self._approval_notices.add(key)
                    self._outbox.append(AttentionEvent(
                        agent=CODEX, session_id=info.id, title=codex_watch.task_name(info), cwd=cwd,
                        reason=tr('Waiting for approval (handle this in Codex Desktop)')))
                continue
            # Stale requests from an earlier turn cannot authorize a decision card.
            if details.get("turn_id") != turn_id or not isinstance(item_id, str) or not item_id:
                continue
            present.add(key)
            if key in self._approvals:
                continue
            command_cwd = codex_threads.strip_verbatim(str(details.get("cwd") or ""))
            excluded = self._excluded(cwd) or self._excluded(command_cwd)
            event = ApprovalEvent(
                agent=CODEX, session_id=info.id, title=codex_watch.task_name(info), cwd=cwd,
                request_id=request_id, method=request["method"], item_id=item_id, turn_id=turn_id,
                approval_id=details.get("approval_id"), digest=codex_ipc.approval_digest(request),
                kind=details["kind"], command="" if excluded else details["command"],
                command_cwd="" if excluded else command_cwd,
                reason="" if excluded else str(details.get("reason") or ""),
                network=None if excluded else details["network"],
                extra_permissions=None if excluded else details["extra_permissions"],
                files=[] if excluded else list(details["files"]),
                available_decisions=details["available_decisions"], excluded=excluded)
            self._approvals[key] = event
            self._outbox.append(event)
        for key in [key for key in self._approvals if key[0] == info.id and key not in present]:
            self._outbox.append(replace(self._approvals.pop(key), closed=True, by="pc"))
        self._approval_notices = {key for key in self._approval_notices if key[0] != info.id or key in present}

    def _on_turn_finished(self, watch: codex_watch.ThreadWatch, turn: TurnSummary, now: float) -> None:
        key = (watch.info.id, turn.turn_id)
        if key in self._emitted or key in self._deferred:
            return
        event = self._turn_event(watch.info, turn, final=self.failure_grace_s <= 0)
        if event is None:
            self._deferred[key] = _Deferred(watch.info, turn, now + self.failure_grace_s)
            return
        self._queue(key, event)

    def _flush(self, now: float) -> None:
        for key, item in list(self._deferred.items()):
            event = self._turn_event(item.info, item.turn, final=now >= item.deadline)
            if event is not None:
                del self._deferred[key]
                self._queue(key, event)

    def _queue(self, key: tuple[str, str], event: TurnEvent) -> None:
        self._emitted[key] = None
        if len(self._emitted) > _REMEMBERED:
            del self._emitted[next(iter(self._emitted))]
        self._outbox.append(event)

    def _turn_event(self, info: ThreadInfo, turn: TurnSummary, *, final: bool) -> TurnEvent | None:
        """The event for a finished turn; None while a resumable failure waits for codex-watch's plan."""

        cwd = codex_threads.strip_verbatim(info.cwd)
        labels = origin_labels(turn.user_text)
        quiet = False
        if turn.status == "failed":
            resumable, reason = codex_watch.classify(turn.error_info, turn.error)
            due = codex_watch.continuation_due(info.id, turn.turn_id, log_dir=self.watch_log_dir) if resumable else None
            if due is not None:
                # continuation_due adds one watcher step to the planned time.
                labels.append(tr('codex-watch will auto-continue at {v0}', v0=_hhmm(due - codex_watch._STEP_S)))
                quiet = True
            elif resumable and not final:
                return None
            else:
                labels.append(reason)
        error = turn.error
        if turn.status == "aborted" and not error:
            error = tr('This turn was aborted ({v0})', v0=turn.abort_reason) if turn.abort_reason else tr('This turn was aborted')
        final_text = turn.final_message
        if self._excluded(cwd):
            final_text = None
            labels.append(tr('Content hidden'))
        watch = self.watcher.watches.get(info.id)
        collector = watch.collector if watch is not None else None
        plan = (turn.status == "completed" and isinstance(collector, CommentaryCollector)
                and bool(collector.plans.get(turn.turn_id)))
        limits = self.quota()
        return TurnEvent(
            agent=CODEX,
            session_id=info.id,
            title=codex_watch.task_name(info),
            cwd=cwd,
            status=turn.status if turn.status in _FINISHED else "completed",
            final_text=final_text,
            error=error,
            turn_id=turn.turn_id,
            started_at=_epoch(turn.started_at),
            completed_at=_epoch(turn.completed_at),
            labels=labels,
            quiet=quiet,
            plan=plan,
            quota=limits,
        )

    def quota(self) -> Quota | None:
        """The account's plan limits from the newest token_count of any watched local conversation."""

        with self._lock:
            newest: tuple[float, dict[str, Any]] | None = None
            for watch in self.watcher.watches.values():
                seen = watch.collector.rate_limits if isinstance(watch.collector, CommentaryCollector) else None
                if seen is not None and (newest is None or seen[0] > newest[0]):
                    newest = seen
        return quota.codex_quota(newest[1], newest[0]) if newest is not None else None

    # --- listing -------------------------------------------------------------------

    def sessions(self, limit: int) -> list[SessionView]:
        """Recent conversations (no sub-agents, archived or automation runs), remote ones included; running first."""

        if limit <= 0:
            return []
        threads = [info for info in self.load_threads() if not (info.is_subagent or info.archived)]
        pid = self._pid()
        app_start = self._app_start(pid)
        with self._lock:
            views = [self._view(info, pid is not None, app_start) for info in threads]
        if self.remote is not None:
            views.extend(self.remote.sessions(limit))
        views.sort(key=lambda view: (view.state not in ("running", "waiting"), -view.last_activity))
        return views[:limit]

    def _remote_owns(self, thread_id: str) -> bool:
        return self.remote is not None and self.remote.owns(thread_id)

    def poll_remote(self) -> None:
        """One poll of the remote-host threads (slow: run it on its own thread)."""

        if self.remote is not None:
            self.remote.poll()

    def view(self, thread_id: str) -> SessionView | None:
        if self._remote_owns(thread_id):
            return self.remote.view(thread_id)  # type: ignore[union-attr]
        info = self._find(thread_id)
        if info is None:
            return None
        pid = self._pid()
        app_start = self._app_start(pid)
        with self._lock:
            return self._view(info, pid is not None, app_start)

    def _view(self, info: ThreadInfo, app_running: bool, app_start: float | None = None) -> SessionView:
        cwd = codex_threads.strip_verbatim(info.cwd)
        state, detail = "idle", ""
        watch = self.watcher.watches.get(info.id)
        turns = watch.collector.ordered() if watch is not None else []
        last = turns[-1] if turns else None
        if last is not None and last.status == "running":
            if not app_running:
                state, detail = "unknown", tr('Codex is not running; the previous turn has not ended')
            elif _cut_by_restart(_epoch(last.started_at), app_start):
                # An app restart leaves the turn open in the rollout forever; it cannot still run.
                detail = tr('The previous turn was interrupted by a Codex restart')
            else:
                state = "running"
                collector = watch.collector if watch is not None else None
                if isinstance(collector, CommentaryCollector) and not self._excluded(cwd):
                    notes = collector.commentary.get(last.turn_id)
                    detail = _one_line(notes[-1]) if notes else ""  # the latest progress note
        elif last is not None and last.status == "failed":
            detail = tr('Previous turn failed: {v0}', v0=codex_watch.classify(last.error_info, last.error)[1])
        elif last is not None and last.status == "aborted":
            detail = tr('Previous turn was aborted')
        return SessionView(
            agent=CODEX,
            id=info.id,
            title=codex_watch.task_name(info),
            cwd=cwd,
            state=state,
            last_activity=_epoch(info.updated_at) or 0.0,
            live=codex_watch.watchable(info) and app_running,
            detail=detail,
            kind=info.source.strip().lower() if info.is_headless else "desktop",
        )

    # --- reading -------------------------------------------------------------------

    def _read(self, info: ThreadInfo, k: int) -> tuple[CommentaryCollector, list[TurnSummary]]:
        path = info.rollout
        collector = CommentaryCollector()
        if path is None or not path.is_file():
            return collector, []
        budget = min(READ_START_BYTES, READ_MAX_BYTES)
        while True:
            records, complete = codex_threads.read_tail_records(path, budget)
            collector = CommentaryCollector()
            collector.feed(records)
            ordered = collector.ordered()
            # One extra turn guarantees the oldest reported turn was read from its start.
            if complete or len(ordered) > k or budget >= READ_MAX_BYTES:
                break
            budget = min(budget * 4, READ_MAX_BYTES)
        return collector, ordered[-k:]

    def read_turns(self, thread_id: str, k: int) -> list[dict[str, Any]]:
        """The last k turns, oldest first. Content fields are blank for content-excluded folders."""

        if self._remote_owns(thread_id):
            return self.remote.read_turns(thread_id, k)  # type: ignore[union-attr]
        info = self._find(thread_id)
        if info is None:
            return []
        collector, turns = self._read(info, max(1, min(int(k), 20)))
        excluded = self._excluded(codex_threads.strip_verbatim(info.cwd))
        items = []
        for turn in turns:
            details = collector.details(turn.turn_id)
            item = {
                "turn_id": turn.turn_id,
                "status": turn.status,
                "user_text": turn.user_text,
                "final_message": turn.final_message,
                "error": turn.error,
                "error_info": turn.error_info,
                "abort_reason": turn.abort_reason,
                "commentary": details["commentary"],
                "plan": details["plan"],
                "commands": details["commands"],
                "started_at": _epoch(turn.started_at),
                "completed_at": _epoch(turn.completed_at),
                "content_excluded": excluded,
            }
            if excluded:
                item.update(user_text="", final_message=None, commentary=[], plan="")
            items.append(item)
        return items

    def full_text(self, thread_id: str) -> str | None:
        """Final answer of the newest finished turn (a running turn is skipped); None if none or excluded."""

        if self._remote_owns(thread_id):
            return self.remote.full_text(thread_id)  # type: ignore[union-attr]
        info = self._find(thread_id)
        if info is None or self._excluded(codex_threads.strip_verbatim(info.cwd)):
            return None
        _, turns = self._read(info, 2)
        for turn in reversed(turns):
            if turn.status != "running":
                return turn.final_message
        return None

    def busy(self, thread_id: str) -> bool | None:
        """Running a turn: the rollout says so and Codex Desktop does not deny it; None when unknown."""

        info = self._find(thread_id)
        if info is None:
            return None
        busy = codex_threads.thread_activity(info)["busy"]
        if busy is not False:
            confirmed = self._busy_check(info.id)
            if confirmed is not None:
                busy = confirmed  # False e.g. for a turn an app restart cut short
        return busy

    # --- writing -------------------------------------------------------------------

    def submit_send(self, thread_id: str, text: str, *, nonce: str, if_busy: str = "wait",
                    clash_check: bool = True) -> str:
        """Start a raw "codex" job that sends text (led by nonce) and follows the turn; returns the job id."""

        info, params = self._send_params(thread_id, text, nonce=nonce, if_busy=if_busy)
        with self._send_lock:
            if clash_check:
                self._refuse_clash(info.id)
            job_id = jobs.create_job("codex", params)
        return self._spawn(job_id)

    def _send_params(self, thread_id: str, text: str, *, nonce: str, if_busy: str) -> tuple[ThreadInfo, dict[str, Any]]:
        """The checks of a send that need no lock, and the job's parameters (ValueError when refused)."""

        if not text.strip():
            raise ValueError(tr('Message cannot be empty.'))
        if len(text) > MAX_SEND_CHARS:
            raise ValueError(tr('Message is too long ({v0} characters, limit {v1}).', v0=len(text), v1=MAX_SEND_CHARS))
        if not _NONCE_RE.fullmatch(nonce or ""):
            raise ValueError(tr('nonce must be 1-64 non-whitespace characters.'))
        if if_busy not in IF_BUSY:
            raise ValueError(tr('if_busy must be one of {v0}.', v0=tr(', ').join(IF_BUSY)))
        try:
            info = codex_threads.resolve_thread(thread_id)
        except codex_threads.ThreadLookupError as exc:
            raise ValueError(str(exc)) from exc
        if not codex_watch.watchable(info):
            raise ValueError(tr('“{v0}” is not a Codex Desktop conversation (it is a sub-agent, CLI or exec session); messages cannot be sent to it.', v0=codex_watch.task_name(info)))
        available = codex_app_client.availability()
        if not available.get("node") or not available.get("app_tools_server"):
            raise ValueError(tr("Cannot find Codex Desktop's app-tools component. Make sure Codex Desktop is installed and has been run."))
        if not available.get("pipes"):
            raise ValueError(tr('No Codex Desktop app-tools pipe detected. Open Codex Desktop first.'))
        params = {
            "thread_id": info.id,
            "thread_name": info.name,
            "message": text,
            "raw": True,
            "nonce": nonce,
            "if_busy": if_busy,
            "timeout_s": SEND_TIMEOUT_S,
        }
        return info, params

    @staticmethod
    def _refuse_clash(thread_id: str) -> None:
        """ValueError while a codex job uses the conversation (call with _send_lock held)."""

        active = jobs.list_jobs(200, active_only=True)
        clash = [item for item in active if item.get("kind") == "codex" and item.get("thread_id") == thread_id]
        if clash:
            raise ValueError(tr('Job {v0} is using this Codex conversation; wait for it to finish.', v0=clash[0]['job_id']))

    @staticmethod
    def _spawn(job_id: str) -> str:
        try:
            jobs.spawn_worker(job_id)
        except Exception as exc:
            jobs.update_state(job_id, status="failed", phase="spawn_failed", error=str(exc),
                              finished_at=jobs.now_iso())
            raise
        return job_id

    def job(self, job_id: str) -> dict[str, Any]:
        return {"state": jobs.refresh_state(job_id), "result": jobs.load_result(job_id)}

    def _desktop_thread(self, thread_id: str) -> ThreadInfo:
        """The local Codex Desktop conversation a write from Telegram may touch (ValueError otherwise)."""

        if self._remote_owns(thread_id):
            raise ValueError(tr('This Codex conversation is on a remote host; Telegram can only view it.'))
        info = self._find(thread_id)
        if info is None or info.archived:
            raise ValueError(tr('Cannot find this Codex conversation (it may be archived).'))
        if not codex_watch.watchable(info):
            raise ValueError(tr('“{v0}” is not a Codex Desktop conversation; Telegram cannot operate on it.', v0=codex_watch.task_name(info)))
        if not self.desktop_running():
            raise ValueError(tr('Codex Desktop is not running.'))
        return info

    def answer_question(self, thread_id: str, call_id: str, answers: dict[str, list[str]]) -> None:
        """Answer a pending request_user_input (question id -> chosen labels or typed text) in Codex Desktop."""

        info = self._desktop_thread(thread_id)
        try:
            self.ipc.answer_user_input(info.id, call_id=call_id, answers=answers)
        except codex_ipc.CodexIpcError as exc:
            raise ValueError(str(exc)) from exc

    def decide_approval(self, thread_id: str, *, method: str, item_id: str, turn_id: str,
                        approval_id: str | None, digest: str, decision: str,
                        request_id: int | str | None = None) -> None:
        """Decide an unchanged local command/file request; IPC errors retain their exact type.

        request_id is the card's audit identity; codex_ipc resolves and preserves the
        current snapshot's exact JSON-RPC id after checking every bound field/digest.
        """

        info = self._desktop_thread(thread_id)
        self.ipc.decide_approval(info.id, method=method, item_id=item_id, turn_id=turn_id,
                                 approval_id=approval_id, digest=digest, decision=decision,
                                 connect=self.ipc.connect)

    def answer_async_question(self, thread_id: str, call_id: str, turn_id: str,
                              answers: dict[int, tuple[str, str]], *, client_id: str, marker: str) -> dict[str, Any]:
        """Steer an async reply into its running turn, or send it as raw input once that turn ended."""

        info = self._desktop_thread(thread_id)

        def current() -> dict[str, Any]:
            state = self.async_question_state(info.id, call_id)
            if state is None or not state["waiting"]:
                raise ValueError(tr('This question no longer needs an answer (it may already be answered, or the conversation has continued)'))
            if any(index in state["answered"] for index in answers):
                raise ValueError(tr('This question has already been answered'))
            return state

        st = current()
        envelope = codex_threads.build_question_reply(call_id, answers)
        if st["turn_running"] and st["turn_last"]:
            try:
                result = self.ipc.steer_question_reply(
                    info.id, turn_id=turn_id, call_id=call_id, indices=list(answers), envelope=envelope,
                    client_id=client_id, cwd=codex_threads.strip_verbatim(info.cwd))
                return {"via": "ipc", "turn_id": result["turn_id"], "other_turn": result["other_turn"]}
            except (codex_ipc.OutcomeUnknown, codex_ipc.AlreadyAnswered):
                raise
            except codex_ipc.CodexIpcError as exc:
                no_owner = (exc.code == "no-client-found" or any(text in str(exc) for text in (
                    "No Codex Desktop window owns this conversation",
                    "no Codex Desktop window has this conversation open",
                    "Waiting for Codex Desktop to send the conversation state",
                    "no-client-found", "没有窗口负责这个对话", "没有窗口打开这个对话",  # i18n: data
                    "等待 Codex Desktop 发来对话状态", "is not being streamed")))  # i18n: data
                if not isinstance(exc, codex_ipc.TurnEnded) and not no_owner:
                    raise ValueError(str(exc)) from exc
                # A missing owner/snapshot alone cannot authorize a second send. The rollout
                # must have reported the end before the ordinary app-tools path is usable.
                st = self.async_question_state(info.id, call_id)
                if st is None or st["turn_running"]:
                    raise ValueError(str(exc)) from exc

        with self._send_lock:
            st = current()
            if not st["turn_last"] or st["thread_busy"]:
                raise ValueError(tr('A new turn has started in this conversation; answer on the PC'))
            for item in jobs.list_jobs(200, active_only=True):
                if item.get("kind") != "codex" or item.get("thread_id") != info.id:
                    continue
                request = jobs.load_request(item["job_id"])
                message = (request.get("params") or {}).get("message")
                if isinstance(message, str) and codex_threads.QUESTION_REPLY_OPEN in message and call_id in message:
                    raise ValueError(tr('The previous answer is still being sent; please wait'))
            # An existing Telegram-started job may still be following the ended turn. Only
            # another reply to this call clashes; the worker checks the conversation is idle.
            job_id = self.submit_send(info.id, envelope, nonce=marker, if_busy="fail", clash_check=False)
        return {"via": "app_tools", "job_id": job_id}

    def implement_plan(self, thread_id: str, turn_id: str, *, nonce: str) -> str:
        """Do what Codex Desktop's "implement this plan" does: Default mode, then send the plan; the job id.

        Everything that can refuse the send (busy, app-tools missing, another job on the
        conversation, a plan too long to send) is checked before the mode switch; holding
        _send_lock from the clash check to the job creation keeps another send out in between.
        """

        info = self._desktop_thread(thread_id)
        if self.busy(info.id):
            raise ValueError(tr('This conversation is running; wait until the turn ends before implementing a plan.'))
        _, params = self._send_params(info.id, IMPLEMENT_PLAN_PREFIX, nonce=nonce, if_busy="fail")

        def sendable(plan: str) -> None:
            text = f"{IMPLEMENT_PLAN_PREFIX}\n{plan}"
            if len(text) > MAX_SEND_CHARS:
                raise ValueError(tr('The plan is too long ({v0} characters, limit {v1}); implement it in Codex Desktop.', v0=len(text), v1=MAX_SEND_CHARS))

        with self._send_lock:
            self._refuse_clash(info.id)
            try:
                plan = self.ipc.prepare_plan_implementation(info.id, turn_id=turn_id, check=sendable)
            except codex_ipc.CodexIpcError as exc:
                raise ValueError(str(exc)) from exc
            params["message"] = f"{IMPLEMENT_PLAN_PREFIX}\n{plan}"
            try:
                job_id = jobs.create_job("codex", params)
            except Exception as exc:
                raise ValueError(tr('The conversation switched back to default mode, but the plan implementation message could not be sent: {v0}', v0=exc)) from exc
        try:
            return self._spawn(job_id)
        except Exception as exc:
            raise ValueError(tr('The conversation switched back to default mode, but the plan implementation message could not be sent: {v0}', v0=exc)) from exc

    # --- new tasks -----------------------------------------------------------------

    def anchor_thread(self) -> str | None:
        """Executor thread for app-tools calls: cfg.codex_anchor_thread, else the newest Desktop conversation."""

        configured = str(getattr(self.cfg, "codex_anchor_thread", "") or "").strip()
        if configured:
            # A UUID is checked too: an archived or deleted anchor fails the app's read_thread
            # probe on every pipe, and each app-tools call would hang until its timeout.
            try:
                return codex_threads.resolve_thread(configured).id
            except codex_threads.ThreadLookupError:
                log.warning("codex_anchor_thread is missing or archived; using the newest Codex Desktop conversation")
        best: tuple[float, str] | None = None
        for info in self.load_threads():
            if codex_watch.watchable(info):
                updated = _epoch(info.updated_at) or 0.0
                if best is None or updated > best[0]:
                    best = (updated, info.id)
        return best[1] if best else None

    def _require_anchor(self) -> str:
        anchor = self.anchor_thread()
        if anchor is None:
            raise ValueError(tr('No Codex Desktop conversation is available as an anchor; open one in Codex Desktop first, or set codex_anchor_thread in the configuration.'))
        return anchor

    def _call_app_tool(self, name: str, arguments: dict[str, Any], *, timeout_s: float) -> str:
        anchor = self._require_anchor()
        with codex_app_client.open_session(thread_id=anchor) as session:
            result = session.call_tool(name, arguments, thread_id=anchor, timeout_s=timeout_s)
        return codex_app_client.tool_text(result)

    def list_projects(self) -> list[dict[str, Any]]:
        """Projects for create_thread: {"id", "name", "path", "kind", "git", "raw"} each (entries without id dropped)."""

        data = _json_or_text(self._call_app_tool("list_projects", {}, timeout_s=30.0))
        return [project for project in map(_project, _project_items(data)) if project["id"]]

    def create_thread(self, prompt: str, *, project_id: str | None, title: str | None) -> dict[str, Any]:
        """Start a new Codex Desktop task; {"ok", "thread_id", "host_id", "result"} (ids None when not reported)."""

        if not prompt.strip():
            raise ValueError(tr('Prompt cannot be empty.'))
        arguments: dict[str, Any] = {"prompt": prompt, "target": thread_target(project_id)}
        if title and title.strip():
            arguments["title"] = title.strip()
        data = _json_or_text(self._call_app_tool("create_thread", arguments, timeout_s=60.0))
        thread_id, host_id = _created_ids(data)
        return {"ok": True, "thread_id": thread_id, "host_id": host_id, "result": data}


def __getattr__(name: str) -> str:
    """Resolve legacy message exports at access time, including for the pending bridge."""
    if name == 'LABEL_AGENT_TG_LEGACY':
        return tr('agent_telegram task')
    if name == 'LABEL_AUTO_CONTINUE':
        return tr('Auto-continue')
    if name == 'LABEL_TELEGRAM':
        return tr('From Telegram')
    if name == 'LABEL_EXCLUDED':
        return tr('Content hidden')
    raise AttributeError(name)

"""Codex Desktop conversations on SSH remote hosts, seen through the local app-tools pipe.

Codex Desktop can attach remote hosts over SSH. Their threads live on those hosts,
so the local state database and rollouts do not have them, but the app-tools tools
do: ``list_threads`` lists the recent threads of every host (``hostId`` such as
``remote-ssh-discovered:<ssh alias>``) and ``read_thread`` returns a thread's recent
turns with their items without opening it. RemoteCodex polls both, keeps a view per
remote thread and reports the turns that finish after the bridge started. It only
reads: sending to, stopping or approving in remote threads is not supported.

One app-tools session (a node child process without a window) is kept open and
serialized by a lock; any failed call closes it and the next call opens a new one.
``list_threads`` returns at most 50 unpinned threads across all hosts (plus every
pinned one), so a remote thread is seen while it is among the recent ones.

``list_threads`` can hang for a long time (the app's thread index; on one machine it timed
out for hours most days) while ``read_thread`` keeps answering in about a second. A failed
listing is retried with backoff, and meanwhile the threads already known are read directly:
running or waiting ones at every poll, the others every RECHECK_IDLE_S. Only threads that
appear during such a stretch wait for the listing.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent_telegram.i18n import tr
from agent_telegram import codex_app_client
from agent_telegram.tgbridge import progress
from agent_telegram.tgbridge.model import CODEX, AttentionEvent, Event, ProgressEvent, SessionView, TurnEvent

if TYPE_CHECKING:
    from agent_telegram.tgbridge.config import Config

log = logging.getLogger(__name__)

LOCAL_HOST = "local"
HOST_PREFIX = "remote-ssh-discovered:"
LIST_LIMIT = 50  # list_threads' maximum
TURN_LIMIT = 10  # read_thread's maximum
CHECK_TURNS = 3  # turns read when a thread changed
CALL_TIMEOUT_S = 30.0
LIST_TIMEOUT_S = 90.0  # list_threads may take minutes; read_thread answers in seconds
LIST_RETRY_MIN_S = 60.0  # after a failed listing, the next one waits this long, doubling ...
LIST_RETRY_MAX_S = 600.0  # ... up to this
RECHECK_IDLE_S = 300.0  # without a listing, an idle known thread is read this often
UNLISTED_MAX_S = 24 * 3600.0  # ... if a listing showed it within this long
MAX_COMMENTARY = 5
_TURN_STATUS = {"completed": "completed", "failed": "failed", "interrupted": "aborted", "cancelled": "aborted",
                "canceled": "aborted", "aborted": "aborted", "inProgress": "running", "in_progress": "running",
                "running": "running", "active": "running"}
_FINISHED = frozenset({"completed", "failed", "aborted"})
_SEEN_TURNS = 200  # remembered turn ids per thread
_DROP_AFTER_S = 7 * 24 * 3600  # threads gone from list_threads this long are forgotten


class ListUnavailable(codex_app_client.AppToolsError):
    """The listing failed (or waits for its retry) but the known threads were read directly."""


def host_alias(host_id: str) -> str:
    """remote-ssh-discovered:lab_box -> lab_box."""

    return host_id[len(HOST_PREFIX):] if host_id.startswith(HOST_PREFIX) else host_id


def epoch(value: Any) -> float:
    """updatedAt and friends come in seconds or milliseconds (also as strings); 0.0 when missing."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number or number <= 0:  # NaN or not a time
        return 0.0
    return number / 1000.0 if number > 1e11 else number


def _one_line(text: str, limit: int = 60) -> str:
    line = " ".join(str(text or "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _text_of(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict)).strip()
    return str(item.get("text") or "").strip()


def _error_text(error: Any) -> str:
    if not error:
        return ""
    if isinstance(error, dict):
        text = error.get("message") or error.get("detail") or json.dumps(error, ensure_ascii=False)
        return str(text)[:2000]
    return str(error)[:2000]


def thread_state(status: Any) -> tuple[str, str]:
    """(SessionView state, waiting reason) for a list_threads status: a string or {"type", "activeFlags"}."""

    flags: list[str] = []
    if isinstance(status, dict):
        flags = [str(flag) for flag in status.get("activeFlags") or [] if flag]
        status = status.get("type")
    status = str(status or "")
    if status in ("idle", "notLoaded"):
        return "idle", ""
    if status == "active":
        waiting = [flag for flag in flags if "approval" in flag.lower() or "input" in flag.lower()]
        if waiting:
            return "waiting", tr('Waiting for approval') if any("approval" in flag.lower() for flag in waiting) else tr('Waiting for input')
        return "running", ""
    return "unknown", ""


def summarize_turn(turn: dict[str, Any]) -> dict[str, Any]:
    """One read_thread turn in the dict shape CodexSource.read_turns returns."""

    items = [item for item in turn.get("items") or [] if isinstance(item, dict)]
    users = [_text_of(item) for item in items if item.get("type") == "userMessage"]
    agents = [item for item in items if item.get("type") == "agentMessage"]
    finals = [str(item.get("text") or "") for item in agents if item.get("phase") == "final_answer"]
    commentary = [str(item.get("text") or "") for item in agents if item.get("phase") != "final_answer"]
    raw = str(turn.get("status") or "")
    status = _TURN_STATUS.get(raw, raw or "unknown")
    final = finals[-1] if finals else None
    if final is None and status == "completed" and commentary:
        final = commentary[-1]  # an older turn shape without phases
    return {
        "turn_id": str(turn.get("id") or ""),
        "status": status,
        "user_text": "\n".join(text for text in users if text),
        "final_message": final,
        "error": _error_text(turn.get("error")),
        "commentary": commentary[-MAX_COMMENTARY:],
        "commentary_count": len(commentary),
        "plan": "",
        "started_at": epoch(turn.get("startedAt")) or None,
        "completed_at": epoch(turn.get("completedAt")) or None,
    }


def _newest_first(data: dict[str, Any]) -> list[dict[str, Any]]:
    turns = [turn for turn in data.get("turns") or [] if isinstance(turn, dict)]
    page = data.get("page") if isinstance(data.get("page"), dict) else {}
    if str(page.get("order") or "newest_first") == "oldest_first":
        turns.reverse()
    return turns


@dataclass
class RemoteThread:
    id: str
    host_id: str
    title: str
    cwd: str
    status: Any  # as list_threads reports it
    updated: float
    summary: str = ""
    listed_at: float = 0.0  # last poll that listed it
    checked_updated: float = -1.0  # updated when the turns were last read; -1 = never
    read_at: float = 0.0  # when its turns were last read
    progress: str = ""  # latest commentary of a running turn
    progress_seen: dict[str, int] = field(default_factory=dict)  # running turn -> commentary items reported
    waiting_reported: bool = False
    seen: dict[str, None] = field(default_factory=dict)  # turn ids reported or older than the bridge


class RemoteCodex:
    """Remote-host Codex threads: polling, views, reading. Thread-safe."""

    def __init__(
        self,
        cfg: Config,
        emit: Callable[[Event], None],
        *,
        anchor: Callable[[], str | None],
        open_session: Callable[..., Any] = codex_app_client.open_session,
        desktop_running: Callable[[], bool] = lambda: True,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.emit = emit
        self.anchor = anchor
        self._open_session = open_session
        self.desktop_running = desktop_running
        self.clock = clock
        self.started = clock()
        self.threads: dict[str, RemoteThread] = {}
        self.unavailable: set[str] = set()
        self.last_error = ""
        self.last_poll = 0.0
        self._session: Any = None
        self._anchor_id: str | None = None
        self._list_due_at = 0.0  # no listing before this (backoff after a failed one)
        self._list_backoff = 0.0
        self.list_error = ""  # why the last listing failed ("" after a good one)
        self._call_lock = threading.Lock()
        self._lock = threading.Lock()

    # --- app-tools session ---

    def close(self) -> None:
        with self._call_lock:
            self._close_locked()

    def _close_locked(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass

    def _call(self, name: str, arguments: dict[str, Any], *, timeout_s: float = CALL_TIMEOUT_S) -> dict[str, Any]:
        """One app-tools call on the shared session (JSON result); a failure closes the session."""

        with self._call_lock:
            if self._session is None:
                anchor = self.anchor()
                if not anchor:
                    raise codex_app_client.AppToolsError(tr('No local Codex Desktop conversation is available as the app-tools execution thread'))
                self._session = self._open_session(thread_id=anchor, timeout_s=CALL_TIMEOUT_S)
                self._anchor_id = anchor
            try:
                result = self._session.call_tool(name, arguments, thread_id=self._anchor_id, timeout_s=timeout_s)
                data = json.loads(codex_app_client.tool_text(result))
            except Exception:
                self._close_locked()
                raise
        if not isinstance(data, dict):
            raise codex_app_client.AppToolsError(tr('{v0} did not return a JSON object', v0=name))
        return data

    # --- polling ---

    def _wanted(self, host_id: str) -> bool:
        hosts = [str(host).strip() for host in self.cfg.codex_remote_hosts if str(host).strip()]
        return not hosts or host_id in hosts or host_alias(host_id) in hosts

    def _excluded(self, cwd: str) -> bool:
        return bool(cwd) and bool(self.cfg.excluded(cwd))

    def poll(self) -> None:
        """List the threads, read those that changed and emit their finished turns (outside the locks).

        A listing that fails or waits for its retry does not stop the known threads from being
        read; ListUnavailable then reports it. Raises the listing's own error when nothing
        could be read either (the caller backs off).
        """

        if not self.desktop_running():
            self.close()  # the node child would only fail against a dead pipe
            return
        now = self.clock()
        data: dict[str, Any] | None = None
        if now >= self._list_due_at:
            try:
                data = self._call("list_threads", {"limit": LIST_LIMIT}, timeout_s=LIST_TIMEOUT_S)
            except Exception as exc:  # noqa: BLE001 - the known threads are still read below
                self.list_error = f"list_threads: {type(exc).__name__}: {exc}"
                self._list_backoff = min(max(self._list_backoff * 2, LIST_RETRY_MIN_S), LIST_RETRY_MAX_S)
                self._list_due_at = now + self._list_backoff
                failure: Exception = exc
            else:
                self.list_error, self._list_backoff, self._list_due_at = "", 0.0, 0.0
        else:
            failure = ListUnavailable(self.list_error or tr('list_threads is waiting to retry'))
        if data is not None:
            due, events = self._listed(data, now)
        else:
            due, events = self._unlisted(now), []
        self.last_error = self.list_error  # a failed read below puts its own
        read_ok = read_failed = False
        for thread in due:
            found = self._check_turns(thread, listed=data is not None)
            if found is None:
                read_failed = True
                continue
            read_ok = True
            events.extend(found)
        for event in events:
            self.emit(event)
        if data is None:
            if read_ok or (self.threads and not read_failed):
                raise ListUnavailable(self.list_error or tr('list_threads is waiting to retry'))
            raise failure

    def _listed(self, data: dict[str, Any], now: float) -> tuple[list[RemoteThread], list[Event]]:
        """Merge a listing into the known threads: (threads to read, attention events)."""

        listed: list[RemoteThread] = []
        for item in (data.get("pinnedThreads") or []) + (data.get("threads") or []):
            if not isinstance(item, dict):
                continue
            host_id = str(item.get("hostId") or "")
            if item.get("kind") != "codex" or host_id in ("", LOCAL_HOST) or not item.get("id"):
                continue
            if not self._wanted(host_id):
                continue
            listed.append(RemoteThread(
                id=str(item["id"]), host_id=host_id, title=str(item.get("title") or ""),
                cwd=str(item.get("cwd") or ""), status=item.get("status"), updated=epoch(item.get("updatedAt")),
                summary=str(item.get("summary") or ""), listed_at=now))
        due: list[RemoteThread] = []
        events: list[Event] = []
        with self._lock:
            self.unavailable = {str(host) for host in data.get("unavailableHosts") or [] if host}
            self.last_poll = now
            for fresh in listed:
                known = self.threads.get(fresh.id)
                if known is None:
                    self.threads[fresh.id] = known = fresh
                    if fresh.updated and fresh.updated <= self.started and thread_state(fresh.status)[0] == "idle":
                        known.checked_updated = fresh.updated  # nothing new since before the bridge started
                        continue
                    due.append(known)
                    continue
                was_busy = thread_state(known.status)[0] in ("running", "waiting")
                known.title, known.cwd, known.summary, known.listed_at = fresh.title, fresh.cwd, fresh.summary, now
                known.status, known.updated = fresh.status, max(known.updated, fresh.updated)
                state, _ = thread_state(known.status)
                if known.updated > known.checked_updated or was_busy or state in ("running", "waiting"):
                    due.append(known)
            for thread in due:
                event = self._attention(thread)
                if event is not None:
                    events.append(event)
            for thread_id, thread in list(self.threads.items()):
                if now - thread.listed_at > _DROP_AFTER_S:
                    del self.threads[thread_id]
        return due, events

    def _unlisted(self, now: float) -> list[RemoteThread]:
        """Known threads to read without a listing: busy ones always, idle ones every RECHECK_IDLE_S."""

        with self._lock:
            self.last_poll = now
            return [thread for thread in self.threads.values()
                    if now - thread.listed_at < UNLISTED_MAX_S
                    and (thread_state(thread.status)[0] in ("running", "waiting")
                         or now - thread.read_at >= RECHECK_IDLE_S)]

    def _attention(self, thread: RemoteThread) -> AttentionEvent | None:
        state, reason = thread_state(thread.status)
        if state != "waiting":
            thread.waiting_reported = False
            return None
        if thread.waiting_reported:
            return None
        thread.waiting_reported = True
        return AttentionEvent(agent=CODEX, session_id=thread.id, title=self.title(thread), cwd=thread.cwd,
                              reason=tr('{v0} (remote {v1}; handle this in Codex Desktop)', v0=reason, v1=host_alias(thread.host_id)))

    def _check_turns(self, thread: RemoteThread, *, listed: bool = True) -> list[Event] | None:
        """Read the thread's newest turns and report what finished; None when the read failed.

        Read without a listing (``listed`` False), the newest turn also stands for the
        thread's status, so a turn that started meanwhile keeps it read at every poll.
        """

        try:
            data = self._call("read_thread", {"threadId": thread.id, "hostId": thread.host_id, "turnLimit": CHECK_TURNS})
        except Exception as exc:  # noqa: BLE001 - one thread failing must not stop the others
            self.last_error = f"{host_alias(thread.host_id)}: {type(exc).__name__}: {exc}"
            log.warning(tr('Failed to read remote Codex conversation (%s): %s'), host_alias(thread.host_id), exc)
            return None
        turns = [summarize_turn(turn) for turn in _newest_first(data)]
        events: list[Event] = []
        with self._lock:
            thread.checked_updated = thread.updated
            thread.read_at = self.clock()
            newest = turns[0] if turns else None
            if not listed and newest is not None and thread_state(thread.status)[0] != "waiting":
                thread.status = "active" if newest["status"] == "running" else "idle"
            thread.progress = ""
            running: ProgressEvent | None = None
            if newest is not None and newest["status"] == "running":
                if newest["commentary"]:
                    thread.progress = _one_line(newest["commentary"][-1])
                running = self._progress_event(thread, newest)
            else:
                thread.progress_seen = {}
            for summary in reversed(turns):  # oldest first
                turn_id = summary["turn_id"]
                if not turn_id or summary["status"] == "running" or turn_id in thread.seen:
                    continue
                if summary["status"] not in _FINISHED and not summary["completed_at"]:
                    continue  # an unknown status without an end time may still be running
                thread.seen[turn_id] = None
                while len(thread.seen) > _SEEN_TURNS:
                    del thread.seen[next(iter(thread.seen))]
                ended = summary["completed_at"] or thread.updated
                if ended < self.started:
                    continue  # finished before the bridge started
                events.append(self._event(thread, summary))
            if running is not None:
                events.append(running)  # after the finished turns: their pushes close the previous message
        return events

    def _progress_event(self, thread: RemoteThread, turn: dict[str, Any]) -> ProgressEvent | None:
        """Commentary of the running turn that was not reported yet (a turn running at start: only newer)."""

        if not getattr(self.cfg, "progress_enabled", True):
            return None
        turn_id = turn["turn_id"]
        total = int(turn.get("commentary_count") or 0)
        seen = thread.progress_seen.get(turn_id)
        if seen is None:
            seen = total if (turn["started_at"] or 0.0) < self.started else 0
        thread.progress_seen = {turn_id: total}
        if total <= seen or self._excluded(thread.cwd):
            return None
        notes = turn["commentary"][-(total - seen):]
        if not notes:
            return None
        return ProgressEvent(agent=CODEX, session_id=thread.id, title=self.title(thread), cwd=thread.cwd,
                             texts=list(notes), kind="commentary", asks_user=any(map(progress.asks_user, notes)))

    def _event(self, thread: RemoteThread, summary: dict[str, Any]) -> TurnEvent:
        labels = [tr('Remote {v0}', v0=host_alias(thread.host_id))]
        excluded = self._excluded(thread.cwd)
        if excluded:
            labels.append(tr('Content hidden'))
        status = summary["status"]
        if status not in _FINISHED:  # an unknown status that has an end time
            status = "failed" if summary["error"] else "completed"
        error = summary["error"] or (tr('This turn was aborted') if status == "aborted" else "")
        return TurnEvent(
            agent=CODEX, session_id=thread.id, title=self.title(thread), cwd=thread.cwd, status=status,
            final_text=None if excluded else summary["final_message"],
            error="" if excluded and status == "completed" else error,
            turn_id=summary["turn_id"], started_at=summary["started_at"], completed_at=summary["completed_at"],
            labels=labels,
        )

    # --- views and reading ---

    @staticmethod
    def title(thread: RemoteThread) -> str:
        name = thread.title.strip() or _one_line(thread.summary, 40) or thread.id[:8]
        return f"{host_alias(thread.host_id)}·{name}"

    def _view(self, thread: RemoteThread) -> SessionView:
        state, reason = thread_state(thread.status)
        detail = reason or (thread.progress if state == "running" and not self._excluded(thread.cwd) else "")
        if thread.host_id in self.unavailable:
            state, detail = "unknown", tr('Remote host is unreachable')
        return SessionView(agent=CODEX, id=thread.id, title=self.title(thread), cwd=thread.cwd, state=state,
                           last_activity=thread.updated, live=False, detail=detail,
                           kind=f"remote:{host_alias(thread.host_id)}")

    def sessions(self, limit: int) -> list[SessionView]:
        with self._lock:
            views = [self._view(thread) for thread in self.threads.values()]
        views.sort(key=lambda view: (view.state not in ("running", "waiting"), -view.last_activity))
        return views[:max(0, limit)]

    def owns(self, thread_id: str) -> bool:
        with self._lock:
            return thread_id.strip().lower() in {key.lower() for key in self.threads}

    def _get(self, thread_id: str) -> RemoteThread | None:
        wanted = thread_id.strip().lower()
        with self._lock:
            return next((thread for key, thread in self.threads.items() if key.lower() == wanted), None)

    def view(self, thread_id: str) -> SessionView | None:
        thread = self._get(thread_id)
        if thread is None:
            return None
        with self._lock:
            return self._view(thread)

    def read_turns(self, thread_id: str, k: int) -> list[dict[str, Any]]:
        """The last k turns (at most 10), oldest first; content fields blank for content-excluded folders."""

        thread = self._get(thread_id)
        if thread is None:
            return []
        data = self._call("read_thread", {"threadId": thread.id, "hostId": thread.host_id,
                                          "turnLimit": max(1, min(int(k), TURN_LIMIT))})
        turns = [summarize_turn(turn) for turn in reversed(_newest_first(data))]
        excluded = self._excluded(thread.cwd)
        for turn in turns:
            turn["content_excluded"] = excluded
            if excluded:
                turn.update(user_text="", final_message=None, commentary=[], plan="")
        return turns

    def full_text(self, thread_id: str) -> str | None:
        """Final answer of the newest finished turn; None if none or content-excluded."""

        for turn in reversed(self.read_turns(thread_id, 2)):
            if turn["status"] != "running":
                return turn["final_message"]
        return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            hosts = sorted({host_alias(thread.host_id) for thread in self.threads.values()})
            return {"threads": len(self.threads), "hosts": hosts,
                    "unavailable": sorted(host_alias(host) for host in self.unavailable),
                    "last_poll": self.last_poll, "last_error": self.last_error, "list_error": self.list_error}

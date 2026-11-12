"""Claude Code conversations for the Telegram bridge.

Finished turns normally arrive through the tgbridge hooks (hook_report POSTs
Stop / StopFailure / SessionStart to the bridge, which calls ClaudeSource.on_hook).
This module adds what the hooks cannot tell:

- which sessions have a live process and whether they are busy, idle or waiting
  (the registry ~/.claude/sessions/<pid>.json; files of exited processes stay behind,
  so an entry counts only while jobs.process_alive(pid, procStart) holds);
- Claude Desktop titles, archive/scheduled flags and post-turn summaries
  (claude-code-sessions/<acct>/<org>/local_<uuid>.json, cached by mtime and size);
- on-demand transcript reads (~/.claude/projects/<slug>/<session>.jsonl) for /read,
  /full, delivery confirmation and a busy -> idle fallback push for sessions that
  have not loaded the hooks yet;
- delivery of Telegram messages through the session's own inbox pipe.

Everything here only reads Claude's files; the inbox pipe is the only write path.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from agent_telegram.i18n import tr
from agent_telegram import jobs
from agent_telegram.tgbridge import progress, quota
from agent_telegram.tgbridge.model import CLAUDE, AttentionEvent, Event, ProgressEvent, Quota, SessionView, TurnEvent, session_key

if TYPE_CHECKING:
    from agent_telegram.tgbridge.config import Config, SecretStore

log = logging.getLogger(__name__)

DESKTOP_REFRESH_S = 15.0  # DesktopIndex.refresh cadence inside poll()
DESKTOP_ROOT_RECHECK_S = 600.0  # re-resolve the auto-detected Desktop metadata dir
STOP_RECENT_S = 90.0  # a Stop hook this close to busy -> idle means the hooks handled the turn
FALLBACK_GRACE_S = 10.0  # wait for a late Stop hook / transcript flush before the fallback push
# Sessions that already reported a hook to this bridge will send Stop themselves; the fallback
# only covers a lost Stop (or an interrupted turn, which runs no Stop hook), so it waits longer.
HOOKED_GRACE_S = 45.0
FALLBACK_RETRIES = 2  # extra grace periods while the transcript still shows the turn running
STALE_RUNNING_S = 30.0  # a live session idle this long cannot still be running its last turn
HOOK_TEXT_CAP = 20_000  # hook_report.MAX_MESSAGE_CHARS: longer last_assistant_message values arrive cut
HOOK_SCAN_BYTES = 4 * 1024 * 1024  # transcript tail read for a Stop hook (full reply, turn start)
_MISSING_POLLS = 2  # forget a tracked session only after it is missing from this many polls in a row
INBOX_PRUNE_S = 600.0  # drop stored inbox tokens of dead sessions this old
_SOURCE_EXCLUDED_LABEL = 'Content hidden'
_SOURCE_WAITING_REASON = 'Waiting for attention'

_SESSION_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]{0,127}$")
_INTERRUPT_PREFIX = "[Request interrupted by user"
# Output of a local slash command or of a `!` bash-mode command: ends the "turn" its input started.
_LOCAL_OUTPUT_PREFIXES = ("<local-command-stdout>", "<local-command-stderr>", "<bash-stdout>", "<bash-stderr>")
_FALLBACK_KINDS = frozenset({"interactive", "bg"})
_STATES = {"busy": "running", "waiting": "waiting", "idle": "idle"}
_MAX_REMEMBERED = 2000
_TITLE_SCAN_BYTES = 8 * 1024 * 1024
PROGRESS_READ_BYTES = 1024 * 1024  # one read of new transcript bytes (tool results can be big)
PROGRESS_POLL_BYTES = 8 * 1024 * 1024  # per session and poll; more waits for the next poll
_CWD_SCAN_BYTES = 1024 * 1024


# --- small helpers ----------------------------------------------------------------------


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _ms(value: Any) -> float:
    """Epoch seconds from a millisecond timestamp; 0.0 when missing or invalid."""

    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return 0.0
    return float(value) / 1000.0


def _iso_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError, OSError):  # naive pre-1970 times raise OSError on Windows
        return None


def _one_line(text: str, limit: int = 200) -> str:
    return " ".join(text.split())[:limit]


def _basename(cwd: str) -> str:
    return re.split(r"[\\/]", cwd.rstrip("\\/"))[-1] if cwd else ""


def _text_of(content: Any) -> str:
    """Text of a message content: a plain string or the joined text blocks of a list."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [block.get("text") for block in content if isinstance(block, dict) and block.get("type") == "text"]
        return "\n".join(part for part in parts if isinstance(part, str))
    return ""


def _origin_kind(record: dict[str, Any]) -> str:
    origin = record.get("origin")
    if isinstance(origin, dict):
        return _str(origin.get("kind"))
    return _str(origin)


def _socket_id(socket: str) -> str:
    """Comparable form of an inbox pipe path (hooks may report it with a "uds:" prefix)."""

    value = socket.strip()
    if value[:4].lower() == "uds:":
        value = value[4:]
    return value.casefold()


def _read_json_object(path: Path) -> dict[str, Any] | None:
    """A JSON object from path, retrying once for a file caught mid-write; None otherwise."""

    for attempt in range(2):
        try:
            value = json.loads(path.read_bytes().decode("utf-8-sig"))
        except (OSError, ValueError):
            if attempt == 0:
                time.sleep(0.05)
                continue
            return None
        return value if isinstance(value, dict) else None
    return None


def _trim(mapping: dict[Any, Any], limit: int = _MAX_REMEMBERED) -> None:
    """Drop the oldest half of an insertion-ordered dict once it exceeds limit."""

    if len(mapping) > limit:
        for key in list(mapping)[: len(mapping) // 2]:
            mapping.pop(key, None)


def _inbox_module() -> ModuleType:
    # Imported lazily: inbox is optional for everything except delivery.
    return importlib.import_module(f"{__package__}.inbox")


# --- registry (~/.claude/sessions/<pid>.json) --------------------------------------------


@dataclass
class RegistryEntry:
    """One live Claude Code process as registered in ~/.claude/sessions."""

    pid: int
    session_id: str
    cwd: str
    status: str  # busy | idle | waiting | ""
    waiting_for: str
    kind: str  # interactive | bg | sdk
    entrypoint: str  # claude-desktop | cli | ...
    host_session_id: str  # "local_<uuid>" for Claude Desktop sessions
    name: str
    proc_start: int | None
    updated_at: float  # epoch seconds
    status_updated_at: float
    socket: str  # messagingSocketPath
    version: str


def _registry_entry(data: dict[str, Any]) -> RegistryEntry | None:
    pid = _int(data.get("pid"))
    session_id = _str(data.get("sessionId"))
    if pid is None or pid <= 0 or not _SESSION_ID_RE.match(session_id):
        return None
    status_updated = _ms(data.get("statusUpdatedAt"))
    return RegistryEntry(
        pid=pid,
        session_id=session_id,
        cwd=_str(data.get("cwd")),
        status=_str(data.get("status")).lower(),
        waiting_for=_str(data.get("waitingFor")),
        kind=_str(data.get("kind")),
        entrypoint=_str(data.get("entrypoint")),
        host_session_id=_str(data.get("hostSessionId")),
        name=_str(data.get("name")),
        proc_start=_int(data.get("procStart")),
        updated_at=_ms(data.get("updatedAt")) or status_updated or _ms(data.get("startedAt")),
        status_updated_at=status_updated,
        socket=_str(data.get("messagingSocketPath")),
        version=_str(data.get("version")),
    )


def read_registry(home: Path, *, alive: Callable[[int, int | None], bool] = jobs.process_alive) -> list[RegistryEntry]:
    """Live registry entries, newest first; one per session (the most recently updated)."""

    try:
        paths = sorted((home / "sessions").glob("*.json"))
    except OSError:
        return []
    found: dict[str, RegistryEntry] = {}
    for path in paths:
        if path.name.lower().endswith(".key"):  # never read the key files
            continue
        data = _read_json_object(path)
        entry = _registry_entry(data) if data is not None else None
        if entry is None:
            continue
        try:
            if not alive(entry.pid, entry.proc_start):
                continue
        except Exception:  # noqa: BLE001 - a failed liveness probe means "not alive"
            continue
        known = found.get(entry.session_id)
        if known is None or entry.updated_at > known.updated_at:
            found[entry.session_id] = entry
    return sorted(found.values(), key=lambda entry: entry.updated_at, reverse=True)


# --- Claude Desktop metadata ------------------------------------------------------------


def desktop_sessions_dir(*, localappdata: str | None = None, appdata: str | None = None) -> Path | None:
    """The <acct>/<org> dir holding the newest local_*.json of Claude Desktop, or None.

    The MSIX package keeps the real files under %LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache;
    %APPDATA%\\Claude is the fallback (non-packaged installs). On a tie the package dir wins.
    """

    localappdata = os.environ.get("LOCALAPPDATA", "") if localappdata is None else localappdata
    appdata = os.environ.get("APPDATA", "") if appdata is None else appdata
    roots: list[Path] = []
    try:
        if localappdata:
            packages = Path(localappdata, "Packages")
            roots += sorted(packages.glob("Claude_*/LocalCache/Roaming/Claude/claude-code-sessions"))
    except OSError:
        pass
    if appdata:
        roots.append(Path(appdata, "Claude", "claude-code-sessions"))
    best: Path | None = None
    best_mtime = -1.0
    for root in roots:
        try:
            org_dirs = sorted(path for path in root.glob("*/*") if path.is_dir())
        except OSError:
            continue
        for org_dir in org_dirs:
            try:
                newest = max((path.stat().st_mtime for path in org_dir.glob("local_*.json")), default=None)
            except OSError:
                continue
            if newest is not None and newest > best_mtime:
                best, best_mtime = org_dir, newest
    return best


@dataclass
class DesktopMeta:
    """The parts of a Claude Desktop local_<uuid>.json the bridge uses."""

    local_id: str
    cli_session_id: str
    title: str
    cwd: str
    last_activity: float  # epoch seconds
    archived: bool
    permission_mode: str
    status_detail: str
    status_category: str  # review_ready | completed | blocked
    needs_action: bool
    scheduled: bool  # a scheduled-task session
    completed_turns: int
    last_assistant_uuid: str
    summarizes_uuid: str


def _desktop_meta(data: dict[str, Any], stem: str) -> DesktopMeta:
    summary = data.get("postTurnSummary")
    summary = summary if isinstance(summary, dict) else {}
    needs_action = summary.get("needs_action")
    completed = _int(data.get("completedTurns"))
    return DesktopMeta(
        local_id=_str(data.get("sessionId")) or stem,
        cli_session_id=_str(data.get("cliSessionId")),
        title=_str(data.get("title")),
        cwd=_str(data.get("cwd")),
        last_activity=_ms(data.get("lastActivityAt")),
        archived=data.get("isArchived") is True,
        permission_mode=_str(data.get("permissionMode")),
        status_detail=_str(summary.get("status_detail")),
        status_category=_str(summary.get("status_category")),
        # Desktop writes a string (usually ""); a non-empty one describes the needed action.
        needs_action=needs_action if isinstance(needs_action, bool) else bool(_str(needs_action).strip()),
        scheduled=bool(data.get("scheduledTaskId")),
        completed_turns=completed if completed is not None else 0,
        last_assistant_uuid=_str(data.get("lastAssistantUuid")),
        summarizes_uuid=_str(summary.get("summarizes_uuid")),
    )


class DesktopIndex:
    """local_*.json files of one Desktop account/org dir, parsed once per (mtime, size)."""

    def __init__(self, root: Path | None) -> None:
        self.root = root
        self._files: dict[str, tuple[tuple[int, int], DesktopMeta | None]] = {}
        self._by_local: dict[str, DesktopMeta] = {}
        self._by_cli: dict[str, DesktopMeta] = {}
        self._lock = threading.Lock()

    def refresh(self) -> None:
        with self._lock:
            files: dict[str, tuple[tuple[int, int], DesktopMeta | None]] = {}
            for name, path, stamp in self._scan():
                cached = self._files.get(name)
                if cached is not None and cached[0] == stamp:
                    files[name] = cached
                    continue
                data = _read_json_object(path)
                if data is None:
                    # Caught mid-write: keep the last good parse and its old stamp so the next refresh retries.
                    if cached is not None:
                        files[name] = cached
                    continue
                files[name] = (stamp, _desktop_meta(data, name[: -len(".json")]))
            by_local: dict[str, DesktopMeta] = {}
            by_cli: dict[str, DesktopMeta] = {}
            for _stamp, meta in files.values():
                if meta is None:
                    continue
                by_local[meta.local_id] = meta
                if meta.cli_session_id:
                    known = by_cli.get(meta.cli_session_id)
                    if known is None or meta.last_activity > known.last_activity:
                        by_cli[meta.cli_session_id] = meta
            self._files, self._by_local, self._by_cli = files, by_local, by_cli

    def _scan(self) -> Iterator[tuple[str, Path, tuple[int, int]]]:
        if self.root is None:
            return
        try:
            with os.scandir(self.root) as entries:
                items = list(entries)
        except OSError:
            return
        for entry in items:
            name = entry.name
            if not (name.startswith("local_") and name.endswith(".json")):
                continue
            try:
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            yield name, Path(entry.path), (stat.st_mtime_ns, stat.st_size)

    def by_local_id(self, local_id: str) -> DesktopMeta | None:
        return self._by_local.get(local_id) if local_id else None

    def by_cli_session(self, session_id: str) -> DesktopMeta | None:
        return self._by_cli.get(session_id) if session_id else None

    def all(self) -> list[DesktopMeta]:
        return sorted(self._by_local.values(), key=lambda meta: meta.last_activity, reverse=True)


# --- transcripts ------------------------------------------------------------------------


def project_slug(cwd: str) -> str:
    """Claude's projects/<slug> name: every non-alphanumeric character becomes "-"."""

    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def find_transcript(home: Path, session_id: str, cwd: str = "") -> Path | None:
    """projects/<slug>/<session_id>.jsonl, else the newest projects/*/<session_id>.jsonl."""

    if not _SESSION_ID_RE.match(session_id or ""):
        return None
    projects = home / "projects"
    name = f"{session_id}.jsonl"
    if cwd:
        candidate = projects / project_slug(cwd) / name
        if candidate.is_file():
            return candidate
    best: Path | None = None
    best_mtime = -1.0
    try:
        matches = list(projects.glob(f"*/{name}"))  # direct children only: subagents/ folders never match
    except OSError:
        return None
    for path in matches:
        try:
            if not path.is_file():
                continue
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    return best


def recent_transcripts(home: Path, since: float) -> list[tuple[Path, float]]:
    """(path, mtime) of main transcripts modified at or after since, newest first."""

    found: list[tuple[Path, float]] = []
    try:
        with os.scandir(home / "projects") as entries:
            project_dirs = [entry.path for entry in entries if entry.is_dir()]
    except OSError:
        return []
    for project_dir in project_dirs:
        try:
            with os.scandir(project_dir) as entries:
                items = list(entries)
        except OSError:
            continue
        for entry in items:
            # agent-*.jsonl: sidechain transcripts of older Claude Code versions, not sessions.
            if not entry.name.endswith(".jsonl") or entry.name.startswith("agent-"):
                continue
            try:
                if not entry.is_file():
                    continue
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime >= since:
                found.append((Path(entry.path), mtime))
    found.sort(key=lambda item: item[1], reverse=True)
    return found


def _reverse_lines(path: Path, max_bytes: int, block: int = 256 * 1024) -> Iterator[bytes]:
    """Complete lines from the end of the file backwards, within its last max_bytes.

    The text after the last newline (a record still being written) is skipped, and so is
    the line cut by the max_bytes boundary.
    """

    with path.open("rb") as stream:
        end = stream.seek(0, os.SEEK_END)
        limit = max(0, end - max_bytes)
        position = end
        carry = b""
        skipped_tail = False
        while position > limit:
            size = min(block, position - limit)
            position -= size
            stream.seek(position)
            lines = (stream.read(size) + carry).split(b"\n")
            carry = lines[0]
            for line in reversed(lines[1:]):
                if not skipped_tail:
                    skipped_tail = True
                    continue
                if line.strip():
                    yield line
        if not skipped_tail or not carry.strip():
            return
        if position > 0:  # complete only when the window starts right after a newline
            stream.seek(position - 1)
            if stream.read(1) != b"\n":
                return
        yield carry


def transcript_title(path: Path, *, max_bytes: int = _TITLE_SCAN_BYTES) -> str:
    """The last custom-title record of a transcript, else ""."""

    try:
        for line in _reverse_lines(path, max_bytes):
            if b"custom-title" not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("type") == "custom-title":
                title = _one_line(_str(record.get("customTitle")))
                if title:
                    return title
    except OSError:
        return ""
    return ""


def _transcript_cwd(path: Path) -> str:
    try:
        for line in _reverse_lines(path, _CWD_SCAN_BYTES):
            if b'"cwd"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and _str(record.get("cwd")):
                return _str(record.get("cwd"))
    except OSError:
        return ""
    return ""


def _last_assistant_uuid(path: Path, max_bytes: int = 4 * 1024 * 1024) -> str:
    """uuid of the last main-chain assistant record (what Desktop's summaries point at)."""

    try:
        for line in _reverse_lines(path, max_bytes):
            if b'"assistant"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("type") == "assistant" and not record.get("isSidechain"):
                return _str(record.get("uuid"))
    except OSError:
        return ""
    return ""


def _tail_records(path: Path, max_bytes: int) -> list[dict[str, Any]]:
    size = path.stat().st_size
    start = max(0, size - max_bytes)
    with path.open("rb") as stream:
        stream.seek(start)
        data = stream.read()
    first = 0
    if start > 0:
        newline = data.find(b"\n")
        first = newline + 1 if newline >= 0 else len(data)  # skip the partial first line
    end = data.rfind(b"\n") + 1  # skip a record still being written
    records = []
    position = first
    while position < end:
        stop = data.find(b"\n", position, end)
        stop = end if stop < 0 else stop
        line = data[position:stop]
        position = stop + 1
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


@dataclass
class ClaudeTurn:
    user_text: str
    final_text: str | None
    status: str  # running | completed | failed | aborted
    started_at: float | None
    completed_at: float | None
    error: str = ""


class _TurnBuilder:
    def __init__(self, user_text: str, started_at: float | None, *, via_queue: bool) -> None:
        self.user_text = user_text
        self.started_at = started_at
        self.via_queue = via_queue  # started by a message queued into the session (inbox / peer)
        # prompt | tool_use | tool_result | assistant_end | interrupt | api_error | error_message | local_output
        self.last_kind = "prompt"
        self.last_at = started_at
        self.answered = False
        self.error = ""
        self.texts: dict[str, list[str]] = {}
        self.last_text_id = ""
        self.seen: set[str] = set()

    def note(self, kind: str, at: float | None) -> None:
        self.last_kind = kind
        if at is not None:
            self.last_at = at

    def add_text(self, message_id: str, blocks: list[str]) -> None:
        current = self.texts.get(message_id)
        if current is None or blocks[: len(current)] == current:
            self.texts[message_id] = list(blocks)  # first record, or a record repeating the earlier blocks
        elif current[-len(blocks):] != blocks:
            current.extend(blocks)
        self.last_text_id = message_id

    def finish(self, *, is_last: bool, next_via_queue: bool) -> ClaudeTurn:
        final = "\n\n".join(self.texts.get(self.last_text_id, [])).strip() or None
        kind = self.last_kind
        if kind == "interrupt":
            status = "aborted"
        elif kind in ("api_error", "error_message"):
            status = "failed"
        elif kind in ("prompt", "tool_use", "tool_result"):
            # A later turn started: a queued message continued this one, anything else cut it off.
            status = "running" if is_last else ("completed" if next_via_queue else "aborted")
        else:
            status = "completed"
        return ClaudeTurn(
            user_text=self.user_text,
            final_text=final,
            status=status,
            started_at=self.started_at,
            completed_at=None if status == "running" else self.last_at,
            error=self.error if status == "failed" else "",
        )


def _api_error_text(record: dict[str, Any]) -> str:
    error = record.get("error")
    if isinstance(error, dict):
        text = _str(error.get("formatted")) or _str(error.get("message"))
        return text or json.dumps(error, ensure_ascii=False)[:500]
    return _str(error) or tr('API error')


def read_turns(path: Path, *, turns: int = 3, max_bytes: int = 16 * 1024 * 1024) -> list[ClaudeTurn]:
    """The last `turns` main-chain turns of a transcript, oldest first.

    A turn starts at a human prompt (a user record with string content or text blocks that
    is not isMeta and carries no tool_result) or at a queued_command attachment from a peer
    (an inbox message). final_text is the text of the last assistant message that has text.
    """

    if turns <= 0:
        return []
    builders: list[_TurnBuilder] = []
    current: _TurnBuilder | None = None

    def ensure() -> _TurnBuilder:
        nonlocal current
        if current is None:  # records before the first prompt inside the tail window
            current = _TurnBuilder("", None, via_queue=False)
            builders.append(current)
        return current

    def start(text: str, at: float | None, *, via_queue: bool) -> None:
        nonlocal current
        if current is not None and current.last_kind == "prompt" and not current.answered and current.user_text == text:
            return  # the same message recorded twice (queued_command + user record)
        current = _TurnBuilder(text, at, via_queue=via_queue)
        builders.append(current)

    for record in _tail_records(path, max_bytes):
        if record.get("isSidechain"):
            continue
        kind = record.get("type")
        at = _iso_epoch(record.get("timestamp"))
        if kind == "user":
            if record.get("isMeta") or record.get("isCompactSummary") or record.get("isVisibleInTranscriptOnly"):
                continue
            message = record.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                blocks = [block for block in content if isinstance(block, dict)]
                if any(block.get("type") == "tool_result" for block in blocks):
                    ensure().note("tool_result", at)
                    continue
                if not blocks:
                    continue
            elif not isinstance(content, str):
                continue
            text = _text_of(content).strip()
            if text.startswith(_INTERRUPT_PREFIX):
                ensure().note("interrupt", at)
            elif text.startswith(_LOCAL_OUTPUT_PREFIXES):
                ensure().note("local_output", at)
            else:
                start(text, at, via_queue=_origin_kind(record) == "peer")
        elif kind == "attachment":
            attachment = record.get("attachment")
            queued = isinstance(attachment, dict) and attachment.get("type") == "queued_command"
            if queued and _origin_kind(attachment) == "peer":
                start(_text_of(attachment.get("prompt")).strip(), at, via_queue=True)
        elif kind == "assistant":
            turn = ensure()
            uuid = _str(record.get("uuid"))
            if uuid:
                if uuid in turn.seen:
                    continue
                turn.seen.add(uuid)
            message = record.get("message")
            message = message if isinstance(message, dict) else {}
            content = message.get("content")
            texts = [
                block["text"]
                for block in (content if isinstance(content, list) else [])
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
            ]
            if record.get("isApiErrorMessage"):
                turn.error = "\n".join(texts).strip() or tr('API error')
                turn.note("error_message", at)
                continue
            if message.get("model") == "<synthetic>":  # "No response requested." and similar
                turn.note("assistant_end", at)
                continue
            turn.answered = True
            turn.error = ""  # a retry succeeded
            if texts:
                turn.add_text(_str(message.get("id")) or uuid or f"#{len(turn.texts)}", texts)
            turn.note("tool_use" if message.get("stop_reason") == "tool_use" else "assistant_end", at)
        elif kind == "system" and record.get("subtype") == "api_error":
            turn = ensure()
            turn.error = _api_error_text(record)
            turn.note("api_error", at)

    selected = builders[-turns:]
    offset = len(builders) - len(selected)
    result = []
    for index, builder in enumerate(selected, start=offset):
        is_last = index == len(builders) - 1
        next_via_queue = not is_last and builders[index + 1].via_queue
        result.append(builder.finish(is_last=is_last, next_via_queue=next_via_queue))
    return result


def _carries_marker(record: dict[str, Any], marker: str) -> bool:
    kind = record.get("type")
    if kind == "queue-operation":
        return record.get("operation") == "enqueue" and marker in _text_of(record.get("content"))
    if kind == "attachment":
        attachment = record.get("attachment")
        return (
            isinstance(attachment, dict)
            and attachment.get("type") == "queued_command"
            and marker in _text_of(attachment.get("prompt"))
        )
    if kind == "user":
        message = record.get("message")
        return (
            _origin_kind(record) == "peer" and isinstance(message, dict) and marker in _text_of(message.get("content"))
        )
    return False


def delivered(path: Path, marker: str, since_offset: int) -> bool:
    """True when a record written at or after since_offset shows the inbox message carrying marker.

    Only queue-operation enqueue content, queued_command attachment prompts and user records
    from a peer count, so a marker merely quoted by the assistant or a tool is not taken as delivery.
    """

    if not marker:
        return False
    # Pre-filter lines on the raw bytes when JSON keeps the marker unescaped.
    needle = marker.encode("utf-8") if json.dumps(marker, ensure_ascii=False)[1:-1] == marker else b""
    try:
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            stream.seek(since_offset if 0 <= since_offset <= size else 0)
            for line in stream:
                if needle and needle not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:  # includes the partial line an offset may cut
                    continue
                if isinstance(record, dict) and _carries_marker(record, marker):
                    return True
    except OSError:
        return False
    return False


# --- the source -------------------------------------------------------------------------


@dataclass
class _Tracked:
    """What poll() remembers about one live session between polls."""

    status: str = ""
    entry: RegistryEntry | None = None
    busy_since: float | None = None  # start of the turn seen running; consumed by Stop / fallback
    waiting_since: float | None = None
    loud_sent: bool = False
    idle_at: float | None = None  # busy -> idle seen; a fallback push is pending
    ended_busy_since: float | None = None  # busy_since of the busy period that idle_at ended
    fallback_due: float = 0.0
    fallback_tries: int = 0
    missing: int = 0  # consecutive polls without a live registry entry


@dataclass
class _Tail:
    """How far poll() has read a live session's transcript for mid-turn messages."""

    path: Path
    offset: int
    skipping: bool = False  # inside a line longer than a chunk (a big tool result): dropped unread
    missing: int = 0  # consecutive polls without a registry entry
    cwd: str = ""  # the latest record's cwd (sessions change directory)


def _new_lines(tail: _Tail, size: int) -> list[bytes]:
    """Complete transcript lines after tail.offset that may hold assistant records; advances the tail.

    Reads forward in chunks (at most PROGRESS_POLL_BYTES per call; the rest waits for the next
    poll). A line longer than a chunk is a tool result or similar, never a message to the user,
    and is dropped without being parsed; a short line still being written waits.
    """

    lines: list[bytes] = []
    budget = PROGRESS_POLL_BYTES
    try:
        with tail.path.open("rb") as handle:
            while tail.offset < size and budget > 0:
                handle.seek(tail.offset)
                chunk = handle.read(min(PROGRESS_READ_BYTES, size - tail.offset))
                if not chunk:
                    break
                budget -= len(chunk)
                end = chunk.rfind(b"\n")
                if end < 0:
                    if len(chunk) < PROGRESS_READ_BYTES and not tail.skipping:
                        break  # a record still being written
                    tail.skipping = True
                    tail.offset += len(chunk)
                    continue
                parts = chunk[: end + 1].split(b"\n")
                tail.offset += end + 1
                if tail.skipping:
                    parts = parts[1:]  # the end of the long line
                    tail.skipping = False
                lines.extend(part for part in parts if b'"assistant"' in part)
    except OSError:
        pass
    return lines


@dataclass
class _Due:
    session_id: str
    entry: RegistryEntry | None
    idle_at: float
    busy_since: float | None  # start of the busy period that ended at idle_at
    tries: int
    still_busy: bool = False


class ClaudeSource:
    """Claude Code sessions for the bridge: hook events, registry polling, reads and delivery."""

    def __init__(
        self,
        cfg: Config,
        emit: Callable[[Event], None],
        *,
        home: Path | None = None,
        desktop_root: Path | None = None,
        secrets: SecretStore | None = None,
        clock: Callable[[], float] = time.time,
        alive: Callable[[int, int | None], bool] = jobs.process_alive,
    ) -> None:
        self.cfg = cfg
        self.emit = emit
        if home is None or secrets is None:
            config = importlib.import_module(f"{__package__}.config")
            home = config.claude_home() if home is None else home
            secrets = config.SecretStore() if secrets is None else secrets
        self.home = Path(home)
        self.secrets = secrets
        self.clock = clock
        self.alive = alive
        self._auto_desktop = desktop_root is None
        self._index = DesktopIndex(desktop_root)
        self._index_at: float | None = None
        self._root_at: float | None = None
        self._lock = threading.RLock()
        self._tracked: dict[str, _Tracked] = {}
        self._last_stop: dict[str, float] = {}
        self._modes: dict[str, str] = {}  # permission mode reported by the hooks
        self._headless: dict[str, float] = {}  # sessions whose hooks ran inside a bridge-owned `claude -p`
        self._paths: dict[str, Path] = {}
        self._tinfo: dict[str, tuple[tuple[int, int], dict[str, str]]] = {}
        self._fallback_sent: dict[str, tuple[Any, ...]] = {}
        self._hooked: dict[str, float] = {}  # sessions that reported any hook to this bridge
        self._pruned_at: float | None = None
        self._prune_candidates: set[str] = set()
        self._desktop_lock = threading.Lock()  # poll, hook-server and Telegram threads all refresh
        self._tails: dict[str, _Tail] = {}  # poll thread only

    # -- polling -------------------------------------------------------------------------

    def poll(self) -> None:
        """Track registry status changes: waiting alerts and the busy -> idle fallback push."""

        if not getattr(self.cfg, "claude_enabled", True):
            return
        now = self.clock()
        self._refresh_desktop(now, DESKTOP_REFRESH_S)
        entries = read_registry(self.home, alive=self.alive)
        live = {entry.session_id: entry for entry in entries}
        alerts: list[tuple[RegistryEntry, DesktopMeta | None, bool]] = []
        due: list[_Due] = []
        with self._lock:
            for entry in entries:
                try:
                    alerts.extend(self._track(entry, now))
                except Exception:  # noqa: BLE001 - one odd session must not stop the poll
                    log.exception(tr('Failed to track Claude session state'))
            for session_id, tracked in list(self._tracked.items()):
                entry = live.get(session_id)
                if entry is None:
                    # Keep the last status across a registry file caught mid-write, so the next
                    # poll neither repeats a waiting alert nor misses a busy -> idle change.
                    tracked.missing += 1
                    if tracked.idle_at is None and tracked.missing >= _MISSING_POLLS:
                        del self._tracked[session_id]  # the process exited
                        continue
                if tracked.idle_at is not None and now >= tracked.fallback_due:
                    due.append(
                        _Due(
                            session_id=session_id,
                            entry=tracked.entry,
                            idle_at=tracked.idle_at,
                            busy_since=tracked.ended_busy_since,
                            tries=tracked.fallback_tries,
                            still_busy=entry is not None and entry.status in ("busy", "waiting"),
                        )
                    )
                    tracked.idle_at = None
            for session_id, stopped in list(self._last_stop.items()):
                if now - stopped > STOP_RECENT_S * 4:
                    del self._last_stop[session_id]
        events: list[Event] = []
        for entry, meta, loud in alerts:  # titles may read a transcript: build them outside the lock
            try:
                events.append(self._attention(entry, meta, loud=loud))
            except Exception:  # noqa: BLE001
                log.exception(tr('Failed to prepare Claude waiting alerts'))
        for item in due:
            try:
                event = self._fallback(item, now)
            except Exception:  # noqa: BLE001
                log.exception(tr('Failed to read Claude session transcript'))
                event = None
            if event is not None:
                events.append(event)
        if getattr(self.cfg, "progress_enabled", True):
            try:
                events.extend(self._progress_events(entries))
            except Exception:  # noqa: BLE001 - turn pushes must not depend on it
                log.exception(tr('Failed to read Claude messages in progress'))
        self._prune_inboxes(now, live)
        for event in events:
            self._emit(event)

    def _progress_events(self, entries: list[RegistryEntry]) -> list[ProgressEvent]:
        """Messages to the user that live sessions wrote since the last poll (never older ones)."""

        events: list[ProgressEvent] = []
        live: set[str] = set()
        for entry in entries:
            session_id = entry.session_id
            live.add(session_id)
            meta = self._meta_for(session_id, entry)
            if meta is not None and meta.scheduled and not self.cfg.claude_include_scheduled:
                continue
            cwd = entry.cwd or (meta.cwd if meta else "")
            path = self._transcript(session_id, cwd)
            if path is None:
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            tail = self._tails.get(session_id)
            if tail is None or tail.path != path or size < tail.offset:
                self._tails[session_id] = _Tail(path, size)  # start at the end: history is not news
                continue
            tail.missing = 0
            if size == tail.offset:
                continue
            found: list[tuple[str, str, str]] = []
            for line in _new_lines(tail, size):
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue
                record_cwd = _str(record.get("cwd"))
                if record_cwd:
                    tail.cwd = record_cwd  # where the session works now (it may have changed directory)
                if self._excluded(cwd, meta.cwd if meta else "", record_cwd, tail.cwd):
                    continue  # content of excluded folders never goes out
                found.extend(progress.claude_items(record))
            if not found:
                continue
            kind, asks = progress.summarize([(name, text) for name, text, _ in found])
            events.append(ProgressEvent(agent=CLAUDE, session_id=session_id,
                                        title=self._title(session_id, cwd, entry, meta), cwd=tail.cwd or cwd,
                                        texts=[text for _, text, _ in found], kind=kind, asks_user=asks,
                                        items=found))
        for session_id, tail in list(self._tails.items()):
            if session_id not in live:
                # A registry file caught mid-write must not reset the position (texts would be lost).
                tail.missing += 1
                if tail.missing >= _MISSING_POLLS:
                    del self._tails[session_id]
        return events

    def _skip_progress(self, session_id: str) -> None:
        """A turn ended (Stop hook): what it wrote is pushed with the result, not as progress after it."""

        tail = self._tails.get(session_id)
        if tail is None:
            return
        try:
            tail.offset = max(tail.offset, tail.path.stat().st_size)
        except OSError:
            return
        tail.skipping = False

    def _track(self, entry: RegistryEntry, now: float) -> list[tuple[RegistryEntry, DesktopMeta | None, bool]]:
        """Record the entry's status; returns the waiting alerts to send as (entry, meta, loud)."""

        session_id = entry.session_id
        tracked = self._tracked.setdefault(session_id, _Tracked())
        previous, status = tracked.status, entry.status
        tracked.entry = entry
        tracked.status = status
        tracked.missing = 0
        running = ("busy", "waiting")
        if status in running and previous not in running:
            since = entry.status_updated_at
            tracked.busy_since = since if 0 < since <= now else now
        meta = self._meta_for(session_id, entry)
        if meta is not None and meta.scheduled and not self.cfg.claude_include_scheduled:
            return []
        if previous in running and status == "idle" and entry.kind in _FALLBACK_KINDS:
            tracked.idle_at = now
            tracked.ended_busy_since = tracked.busy_since
            tracked.fallback_due = now + (HOOKED_GRACE_S if session_id in self._hooked else FALLBACK_GRACE_S)
            tracked.fallback_tries = 0
        alerts: list[tuple[RegistryEntry, DesktopMeta | None, bool]] = []
        if status == "waiting":
            alert_s = float(self.cfg.waiting_alert_s)
            if previous != "waiting":
                since = entry.status_updated_at
                tracked.waiting_since = since if 0 < since <= now else now
                tracked.loud_sent = now - tracked.waiting_since >= alert_s
                alerts.append((entry, meta, tracked.loud_sent))
            elif not tracked.loud_sent and tracked.waiting_since is not None and now - tracked.waiting_since >= alert_s:
                tracked.loud_sent = True
                alerts.append((entry, meta, True))
        else:
            tracked.waiting_since = None
            tracked.loud_sent = False
        return alerts

    def _attention(self, entry: RegistryEntry, meta: DesktopMeta | None, *, loud: bool) -> AttentionEvent:
        cwd = entry.cwd or (meta.cwd if meta else "")
        return AttentionEvent(
            agent=CLAUDE,
            session_id=entry.session_id,
            title=self._title(entry.session_id, cwd, entry, meta),
            cwd=cwd,
            reason=_one_line(entry.waiting_for) or tr('Waiting for attention'),
            loud=loud,
        )

    def _fallback(self, item: _Due, now: float) -> TurnEvent | None:
        """Push the turn a hook-less session just finished, read from its transcript."""

        session_id = item.session_id
        if self._last_stop.get(session_id, float("-inf")) >= item.idle_at - STOP_RECENT_S:
            return None  # the Stop hook reported it
        entry = item.entry
        meta = self._meta_for(session_id, entry)
        cwd = (entry.cwd if entry else "") or (meta.cwd if meta else "")
        path = self._transcript(session_id, cwd)
        if path is None:
            return None
        turns = read_turns(path, turns=2)
        if not turns:
            return None
        turn = turns[-1]
        if turn.status == "running":
            if item.still_busy and len(turns) > 1:
                turn = turns[-2]  # a new turn already started; push the one that ended
                ended = turn.completed_at
                if ended is not None and item.busy_since is not None and ended < item.busy_since - 5:
                    return None  # it ended before the busy period that just ended: not this one
            elif item.still_busy:
                return None
            elif item.tries < FALLBACK_RETRIES:
                self._reschedule(item, now)  # the transcript lags behind the registry
                return None
            else:
                turn = ClaudeTurn(turn.user_text, turn.final_text, "completed", turn.started_at, now, turn.error)
        identity = (turn.started_at, turn.user_text[:200], turn.status, (turn.final_text or "")[:200])
        with self._lock:
            if self._fallback_sent.get(session_id) == identity:
                return None
            self._fallback_sent[session_id] = identity
            _trim(self._fallback_sent)
            tracked = self._tracked.get(session_id)
            if tracked is not None and tracked.busy_since == item.busy_since and not item.still_busy:
                tracked.busy_since = None
        event = TurnEvent(
            agent=CLAUDE,
            session_id=session_id,
            title=self._title(session_id, cwd, entry, meta, path),
            cwd=cwd,
            status=turn.status,
            final_text=turn.final_text,
            error=turn.error,
            started_at=turn.started_at if turn.started_at is not None else item.busy_since,
            completed_at=turn.completed_at if turn.completed_at is not None else item.idle_at,
            summary=self._summary(meta, path),
            quota=self.quota(),
        )
        return self._apply_exclusion(event, meta.cwd if meta else "", _transcript_cwd(path))

    def _reschedule(self, item: _Due, now: float) -> None:
        with self._lock:
            status = "" if item.entry is None else item.entry.status
            tracked = self._tracked.setdefault(item.session_id, _Tracked(status=status))
            if tracked.entry is None:
                tracked.entry = item.entry
            tracked.idle_at = item.idle_at
            tracked.ended_busy_since = item.busy_since
            tracked.fallback_due = now + FALLBACK_GRACE_S
            tracked.fallback_tries = item.tries + 1

    # -- hooks ---------------------------------------------------------------------------

    def on_hook(self, payload: dict[str, Any]) -> None:
        """Handle a hook_report payload (SessionStart, Stop, StopFailure)."""

        if not isinstance(payload, dict) or not getattr(self.cfg, "claude_enabled", True):
            return
        session_id = _str(payload.get("session_id"))
        if not _SESSION_ID_RE.match(session_id):
            return
        now = self.clock()
        self._store_inbox(session_id, payload.get("inbox"), now)
        subagent = bool(_str(payload.get("agent_id")))
        with self._lock:
            self._hooked.pop(session_id, None)  # re-insert: _trim drops the least recently seen
            self._hooked[session_id] = now
            _trim(self._hooked)
            if payload.get("headless") is True:
                self._headless.pop(session_id, None)
                self._headless[session_id] = now
                _trim(self._headless)
            mode = _str(payload.get("permission_mode"))
            if mode and not subagent:
                self._modes[session_id] = mode
                _trim(self._modes)
            transcript = _local_transcript_path(_str(payload.get("transcript_path")), session_id)
            if transcript is not None:
                self._paths[session_id] = transcript
                _trim(self._paths)
        event_name = _str(payload.get("event"))
        if subagent or event_name not in ("Stop", "StopFailure"):
            return
        self._skip_progress(session_id)
        with self._lock:
            self._last_stop[session_id] = now
            tracked = self._tracked.get(session_id)
            started = tracked.busy_since if tracked is not None else None
            if tracked is not None:
                tracked.busy_since = None
        if started is not None and started > now:
            started = None
        self._refresh_desktop(now, 1.0)
        entry = self.live(session_id)
        meta = self._meta_for(session_id, entry)
        if meta is not None and meta.scheduled and not self.cfg.claude_include_scheduled:
            return
        cwd = _str(payload.get("cwd")) or (entry.cwd if entry else "") or (meta.cwd if meta else "")
        path = self._transcript(session_id, cwd)
        raw_text = _str(payload.get("last_assistant_message"))
        final_text = raw_text.strip() or None
        status, error = "completed", ""
        if event_name == "StopFailure":
            parts = (_error_part(payload.get("error")), _error_part(payload.get("error_details")))
            status, error = "failed", tr(': ').join(part for part in parts if part) or tr('Unknown error')
        # hook_report cuts the reply at HOOK_TEXT_CAP characters; the transcript has all of it.
        # It also knows when the turn started if poll() never saw the session busy.
        needs_text = status == "completed" and (final_text is None or len(raw_text) >= HOOK_TEXT_CAP)
        if path is not None and (needs_text or started is None):
            turn = self._finished_turn(path, now)
            if turn is not None:
                if started is None:
                    started = turn.started_at
                if needs_text and turn.final_text and _same_reply(final_text, turn.final_text):
                    final_text = turn.final_text
        event = TurnEvent(
            agent=CLAUDE,
            session_id=session_id,
            title=self._title(session_id, cwd, entry, meta, path),
            cwd=cwd,
            status=status,
            final_text=final_text,
            error=error,
            started_at=started,
            completed_at=now,
            summary=self._summary(meta, path),
            quota=self.quota(),
        )
        # The hook's cwd is the current directory, which a session can leave; the registry and
        # Desktop keep the directory it was started in. Excluded if any of them is.
        self._emit(self._apply_exclusion(event, entry.cwd if entry else "", meta.cwd if meta else ""))

    def _finished_turn(self, path: Path, now: float) -> ClaudeTurn | None:
        """The transcript's last turn when it has just finished (what a Stop hook reports)."""

        try:
            turns = read_turns(path, turns=1, max_bytes=HOOK_SCAN_BYTES)
        except OSError:
            return None
        if not turns or turns[-1].status == "running":
            return None
        turn = turns[-1]
        if turn.completed_at is not None and abs(now - turn.completed_at) > STOP_RECENT_S:
            return None  # an older turn: the transcript does not show the one the hook reported
        if turn.started_at is not None and turn.started_at > now:
            turn.started_at = None
        return turn

    def _store_inbox(self, session_id: str, inbox: Any, now: float) -> None:
        if not isinstance(inbox, dict):
            return
        socket = _str(inbox.get("socket")).strip()
        token = _str(inbox.get("token")).strip()
        if not socket or not token or len(socket) > 512 or len(token) > 4096:
            return
        key = session_key(CLAUDE, session_id)
        try:
            stored = self.secrets.get(key)
            if isinstance(stored, dict) and stored.get("socket") == socket and stored.get("token") == token:
                return
            self.secrets.put(key, {"socket": socket, "token": token, "at": now})
        except Exception:  # noqa: BLE001 - never log the values
            log.warning(tr('Failed to save Claude session inbox information'))

    def _prune_inboxes(self, now: float, live: dict[str, RegistryEntry]) -> None:
        """Drop stored inbox tokens of sessions without a live process (their pipe is gone).

        A token goes only after two checks INBOX_PRUNE_S apart both found its session dead,
        so one unreadable registry file cannot cost a live session its inbox.
        """

        if self._pruned_at is not None and now - self._pruned_at < INBOX_PRUNE_S:
            return
        self._pruned_at = now
        prefix = session_key(CLAUDE, "")
        try:
            dead = set()
            for key in self.secrets.keys():
                if not key.startswith(prefix) or key[len(prefix):] in live:
                    continue
                stored = self.secrets.get(key)
                at = stored.get("at") if isinstance(stored, dict) else None
                if not isinstance(at, (int, float)) or now - at >= INBOX_PRUNE_S:
                    dead.add(key)
            for key in dead & self._prune_candidates:
                self.secrets.drop(key)
            self._prune_candidates = dead - self._prune_candidates
        except Exception:  # noqa: BLE001
            log.warning(tr('Failed to clean up Claude session inbox information'))

    # -- views ---------------------------------------------------------------------------

    def sessions(self, limit: int) -> list[SessionView]:
        """Live, recently active Desktop and recently written sessions; busy/waiting first."""

        if limit <= 0 or not getattr(self.cfg, "claude_enabled", True):
            return []
        now = self.clock()
        since = now - float(self.cfg.active_window_h) * 3600.0
        self._refresh_desktop(now, DESKTOP_REFRESH_S)
        metas = self._index.all()
        include_scheduled = self.cfg.claude_include_scheduled
        entries = {entry.session_id: entry for entry in read_registry(self.home, alive=self.alive)}
        # Claude Desktop may run a second, idle claude.exe for the same local_<id> under a new session id
        # (seen live: a --resume fork without a transcript). List one process per Desktop session: the
        # one the metadata names, else a busy one, else the most recently updated.
        by_host: dict[str, list[RegistryEntry]] = {}
        dropped: set[str] = set()
        for entry in entries.values():
            if entry.host_session_id:
                by_host.setdefault(entry.host_session_id, []).append(entry)
        for host, group in by_host.items():
            if len(group) < 2:
                continue
            meta = self._index.by_local_id(host)
            named = meta.cli_session_id if meta is not None else ""
            group.sort(key=lambda item: (item.session_id != named, item.status not in ("busy", "waiting"),
                                         -item.updated_at))
            for extra in group[1:]:
                if extra.status not in ("busy", "waiting"):
                    entries.pop(extra.session_id, None)
                    dropped.add(extra.session_id)
        # A resumed or forked Desktop session keeps its local_<id> while the live process runs under a
        # new session id; the metadata's cliSessionId then names the old one, which must not be listed twice.
        claimed = {entry.host_session_id: entry.session_id for entry in entries.values() if entry.host_session_id}
        superseded = {meta.cli_session_id for meta in metas
                      if meta.cli_session_id and claimed.get(meta.local_id, meta.cli_session_id) != meta.cli_session_id}
        candidates: dict[str, float] = {session_id: 0.0 for session_id in entries}
        for meta in metas:
            if (meta.cli_session_id and not meta.archived and meta.last_activity >= since
                    and meta.cli_session_id not in superseded):
                candidates.setdefault(meta.cli_session_id, 0.0)
        transcripts: dict[str, Path] = {}
        for path, mtime in recent_transcripts(self.home, since):
            session_id = path.stem
            if (session_id in superseded or session_id in dropped) and session_id not in entries:
                continue
            if _SESSION_ID_RE.match(session_id) and session_id not in transcripts:
                transcripts[session_id] = path
                candidates[session_id] = max(candidates.get(session_id, 0.0), mtime)
        # Rank on cheap fields first (same order as the views: running/waiting, then activity) and
        # build views, whose titles may need a transcript scan, only for the first `limit`.
        ranked: list[tuple[bool, float, str, RegistryEntry | None, float]] = []
        for session_id, mtime in candidates.items():
            entry = entries.get(session_id)
            meta = self._meta_for(session_id, entry)  # the same (newest) meta the view is built from
            if meta is not None and meta.scheduled and not include_scheduled:
                continue
            if meta is not None and meta.archived and entry is None:
                continue
            state = _STATES.get(entry.status, "unknown") if entry is not None else "idle"
            activity = max(entry.updated_at if entry else 0.0, meta.last_activity if meta else 0.0, mtime)
            ranked.append((state not in ("running", "waiting"), -activity, session_id, entry, mtime))
        ranked.sort(key=lambda item: item[:2])
        views: list[SessionView] = []
        for _idle, _activity, session_id, entry, mtime in ranked:
            if len(views) >= limit:
                break
            try:
                views.append(self._build_view(session_id, entry, transcripts.get(session_id), mtime))
            except Exception:  # noqa: BLE001
                log.exception(tr('Failed to prepare the Claude session list'))
        return views

    def view(self, session_id: str) -> SessionView | None:
        if not _SESSION_ID_RE.match(session_id or ""):
            return None
        self._refresh_desktop(self.clock(), DESKTOP_REFRESH_S)
        entry = self.live(session_id)
        meta = self._meta_for(session_id, entry)
        cwd = (entry.cwd if entry else "") or (meta.cwd if meta else "")
        path = self._transcript(session_id, cwd)
        if entry is None and meta is None and path is None:
            return None
        mtime = 0.0
        if path is not None:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
        return self._build_view(session_id, entry, path, mtime)

    def _build_view(self, session_id: str, entry: RegistryEntry | None, path: Path | None, mtime: float) -> SessionView:
        meta = self._meta_for(session_id, entry)
        cwd = (entry.cwd if entry else "") or (meta.cwd if meta else "")
        if not cwd and path is not None:
            cwd = self._transcript_info(path).get("cwd", "")
        if entry is not None:
            state = _STATES.get(entry.status, "unknown")
        else:
            state = "idle"
        if state == "waiting":
            detail = _one_line(entry.waiting_for if entry else "") or tr('Waiting for attention')
        elif meta is not None and not self._excluded(cwd, meta.cwd):
            detail = _one_line(meta.status_detail)
        else:
            detail = ""
        with self._lock:
            headless = session_id in self._headless
        if headless:
            kind = "headless"
        elif meta is not None or (entry is not None and entry.entrypoint == "claude-desktop"):
            kind = "desktop"
        else:
            kind = "cli"
        return SessionView(
            agent=CLAUDE,
            id=session_id,
            title=self._title(session_id, cwd, entry, meta, path),
            cwd=cwd,
            state=state,
            last_activity=max(entry.updated_at if entry else 0.0, meta.last_activity if meta else 0.0, mtime),
            live=entry is not None,
            detail=detail,
            kind=kind,
        )

    @staticmethod
    def quota() -> Quota | None:
        """The account's plan limits as Claude Desktop last recorded them (they may be hours old)."""

        return quota.claude_desktop_quota()

    def hooked(self, session_id: str) -> bool:
        """The session reported a hook to this bridge (its PermissionRequest hooks reach it too)."""

        with self._lock:
            return session_id in self._hooked

    def live(self, session_id: str) -> RegistryEntry | None:
        for entry in read_registry(self.home, alive=self.alive):
            if entry.session_id == session_id:
                return entry
        return None

    def read_turns(self, session_id: str, k: int) -> list[ClaudeTurn]:
        """The last k turns (oldest first); content is blanked for content_exclude dirs."""

        path, cwds, entry = self._locate(session_id)
        if path is None:
            return []
        try:
            turns = read_turns(path, turns=k)
        except OSError:
            return []
        if turns and turns[-1].status == "running" and self._not_running(entry):
            last = turns[-1]  # the process died mid-turn (or went idle without finishing it)
            turns[-1] = ClaudeTurn(last.user_text, last.final_text, "aborted", last.started_at, None, last.error)
        if self._excluded(*cwds):
            return [ClaudeTurn("", None, turn.status, turn.started_at, turn.completed_at, turn.error) for turn in turns]
        return turns

    def _not_running(self, entry: RegistryEntry | None) -> bool:
        """True when the registry rules out a running turn: no live process, or idle for a while."""

        if entry is None:
            return True
        if entry.status != "idle":
            return False
        since = entry.status_updated_at or entry.updated_at
        return since > 0 and self.clock() - since >= STALE_RUNNING_S

    def full_text(self, session_id: str) -> str | None:
        """The final reply of the latest finished turn (the running turn's latest text otherwise)."""

        turns = self.read_turns(session_id, 2)
        for turn in reversed(turns):
            if turn.status != "running" and turn.final_text:
                return turn.final_text
        return turns[-1].final_text if turns else None

    def latest_summary(self, session_id: str) -> str:
        """Desktop's post-turn summary when it describes the latest assistant message, else ""."""

        self._refresh_desktop(self.clock(), 1.0)
        entry = self.live(session_id)
        meta = self._meta_for(session_id, entry)
        if meta is None:
            return ""
        cwd = (entry.cwd if entry else "") or meta.cwd
        return "" if self._excluded(cwd, meta.cwd) else self._summary(meta, self._transcript(session_id, cwd))

    def permission_mode(self, session_id: str) -> str:
        """DesktopMeta.permission_mode, else the mode the hooks last reported, else "default"."""

        self._refresh_desktop(self.clock(), DESKTOP_REFRESH_S)
        meta = self._meta_for(session_id, self.live(session_id))
        if meta is not None and meta.permission_mode:
            return meta.permission_mode
        with self._lock:
            return self._modes.get(session_id) or "default"

    # -- delivery ------------------------------------------------------------------------

    def inbox_for(self, session_id: str) -> tuple[str, str] | None:
        """The stored (socket, token), only while a live process still owns that socket."""

        entry = self.live(session_id)
        stored = self._stored_inbox(session_id)
        if entry is None or stored is None or not entry.socket:
            return None
        if _socket_id(entry.socket) != _socket_id(stored[0]):
            return None
        return stored

    def _stored_inbox(self, session_id: str) -> tuple[str, str] | None:
        try:
            stored = self.secrets.get(session_key(CLAUDE, session_id))
        except Exception:  # noqa: BLE001
            log.warning(tr('Failed to read Claude session inbox information'))
            return None
        if not isinstance(stored, dict):
            return None
        socket, token = _str(stored.get("socket")), _str(stored.get("token"))
        return (socket, token) if socket and token else None

    def deliver(self, session_id: str, text: str, *, post: Callable[..., None] | None = None,
                priority: str = "next") -> dict[str, Any]:
        """Post text into the session's inbox; returns what check_delivered needs.

        ``priority`` "next" steers a running turn, "later" queues the text as the next turn
        (see inbox.PRIORITIES). Raises inbox.InboxError with a Chinese reason when the session
        cannot receive it.
        """

        inbox = _inbox_module()
        post = inbox.post if post is None else post
        entry = self.live(session_id)
        if entry is None:
            raise inbox.InboxError(tr('This session has no running process; its inbox cannot receive messages'))
        stored = self._stored_inbox(session_id)
        if stored is None:
            raise inbox.InboxError(tr('No inbox information received for this session yet (trigger a hook first, for example by finishing a turn)'))
        if not entry.socket or _socket_id(entry.socket) != _socket_id(stored[0]):
            raise inbox.InboxError(tr('The saved inbox information is stale (the session process restarted); try again after its next hook'))
        path = self._transcript(session_id, entry.cwd)
        offset = 0
        if path is not None:
            try:
                offset = path.stat().st_size
            except OSError:
                path, offset = None, 0
        try:
            if priority == "next":
                post(stored[0], stored[1], text)  # the default frame, as before
            else:
                post(stored[0], stored[1], text, priority=priority)
        except inbox.InboxError:
            raise
        except OSError as exc:
            code = getattr(exc, "winerror", None) or exc.errno
            raise inbox.InboxError(tr('Failed to deliver to the session inbox ({v0} {v1})', v0=type(exc).__name__, v1=code)) from None
        return {"transcript": str(path) if path is not None else "", "offset": offset, "at": self.clock()}

    def check_delivered(self, session_id: str, marker: str, info: dict[str, Any]) -> bool:
        transcript = _str(info.get("transcript"))
        offset = _int(info.get("offset")) or 0
        path = Path(transcript) if transcript else None
        if path is None or not path.is_file():
            found, _cwds, _entry = self._locate(session_id)
            if found is None:
                return False
            if path is None or found != path:
                offset = 0  # the transcript did not exist when the message was posted
            path = found
        return delivered(path, marker, offset)

    # -- internals -----------------------------------------------------------------------

    def _emit(self, event: Event) -> None:
        try:
            self.emit(event)
        except Exception:  # noqa: BLE001 - a failing consumer must not stop the source
            log.exception(tr('Failed to push a Claude event'))

    def _refresh_desktop(self, now: float, max_age: float) -> None:
        with self._desktop_lock:  # one scan at a time; other threads keep reading the current index
            if self._auto_desktop and (self._root_at is None or now - self._root_at >= DESKTOP_ROOT_RECHECK_S):
                self._root_at = now
                try:
                    root = desktop_sessions_dir()
                except Exception:  # noqa: BLE001
                    log.exception(tr('Failed to locate the Claude Desktop sessions directory'))
                    root = self._index.root
                if root != self._index.root:
                    index = DesktopIndex(root)
                    try:
                        index.refresh()  # fill it before other threads can see it
                    except Exception:  # noqa: BLE001
                        log.exception(tr('Failed to read Claude Desktop session information'))
                    self._index, self._index_at = index, now
                    return
            if self._index_at is None or now - self._index_at >= max_age or now < self._index_at:
                self._index_at = now
                try:
                    self._index.refresh()
                except Exception:  # noqa: BLE001
                    log.exception(tr('Failed to read Claude Desktop session information'))

    def _meta_for(self, session_id: str, entry: RegistryEntry | None) -> DesktopMeta | None:
        meta = self._index.by_local_id(entry.host_session_id) if entry is not None and entry.host_session_id else None
        return meta or self._index.by_cli_session(session_id)

    def _transcript(self, session_id: str, cwd: str = "") -> Path | None:
        with self._lock:
            cached = self._paths.get(session_id)
        if cached is not None and cached.is_file():
            return cached
        path = find_transcript(self.home, session_id, cwd)
        if path is not None:
            with self._lock:
                self._paths[session_id] = path
                _trim(self._paths)
        return path

    def _locate(self, session_id: str) -> tuple[Path | None, tuple[str, ...], RegistryEntry | None]:
        """(transcript, every known cwd of the session, live entry)."""

        self._refresh_desktop(self.clock(), DESKTOP_REFRESH_S)
        entry = self.live(session_id)
        meta = self._meta_for(session_id, entry)
        cwds = (entry.cwd if entry else "", meta.cwd if meta else "")
        path = self._transcript(session_id, cwds[0] or cwds[1])
        if path is not None:
            cwds += (_transcript_cwd(path),)
        return path, cwds, entry

    def _transcript_info(self, path: Path) -> dict[str, str]:
        """{"title", "cwd"} of a transcript, cached by (mtime, size)."""

        try:
            stat = path.stat()
        except OSError:
            return {"title": "", "cwd": ""}
        stamp = (stat.st_mtime_ns, stat.st_size)
        key = str(path)
        with self._lock:
            cached = self._tinfo.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        info = {"title": transcript_title(path), "cwd": _transcript_cwd(path)}
        with self._lock:
            self._tinfo[key] = (stamp, info)
            _trim(self._tinfo, 500)
        return info

    def _title(
        self,
        session_id: str,
        cwd: str,
        entry: RegistryEntry | None,
        meta: DesktopMeta | None,
        path: Path | None = None,
    ) -> str:
        """DesktopMeta.title > registry name > transcript custom title > basename(cwd)."""

        for candidate in (meta.title if meta else "", entry.name if entry else ""):
            title = _one_line(candidate)
            if title:
                return title
        if path is None:
            path = self._transcript(session_id, cwd)
        if path is not None:
            title = self._transcript_info(path).get("title", "")
            if title:
                return title
        return _one_line(_basename(cwd)) or session_id[:8]

    def _summary(self, meta: DesktopMeta | None, path: Path | None) -> str:
        if meta is None or path is None or not meta.status_detail or not meta.summarizes_uuid:
            return ""
        return _one_line(meta.status_detail, 500) if meta.summarizes_uuid == _last_assistant_uuid(path) else ""

    def _excluded(self, *cwds: str) -> bool:
        """True when any known directory of the session is in cfg.content_exclude."""

        return any(cwd and self.cfg.excluded(cwd) for cwd in cwds)

    def _apply_exclusion(self, event: TurnEvent, *other_cwds: str) -> TurnEvent:
        if self._excluded(event.cwd, *other_cwds):
            event.final_text = None
            event.summary = ""
            if tr('Content hidden') not in event.labels:
                event.labels.append(tr('Content hidden'))
        return event


def _same_reply(hook_text: str | None, transcript_text: str) -> bool:
    """True when the transcript reply is the one a (cut) hook message starts, or there is none."""

    if hook_text is None:
        return True
    head = _one_line(hook_text, 300)
    return _one_line(transcript_text, len(head) + 50).startswith(head) and len(transcript_text) > len(hook_text)


def _local_transcript_path(value: str, session_id: str) -> Path | None:
    """A hook's transcript_path when it is a local absolute <session_id>.jsonl path.

    UNC paths are refused: probing one would make Windows contact (and authenticate to) that host.
    """

    if not value or value.startswith(("\\\\", "//")):
        return None
    path = Path(value)
    if not path.is_absolute() or path.name.lower() != f"{session_id}.jsonl".lower():
        return None
    return path


def _error_part(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        return str(value)[:2000]


def __getattr__(name: str) -> str:
    """Resolve legacy message exports at access time, including for the pending bridge."""
    if name == 'EXCLUDED_LABEL':
        return tr('Content hidden')
    if name == 'WAITING_REASON':
        return tr('Waiting for attention')
    raise AttributeError(name)

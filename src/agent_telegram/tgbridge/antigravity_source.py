"""Read-only Antigravity conversations for the Telegram bridge.

This module reads only ``conversation_summaries.db`` (opened in SQLite
``mode=ro``) and ``brain/*/.system_generated/logs/transcript*.jsonl`` below
the configured Antigravity home.  It never writes below ``~/.gemini``, never
runs ``agy``, and never talks to Antigravity or Google.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from agent_telegram.i18n import tr
from agent_telegram.tgbridge import progress
from agent_telegram.tgbridge.model import ANTIGRAVITY, Event, ProgressEvent, SessionView, TurnEvent

SETTLE_S = 15.0
MAX_COMMENTARY = 5
MAX_PROGRESS = 4000
MAX_ERROR = 500
REMEMBERED = 2000
READ_START_BYTES = 4 * 1024 * 1024
READ_MAX_BYTES = 64 * 1024 * 1024
FULL_SCAN_CHUNK = 1024 * 1024

log = logging.getLogger(__name__)

MAILBOX_TICK_MARKER = "[agent-mailbox tick]"
MAILBOX_IDLE_REPLY = "MAILBOX_IDLE"


def is_mailbox_tick(user_text: str) -> bool:
    """Return whether a user request starts an automated mailbox tick."""

    return isinstance(user_text, str) and user_text.strip().startswith(MAILBOX_TICK_MARKER)


def _normalized_mailbox_reply(final_text: str) -> str:
    """Normalize the short wrapper punctuation used by mailbox replies."""

    value = final_text if isinstance(final_text, str) else ""
    while True:
        previous = value
        value = value.strip().strip("`'\"*“”‘’ \t\r\n")
        value = value.rstrip(".,。！!")  # i18n: data
        if value == previous:
            return value


def is_mailbox_idle(user_text: str, final_text: str) -> bool:
    """Return whether a mailbox tick completed without claiming a task."""

    return is_mailbox_tick(user_text) and _normalized_mailbox_reply(final_text) == MAILBOX_IDLE_REPLY


def default_home(cfg: Any) -> Path:
    """Return the configured Antigravity root (``~/.gemini`` by default)."""

    value = str(getattr(cfg, "antigravity_home", "") or "").strip()
    return Path(value).expanduser() if value else Path.home() / ".gemini"


def find_stores(home: Path) -> list[tuple[str, Path]]:
    """Return existing desktop and CLI stores in their stable order."""

    home = Path(home)
    found: list[tuple[str, Path]] = []
    for name, path in (("desktop", home / "antigravity"), ("cli", home / "antigravity-cli")):
        if path.is_dir():
            found.append((name, path))
    return found


def _one_line(text: str, limit: int = 60) -> str:
    return " ".join(str(text or "").split())[:limit]


def _epoch(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    # Antigravity emits seven fractional digits.  datetime.fromisoformat on
    # Python 3.11 accepts six, so trim only the excess part.
    try:
        if "." in value:
            head, tail = value.split(".", 1)
            zone = ""
            for marker in ("+", "-"):
                pos = tail.find(marker)
                if pos > 0:
                    zone, tail = tail[pos:], tail[:pos]
                    break
            value = head + "." + tail[:6] + zone
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _path_from_uri(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        parts = urlsplit(unquote(value))
    except ValueError:
        return ""
    if parts.scheme and parts.scheme.lower() != "file":
        return unquote(value)
    path = unquote(parts.path or value)
    if parts.netloc and parts.netloc.lower() != "localhost":
        path = "//" + parts.netloc + path
    # The stores contain URI paths with either ``/c:/...`` or ``/C:/...``.
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    if os.name == "nt":
        path = path.replace("/", "\\")
    return path


def _cwd(workspace_uris: Any) -> str:
    if isinstance(workspace_uris, str):
        try:
            workspace_uris = json.loads(workspace_uris)
        except (TypeError, ValueError):
            workspace_uris = []
    if isinstance(workspace_uris, list) and workspace_uris:
        return _path_from_uri(workspace_uris[0])
    return ""


def _json_line(data: bytes) -> dict[str, Any] | None:
    try:
        item = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return item if isinstance(item, dict) else None


@dataclass
class _Meta:
    store: str
    root: Path
    conversation_id: str
    title: str = ""
    cwd: str = ""
    status: str = ""
    step_count: int = 0
    last_modified: float = 0.0
    not_fully_idle: int = 0
    killed: int = 0
    parent: str = ""
    index_available: bool = True
    transcript: Path | None = None
    archived: bool = False
    archived_at: float = 0.0


@dataclass
class _State:
    meta: _Meta
    offset: int = 0
    file_size: int = 0
    last_growth: float = 0.0
    steps: list[dict[str, Any]] = field(default_factory=list)
    baseline_cutoff: float = 0.0
    initialized: bool = False
    progress_seen: set[int] = field(default_factory=set)
    previous_status: str = ""
    last_finished: tuple[str, str] | None = None
    last_progress: str = ""
    baseline_max_user_step: int = -1
    file_inode: int = 0
    pending_steps: list[dict[str, Any]] = field(default_factory=list)


# Public callers use the same metadata object as the polling source without
# depending on its private class name.
ConversationMeta = _Meta


def _step_index(step: dict[str, Any]) -> int:
    try:
        return int(step.get("step_index"))
    except (TypeError, ValueError):
        return -1


def _turn_groups(steps: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] | None = None
    for step in steps:
        if step.get("source") == "USER_EXPLICIT" and step.get("type") == "USER_INPUT":
            current = [step]
            groups.append(current)
        elif current is not None:
            current.append(step)
    return groups


def _relevant(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [step for step in group if step.get("type") in ("PLANNER_RESPONSE", "ERROR_MESSAGE", "GENERIC")]


def _content(step: dict[str, Any]) -> str:
    value = step.get("content")
    return value if isinstance(value, str) else ""


_USER_REQUEST = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)


def _user_text(step: dict[str, Any]) -> str:
    """What the user typed: Antigravity wraps it in <USER_REQUEST> and appends metadata blocks."""

    text = _content(step)
    match = _USER_REQUEST.search(text)
    return match.group(1) if match else text


def _has_tools(step: dict[str, Any]) -> bool:
    return bool(step.get("tool_calls"))


def _slim_step(step: dict[str, Any]) -> dict[str, Any]:
    """Keep only user-visible transcript fields; never retain private payloads."""

    slim: dict[str, Any] = {}
    for key in ("step_index", "source", "type", "status", "created_at", "content", "error", "truncated_fields"):
        if key in step:
            value = step[key]
            if key == "content" and not isinstance(value, str):
                value = ""
            if key == "truncated_fields" and not isinstance(value, list):
                value = []
            slim[key] = value
    if "tool_calls" in step:
        slim["tool_calls"] = bool(step.get("tool_calls"))
    return slim


def archive_dir(root: Path | None = None, environ: dict[str, str] | None = None) -> Path:
    """Return the persistent transcript archive directory for an Antigravity store."""

    env = os.environ if environ is None else environ
    explicit = env.get("ANTIGRAVITY_ARCHIVE_DIR", "").strip()
    if explicit:
        return Path(explicit)

    gem_env = env.get("AGENT_CONTEXT_GEMINI_HOME", "").strip()
    expected_home = Path(gem_env) if gem_env else (Path.home() / ".gemini")
    if root is not None:
        try:
            if Path(root).parent.resolve() != expected_home.resolve():
                return Path(root) / "brain_archive"
        except OSError:
            return Path(root) / "brain_archive"

    title_state = env.get("ANTIGRAVITY_TITLE_STATE_DIR", "").strip()
    if title_state:
        return Path(title_state) / "antigravity_archive"
    var_override = env.get("AGENT_TG_VAR_DIR", "").strip()
    if var_override:
        return Path(var_override) / "agent_context" / "antigravity_archive"
    if gem_env:
        return Path(gem_env) / "antigravity_archive"

    from agent_telegram import settings

    return settings.var_dir() / "agent_context" / "antigravity_archive"


def resolve_transcript(root: Path, conversation_id: str) -> Path:
    """Return the live transcript path if present, otherwise fall back to the archive if available."""

    live = Path(root) / "brain" / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
    if live.is_file():
        return live
    if Path(root).name == "antigravity":
        archived = archive_dir(root) / conversation_id / "transcript.jsonl"
        if archived.is_file():
            return archived
    return live


def _merge_archived_rows(name: str, root: Path, rows: dict[str, _Meta]) -> None:
    """Include archived desktop conversations that are no longer in `rows`."""

    if Path(root).name != "antigravity":
        return
    arch_root = archive_dir(root)
    if not arch_root.is_dir():
        return
    try:
        entries = list(arch_root.iterdir())
    except OSError:
        return
    for item in entries:
        if not item.is_dir():
            continue
        cid = item.name
        if not cid or cid in rows:
            continue
        tp = item / "transcript.jsonl"
        if not tp.is_file():
            continue
        meta_file = item / "meta.json"
        meta_doc: dict[str, Any] = {}
        if meta_file.is_file():
            try:
                loaded = json.loads(meta_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    meta_doc = loaded
            except (OSError, ValueError, UnicodeError):
                meta_doc = {}
        if str(meta_doc.get("parent") or ""):
            continue
        try:
            modified = float(meta_doc.get("last_modified") or tp.stat().st_mtime)
        except (OSError, TypeError, ValueError):
            modified = 0.0
        rows[cid] = _Meta(
            store=name,
            root=Path(root),
            conversation_id=cid,
            title=str(meta_doc.get("title") or ""),
            cwd=str(meta_doc.get("cwd") or ""),
            status=str(meta_doc.get("status") or "CASCADE_RUN_STATUS_IDLE"),
            step_count=int(meta_doc.get("step_count") or 0),
            last_modified=modified,
            not_fully_idle=0,
            killed=0,
            parent="",
            index_available=bool(meta_doc),
            transcript=tp,
        )


def _read_proto_varint(data: bytes, pos: int) -> tuple[int, int] | None:
    n = len(data)
    result = 0
    shift = 0
    while pos < n:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift >= 64:
            return None
    return None


def _iter_proto_fields(data: bytes):
    pos = 0
    n = len(data)
    while pos < n:
        tag_res = _read_proto_varint(data, pos)
        if tag_res is None:
            return
        tag, pos = tag_res
        field_num = tag >> 3
        wire_type = tag & 0x07
        if field_num <= 0:
            return
        if wire_type == 0:
            val_res = _read_proto_varint(data, pos)
            if val_res is None:
                return
            val, pos = val_res
            yield field_num, wire_type, val
        elif wire_type == 1:
            if pos + 8 > n:
                return
            yield field_num, wire_type, data[pos : pos + 8]
            pos += 8
        elif wire_type == 2:
            len_res = _read_proto_varint(data, pos)
            if len_res is None:
                return
            length, pos = len_res
            if length < 0 or pos + length > n:
                return
            yield field_num, wire_type, data[pos : pos + length]
            pos += length
        elif wire_type == 5:
            if pos + 4 > n:
                return
            yield field_num, wire_type, data[pos : pos + 4]
            pos += 4
        else:
            return


def _extract_summary_archived(raw_summary: bytes | None) -> tuple[bool, float]:
    """Return ``(archived, archived_at_epoch)`` from ``raw_summary`` proto field 15 (annotations)."""

    if not isinstance(raw_summary, (bytes, bytearray)) or not raw_summary:
        return False, 0.0
    data = bytes(raw_summary)
    archived = False
    archived_at = 0.0
    for fnum, wtype, val in _iter_proto_fields(data):
        if fnum == 15 and wtype == 2 and isinstance(val, bytes):
            for afnum, awtype, aval in _iter_proto_fields(val):
                if afnum == 4 and awtype == 0 and isinstance(aval, int) and aval != 0:
                    archived = True
                elif afnum == 5 and awtype == 2 and isinstance(aval, bytes):
                    sec = 0
                    nanos = 0
                    for tfnum, twtype, tval in _iter_proto_fields(aval):
                        if tfnum == 1 and twtype == 0 and isinstance(tval, int):
                            sec = tval
                        elif tfnum == 2 and twtype == 0 and isinstance(tval, int):
                            nanos = tval
                    if sec > 0:
                        archived_at = float(sec) + float(nanos) / 1_000_000_000.0
    return archived, archived_at


def inspect_conversation_state(
    conversation_id: str,
    home: Path | str | None = None,
) -> dict[str, Any] | None:
    """Read-only lookup of a single Antigravity conversation's live run/archive state."""

    cid = str(conversation_id or "").strip()
    if not cid:
        return None
    resolved_home = (
        Path(home).expanduser()
        if home is not None
        else Path(os.environ.get("AGENT_CONTEXT_GEMINI_HOME") or (Path.home() / ".gemini")).expanduser()
    )
    for store_name, root in find_stores(resolved_home):
        db = root / "conversation_summaries.db"
        if not db.is_file():
            continue
        uri = f"file:{db.as_posix()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=0.5)
            try:
                row = conn.execute(
                    "SELECT status, not_fully_idle, killed, last_modified_time, raw_summary "
                    "FROM conversation_summaries WHERE conversation_id = ?",
                    (cid,),
                ).fetchone()
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            continue
        if not row:
            continue
        status = str(row[0] or "").strip()
        not_fully_idle = int(row[1] or 0)
        killed = int(row[2] or 0)
        last_modified = _epoch(row[3]) or 0.0
        archived, archived_at = _extract_summary_archived(row[4])
        tp = resolve_transcript(root, cid)
        tp_mtime = 0.0
        if tp.is_file():
            try:
                tp_mtime = float(tp.stat().st_mtime)
            except OSError:
                tp_mtime = 0.0
        last_activity = max(last_modified, tp_mtime, archived_at)
        return {
            "store": store_name,
            "conversation_id": cid,
            "status": status,
            "not_fully_idle": not_fully_idle,
            "killed": killed,
            "archived": archived,
            "archived_at": archived_at,
            "last_modified": last_modified,
            "last_activity": last_activity,
            "is_idle": status == "CASCADE_RUN_STATUS_IDLE" and not_fully_idle == 0,
        }
    return None


def query_index(name: str, root: Path) -> dict[str, _Meta]:
    """Read one Antigravity ``conversation_summaries.db`` in read-only mode.

    Child conversations and rows without an id are omitted.  The SQLite
    connection is always closed before this function returns or raises.
    """

    root = Path(root)
    db = root / "conversation_summaries.db"
    if not db.is_file():
        raise FileNotFoundError(str(db))
    columns = (
        "conversation_id,title,preview,step_count,last_modified_time,workspace_uris,status,source,"
        "project_id,agent_name,parent_conversation_id,nesting_depth,battle_id,winning_conversation_id,"
        "not_fully_idle,killed,last_user_input_time,last_user_input_step_index,app_data_dir,raw_summary,group_id"
    )
    rows: dict[str, _Meta] = {}
    uri = f"file:{db.as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=1.0)
    try:
        for values in connection.execute(f"SELECT {columns} FROM conversation_summaries"):
            conversation_id = str(values[0] or "")
            if not conversation_id or str(values[10] or ""):
                continue
            archived, archived_at = _extract_summary_archived(values[19] if isinstance(values[19], bytes) else None)
            rows[conversation_id] = _Meta(
                store=name,
                root=root,
                conversation_id=conversation_id,
                title=str(values[1] or ""),
                cwd=_cwd(values[5]),
                status=str(values[6] or ""),
                step_count=int(values[3] or 0),
                last_modified=_epoch(values[4]) or 0.0,
                not_fully_idle=int(values[14] or 0),
                killed=int(values[15] or 0),
                parent=str(values[10] or ""),
                index_available=True,
                transcript=resolve_transcript(root, conversation_id),
                archived=archived,
                archived_at=archived_at,
            )
    finally:
        connection.close()
    _merge_archived_rows(name, root, rows)
    return rows


def fallback_index(name: str, root: Path) -> dict[str, _Meta]:
    """Build an index by scanning transcript files when SQLite is unavailable."""

    root = Path(root)
    rows: dict[str, _Meta] = {}
    brain = root / "brain"
    if brain.is_dir():
        try:
            candidates = list(brain.glob("*/.system_generated/logs/transcript.jsonl"))
        except OSError:
            candidates = []
        for transcript in candidates:
            conversation_id = transcript.parent.parent.parent.name
            try:
                modified = transcript.stat().st_mtime
            except OSError:
                modified = 0.0
            title = ""
            try:
                with transcript.open("rb") as stream:
                    for raw in stream:
                        step = _json_line(raw.rstrip(b"\r\n"))
                        if step and step.get("type") == "USER_INPUT":
                            text = _user_text(step)
                            title = _one_line(text.splitlines()[0] if text.splitlines() else "")
                            break
            except OSError:
                continue
            rows[conversation_id] = _Meta(
                store=name, root=root, conversation_id=conversation_id, title=title,
                cwd="", status="", last_modified=modified, index_available=False,
                transcript=transcript,
            )
    _merge_archived_rows(name, root, rows)
    return rows


def list_conversations(home: Path) -> list[_Meta]:
    """List conversations from every Antigravity store, newest first.

    Each store is read independently.  A missing, locked, or corrupt SQLite
    index falls back to its transcript files; this function never writes.
    """

    conversations: list[_Meta] = []
    for name, root in find_stores(Path(home)):
        try:
            rows = query_index(name, root)
        except (sqlite3.Error, OSError):
            rows = fallback_index(name, root)
        conversations.extend(rows.values())
    conversations.sort(key=lambda meta: meta.last_modified, reverse=True)
    return conversations


def _read_tail(path: Path | None, turns: int) -> list[dict[str, Any]]:
    """Read a transcript tail, widening it until one extra turn is present."""

    if path is None or not path.is_file():
        return []
    try:
        size = path.stat().st_size
    except OSError:
        return []
    budget = min(READ_START_BYTES, READ_MAX_BYTES)
    while True:
        start = max(0, size - budget)
        try:
            with path.open("rb") as stream:
                stream.seek(start)
                data = stream.read()
        except OSError:
            return []
        if start:
            boundary = data.find(b"\n")
            data = data[boundary + 1:] if boundary >= 0 else b""
        records: list[dict[str, Any]] = []
        for raw in data.splitlines():
            item = _json_line(raw.rstrip(b"\r"))
            if item is not None:
                records.append(item)
        if start == 0 or len(_turn_groups(records)) > turns or budget >= READ_MAX_BYTES:
            return records
        budget = min(budget * 4, READ_MAX_BYTES)


def _lookup_full_step(path: Path | None, wanted_index: int) -> str | None:
    """Find one full-content record by scanning transcript_full backwards."""

    if path is None or wanted_index < 0 or not path.is_file():
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            position = size
            carry = b""
            while position > 0:
                start = max(0, position - FULL_SCAN_CHUNK)
                stream.seek(start)
                data = stream.read(position - start) + carry
                lines = data.split(b"\n")
                carry = lines[0]
                for raw in reversed(lines[1:]):
                    item = _json_line(raw.rstrip(b"\r"))
                    if item is not None and _step_index(item) == wanted_index:
                        value = item.get("content")
                        return value if isinstance(value, str) else None
                position = start
            if carry:
                item = _json_line(carry.rstrip(b"\r"))
                if item is not None and _step_index(item) == wanted_index:
                    value = item.get("content")
                    return value if isinstance(value, str) else None
    except OSError:
        return None
    return None


def _turn_result(
    state: _State,
    group: list[dict[str, Any]],
    *,
    now: float,
    allow_settle: bool = True,
    historical: bool = False,
) -> dict[str, Any] | None:
    if not group:
        return None
    user = group[0]
    relevant = _relevant(group)
    last = relevant[-1] if relevant else None
    row = state.meta
    status = row.status
    idle = status == "CASCADE_RUN_STATUS_IDLE" and row.not_fully_idle == 0
    cancel_to_idle = state.previous_status == "CASCADE_RUN_STATUS_CANCELING" and idle
    if last is None:
        max_index = max((_step_index(s) for s in group), default=-1)
        no_last_finished = (not historical and (row.killed or cancel_to_idle)) or (
            row.index_available and idle and bool(row.step_count) and max_index + 1 >= row.step_count
        )
        if no_last_finished:
            return {
                "turn_id": str(_step_index(user)), "status": "aborted",
                "user_text": _user_text(user), "final_message": None,
                "error": tr('This turn was aborted'), "started_at": _epoch(user.get("created_at")),
                "completed_at": _epoch(user.get("created_at")), "last": None,
            }
        return None
    settled = now - state.last_growth >= SETTLE_S
    if historical:
        finished = True
    elif row.index_available:
        complete_count = bool(row.step_count) and max((_step_index(s) for s in group), default=-1) + 1 >= row.step_count
        finished = (idle and (complete_count or settled)) or bool(row.killed) or cancel_to_idle
    else:
        finished = settled and (last.get("type") == "ERROR_MESSAGE" or
                                 (last.get("type") == "PLANNER_RESPONSE" and not _has_tools(last)))
    if not finished:
        return None
    if not historical and (row.killed or cancel_to_idle):
        result_status = "aborted"
    elif last.get("type") == "PLANNER_RESPONSE" and not _has_tools(last) and _content(last):
        result_status = "completed"
    elif last.get("type") == "ERROR_MESSAGE":
        result_status = "failed"
    else:
        result_status = "aborted"
    content = _content(last)
    if result_status == "completed":
        final = content
        final_step = last
    elif result_status == "failed":
        previous_answers = [item for item in relevant
                            if item.get("type") == "PLANNER_RESPONSE" and _content(item)]
        answer = previous_answers[-1] if previous_answers else None
        final = _content(answer) if answer is not None else None
        final_step = answer
    else:
        final = None
        final_step = None
    error = ""
    if result_status == "failed":
        error = _one_line(str(last.get("error") or _content(last)), MAX_ERROR)
    elif result_status == "aborted":
        error = tr('This turn was aborted')
    completed = _epoch(last.get("created_at"))
    return {
        "turn_id": str(_step_index(user)),
        "status": result_status,
        "user_text": _user_text(user),
        "final_message": final,
        "error": error,
        "started_at": _epoch(user.get("created_at")),
        "completed_at": completed,
        "last": last,
        "final_step": final_step,
    }


def _read_lookup_full_step(path: Path | None, wanted_index: int) -> str | None:
    """Call the module helper, honoring the legacy class hook used by tests."""

    source_cls = globals().get("AntigravitySource")
    if source_cls is not None:
        candidate = getattr(source_cls, "_lookup_full_step", None)
        if candidate is not None and candidate is not _lookup_full_step:
            return candidate(path, wanted_index)
    return _lookup_full_step(path, wanted_index)


def read_conversation(
    meta: _Meta,
    turns: int = 20,
    *,
    excluded: bool = False,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Read the newest transcript turns for one conversation metadata row."""

    if now is None:
        now = time.time()
    state = _State(meta=meta)
    state.steps = _read_tail(meta.transcript, turns)
    groups = _turn_groups(state.steps)
    items: list[dict[str, Any]] = []
    for position, group in enumerate(groups):
        # transcript.jsonl may truncate user, commentary, and answer text;
        # transcript_full.jsonl carries the same step index with the full
        # content.  Use the latter for all read methods while retaining the
        # lightweight incremental representation for polling.
        complete_group: list[dict[str, Any]] = []
        for original in group:
            complete = dict(original)
            index = _step_index(original)
            # Only texts read_turns shows; tool output (GENERIC) is never sent.
            if (original.get("type") in ("USER_INPUT", "PLANNER_RESPONSE", "ERROR_MESSAGE")
                    and "content" in (original.get("truncated_fields") or [])):
                full = _read_lookup_full_step(
                    meta.transcript.with_name("transcript_full.jsonl") if meta.transcript else None,
                    index,
                )
                if full is not None:
                    complete["content"] = full
            complete_group.append(complete)
        result = _turn_result(state, complete_group, now=now,
                              historical=position < len(groups) - 1)
        if result is None:
            user = complete_group[0]
            result = {
                "turn_id": str(_step_index(user)), "status": "running",
                "user_text": _user_text(user), "final_message": None, "error": "",
                "started_at": _epoch(user.get("created_at")), "completed_at": None,
            }
        commentary = [_content(step)[:MAX_PROGRESS] for step in complete_group
                      if step.get("type") == "PLANNER_RESPONSE" and _content(step) and _has_tools(step)]
        item = {
            "turn_id": result["turn_id"], "status": result["status"],
            "user_text": result["user_text"], "final_message": result["final_message"],
            "error": result["error"], "commentary": commentary[-MAX_COMMENTARY:],
            "started_at": result["started_at"], "completed_at": result["completed_at"],
            "content_excluded": excluded,
        }
        if excluded:
            item.update(user_text="", final_message=None, commentary=[])
        items.append(item)
    # Index status takes precedence for an unfinished last turn.
    if items and meta.status in ("CASCADE_RUN_STATUS_RUNNING", "CASCADE_RUN_STATUS_BUSY", "CASCADE_RUN_STATUS_CANCELING"):
        items[-1]["status"] = "running"
    return items


class AntigravitySource:
    """View-only reader for Antigravity desktop and ``agy`` CLI stores."""

    last_error: str | None

    def __init__(
        self,
        cfg: Any,
        emit: Callable[[Event], None],
        *,
        home: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.emit = emit
        self.home = Path(home) if home is not None else default_home(cfg)
        self.clock = clock
        self._lock = threading.RLock()
        self._baseline: float | None = None
        self._states: dict[str, _State] = {}
        self._rows: dict[str, _Meta] = {}
        self._db_mtimes: dict[str, tuple[int, int]] = {}
        self._store_rows: dict[str, dict[str, _Meta]] = {}
        self._emitted: dict[tuple[str, str], None] = {}
        self._outbox: list[Event] = []
        self.last_error: str | None = None

    # --- store/index handling -------------------------------------------------

    @staticmethod
    def _mtime_pair(path: Path) -> tuple[int, int]:
        try:
            db = path.stat().st_mtime_ns
        except OSError:
            db = -1
        try:
            wal = path.with_name(path.name + "-wal").stat().st_mtime_ns
        except OSError:
            wal = 0
        return db, wal

    def _query_index(self, name: str, root: Path) -> tuple[dict[str, _Meta], str | None]:
        db = root / "conversation_summaries.db"
        pair = self._mtime_pair(db)
        if pair == self._db_mtimes.get(name) and name in self._store_rows:
            return dict(self._store_rows[name]), None
        rows = query_index(name, root)
        self._db_mtimes[name] = pair
        self._store_rows[name] = rows
        return dict(rows), None

    def _fallback_index(self, name: str, root: Path) -> dict[str, _Meta]:
        return fallback_index(name, root)

    def _stores(self) -> list[tuple[str, Path]]:
        return find_stores(self.home)

    def _active(self, meta: _Meta, now: float) -> bool:
        window = float(getattr(self.cfg, "active_window_h", 24.0)) * 3600.0
        return meta.last_modified >= now - window

    # --- transcript reading ---------------------------------------------------

    def _read_incremental(self, state: _State, now: float, *, initial: bool = False) -> list[dict[str, Any]]:
        path = state.meta.transcript
        if path is None:
            return []
        try:
            stat = path.stat()
            size = stat.st_size
            inode = int(getattr(stat, "st_ino", 0) or 0)
        except OSError:
            return []
        if not initial and state.file_inode and inode and inode != state.file_inode:
            state.offset, state.file_size, state.file_inode = size, size, inode
            state.steps.clear()
            state.progress_seen.clear()
            state.last_growth = now
            return []
        state.file_inode = inode or state.file_inode
        if not initial and size < state.offset:
            # A replacement/shrink is treated as a new baseline.  Do not push
            # old lines from the replacement; subsequent appended bytes are read.
            state.offset, state.file_size = size, size
            state.steps.clear()
            state.progress_seen.clear()
            state.last_growth = now
            return []
        start = 0 if initial else state.offset
        parsed: list[dict[str, Any]] = []
        try:
            with path.open("rb") as stream:
                stream.seek(start)
                if initial:
                    current: list[dict[str, Any]] = []
                    complete_offset = start
                    while True:
                        raw = stream.readline()
                        if not raw:
                            break
                        if not raw.endswith(b"\n"):
                            break
                        step = _json_line(raw.rstrip(b"\r\n"))
                        if step is not None:
                            slim = _slim_step(step)
                            if slim.get("source") == "USER_EXPLICIT" and slim.get("type") == "USER_INPUT":
                                current.clear()
                            current.append(slim)
                        complete_offset = stream.tell()
                    state.offset = complete_offset
                    state.file_size = size
                    return current
                chunk = stream.read()
        except OSError:
            return []
        if not chunk:
            state.file_size = size
            return []
        # A trailing partial line still means the transcript grew.  It must
        # postpone the settle timer until the line is completed.
        state.last_growth = now
        complete_end = chunk.rfind(b"\n")
        if complete_end < 0:
            state.file_size = size
            return []
        complete = chunk[: complete_end + 1]
        state.offset = start + complete_end + 1
        state.file_size = size
        for raw in complete.splitlines():
            step = _json_line(raw.rstrip(b"\r"))
            if step is not None:
                parsed.append(_slim_step(step))
        return parsed

    @staticmethod
    def _merge_current(state: _State, parsed: list[dict[str, Any]]) -> None:
        """Append slim records while retaining only the newest USER_INPUT turn."""

        for step in parsed:
            if step.get("source") == "USER_EXPLICIT" and step.get("type") == "USER_INPUT":
                state.steps.clear()
                state.progress_seen.clear()
            state.steps.append(step)

    @classmethod
    def _read_tail(cls, path: Path | None, turns: int) -> list[dict[str, Any]]:
        return _read_tail(path, turns)

    @staticmethod
    def _lookup_full_step(path: Path | None, wanted_index: int) -> str | None:
        return _lookup_full_step(path, wanted_index)

    # --- turns ---------------------------------------------------------------

    def _turn_result(
        self,
        state: _State,
        group: list[dict[str, Any]],
        *,
        now: float,
        allow_settle: bool = True,
        historical: bool = False,
    ) -> dict[str, Any] | None:
        return _turn_result(state, group, now=now, allow_settle=allow_settle, historical=historical)

    def _restore_result_content(self, state: _State, result: dict[str, Any]) -> dict[str, Any]:
        step = result.get("final_step")
        if isinstance(step, dict) and "content" in (step.get("truncated_fields") or []):
            full = self._lookup_full_step(
                state.meta.transcript.with_name("transcript_full.jsonl") if state.meta.transcript else None,
                _step_index(step),
            )
            if full is not None:
                result["final_message"] = full
        return result

    def _title(self, meta: _Meta, group: list[dict[str, Any]] | None = None) -> str:
        if meta.title.strip():
            return _one_line(meta.title)
        if group:
            raw = _user_text(group[0])
            text = _one_line(raw.splitlines()[0] if raw.splitlines() else "")
            if text:
                return text
        return meta.conversation_id[:8]

    def _excluded(self, cwd: str) -> bool:
        try:
            return bool(self.cfg.excluded(cwd))
        except (AttributeError, TypeError):
            return False

    def _queue(self, key: tuple[str, str], event: TurnEvent) -> None:
        if key in self._emitted:
            return
        self._remember(key)
        self._outbox.append(event)

    def _remember(self, key: tuple[str, str]) -> None:
        """Remember a delivered or intentionally suppressed turn."""

        self._emitted[key] = None
        if len(self._emitted) > REMEMBERED:
            del self._emitted[next(iter(self._emitted))]

    def _process_state(self, state: _State, now: float) -> None:
        historical_steps = state.pending_steps
        groups = _turn_groups(historical_steps) if historical_steps else []
        if not groups:
            groups = []
        # A conversation discovered after the first poll may contain several
        # post-baseline turns.  Report the already finished ones oldest first;
        # a turn that stopped on a tool call before a newer USER_INPUT is
        # deliberately dropped.
        for historical_group in groups[:-1]:
            started_at = _epoch(historical_group[0].get("created_at")) or 0.0
            if (_step_index(historical_group[0]) <= state.baseline_max_user_step
                    or started_at < state.baseline_cutoff):
                continue
            key = (state.meta.conversation_id, str(_step_index(historical_group[0])))
            if key in self._emitted:
                continue
            result = self._turn_result(state, historical_group, now=now, historical=True)
            if result is None or result["status"] == "aborted":
                continue
            if not self._excluded(state.meta.cwd):
                result = self._restore_result_content(state, result)
            key = (state.meta.conversation_id, result["turn_id"])
            if is_mailbox_idle(result["user_text"], result["final_message"]):
                self._remember(key)
                state.last_finished = (result["status"], result["error"])
                continue
            excluded = self._excluded(state.meta.cwd)
            self._queue(key, TurnEvent(
                agent=ANTIGRAVITY, session_id=state.meta.conversation_id,
                title=self._title(state.meta, historical_group), cwd=state.meta.cwd,
                status=result["status"], final_text=None if excluded else result["final_message"],
                error=result["error"], turn_id=result["turn_id"],
                started_at=result["started_at"], completed_at=result["completed_at"],
                labels=[tr('Content hidden')] if excluded else [], quota=None,
            ))
            state.last_finished = (result["status"], result["error"])
        current_groups = _turn_groups(state.steps)
        if not current_groups:
            state.pending_steps.clear()
            return
        current = current_groups[-1]
        started = _epoch(current[0].get("created_at")) or 0.0
        if _step_index(current[0]) <= state.baseline_max_user_step or started < state.baseline_cutoff:
            state.pending_steps.clear()
            return
        current_key = (state.meta.conversation_id, str(_step_index(current[0])))
        if current_key in self._emitted:
            state.pending_steps.clear()
            return
        mailbox_tick = is_mailbox_tick(_user_text(current[0]))
        # Progress is emitted only for the current running turn.  Text already
        # present in a baseline transcript is marked seen without being pushed.
        if (not mailbox_tick and getattr(self.cfg, "progress_enabled", True)
                and not self._excluded(state.meta.cwd)
                and state.meta.status in ("CASCADE_RUN_STATUS_RUNNING", "CASCADE_RUN_STATUS_BUSY",
                                           "CASCADE_RUN_STATUS_CANCELING", "")):
            new_texts: list[str] = []
            for step in current:
                index = _step_index(step)
                text = _content(step)
                if (step.get("type") == "PLANNER_RESPONSE" and text and _has_tools(step)
                        and index not in state.progress_seen):
                    state.progress_seen.add(index)
                    new_texts.append(text[:MAX_PROGRESS])
            if new_texts:
                state.last_progress = new_texts[-1]
                self._outbox.append(ProgressEvent(
                    agent=ANTIGRAVITY, session_id=state.meta.conversation_id,
                    title=self._title(state.meta, current), cwd=state.meta.cwd,
                    texts=new_texts, kind="commentary",
                    asks_user=any(progress.asks_user(text) for text in new_texts),
                ))
        elif mailbox_tick or getattr(self.cfg, "progress_enabled", True):
            for step in current:
                if step.get("type") == "PLANNER_RESPONSE" and _has_tools(step):
                    state.progress_seen.add(_step_index(step))
        result = self._turn_result(state, current, now=now)
        if result is None:
            state.pending_steps.clear()
            return
        if not self._excluded(state.meta.cwd):
            result = self._restore_result_content(state, result)
        key = (state.meta.conversation_id, result["turn_id"])
        if is_mailbox_idle(result["user_text"], result["final_message"]):
            self._remember(key)
            state.last_finished = (result["status"], result["error"])
            state.pending_steps.clear()
            return
        excluded = self._excluded(state.meta.cwd)
        labels = [tr('Content hidden')] if excluded else []
        self._queue(key, TurnEvent(
            agent=ANTIGRAVITY, session_id=state.meta.conversation_id,
            title=self._title(state.meta, current), cwd=state.meta.cwd,
            status=result["status"], final_text=None if excluded else result["final_message"],
            error=result["error"], turn_id=result["turn_id"],
            started_at=result["started_at"], completed_at=result["completed_at"],
            labels=labels, quota=None,
        ))
        state.last_finished = (result["status"], result["error"])
        state.pending_steps.clear()

    # --- public polling -------------------------------------------------------

    def poll(self) -> None:
        failure: Exception | None = None
        errors: list[str] = []
        with self._lock:
            now = self.clock()
            first_poll = self._baseline is None
            if self._baseline is None:
                self._baseline = now
            try:
                stores = self._stores()
                seen_ids: set[str] = set()
                for name, root in stores:
                    try:
                        rows, _ = self._query_index(name, root)
                    except Exception as exc:  # locked/corrupt/missing index: use transcript fallback
                        errors.append(f"{type(exc).__name__}: {exc}")
                        rows = self._fallback_index(name, root)
                        for item in rows.values():
                            item.index_available = False
                    self._store_rows[name] = rows
                    self._rows = {cid: meta for cid, meta in self._rows.items()
                                  if meta.store != name or cid in rows}
                    for cid, meta in rows.items():
                        self._rows[cid] = meta
                        if not self._active(meta, now):
                            continue
                        seen_ids.add(cid)
                        state = self._states.get(cid)
                        if state is None or state.meta.store != name:
                            state = _State(meta=meta, baseline_cutoff=self._baseline)
                            self._states[cid] = state
                            initial = first_poll and not state.initialized
                            parsed = self._read_incremental(state, now, initial=initial)
                            if initial:
                                self._merge_current(state, parsed)
                            else:
                                state.pending_steps = parsed
                                self._merge_current(state, parsed)
                            state.initialized = True
                            if initial:
                                state.baseline_max_user_step = max(
                                    (_step_index(step) for step in state.steps
                                     if step.get("source") == "USER_EXPLICIT" and step.get("type") == "USER_INPUT"),
                                    default=-1,
                                )
                                for step in state.steps:
                                    if step.get("type") == "PLANNER_RESPONSE" and _has_tools(step):
                                        state.progress_seen.add(_step_index(step))
                        else:
                            state.meta = meta
                            parsed = self._read_incremental(state, now)
                            state.pending_steps = []
                            self._merge_current(state, parsed)
                        state.previous_status = state.meta.status if state.previous_status == "" else state.previous_status
                        self._process_state(state, now)
                        state.previous_status = state.meta.status
                for cid in [cid for cid, state in self._states.items()
                            if cid not in seen_ids or not self._active(state.meta, now)]:
                    del self._states[cid]
                # Remove rows from stores that disappeared, but retain fresh rows
                # for view() until the next successful index read of that store.
                current_names = {name for name, _ in stores}
                self._rows = {cid: meta for cid, meta in self._rows.items() if meta.store in current_names}
            except Exception as exc:  # unexpected poll failure: emit what is ready, then re-raise
                failure = exc
                errors.append(f"{type(exc).__name__}: {exc}")
            self.last_error = "; ".join(errors) if errors else None
            ready, self._outbox = self._outbox, []
        for event in ready:
            try:
                self.emit(event)
            except Exception as exc:
                # Event delivery must not prevent the next source from being polled.
                log.error("Antigravity turn event not delivered: %s: %s", type(exc).__name__, exc)
        if failure is not None:
            raise failure

    # --- views and reading ----------------------------------------------------

    def _lookup(self, conversation_id: str) -> _Meta | None:
        wanted = str(conversation_id or "").lower()
        for cid, meta in self._rows.items():
            if cid.lower() == wanted and not meta.parent:
                return meta
        return None

    @staticmethod
    def _copy_state(state: _State | None, meta: _Meta) -> _State | None:
        if state is None:
            return None
        return replace(
            state,
            meta=meta,
            steps=list(state.steps),
            pending_steps=[],
            progress_seen=set(state.progress_seen),
        )

    def _view(self, meta: _Meta, state: _State | None = None) -> SessionView:
        if state is None:
            state = _State(meta=meta, baseline_cutoff=0.0)
            state.steps = self._read_tail(meta.transcript, 2)
        groups = _turn_groups(state.steps)
        current = groups[-1] if groups else []
        row_state = meta.status
        if row_state in ("CASCADE_RUN_STATUS_RUNNING", "CASCADE_RUN_STATUS_BUSY", "CASCADE_RUN_STATUS_CANCELING"):
            state_name = "running"
        elif row_state == "CASCADE_RUN_STATUS_IDLE":
            state_name = "idle"
        else:
            state_name = "unknown"
        detail = ""
        if state_name == "running" and not self._excluded(meta.cwd):
            latest = ""
            for step in reversed(current):
                if step.get("type") == "PLANNER_RESPONSE" and _content(step) and _has_tools(step):
                    latest = _content(step)
                    break
            detail = _one_line(latest or state.last_progress)
        if state.last_finished is None and groups:
            complete = self._turn_result(
                state, groups[-1], now=self.clock(), historical=state_name != "running",
            )
            if complete is not None and complete["status"] in ("failed", "aborted"):
                state.last_finished = (complete["status"], complete["error"])
        if state.last_finished is not None:
            status, error = state.last_finished
            if status == "failed":
                detail = tr('Previous turn failed: {v0}', v0=_one_line(error))
            elif status == "aborted":
                detail = tr('Previous turn was aborted')
        return SessionView(agent=ANTIGRAVITY, id=meta.conversation_id,
                           title=self._title(meta, current), cwd=meta.cwd, state=state_name,
                           last_activity=meta.last_modified, live=False, detail=detail, kind=meta.store)

    def sessions(self, limit: int) -> list[SessionView]:
        if limit <= 0:
            return []
        with self._lock:
            snapshots = []
            for state in self._states.values():
                meta = replace(state.meta)
                snapshots.append((meta, self._copy_state(state, meta)))
        views = [self._view(meta, state) for meta, state in snapshots]
        views.sort(key=lambda item: (item.state != "running", -item.last_activity))
        return views[:limit]

    def view(self, conversation_id: str) -> SessionView | None:
        with self._lock:
            meta = self._lookup(conversation_id)
            if meta is None:
                return None
            meta = replace(meta)
            state = self._copy_state(self._states.get(meta.conversation_id), meta)
        return self._view(meta, state)

    def _read_items(self, meta: _Meta, turns_limit: int = 20) -> list[dict[str, Any]]:
        return read_conversation(meta, turns_limit, excluded=self._excluded(meta.cwd), now=self.clock())

    def read_turns(self, conversation_id: str, k: int) -> list[dict[str, Any]]:
        count = max(1, min(int(k), 20))
        with self._lock:
            meta = self._lookup(conversation_id)
            if meta is None:
                return []
            meta = replace(meta)
        return self._read_items(meta, count)[-count:]

    def full_text(self, conversation_id: str) -> str | None:
        with self._lock:
            meta = self._lookup(conversation_id)
            if meta is None:
                return None
            meta = replace(meta)
            excluded = self._excluded(meta.cwd)
        if excluded:
            return None
        items = self._read_items(meta, 2)
        for item in reversed(items):
            if item["status"] != "running":
                return item["final_message"]
        return None

    def quota(self) -> None:
        return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"stores": [name for name, _ in self._stores()],
                    "conversations": len(self._states), "last_error": self.last_error}

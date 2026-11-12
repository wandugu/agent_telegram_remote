"""Read-only access to Codex Desktop conversations: state database and rollout JSONL."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from agent_telegram.i18n import tr
from agent_telegram.outputs import copy_file_no_clobber, file_record, is_within
from agent_telegram.settings import codex_home

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_STATE_DB_RE = re.compile(r"^state_(\d+)\.sqlite$")
_TEXT_LIMITS = {"title": 400, "first_user_message": 400}
_COLUMNS = ("id", "name", "title", "cwd", "rollout_path", "updated_at", "updated_at_ms", "archived", "source",
            "first_user_message", "is_pinned", "thread_source", "approval_mode")
# Persisted delivery manifests keep both marker families readable indefinitely.
MANIFEST_RE = re.compile(
    r"BEGIN_(?P<tag>AGENT_TG_LEGACY|AGENT_TG)_RESULT\s*(?P<body>.*?)\s*END_(?P=tag)_RESULT", re.DOTALL
)
# Prefix of the message codex_watch sends to continue a turn that failed with a network error.
AUTO_CONTINUE_MARKER = "【codex-watch 自动继续】"  # i18n: data
# Default message the local restart-and-continue tool sends after restarting Codex Desktop.
RESTART_CONTINUE_TEXT = "Codex 已重启，请继续你之前未完成的工作"  # i18n: data
ASYNC_INPUT_TOOL = "request_user_input_async"
QUESTION_REPLY_OPEN = "<send_user_message_question_reply>"
QUESTION_REPLY_CLOSE = "</send_user_message_question_reply>"
# Sources of `codex exec`, terminal CLI and `codex mcp-server` sessions; Codex Desktop records "vscode".
_HEADLESS_SOURCES = frozenset({"exec", "cli", "mcp"})
# read_thread reads at most this much of a rollout's tail; it runs inside the MCP server process.
READ_LIMIT_BYTES = 256 * 1024 * 1024
_INTERESTING_ARGS = ("action", "job_id", "output_dir", "file", "files", "attachments", "project", "source_root",
                     "path", "purpose", "workflow", "thread", "model", "model_version", "conversation_title",
                     "conversation_url", "resume_run")


class ThreadLookupError(LookupError):
    def __init__(self, message: str, candidates: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.candidates = candidates or []


def strip_verbatim(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _norm_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(strip_verbatim(value))).rstrip("\\/")


def _iso(epoch_seconds: float | None) -> str:
    if not epoch_seconds:
        return ""
    return datetime.fromtimestamp(epoch_seconds).astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class ThreadInfo:
    id: str
    name: str
    title: str
    cwd: str
    rollout_path: str
    updated_at: str
    archived: bool
    source: str
    first_user_message: str
    is_pinned: bool = False
    thread_source: str = ""  # user | automation | subagent | agent_created_thread | ... ("" when unknown)
    approval_mode: str = ""  # fallback approval policy; older databases do not have this column

    @property
    def is_subagent(self) -> bool:
        return self.source.startswith("{") and "subagent" in self.source

    @property
    def is_headless(self) -> bool:
        """Started by `codex exec`, the terminal CLI or an MCP client, not by Codex Desktop."""

        return self.source.strip().lower() in _HEADLESS_SOURCES

    @property
    def rollout(self) -> Path | None:
        return Path(strip_verbatim(self.rollout_path)) if self.rollout_path else None

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "title": (self.title.strip().splitlines() or [""])[0][:120],
            "cwd": strip_verbatim(self.cwd),
            "updated_at": self.updated_at,
            "archived": self.archived,
            "pinned": self.is_pinned,
        }


def state_db_path(home: Path | None = None) -> Path | None:
    root = home or codex_home()
    best: tuple[int, Path] | None = None
    for candidate in root.glob("state_*.sqlite"):
        match = _STATE_DB_RE.match(candidate.name)
        if match and (best is None or int(match.group(1)) > best[0]):
            best = (int(match.group(1)), candidate)
    return best[1] if best else None


def _connect(db: Path) -> sqlite3.Connection:
    uri = "file:///" + quote(db.resolve().as_posix(), safe="/:") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def _load_from_db(db: Path) -> list[ThreadInfo]:
    with closing(_connect(db)) as connection:
        available = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
        if "id" not in available:
            raise sqlite3.OperationalError("threads table is missing")
        expressions = []
        for column in _COLUMNS:
            if column not in available:
                expressions.append(f"NULL AS {column}")
            elif column in _TEXT_LIMITS:
                expressions.append(f"substr({column}, 1, {_TEXT_LIMITS[column]}) AS {column}")
            else:
                expressions.append(column)
        order = "updated_at_ms" if "updated_at_ms" in available else "COALESCE(updated_at, 0) * 1000"
        rows = connection.execute(
            f"SELECT {', '.join(expressions)} FROM threads ORDER BY COALESCE({order}, 0) DESC"
        ).fetchall()
    threads = []
    for row in rows:
        updated = (row["updated_at_ms"] or 0) / 1000 or row["updated_at"]
        threads.append(
            ThreadInfo(
                id=str(row["id"]),
                name=str(row["name"] or ""),
                title=str(row["title"] or ""),
                cwd=str(row["cwd"] or ""),
                rollout_path=str(row["rollout_path"] or ""),
                updated_at=_iso(updated),
                archived=bool(row["archived"]),
                source=str(row["source"] or ""),
                first_user_message=str(row["first_user_message"] or ""),
                is_pinned=bool(row["is_pinned"]),
                thread_source=str(row["thread_source"] or ""),
                approval_mode=str(row["approval_mode"] or ""),
            )
        )
    return threads


def _load_from_session_index(home: Path) -> list[ThreadInfo]:
    latest: dict[str, dict[str, Any]] = {}
    index = home / "session_index.jsonl"
    if index.is_file():
        for line in index.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("id"):
                latest[str(item["id"])] = item  # the newest line wins after a rename
    threads = []
    for thread_id, item in latest.items():
        rollouts = sorted((home / "sessions").rglob(f"rollout-*-{thread_id}*.jsonl"), key=lambda p: p.stat().st_mtime)
        threads.append(
            ThreadInfo(
                id=thread_id,
                name=str(item.get("thread_name") or ""),
                title="",
                cwd="",
                rollout_path=str(rollouts[-1]) if rollouts else "",
                updated_at=str(item.get("updated_at") or ""),
                archived=False,
                source="",
                first_user_message="",
            )
        )
    threads.sort(key=lambda info: info.updated_at, reverse=True)
    return threads


def load_threads(home: Path | None = None) -> list[ThreadInfo]:
    root = home or codex_home()
    db = state_db_path(root)
    if db is not None:
        try:
            return _load_from_db(db)
        except sqlite3.Error:
            pass
    return _load_from_session_index(root)


def search_threads(
    query: str | None = None,
    *,
    cwd: str | None = None,
    limit: int = 20,
    include_archived: bool = False,
    home: Path | None = None,
    visible: Callable[[ThreadInfo], bool] | None = None,
) -> list[ThreadInfo]:
    needle = (query or "").strip().casefold()
    cwd_key = _norm_path(cwd) if cwd else None
    found = []
    for info in load_threads(home):
        if visible is not None and not visible(info):
            continue
        if info.is_subagent or (info.archived and not include_archived):
            continue
        if cwd_key and _norm_path(info.cwd) != cwd_key:
            continue
        haystack = (info.id, info.name, info.title, info.first_user_message)
        if needle and not any(needle in value.casefold() for value in haystack):
            continue
        found.append(info)
        if len(found) >= limit:
            break
    return found


def resolve_thread(
    ref: str,
    *,
    allow_substring: bool = False,
    include_archived: bool = False,
    home: Path | None = None,
    visible: Callable[[ThreadInfo], bool] | None = None,
) -> ThreadInfo:
    """Resolve a UUID or an exact name/title; substrings only when explicitly allowed."""

    text = ref.strip()
    if not text:
        raise ThreadLookupError(tr('thread cannot be empty.'))
    threads = [info for info in load_threads(home) if not info.is_subagent and (visible is None or visible(info))]
    if UUID_RE.fullmatch(text):
        for info in threads:
            if info.id.lower() == text.lower():
                if info.archived and not include_archived:
                    raise ThreadLookupError(tr('Codex conversation {v0} is archived.', v0=text), [info.summary()])
                return info
        raise ThreadLookupError(tr('Cannot find Codex conversation {v0}.', v0=text))
    pool = [info for info in threads if include_archived or not info.archived]
    exact = [info for info in pool if info.name == text] or [info for info in pool if info.title.strip() == text]
    if len(exact) == 1:
        return exact[0]
    if exact:
        raise ThreadLookupError(tr('{v0} Codex conversations are named “{v1}”; use a UUID instead.', v0=len(exact), v1=text), [i.summary() for i in exact[:10]])
    needle = text.casefold()
    partial = [info for info in pool if needle in info.name.casefold() or needle in info.title.casefold()]
    if allow_substring and len(partial) == 1:
        return partial[0]
    hint = tr('Specify one (UUID or full name).') if partial else tr("Search first with codex_threads(action='list', query=...).")
    raise ThreadLookupError(tr('No Codex conversation is named exactly “{v0}”. {v1}', v0=text, v1=hint), [i.summary() for i in partial[:10]])


# --- rollout parsing -----------------------------------------------------------


_EVENT_TAG = b'"event_msg"'
_STAMP_RE = re.compile(rb'"timestamp"\s*:\s*"([^"]*)"')
# Response items decoded in full as well: request_user_input calls (the questions plan mode asks
# the user) and short function outputs (their answers). Both tags sit near the start of the line.
_USER_INPUT_TAG = b'"request_user_input"'
_OUTPUT_TAG = b'"function_call_output"'
_HEAD_BYTES = 2048
_SHORT_OUTPUT_BYTES = 64 * 1024


def _decode_line(data: bytes, start: int, end: int) -> bool:
    if data.find(_EVENT_TAG, start, end) >= 0:
        return True
    # These 5-10 KB records carry the current approval policy. Only inspect the
    # header so a tool output mentioning turn_context does not get decoded in full.
    if re.search(rb'"type"\s*:\s*"turn_context"', data[start:min(end, start + 256)]):
        return True
    head = min(end, start + _HEAD_BYTES)
    return data.find(_USER_INPUT_TAG, start, head) >= 0 or (
        end - start <= _SHORT_OUTPUT_BYTES and data.find(_OUTPUT_TAG, start, head) >= 0)


def _parse_lines(data: bytes, start: int = 0, size: int | None = None) -> list[dict[str, Any]]:
    """Parse the JSONL records in data[start:size]; event_msg lines are decoded in full.

    Tool outputs and other response items can be tens of MB per line and only their
    timestamp is used here, so they become {"timestamp": ...} stubs; request_user_input
    calls and short function outputs are the exception. Offsets instead of slices keep
    large tails from being copied.
    """

    records = []
    size = len(data) if size is None else size
    while start < size:
        end = data.find(b"\n", start)
        end = size if end < 0 else end
        if _decode_line(data, start, end):
            try:
                value = json.loads(data[start:end])
            except (json.JSONDecodeError, UnicodeDecodeError):
                value = None
            if isinstance(value, dict):
                records.append(value)
        else:
            stamp = _STAMP_RE.search(data, start, min(end, start + 256))
            if stamp:
                records.append({"timestamp": stamp.group(1).decode("utf-8", "replace")})
        start = end + 1
    return records


def read_records(path: Path, start: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Parse complete JSONL records from a byte offset; returns (records, next offset)."""

    with path.open("rb") as stream:
        stream.seek(start)
        data = stream.read()
    end = data.rfind(b"\n")
    if end < 0:
        return [], start
    return _parse_lines(data, 0, end + 1), start + end + 1


def read_tail_records(path: Path, max_bytes: int) -> tuple[list[dict[str, Any]], bool]:
    """Parse the complete records within the last max_bytes; the flag means the whole file."""

    size = path.stat().st_size
    start = max(0, size - max_bytes)
    with path.open("rb") as stream:
        stream.seek(start)
        data = stream.read()
    first = 0
    if start > 0:
        newline = data.find(b"\n")
        first = newline + 1 if newline >= 0 else len(data)  # skip the partial first line
    end = data.rfind(b"\n")
    return _parse_lines(data, first, end + 1), start == 0


@dataclass
class TurnSummary:
    turn_id: str
    status: str = "running"  # running | completed | failed | aborted | interrupted (DeliveryTracker only)
    started_at: str = ""
    completed_at: str = ""
    user_text: str = ""
    final_message: str | None = None
    error: str = ""
    error_info: str = ""  # codex_error_info kind, for example server_overloaded
    abort_reason: str = ""
    mcp_calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def web_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.mcp_calls if call.get("server") == "gpt_relay" or (
            call.get("server") == "agent_telegram" and str(call.get("tool") or "").startswith("web_")
        )]

    @property
    def relay_calls(self) -> list[dict[str, Any]]:
        """Legacy alias for evidence from both generations of web tools."""

        return self.web_calls

    def to_dict(self, *, max_chars: int = 8000, max_calls: int = 20) -> dict[str, Any]:
        final = self.final_message
        truncated = final is not None and len(final) > max_chars
        return {
            "turn_id": self.turn_id,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "user_text": self.user_text[:2000],
            "final_message": final[:max_chars] if final is not None else None,
            "final_message_truncated": truncated,
            "error": self.error,
            "error_info": self.error_info,
            "abort_reason": self.abort_reason,
            "mcp_call_count": len(self.mcp_calls),
            "web_call_count": len(self.web_calls),
            "web_calls": self.web_calls[-max_calls:],
            "gpt_relay_call_count": len(self.web_calls),  # legacy aliases
            "gpt_relay_calls": self.web_calls[-max_calls:],
        }


def _payload(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("payload")
    return value if isinstance(value, dict) else {}


def error_kind(value: Any) -> str:
    """codex_error_info as one word: a string stays, a one-key object gives its key."""

    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value:
        return str(next(iter(value)))
    return ""


def is_auto_continuation(text: str) -> bool:
    head = text.lstrip()
    return head.startswith(AUTO_CONTINUE_MARKER) or head.startswith(RESTART_CONTINUE_TEXT)


def carries_user_input(turn: TurnSummary) -> bool:
    """Input from a person or another task; codex_watch's and the restart tool's continuations do not count."""

    return bool(turn.user_text) and not is_auto_continuation(turn.user_text)


def question_reply_entries(text: str) -> list[tuple[str, int, str]]:
    """Parse a Desktop async reply into (call_id, index, answer) entries, or []."""

    text = text.strip()
    if not text.startswith(QUESTION_REPLY_OPEN) or not text.endswith(QUESTION_REPLY_CLOSE):
        return []
    try:
        payload = json.loads(text[len(QUESTION_REPLY_OPEN):-len(QUESTION_REPLY_CLOSE)])
        entries = [payload] if isinstance(payload, dict) else payload
        if not isinstance(entries, list) or not entries:
            return []
        replies = []
        for entry in entries:
            if not isinstance(entry, dict) or not all(
                isinstance(entry.get(key), str) for key in ("questionItemId", "question", "answer")
            ):
                return []
            item_id = json.loads(entry["questionItemId"])
            if (not isinstance(item_id, list) or len(item_id) != 3 or item_id[0] != ASYNC_INPUT_TOOL
                    or not isinstance(item_id[1], str) or type(item_id[2]) is not int):
                return []
            replies.append((item_id[1], item_id[2], entry["answer"]))
        return replies
    except (ValueError, RecursionError):
        return []


def build_question_reply(call_id: str, answers: dict[int, tuple[str, str]]) -> str:
    """Build Desktop's reply envelope from indices mapped to (title, answer)."""

    if not answers:
        raise ValueError("answers must not be empty")
    entries = []
    for index, (title, answer) in sorted(answers.items()):
        answer = answer.strip()
        if not answer:
            raise ValueError("answer must not be empty")
        entries.append({
            "questionItemId": json.dumps([ASYNC_INPUT_TOOL, call_id, index], separators=(",", ":")),
            "question": title,
            "answer": answer,
        })
    payload = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
    return f"{QUESTION_REPLY_OPEN}\n{payload}\n{QUESTION_REPLY_CLOSE}"


def _message_text(item: dict[str, Any]) -> str:
    parts = [
        part["text"]
        for part in item.get("content") or []
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    return "\n".join(parts)


_DELEGATED_INPUT_RE = re.compile(r"<input>\s*(.*?)\s*</input>", re.DOTALL)
_TG_MARKER_RE = re.compile(r"\[(tg:[A-Za-z0-9_-]{1,64})\]\s*")


def unescape_delegated(text: str) -> str:
    """Decode only the three XML entities Desktop unescapes, in its order."""

    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def delegated_reply(output: str) -> tuple[str, str] | None:
    """Extract decoded (text, marker); marker is 'tg:XXXX' or '', None means no input."""

    match = _DELEGATED_INPUT_RE.search(output)
    if match is None:
        return None
    text = unescape_delegated(match.group(1))
    marker = _TG_MARKER_RE.match(text)
    return (text[marker.end():], marker.group(1)) if marker else (text, "")


def _delegated_input(output: str) -> str:
    match = _DELEGATED_INPUT_RE.search(output)
    return unescape_delegated(match.group(1).strip()) if match else output


def _compact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in _INTERESTING_ARGS:
        if key in arguments:
            value = arguments[key]
            if isinstance(value, str):
                value = value[:300]
            elif isinstance(value, list):
                value = [str(item)[:300] for item in value[:10]]
            compact[key] = value
    for key in ("message", "prompt"):
        if isinstance(arguments.get(key), str):
            compact[f"{key}_excerpt"] = arguments[key][:200]
    return compact


def _mcp_call(item: dict[str, Any]) -> dict[str, Any]:
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    texts = [
        str(part.get("text") or "")
        for part in result.get("content") or []
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    text = texts[0] if texts else ""
    arguments = item.get("arguments") if isinstance(item.get("arguments"), dict) else {}
    error = item.get("error")
    return {
        "server": item.get("server"),
        "tool": item.get("tool"),
        "status": item.get("status"),
        "is_error": bool(result.get("isError")) or item.get("status") == "failed",
        "arguments": _compact_arguments(arguments),
        "result_excerpt": (text or (json.dumps(error, ensure_ascii=False) if error else ""))[:600],
    }


class TurnCollector:
    """Fold rollout event records into per-turn summaries (order of first appearance)."""

    def __init__(self) -> None:
        self.turns: dict[str, TurnSummary] = {}
        self.order: list[str] = []

    def _turn(self, turn_id: str) -> TurnSummary:
        if turn_id not in self.turns:
            self.turns[turn_id] = TurnSummary(turn_id)
            self.order.append(turn_id)
        return self.turns[turn_id]

    def feed(self, records: list[dict[str, Any]]) -> None:
        for record in records:
            if record.get("type") != "event_msg":
                continue
            payload = _payload(record)
            kind = payload.get("type")
            turn_id = str(payload.get("turn_id") or "")
            if not turn_id:
                continue
            if kind == "task_started":
                self._turn(turn_id).started_at = _iso(payload.get("started_at")) or str(record.get("timestamp") or "")
            elif kind == "item_completed":
                item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
                turn = self._turn(turn_id)
                text = None
                if item.get("type") == "UserMessage":
                    text = _message_text(item)
                elif item.get("type") == "FunctionCallOutput" and item.get("name") == "send_message_to_thread":
                    # Messages delivered through the app-tools pipe arrive as a delegation output.
                    text = _delegated_input(str(item.get("output") or ""))
                if text is not None:
                    turn.user_text = f"{turn.user_text}\n{text}".strip() if turn.user_text else text
                elif item.get("type") == "McpToolCall":
                    turn.mcp_calls.append(_mcp_call(item))
            elif kind == "task_complete":
                turn = self._turn(turn_id)
                turn.completed_at = _iso(payload.get("completed_at")) or str(record.get("timestamp") or "")
                message = payload.get("last_agent_message")
                turn.final_message = message if isinstance(message, str) else None
                error = payload.get("error")
                if isinstance(error, dict) and error.get("message"):
                    turn.error = str(error["message"])
                    turn.error_info = error_kind(error.get("codex_error_info"))
                turn.status = "failed" if turn.final_message is None and turn.error else "completed"
            elif kind == "turn_aborted":
                turn = self._turn(turn_id)
                turn.status = "aborted"
                turn.abort_reason = str(payload.get("reason") or "")
                turn.completed_at = _iso(payload.get("completed_at")) or str(record.get("timestamp") or "")

    def ordered(self) -> list[TurnSummary]:
        return [self.turns[turn_id] for turn_id in self.order]

    def prune(self, keep: int) -> None:
        """Forget all but the newest ``keep`` turns (long-running readers stay bounded)."""

        for turn_id in self.order[:-keep] if keep > 0 else self.order:
            del self.turns[turn_id]
        self.order = self.order[-keep:] if keep > 0 else []


def _last_activity(path: Path, records: list[dict[str, Any]]) -> str:
    # Windows does not refresh mtime while Codex keeps the rollout open for appending.
    for record in reversed(records):
        stamp = record.get("timestamp")
        if isinstance(stamp, str):
            try:
                return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().isoformat(timespec="seconds")
            except ValueError:
                continue
    return _iso(path.stat().st_mtime)


def thread_activity(
    info: ThreadInfo, *, tail_bytes: int = 2 * 1024 * 1024, max_bytes: int = 64 * 1024 * 1024
) -> dict[str, Any]:
    """Busy means the newest turn in the rollout has not completed or aborted.

    A single tool output can be larger than the tail, so the window widens until it
    holds a turn event; busy is None (unknown) when none shows up within max_bytes.
    """

    path = info.rollout
    if path is None or not path.is_file():
        return {"busy": False, "rollout_found": False}
    budget = tail_bytes
    while True:
        records, complete = read_tail_records(path, budget)
        collector = TurnCollector()
        collector.feed(records)
        turns = collector.ordered()
        if turns or complete or budget >= max_bytes:
            break
        budget = min(budget * 4, max_bytes)
    last = turns[-1] if turns else None
    running = bool(last and last.status == "running")
    return {
        "busy": running if turns or complete else None,
        "busy_turn_id": last.turn_id if running else None,
        "busy_since": last.started_at if running else None,
        "last_activity": _last_activity(path, records),
        "rollout_found": True,
    }


def read_thread(
    info: ThreadInfo, *, turns: int = 1, max_chars: int = 8000, max_bytes: int = READ_LIMIT_BYTES
) -> dict[str, Any]:
    path = info.rollout
    if path is None or not path.is_file():
        raise FileNotFoundError(tr('Cannot find the rollout file for conversation {v0}: {v1}', v0=info.id, v1=info.rollout_path))
    budget = min(4 * 1024 * 1024, max_bytes)
    while True:
        records, complete = read_tail_records(path, budget)
        collector = TurnCollector()
        collector.feed(records)
        ordered = collector.ordered()
        # One extra turn guarantees the oldest reported turn was read from its start.
        if complete or len(ordered) > turns or budget >= max_bytes:
            break
        budget = min(budget * 4, max_bytes)
    selected = ordered[-turns:]
    last = ordered[-1] if ordered else None
    report = {
        "thread": info.summary(),
        "busy": bool(last and last.status == "running"),
        "last_activity": _last_activity(path, records),
        "turns": [turn.to_dict(max_chars=max_chars) for turn in selected],
    }
    if not complete and len(ordered) <= turns:
        # Stopped at max_bytes: older turns are missing and the oldest one may lack its start.
        report["history_truncated"] = True
    return report


FOLLOWED_STATUSES = frozenset({"failed", "interrupted"})


class DeliveryTracker:
    """Find the turn that carries a nonce and follow it to its final answer.

    Codex Desktop retries a turn that failed with a transient error (for example
    ``server_overloaded``) by starting a new turn without new user input, and
    codex_watch continues such turns with an AUTO_CONTINUE_MARKER message. A turn
    that an app restart cut short never completes in the rollout; it counts as
    interrupted once a newer turn starts (turns of one thread never overlap) or the
    app reports the thread idle (mark_interrupted), and Resume or the restart tool's
    continuation may follow it. These continuation turns are followed as part of the
    same task. A later turn that carries its own user input belongs to someone else
    and ends the chain for good (superseded).
    """

    def __init__(self, nonce: str) -> None:
        self.nonce = nonce
        # Old requests may be followed by the new worker (or after a rollback).
        # Keep raw caller-owned nonces such as [tg:...] completely literal.
        match = re.fullmatch(r"\[(?:agent_telegram|agent-telegram):([^\]]+)\]", nonce)
        self._nonces = (f"[agent_telegram:{match[1]}]", f"[agent-telegram:{match[1]}]") if match else (nonce,)
        self.collector = TurnCollector()
        self.chain: list[str] = []

    @property
    def turn_id(self) -> str | None:
        return self.chain[-1] if self.chain else None

    def feed(self, records: list[dict[str, Any]]) -> None:
        self.collector.feed(records)
        tail = self.turn
        if tail is not None and tail.status == "interrupted":
            touched = {str(_payload(record).get("turn_id") or "") for record in records
                       if record.get("type") == "event_msg"}
            if tail.turn_id in touched:
                tail.status = "running"  # it was still alive after all
        order = self.collector.order
        if not self.chain:
            for turn_id in order:
                if any(nonce in self.collector.turns[turn_id].user_text for nonce in self._nonces):
                    self.chain.append(turn_id)
                    break
        for position in range(1, len(self.chain)):
            # task_started precedes the user input of a new turn; drop it once input shows up.
            if carries_user_input(self.collector.turns[self.chain[position]]):
                del self.chain[position:]
                break
        self._extend()

    def mark_interrupted(self) -> None:
        """Codex Desktop says the thread is idle although the tracked turn never ended."""

        turn = self.turn
        if turn is not None and turn.status == "running":
            turn.status = "interrupted"
            self._extend()

    def _extend(self) -> None:
        order = self.collector.order
        while self.chain:
            tail = self.collector.turns[self.chain[-1]]
            index = order.index(tail.turn_id)
            if index + 1 >= len(order):
                break
            if tail.status == "running":
                tail.status = "interrupted"  # a newer turn started, so this one was cut short
            if tail.status not in FOLLOWED_STATUSES:
                break
            following = self.collector.turns[order[index + 1]]
            if carries_user_input(following):
                break
            self.chain.append(following.turn_id)

    @property
    def turn(self) -> TurnSummary | None:
        return self.collector.turns.get(self.turn_id) if self.turn_id else None

    @property
    def turns(self) -> list[TurnSummary]:
        return [self.collector.turns[turn_id] for turn_id in self.chain]

    @property
    def superseded(self) -> bool:
        """A turn with its own user input follows the chain's last turn, so nothing can continue the chain."""

        tail = self.turn
        if tail is None:
            return False
        order = self.collector.order
        index = order.index(tail.turn_id)
        return index + 1 < len(order) and carries_user_input(self.collector.turns[order[index + 1]])

    @property
    def web_calls(self) -> list[dict[str, Any]]:
        return [call for turn in self.turns for call in turn.web_calls]

    @property
    def relay_calls(self) -> list[dict[str, Any]]:
        """Legacy alias for evidence from both generations of web tools."""

        return self.web_calls

    @property
    def done(self) -> bool:
        """Completed or aborted; a failed or interrupted turn may still be continued (see run_codex_send)."""

        turn = self.turn
        return turn is not None and turn.status in {"completed", "aborted"}


# --- result manifest ---------------------------------------------------------------


def parse_manifest(text: str | None) -> tuple[dict[str, Any] | None, str]:
    if not text:
        return None, tr("Codex's final reply is empty.")
    matches = [match.group("body") for match in MANIFEST_RE.finditer(text)]
    if not matches:
        return None, tr('The final reply has no {marker} delivery manifest (legacy {legacy_marker} is also accepted).',
                        marker='BEGIN_AGENT_TG_RESULT', legacy_marker='BEGIN_AGENT_TG_LEGACY_RESULT')  # i18n: data
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", matches[-1].strip())
    try:
        value = json.loads(body)
    except json.JSONDecodeError as exc:
        return None, tr('The delivery manifest is not valid JSON: {v0}', v0=exc)
    if not isinstance(value, dict):
        return None, tr('The delivery manifest must be a JSON object.')
    files = value.get("files") if isinstance(value.get("files"), list) else []
    value["files"] = [item if isinstance(item, dict) else {"path": str(item)} for item in files]
    return value, ""


def collect_manifest_files(
    manifest: dict[str, Any], output_dir: Path | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Verify listed files; copy the ones outside output_dir into it (never overwriting)."""

    verified: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for entry in manifest.get("files") or []:
        reported = str(entry.get("path") or "").strip()
        info = {"kind": entry.get("kind"), "description": entry.get("description"), "reported_path": reported}
        path = Path(strip_verbatim(reported)) if reported else None
        if path is None or not path.is_absolute() or not path.is_file():
            missing.append({**info, "reason": tr('File does not exist or is not an absolute path')})
            continue
        if output_dir is not None and not is_within(path, output_dir):
            copied = copy_file_no_clobber(path, output_dir)
            info["copied_from"] = str(path)
            path = copied
        verified.append({**info, **file_record(path)})
    return verified, missing

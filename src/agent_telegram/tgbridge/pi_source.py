"""Read-only Pi conversations for shared context and the Telegram bridge.

This module reads only ``~/.pi/agent/sessions/*/*.jsonl`` (or ``<pi_home>/sessions/*/*.jsonl``).
It never writes below ``~/.pi`` and never starts or contacts a Pi process.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_telegram.i18n import tr
from agent_telegram.tgbridge.model import PI, Event, SessionView, TurnEvent

log = logging.getLogger(__name__)
REMEMBERED = 2000


def default_home(cfg: Any = None) -> Path:
    """Return the configured Pi root (``~/.pi/agent`` or ``~/.pi``)."""

    value = str(getattr(cfg, "pi_home", "") or "").strip() if cfg is not None else ""
    if not value:
        for env_name in ("AGENT_CONTEXT_PI_HOME", "PI_CODING_AGENT_DIR", "PI_CONFIG_DIR"):
            value = os.environ.get(env_name, "").strip()
            if value:
                break
    return Path(value).expanduser() if value else Path.home() / ".pi"


def _sessions_dirs(home: Path) -> list[Path]:
    home = Path(home)
    candidates = [home / "agent" / "sessions", home / "sessions"]
    seen: list[Path] = []
    for candidate in candidates:
        if candidate.is_dir() and candidate not in seen:
            seen.append(candidate)
    return seen


def _one_line(text: str, limit: int = 80) -> str:
    return " ".join(str(text or "").split())[:limit]


def _epoch(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        val = float(value)
        return val / 1000.0 if val > 10_000_000_000 else val
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _blocks_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text")
            for block in content
            if isinstance(block, dict) and block.get("type") in {"text", "input_text", "output_text"} and isinstance(block.get("text"), str)
        ]
        return "\n".join(part for part in parts if part)
    return ""


def user_text_from_record(record: dict[str, Any]) -> str | None:
    """Return user message text if ``record`` is a Pi user message entry, else ``None``."""

    if record.get("type") != "message":
        return None
    message = record.get("message")
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    return _blocks_text(message.get("content"))


@dataclass
class PiSessionMeta:
    session_id: str
    title: str = ""
    cwd: str = ""
    last_modified: float = 0.0
    status: str = "idle"
    transcript: Path | None = None


def _inspect_header_and_title(path: Path, mtime: float) -> PiSessionMeta | None:
    session_id = ""
    cwd = ""
    title = ""
    try:
        with path.open("rb") as stream:
            for _ in range(64):
                raw = stream.readline(1024 * 1024)
                if not raw:
                    break
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                rtype = record.get("type")
                if rtype == "session":
                    session_id = str(record.get("id") or session_id)
                    cwd = str(record.get("cwd") or cwd)
                elif rtype in {"session_info", "custom-title"}:
                    custom = str(record.get("name") or record.get("title") or record.get("customTitle") or "").strip()
                    if custom:
                        title = _one_line(custom, 200)
                elif rtype == "message" and not title:
                    text = user_text_from_record(record)
                    if text:
                        first_line = text.strip().splitlines()[0] if text.strip() else ""
                        title = _one_line(first_line, 200)
    except OSError:
        return None
    if not session_id:
        stem = path.stem
        session_id = stem.split("_", 1)[1] if "_" in stem else stem
    if not session_id:
        return None
    return PiSessionMeta(
        session_id=session_id,
        title=title,
        cwd=cwd,
        last_modified=mtime,
        status="idle",
        transcript=path,
    )


def list_sessions(home: Path, since: float = float("-inf")) -> list[PiSessionMeta]:
    """Return Pi sessions modified at or after ``since``, newest first."""

    by_id: dict[str, PiSessionMeta] = {}
    for sessions_dir in _sessions_dirs(home):
        try:
            subdirs = [entry for entry in os.scandir(sessions_dir) if entry.is_dir()]
        except OSError:
            continue
        for subdir in subdirs:
            try:
                files = list(os.scandir(subdir.path))
            except OSError:
                continue
            for entry in files:
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    mtime = entry.stat().st_mtime
                except OSError:
                    continue
                if mtime < since:
                    continue
                meta = _inspect_header_and_title(Path(entry.path), mtime)
                if meta is None:
                    continue
                prev = by_id.get(meta.session_id)
                if prev is None or meta.last_modified > prev.last_modified:
                    by_id[meta.session_id] = meta
    return sorted(by_id.values(), key=lambda item: item.last_modified, reverse=True)


def find_session(home: Path, session_id: str) -> PiSessionMeta | None:
    wanted = str(session_id or "").strip()
    if not wanted:
        return None
    for meta in list_sessions(home, float("-inf")):
        if meta.session_id == wanted:
            return meta
    return None


def read_turns(
    meta_or_path: PiSessionMeta | Path,
    turns: int = 3,
    *,
    excluded: bool = False,
) -> list[dict[str, Any]]:
    """Parse user/assistant turns from a Pi session JSONL file."""

    path = meta_or_path.transcript if isinstance(meta_or_path, PiSessionMeta) else Path(meta_or_path)
    if path is None or not path.is_file():
        return []
    parsed_turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    try:
        with path.open("rb") as stream:
            for raw in stream:
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict) or record.get("type") != "message":
                    continue
                msg = record.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                stamp = _epoch(record.get("timestamp")) or _epoch(msg.get("timestamp"))
                if role == "user":
                    user_text = _blocks_text(msg.get("content"))
                    current = {
                        "turn_id": str(record.get("id") or len(parsed_turns) + 1),
                        "user_text": "" if excluded else user_text,
                        "final_message": "",
                        "error": "",
                        "status": "running",
                        "started_at": stamp,
                        "completed_at": None,
                    }
                    parsed_turns.append(current)
                elif role == "assistant" and current is not None:
                    text = _blocks_text(msg.get("content"))
                    if text and not excluded:
                        current["final_message"] = text
                    stop_reason = str(msg.get("stopReason") or "").lower()
                    error_msg = str(msg.get("errorMessage") or msg.get("error") or "")
                    if stop_reason in {"error", "aborted"} or error_msg:
                        current["status"] = "aborted" if stop_reason == "aborted" else "failed"
                        current["error"] = "" if excluded else (error_msg or f"stopReason={stop_reason}")
                        current["completed_at"] = stamp
                    elif stop_reason not in {"tooluse", "tool_use", ""}:
                        current["status"] = "completed"
                        current["completed_at"] = stamp
    except OSError:
        return []
    count = max(1, int(turns))
    return parsed_turns[-count:]


@dataclass
class _Tracked:
    meta: PiSessionMeta
    mtime: float = 0.0
    size: int = 0


class PiSource:
    """Read-only Telegram bridge source for Pi sessions."""

    def __init__(
        self,
        cfg: Any,
        emit: Callable[[Event], None],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.emit = emit
        self.clock = clock
        self._lock = threading.Lock()
        self._baseline: float | None = None
        self._tracked: dict[str, _Tracked] = {}
        self._emitted: dict[tuple[str, str], None] = {}
        self._outbox: list[Event] = []
        self.last_error: str | None = None

    def _home(self) -> Path:
        return default_home(self.cfg)

    def _excluded(self, cwd: str) -> bool:
        try:
            return bool(self.cfg.excluded(cwd))
        except (AttributeError, TypeError):
            return False

    def _remember(self, key: tuple[str, str]) -> None:
        self._emitted[key] = None
        if len(self._emitted) > REMEMBERED:
            del self._emitted[next(iter(self._emitted))]

    def poll(self) -> None:
        with self._lock:
            now = self.clock()
            first = self._baseline is None
            if self._baseline is None:
                self._baseline = now
            try:
                sessions = list_sessions(self._home(), now - 7 * 86400)
                seen: set[str] = set()
                for meta in sessions:
                    seen.add(meta.session_id)
                    size = meta.transcript.stat().st_size if meta.transcript and meta.transcript.is_file() else 0
                    prev = self._tracked.get(meta.session_id)
                    self._tracked[meta.session_id] = _Tracked(meta=meta, mtime=meta.last_modified, size=size)
                    if prev is not None and prev.mtime == meta.last_modified and prev.size == size:
                        continue
                    turns = read_turns(meta, 5, excluded=False)
                    for turn in turns:
                        key = (meta.session_id, str(turn.get("turn_id") or ""))
                        if first or key in self._emitted:
                            self._remember(key)
                            continue
                        status = str(turn.get("status") or "")
                        if status not in {"completed", "failed", "aborted"}:
                            continue
                        completed_at = turn.get("completed_at") or meta.last_modified
                        if completed_at and completed_at < self._baseline:
                            self._remember(key)
                            continue
                        self._remember(key)
                        excluded = self._excluded(meta.cwd)
                        self._outbox.append(
                            TurnEvent(
                                agent=PI,
                                session_id=meta.session_id,
                                title=meta.title or meta.session_id[:8],
                                cwd=meta.cwd,
                                status=status,
                                final_text=None if excluded else str(turn.get("final_message") or ""),
                                error="" if excluded else str(turn.get("error") or ""),
                                turn_id=str(turn.get("turn_id") or ""),
                                started_at=turn.get("started_at"),
                                completed_at=completed_at,
                                labels=[tr('Content hidden')] if excluded else [],
                            )
                        )
                for sid in list(self._tracked):
                    if sid not in seen:
                        del self._tracked[sid]
                self.last_error = None
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            ready, self._outbox = self._outbox, []
        for event in ready:
            try:
                self.emit(event)
            except Exception as exc:
                log.error("Pi turn event not delivered: %s: %s", type(exc).__name__, exc)

    def _to_view(self, meta: PiSessionMeta) -> SessionView:
        turns = read_turns(meta, 1, excluded=self._excluded(meta.cwd))
        state = str(turns[-1].get("status") or "idle") if turns else "idle"
        if state == "completed":
            state = "idle"
        return SessionView(
            agent=PI,
            id=meta.session_id,
            title=meta.title or meta.session_id[:8],
            cwd=meta.cwd,
            state=state if state in {"running", "waiting", "idle"} else "idle",
            last_activity=meta.last_modified,
            live=False,
            kind="cli",
        )

    def sessions(self, limit: int) -> list[SessionView]:
        if limit <= 0:
            return []
        metas = list_sessions(self._home(), self.clock() - 7 * 86400)[:limit]
        return [self._to_view(meta) for meta in metas]

    def view(self, session_id: str) -> SessionView | None:
        meta = find_session(self._home(), session_id)
        return self._to_view(meta) if meta is not None else None

    def read_turns(self, session_id: str, k: int) -> list[dict[str, Any]]:
        meta = find_session(self._home(), session_id)
        if meta is None:
            return []
        return read_turns(meta, max(1, min(int(k), 20)), excluded=self._excluded(meta.cwd))

    def full_text(self, session_id: str) -> str | None:
        meta = find_session(self._home(), session_id)
        if meta is None or self._excluded(meta.cwd):
            return None
        for item in reversed(read_turns(meta, 2, excluded=False)):
            if item.get("status") != "running":
                return str(item.get("final_message") or "")
        return None

    def quota(self) -> None:
        return None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"sessions": len(self._tracked), "last_error": self.last_error}

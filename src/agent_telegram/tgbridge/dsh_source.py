"""Read-only DSH (DeepSeek Harness) conversations for shared context and the Telegram bridge.

This module reads only ``~/.dsh/sessions/*/*/session.v4.jsonl.zstd`` (or plain ``.jsonl`` in tests)
and optional ``~/.dsh/storages/session_projcache/sessions/*.json`` / ``~/.dsh/storages/workspace.json``.
It never writes below ``~/.dsh`` and never starts or contacts a DSH process.
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import zstandard

from agent_telegram.i18n import tr
from agent_telegram.tgbridge.model import DSH, Event, SessionView, TurnEvent

log = logging.getLogger(__name__)
REMEMBERED = 2000
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_DECOMPRESS_CACHE: dict[str, tuple[tuple[int, int], bytes]] = {}
_CACHE_LOCK = threading.Lock()


def default_home(cfg: Any = None) -> Path:
    """Return the configured DSH root (``~/.dsh`` by default)."""

    value = str(getattr(cfg, "dsh_home", "") or "").strip() if cfg is not None else ""
    if not value:
        for env_name in ("AGENT_CONTEXT_DSH_HOME", "DSH_HOME", "DSH_CONFIG_DIR"):
            value = os.environ.get(env_name, "").strip()
            if value:
                break
    return Path(value).expanduser() if value else Path.home() / ".dsh"


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


def _blocks_text(content: Any, *, include_reasoning: bool = False) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        allowed = {"text", "input_text", "output_text"}
        if include_reasoning:
            allowed.add("reasoning")
        parts = [
            block.get("text")
            for block in content
            if isinstance(block, dict) and block.get("type") in allowed and isinstance(block.get("text"), str)
        ]
        return "\n".join(part for part in parts if part)
    return ""


def decompress_zstd_bytes(raw: bytes) -> bytes:
    """Decompress multi-frame Zstandard bytes, or return plain UTF-8 bytes as-is."""

    if not raw.startswith(ZSTD_MAGIC):
        return raw
    dctx = zstandard.ZstdDecompressor()
    with dctx.stream_reader(io.BytesIO(raw)) as reader:
        return reader.read()


def read_transcript_bytes(path: Path) -> bytes:
    """Read and decompress a DSH transcript file, caching by ``(mtime_ns, size)``."""

    stat = path.stat()
    stamp = (stat.st_mtime_ns, stat.st_size)
    key = str(path.resolve())
    with _CACHE_LOCK:
        cached = _DECOMPRESS_CACHE.get(key)
        if cached is not None and cached[0] == stamp:
            return cached[1]
    raw = path.read_bytes()
    decoded = decompress_zstd_bytes(raw)
    with _CACHE_LOCK:
        if len(_DECOMPRESS_CACHE) > 256:
            _DECOMPRESS_CACHE.clear()
        _DECOMPRESS_CACHE[key] = (stamp, decoded)
    return decoded


def user_text_from_record(record: dict[str, Any]) -> str | None:
    """Return direct user message text if ``record`` is a DSH user prompt, else ``None``.

    DSH emits ``user/message`` records for both genuine user input (``source.kind == "user"``)
    and injected context (``agent-instructions``, ``runtime-context``, ``skill-catalog``,
    ``time-context``, ``ptc-mode``). Only ``source.kind == "user"`` (or a synthetic test
    fixture with ``source is None``) counts as user input.
    """

    if record.get("type") != "user/message":
        return None
    data = record.get("data")
    if not isinstance(data, dict):
        return None
    source = data.get("source")
    if isinstance(source, dict):
        if source.get("kind") != "user":
            return None
    elif source is not None and str(source) != "user":
        return None
    content = data.get("content")
    if content is None and isinstance(data.get("message"), dict):
        content = data["message"].get("content")
    return _blocks_text(content)


@dataclass
class DshSessionMeta:
    session_id: str
    title: str = ""
    cwd: str = ""
    last_modified: float = 0.0
    status: str = "idle"
    transcript: Path | None = None


def _workspace_map(home: Path) -> dict[str, str]:
    ws_path = home / "storages" / "workspace.json"
    if not ws_path.is_file():
        return {}
    try:
        payload = json.loads(ws_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    result: dict[str, str] = {}
    if isinstance(payload, dict):
        for key, val in payload.items():
            if isinstance(val, str):
                result[str(key)] = val
            elif isinstance(val, dict) and isinstance(val.get("path") or val.get("cwd"), str):
                result[str(key)] = str(val.get("path") or val.get("cwd"))
    return result


def _projcache_map(home: Path) -> dict[str, dict[str, Any]]:
    cache_dir = home / "storages" / "session_projcache" / "sessions"
    if not cache_dir.is_dir():
        return {}
    result: dict[str, dict[str, Any]] = {}
    try:
        entries = list(os.scandir(cache_dir))
    except OSError:
        return {}
    for entry in entries:
        if not entry.name.endswith(".json"):
            continue
        try:
            data = json.loads(Path(entry.path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            sid = str(data.get("sessionId") or data.get("id") or Path(entry.name).stem)
            if sid:
                result[sid] = data
    return result


def _inspect_transcript(path: Path, mtime: float, projcache: dict[str, dict[str, Any]], ws_map: dict[str, str]) -> DshSessionMeta | None:
    session_id = path.parent.name
    cached = projcache.get(session_id) or projcache.get(session_id.removeprefix("session-")) or {}
    cwd = str(cached.get("workspacePath") or cached.get("cwd") or ws_map.get(path.parent.parent.name) or "")
    title = str(cached.get("title") or "").strip()
    user_title = ""
    first_user = ""
    try:
        decoded = read_transcript_bytes(path)
    except Exception:
        return None
    for raw in decoded.splitlines():
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
        elif rtype == "working-directory/change":
            data = record.get("data")
            if isinstance(data, dict) and isinstance(data.get("cwd"), str) and data["cwd"].strip():
                cwd = data["cwd"].strip()
        elif rtype == "session/title":
            data = record.get("data")
            if isinstance(data, dict) and isinstance(data.get("title"), str) and data["title"].strip():
                candidate = _one_line(data["title"].strip(), 200)
                src = data.get("source")
                if isinstance(src, dict) and src.get("kind") == "user":
                    user_title = candidate
                else:
                    title = candidate
        elif rtype == "user/message" and not first_user:
            text = user_text_from_record(record)
            if text and text.strip():
                first_user = _one_line(text.strip().splitlines()[0], 200)
    final_title = user_title or title or first_user
    return DshSessionMeta(
        session_id=session_id,
        title=final_title,
        cwd=cwd,
        last_modified=mtime,
        status="idle",
        transcript=path,
    )


def list_sessions(home: Path, since: float = float("-inf")) -> list[DshSessionMeta]:
    """Return DSH sessions modified at or after ``since``, newest first."""

    home = Path(home)
    sessions_root = home / "sessions"
    if not sessions_root.is_dir():
        return []
    projcache = _projcache_map(home)
    ws_map = _workspace_map(home)
    by_id: dict[str, DshSessionMeta] = {}
    try:
        slug_dirs = [entry for entry in os.scandir(sessions_root) if entry.is_dir()]
    except OSError:
        return []
    for slug_dir in slug_dirs:
        try:
            session_dirs = [entry for entry in os.scandir(slug_dir.path) if entry.is_dir()]
        except OSError:
            continue
        for sdir in session_dirs:
            sdir_path = Path(sdir.path)
            transcript = sdir_path / "session.v4.jsonl.zstd"
            if not transcript.is_file():
                alt = sdir_path / "session.v4.jsonl"
                if alt.is_file():
                    transcript = alt
                else:
                    continue
            try:
                mtime = transcript.stat().st_mtime
            except OSError:
                continue
            if mtime < since:
                continue
            meta = _inspect_transcript(transcript, mtime, projcache, ws_map)
            if meta is None:
                continue
            prev = by_id.get(meta.session_id)
            if prev is None or meta.last_modified > prev.last_modified:
                by_id[meta.session_id] = meta
    return sorted(by_id.values(), key=lambda item: item.last_modified, reverse=True)


def _match_session_id(candidate: str, wanted: str) -> bool:
    if candidate == wanted:
        return True
    return candidate.removeprefix("session-") == wanted.removeprefix("session-")


def find_session(home: Path, session_id: str) -> DshSessionMeta | None:
    wanted = str(session_id or "").strip()
    if not wanted:
        return None
    for meta in list_sessions(home, float("-inf")):
        if _match_session_id(meta.session_id, wanted):
            return meta
    return None


def read_turns(
    meta_or_path: DshSessionMeta | Path,
    turns: int = 3,
    *,
    excluded: bool = False,
) -> list[dict[str, Any]]:
    """Parse user/assistant turns from a DSH session transcript."""

    path = meta_or_path.transcript if isinstance(meta_or_path, DshSessionMeta) else Path(meta_or_path)
    if path is None or not path.is_file():
        return []
    try:
        decoded = read_transcript_bytes(path)
    except Exception:
        return []
    parsed_turns: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in decoded.splitlines():
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        rtype = record.get("type")
        stamp = _epoch(record.get("time")) or _epoch(record.get("timestamp"))
        data = record.get("data") if isinstance(record.get("data"), dict) else {}
        if rtype == "user/message":
            user_text = user_text_from_record(record)
            if user_text is None:
                continue
            current = {
                "turn_id": str(record.get("seq") or len(parsed_turns) + 1),
                "user_text": "" if excluded else user_text,
                "final_message": "",
                "error": "",
                "status": "running",
                "started_at": stamp,
                "completed_at": None,
            }
            parsed_turns.append(current)
        elif rtype == "assistant/message" and current is not None:
            msg = data.get("message") if isinstance(data.get("message"), dict) else data
            text = _blocks_text(msg.get("content"), include_reasoning=False)
            if text and not excluded:
                current["final_message"] = text
        elif rtype == "turn/end" and current is not None:
            reason = data.get("reason")
            kind = str(reason.get("kind") if isinstance(reason, dict) else reason or "completed").lower()
            if kind in {"completed", "stop", "end_turn"}:
                current["status"] = "completed"
            elif kind in {"aborted", "cancelled", "canceled", "interrupted"}:
                current["status"] = "aborted"
            else:
                current["status"] = "failed"
                err = reason.get("message") or reason.get("error") if isinstance(reason, dict) else ""
                current["error"] = "" if excluded else str(err or kind)
            current["completed_at"] = stamp
    count = max(1, int(turns))
    return parsed_turns[-count:]


@dataclass
class _Tracked:
    meta: DshSessionMeta
    mtime: float = 0.0
    size: int = 0


class DshSource:
    """Read-only Telegram bridge source for DSH sessions."""

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
                                agent=DSH,
                                session_id=meta.session_id,
                                title=meta.title or meta.session_id[:12],
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
                log.error("DSH turn event not delivered: %s: %s", type(exc).__name__, exc)

    def _to_view(self, meta: DshSessionMeta) -> SessionView:
        turns = read_turns(meta, 1, excluded=self._excluded(meta.cwd))
        state = str(turns[-1].get("status") or "idle") if turns else "idle"
        if state == "completed":
            state = "idle"
        return SessionView(
            agent=DSH,
            id=meta.session_id,
            title=meta.title or meta.session_id[:12],
            cwd=meta.cwd,
            state=state if state in {"running", "waiting", "idle"} else "idle",
            last_activity=meta.last_modified,
            live=False,
            kind="desktop",
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

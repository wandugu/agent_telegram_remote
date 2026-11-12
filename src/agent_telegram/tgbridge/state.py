"""Persistent bridge state (tg_dir()/state.json).

Holds what must survive a bridge restart: the getUpdates offset and recently
handled update ids (at-most-once handling), the lock flag, the forum topic of
every conversation, which conversation a bot message belongs to (reply
routing), muted conversations and a few small extra values. Every change is
written at once with an atomic replace; a damaged file is kept as
``state.json.corrupt`` and the bridge starts empty.
"""

from __future__ import annotations

import copy
import json
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_telegram.outputs import atomic_write_json
from agent_telegram.tgbridge.config import _keep_corrupt, _read_bytes, tg_dir

STATE_FILE = "state.json"
MAX_SEEN_UPDATES = 2000
MAX_MESSAGES = 3000
# Telegram picks the next update_id at random after a week without updates, so an
# old offset may lie above every new id, and getUpdates(offset=old) would confirm
# (drop) them all. An offset last advanced longer ago than this is forgotten:
# update_offset reads 0 (poll without offset) and the next mark_update starts over.
# Telegram keeps undelivered updates for 24 hours only, and seen_update() still
# guards against handling one twice.
OFFSET_STALE_S = 6 * 24 * 3600.0
# An update id this far below the offset means Telegram restarted its ids (see _follow).
ID_RESTART_GAP = 1_000_000
_VERSION = 1


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return default


def _topic_record(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    thread_id = _int(value.get("thread_id"))
    if thread_id is None:
        return None
    name = value.get("name")
    return {
        "thread_id": thread_id,
        "name": name if isinstance(name, str) else "",
        "closed": value.get("closed") is True,
        "created_at": _num(value.get("created_at")),
        "active_at": _num(value.get("active_at")),
    }


class BridgeState:
    """tg_dir()/state.json, atomic writes, thread-safe (RLock)."""

    def __init__(self, path: Path | None = None, *, clock: Callable[[], float] = time.time) -> None:
        self.path = path or tg_dir() / STATE_FILE
        self._clock = clock
        self._lock = threading.RLock()
        self._offset = 0
        self._offset_at = 0.0
        self._seen: OrderedDict[int, None] = OrderedDict()  # oldest first
        self._locked = False
        self._locked_at = 0.0
        self._locked_reason = ""
        self._topics: dict[str, dict[str, Any]] = {}
        self._messages: OrderedDict[int, str] = OrderedDict()  # bot message id -> session key, oldest first
        self._muted: set[str] = set()
        self._extra: dict[str, Any] = {}
        self._load()

    # --- persistence ---------------------------------------------------------------------

    def _load(self) -> None:
        raw = _read_bytes(self.path)
        if raw is None:
            return
        try:
            data = json.loads(raw.decode("utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("state is not an object")
        except (ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
            _keep_corrupt(self.path)
            return
        self._offset = max(0, _int(data.get("update_offset")) or 0)
        self._offset_at = _num(data.get("update_offset_at"))
        seen = data.get("seen_updates")
        if isinstance(seen, list):
            ids = [item for item in (_int(value) for value in seen) if item is not None]
            self._seen = OrderedDict.fromkeys(ids[-MAX_SEEN_UPDATES:])
        self._locked = data.get("locked") is True
        self._locked_at = _num(data.get("locked_at"))
        reason = data.get("locked_reason")
        self._locked_reason = reason if isinstance(reason, str) else ""
        topics = data.get("topics")
        if isinstance(topics, dict):
            owners: dict[int, str] = {}  # a thread id belongs to one key: keep the latest active
            for key, value in topics.items():
                record = _topic_record(value)
                if record is None:
                    continue
                other = owners.get(record["thread_id"])
                if other is not None:
                    if self._topics[other]["active_at"] >= record["active_at"]:
                        continue
                    del self._topics[other]
                owners[record["thread_id"]] = str(key)
                self._topics[str(key)] = record
        messages = data.get("messages")
        if isinstance(messages, dict):
            for message_id, key in messages.items():
                try:
                    number = int(message_id)
                except ValueError:
                    continue
                if isinstance(key, str):
                    self._messages[number] = key
            while len(self._messages) > MAX_MESSAGES:
                self._messages.popitem(last=False)
        muted = data.get("muted")
        if isinstance(muted, list):
            self._muted = {key for key in muted if isinstance(key, str)}
        extra = data.get("extra")
        if isinstance(extra, dict):
            self._extra = {str(name): value for name, value in extra.items()}

    def _snapshot(self) -> dict[str, Any]:
        return {
            "version": _VERSION,
            "update_offset": self._offset,
            "update_offset_at": self._offset_at,
            "seen_updates": list(self._seen),
            "locked": self._locked,
            "locked_at": self._locked_at,
            "locked_reason": self._locked_reason,
            "topics": self._topics,
            "messages": {str(message_id): key for message_id, key in self._messages.items()},
            "muted": sorted(self._muted),
            "extra": self._extra,
        }

    def save(self) -> None:
        with self._lock:
            atomic_write_json(self.path, self._snapshot())

    # --- Telegram updates ----------------------------------------------------------------

    def _offset_stale(self) -> bool:
        # 0.0 means "unknown" (a hand-written file): keep such an offset.
        return self._offset_at > 0 and self._clock() - self._offset_at > OFFSET_STALE_S

    @property
    def update_offset(self) -> int:
        """Next getUpdates offset; 0 before the first update and after OFFSET_STALE_S of silence."""

        with self._lock:
            return 0 if self._offset_stale() else self._offset

    @property
    def update_offset_at(self) -> float:
        """Epoch seconds of the last mark_update (0.0 never)."""

        with self._lock:
            return self._offset_at

    def seen_update(self, update_id: int) -> bool:
        with self._lock:
            return update_id in self._seen

    def mark_update(self, update_id: int) -> None:
        """Remember update_id as handled and advance the offset; saved before returning."""

        with self._lock:
            self._seen.pop(update_id, None)
            self._seen[update_id] = None
            while len(self._seen) > MAX_SEEN_UPDATES:
                self._seen.popitem(last=False)
            self._follow(update_id)
            self.save()

    def confirm_update(self, update_id: int) -> None:
        """A getUpdates batch held update_id again (already handled): move the offset past it.

        Saved only when the offset changed.
        """

        with self._lock:
            before = self._offset
            self._follow(update_id)
            if self._offset != before:
                self.save()

    def _follow(self, update_id: int) -> None:
        """The offset after update_id (caller holds the lock).

        Telegram only returns an id below our offset when its ids restarted lower (it picks
        them at random after a quiet week). It then ignores that higher offset: it confirms
        nothing, keeps every update for 24 hours and returns the oldest 100 again and again,
        so new ones (taps) stop arriving once a day has more than 100 (observed, 2026-10-11 to
        10-15). Such an id, like a stale offset, starts the offset over from it. Restarted ids
        land far away (951,800,136 to 313,217,739 there); an id only slightly below the
        offset is an old one and never moves it back.
        """

        follow = update_id + 1
        if self._offset_stale() or follow < self._offset - ID_RESTART_GAP:
            self._offset = follow
        else:
            self._offset = max(self._offset, follow)
        self._offset_at = self._clock()

    def adopt_bot(self, bot_id: int) -> bool:
        """Bind the update bookkeeping to one bot; True when it was another bot's (offset reset).

        update_ids are per bot: after set-token switches to another bot, the old offset is
        likely higher than every id the new bot sends, so getUpdates would drop all of them.
        """

        with self._lock:
            previous = self._extra.get("bot_id")
            if previous == bot_id:
                return False
            changed = previous is not None
            if changed:
                self._offset, self._offset_at = 0, 0.0
                self._seen.clear()
                self._messages.clear()  # message ids belong to the old bot's messages
            self._extra["bot_id"] = bot_id
            self.save()
            return changed

    # --- lock ----------------------------------------------------------------------------

    @property
    def locked(self) -> bool:
        with self._lock:
            return self._locked

    @property
    def locked_at(self) -> float:
        with self._lock:
            return self._locked_at

    @property
    def locked_reason(self) -> str:
        with self._lock:
            return self._locked_reason

    def set_locked(self, locked: bool, reason: str = "") -> None:
        with self._lock:
            self._locked = bool(locked)
            self._locked_at = self._clock()
            self._locked_reason = str(reason or "")
            self.save()

    # --- topics --------------------------------------------------------------------------

    def topic_for(self, key: str) -> dict | None:
        """{"thread_id", "name", "closed", "created_at", "active_at"} or None."""

        with self._lock:
            record = self._topics.get(key)
            return dict(record) if record is not None else None

    def set_topic(self, key: str, thread_id: int, name: str) -> None:
        """Map key to a forum topic; a thread id belongs to one key only."""

        with self._lock:
            old = self._topics.get(key)
            if old is not None and old["thread_id"] == thread_id and old["name"] == name:
                return
            now = self._clock()
            for other in [other for other, record in self._topics.items() if record["thread_id"] == thread_id]:
                if other != key:
                    del self._topics[other]
            if old is not None and old["thread_id"] == thread_id:
                record = {**old, "name": name}
            else:
                record = {"thread_id": thread_id, "name": name, "closed": False, "created_at": now, "active_at": now}
            self._topics[key] = record
            self.save()

    def touch_topic(self, key: str, when: float) -> None:
        when = _num(when, math.nan)
        with self._lock:
            record = self._topics.get(key)
            if record is None or not math.isfinite(when) or when <= record["active_at"]:
                return
            record["active_at"] = when
            self.save()

    def set_topic_closed(self, key: str, closed: bool) -> None:
        with self._lock:
            record = self._topics.get(key)
            if record is None or record["closed"] == bool(closed):
                return
            record["closed"] = bool(closed)
            self.save()

    def drop_topic(self, key: str) -> None:
        """Forget a key's topic (deleted in Telegram); the next push creates a new one."""

        with self._lock:
            if self._topics.pop(key, None) is not None:
                self.save()

    def key_for_thread(self, thread_id: int) -> str | None:
        with self._lock:
            for key, record in self._topics.items():
                if record["thread_id"] == thread_id:
                    return key
            return None

    def topics(self) -> dict[str, dict]:
        with self._lock:
            return {key: dict(record) for key, record in self._topics.items()}

    # --- message routing -----------------------------------------------------------------

    def remember_message(self, message_id: int, key: str) -> None:
        with self._lock:
            if self._messages.get(message_id) == key:
                self._messages.move_to_end(message_id)
            else:
                self._messages.pop(message_id, None)
                self._messages[message_id] = key
            while len(self._messages) > MAX_MESSAGES:
                self._messages.popitem(last=False)
            self.save()

    def key_for_message(self, message_id: int) -> str | None:
        with self._lock:
            return self._messages.get(message_id)

    # --- mute ----------------------------------------------------------------------------

    def muted(self, key: str) -> bool:
        with self._lock:
            return key in self._muted

    def set_muted(self, key: str, muted: bool) -> None:
        with self._lock:
            if (key in self._muted) == bool(muted):
                return
            if muted:
                self._muted.add(key)
            else:
                self._muted.discard(key)
            self.save()

    # --- extra values --------------------------------------------------------------------

    def get(self, name: str, default: Any = None) -> Any:
        with self._lock:
            if name not in self._extra:
                return default
            return copy.deepcopy(self._extra[name])

    def put(self, name: str, value: Any) -> None:
        """Store a small JSON value (TypeError/ValueError when it is not plain JSON)."""

        value = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
        with self._lock:
            if name in self._extra and self._extra[name] == value:
                return
            self._extra[name] = value
            self.save()

    def drop(self, name: str) -> None:
        with self._lock:
            if self._extra.pop(name, _MISSING) is not _MISSING:
                self.save()


_MISSING = object()

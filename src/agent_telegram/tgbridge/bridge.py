"""The Telegram bridge: routes finished turns to the group and the owner's commands back.

Threads:
    main          getUpdates long poll; each update is handled on a small worker pool
    tgbridge-poll Codex/Claude sources every few seconds, hook events, delivery tracking, housekeeping
                  (no Telegram I/O of its own: edits and closes go through the outbox)
    tgbridge-remote  Codex threads on SSH remote hosts through the Codex app (slow calls, own pace)
    tgbridge-send outbound pushes and edits (queued so a slow proxy never stalls polling)
    hook server   Claude Code hooks (hook_server.HookServer), including blocking permission waits
    tgbridge-exec confirmed writes (send, stop, new session, implement a plan) and Codex question answers

Every write (send, stop, new session, unlock) goes through a confirmation card whose
button carries a single-use nonce bound to the asking user and to the card; updates
are marked processed before any side effect, so a crash never repeats a command (at
most once).
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
import traceback
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_telegram.i18n import current, normalize, set_language, tr
from agent_telegram.outputs import atomic_write_json
from agent_telegram import codex_threads
from agent_telegram.tgbridge import codex_ipc, codex_remote, headless, progress, quota, render
from agent_telegram.tgbridge.config import Config, config_path, resolve_proxy, tg_dir
from agent_telegram.tgbridge.model import (ANTIGRAVITY, CLAUDE, CODEX, DSH, PI, ApprovalEvent, AttentionEvent, Event, ProgressEvent, Question, QuestionEvent, SessionView,
                    TurnEvent, session_key, split_key)
from agent_telegram.tgbridge.state import BridgeState
from agent_telegram.tgbridge.telegram_api import BotAPI, Conflict, TelegramError
from agent_telegram.tgbridge.winsys import Presence

VERSION = "1"
READONLY_AGENTS = frozenset({ANTIGRAVITY, PI, DSH})
# Telegram's own senders: service notifications, anonymous group admins, channel posts.
SYSTEM_SENDERS = frozenset({777000, 1087968824, 136817688})
BROADCAST_COMMANDS = frozenset({"list", "status", "help", "start", "lock", "lang"})
SESSION_COMMANDS = frozenset({"read", "full", "stop", "mute", "unmute", "ask"})
ALLOWED_UPDATES = ["message", "callback_query", "my_chat_member", "chat_member"]
CONFIRM_TTL_S = 600.0
# PermissionRequest: Claude Code shows its dialog while the hook runs (the hook and the dialog
# race), so an approval card may stay open as long as the hook lives. Hooks installed with the
# X-TgBridge-Timeout header say how long that is; older ones time out at 580 s.
LEGACY_HOOK_TIMEOUT_S = 580.0
PERMISSION_MARGIN_S = 40.0  # answer this long before the hook's own timeout
PERMISSION_WAIT_S = LEGACY_HOOK_TIMEOUT_S - PERMISSION_MARGIN_S
PERMISSION_WAIT_MAX_S = 6 * 3600.0
PERMISSION_POLL_S = 2.0
PERMISSION_LOCAL_MISSES = 2  # registry polls no longer "waiting" before the PC counts as having answered
PERMISSION_NO_DIALOG_S = 15.0  # an interactive session never showing "waiting" this long: nothing waits for it
CARD_RETRY_S = 600.0  # leave a missing card to the PC after this long (owned runs have no dialog)
ALLOW_ALL_TTL_S = 12 * 3600.0
ATTENTION_CARD_WAIT_STEPS = 6  # a permission alert looks for the hook's card for up to 3 s
# Mid-turn messages: one Telegram message per turn, edited at most this often (edits share the
# group's rate limit with pushes and approval cards); text arriving in between waits for the next edit.
PROGRESS_EDIT_MIN_S = 30.0
PROGRESS_LIMIT = 3500  # UTF-16 units shown; older texts give way to newer ones
PROGRESS_ITEM_CHARS = 1500


def _permission_outcomes() -> dict[str, str]:
    return {
        "allow": tr('✅ Allowed'),
        "allow_all": tr('⚡ Allowed; future permission requests in this session will be allowed automatically'),
        "deny": tr('⛔ Denied'),
        "local": tr('💻 Handed over to the PC'),
        "answered": tr('💻 Handled on the PC'),
        "expired": tr('⌛ Timed out; handle on the PC'),
        "gone": tr('↩️ Request withdrawn (the session interrupted or ended this turn)'),
        "restart": tr('🔄 Bridge restarted; handle on the PC'),
        "locked": tr('🔒 Writes locked; handle on the PC'),
    }


# Question cards: decisions of a Claude hook request beyond permission outcomes.
def _question_outcomes() -> dict[str, str]:
    return {
        "answer": tr('✅ Answered on Telegram'),
        "approve": tr('✅ Plan approved; Claude is implementing it'),
        "keep_planning": tr('✏️ Asked Claude to keep planning'),
        "deny": tr('⛔ Not answered (the background run received a denial)'),
    }


QUESTION_TYPED_MAX = 2000  # one typed answer or one set of change requests
QUESTION_HOOK_TTL_S = 3600.0  # how long what a question hook did (a card or none) is remembered
QUESTION_ANSWER_SHOWN = 600  # characters of an answer given on the PC shown on the closed card
ASYNC_ANSWER_CONFIRM_S = 120.0
CLOSED_CARDS_MAX = 200  # closed question cards a reply is still recognized on
PLAN_OFFER_TTL_S = 24 * 3600.0
PLAN_OFFERS_MAX = 100
# The hook's tool input of a plan carries the plan as the client shows it; sending it back would
# mark the plan as edited by the user.
PLAN_SHOWN_FIELDS = ("plan", "planFilePath")
PLAN_FILE_MAX = 256 * 1024
DELIVERY_CONFIRM_S = 90.0
STOP_FLAG_TTL_S = 1800.0
STOP_HIT_GRACE_S = 10.0  # parallel tool calls of the stopped turn still see the flag this long
PUSH_MAX_AGE_S = 6 * 3600.0
OUTBOX_MAX = 300
NEW_PER_HOUR = 6
BACKOFF_MAX_S = 30.0  # a refused loopback proxy costs nothing; recover within half a minute
TOPIC_PAUSE_S = 600.0  # after a permanent createForumTopic error (not a forum, no rights)
TOPIC_TITLE_SYNC_S = 60.0  # renames made in the apps reach the topic within this long
TOPIC_TITLE_SYNC_LIMIT = 200
CONFLICT_CONFIRM_S = 600.0  # a second 409 within this long locks writes
CONFLICT_RETRY_S = 10.0  # the next poll after a first 409
AUTH_CACHE_S = 300.0
REMOTE_FIRST_POLL_S = 5.0
REMOTE_MIN_POLL_S = 10.0
REMOTE_BACKOFF_MAX_S = 600.0
REMOTE_QUIET_S = 600.0  # remote Codex reads failing this long get logged
CLOSES_PER_PASS = 5
OWNER_LOCK = "owner /lock"
_COMMAND_RE = re.compile(r"^/([A-Za-z_]+)(?:@([A-Za-z0-9_]+))?(?:\s+(.*))?$", re.DOTALL)
# Permission modes a run started from Telegram may use; auto and bypass become acceptEdits.
_REMOTE_MODES = ("default", "acceptEdits", "plan", "dontAsk")

def _help() -> str:
    return (
        tr('<b>Usage</b>\n'
            '/list [n] — The n most recent sessions on this machine\n'
            '/status — Bridge status\n'
            '/new — Create a Codex / Claude session\n'
            '/lock — Lock writes (on every machine); /unlock@this_bot — Unlock\n'
            'Type in a session topic — Send to that session (confirmation required); while a turn is running, choose [Queue] (send after this turn ends) or [Steer] (insert into the running turn)\n'
            'Messages from the model during a turn appear in the topic’s “In progress” message\n'
            'In a topic: /stop interrupts, /read [k] shows the last k turns, /full shows the latest full turn, /mute /unmute toggles pushes\n'
            'Claude permission requests: tap [Allow], [Deny], or [Allow all] (twice; future requests in this session are then allowed automatically); send /ask in the topic to restore individual approvals\n'
            'Codex / Claude questions (including plan-mode options): choose options on the question card, or reply to it with your own answer, then tap [Send answers]; for Claude plans, tap [Approve plan] or [Keep planning] (reply with change requests); completed Codex plans have an [Implement this plan] button\n'
            'Codex conversations on remote hosts (via local Codex Desktop) are view-only; sending and interrupting are unavailable\n'
            'Antigravity conversations are view-only: pushes and /list, /read, /full are supported; replies and /stop are refused')
        + tr('\n/lang [en|zh] — Show or change the language on every machine')
    )


def parse_command(text: str) -> tuple[str, str, str] | None:
    """(command, @bot or "", arguments) for a "/cmd@bot args" message."""

    match = _COMMAND_RE.match(text.strip())
    if not match:
        return None
    return match.group(1).lower(), match.group(2) or "", (match.group(3) or "").strip()


def topic_link(chat_id: int, thread_id: int) -> str:
    internal = str(chat_id)
    internal = internal[4:] if internal.startswith("-100") else internal.lstrip("-")
    return f"https://t.me/c/{internal}/{thread_id}"


def _epoch_now() -> float:
    return time.time()


# --- logs ----------------------------------------------------------------------------------


class BridgeLog:
    """bridge.log (readable, rotated) and audit.jsonl (one record per inbound action, rotated).

    Writes never raise: a full disk or a locked file must not end a worker thread.
    """

    def __init__(self, directory: Path, redact: Callable[[str], str] = lambda text: text, *,
                 max_bytes: int = 1024 * 1024, audit_max_bytes: int = 5 * 1024 * 1024, backups: int = 5) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        self.redact = redact
        self.text_path = directory / "bridge.log"
        self.audit_path = directory / "audit.jsonl"
        self.max_bytes = max_bytes
        self.audit_max_bytes = audit_max_bytes
        self.backups = backups
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self._since: dict[str, float] = {}  # trouble(): when each ongoing problem started

    def line(self, text: str, level: str = "INFO") -> None:
        try:
            entry = f"{datetime.now():%Y-%m-%d %H:%M:%S} {level} {self.redact(text)}\n"
            with self._lock:
                self._rotate(self.text_path, self.max_bytes)
                with self.text_path.open("a", encoding="utf-8", errors="replace") as stream:
                    stream.write(entry)
        except Exception:  # noqa: BLE001 - logging must never break the bridge
            pass

    def once(self, key: str, text: str, level: str = "WARNING", *, every_s: float = 600.0) -> None:
        """Log a repeating condition (network down, proxy gone) at most every ``every_s``."""

        now = time.monotonic()
        if now - self._last.get(key, -every_s - 1) >= every_s:
            self._last[key] = now
            self.line(text, level)

    def clear(self, key: str) -> None:
        self._last.pop(key, None)

    def trouble(self, key: str, text: str, *, after_s: float = 120.0, every_s: float = 3600.0) -> None:
        """A problem that usually clears by itself (a dropped connection): silent until it lasts after_s."""

        now = time.monotonic()
        since = self._since.setdefault(key, now)
        if now - since >= after_s:
            self.once(key, tr('{v0} (ongoing for {v1:.0f} minutes; still retrying)', v0=text, v1=(now - since) / 60), every_s=every_s)

    def recovered(self, key: str, text: str) -> None:
        """The trouble under key is over; one line if it had been logged."""

        since = self._since.pop(key, None)
        logged = self._last.pop(key, None) is not None
        if since is not None and logged:
            self.line(tr('{v0} (interrupted for about {v1:.0f} minutes)', v0=text, v1=(time.monotonic() - since) / 60))

    def audit(self, event: str, /, **fields: Any) -> None:
        try:
            record = {"time": datetime.now().astimezone().isoformat(timespec="seconds"), "event": event}
            record.update({key: value for key, value in fields.items() if value is not None})
            text = self.redact(json.dumps(record, ensure_ascii=False, default=str))
            with self._lock:
                self._rotate(self.audit_path, self.audit_max_bytes)
                with self.audit_path.open("a", encoding="utf-8", errors="replace") as stream:
                    stream.write(text + "\n")
        except Exception:  # noqa: BLE001
            pass

    def _rotate(self, path: Path, limit: int) -> None:
        try:
            if path.stat().st_size < limit:
                return
            for index in range(self.backups - 1, 0, -1):
                older = path.with_name(f"{path.name}.{index}")
                if older.exists():
                    older.replace(path.with_name(f"{path.name}.{index + 1}"))
            path.replace(path.with_name(f"{path.name}.1"))
        except OSError:
            pass


def _utf8(text: str) -> bytes:
    return text.encode("utf-8", "replace")  # lone surrogates from transcripts must not lose the file


def _transient(exc: TelegramError) -> bool:
    """A failure that retrying fixes: no answer (network, proxy), 429 or a Telegram server error."""

    return exc.code is None or exc.code == 429 or exc.code >= 500


def text_digest(text: str) -> str:
    import hashlib

    return hashlib.sha256(_utf8(text)).hexdigest()[:16]


# --- runtime records -----------------------------------------------------------------------


@dataclass
class Pending:
    """A write waiting for the owner's tap on its confirmation card."""

    nonce: str
    action: str  # send | stop | new | unlock
    key: str
    user_id: int
    created: float
    text: str = ""
    message_id: int | None = None  # the card
    thread_id: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PermissionWait:
    nonce: str
    session_id: str
    digest: str
    deadline: float
    owned: bool
    user_ids: tuple[int, ...]
    event: threading.Event = field(default_factory=threading.Event)
    decision: str = ""  # a key of PERMISSION_OUTCOMES
    message_id: int | None = None
    thread_id: int | None = None
    html: str = ""
    full: str = ""  # the whole input, lightly redacted, sent on [View full text]
    needs_view: bool = False  # [Allow] appears only after the whole input was sent
    viewed: bool = False
    key: str = ""
    title: str = ""
    tool: str = ""
    subagent: bool = False  # a sub-agent's request: its dialog may wait for the hook
    dialog: bool = False  # Claude Code's own dialog is open meanwhile (an interactive session's main thread)
    arrived: float = 0.0
    reminded: bool = False  # notified loudly (sent while away, or a reminder after leaving the PC)
    confirming_all: bool = False  # [Allow all] tapped once, waiting for the second tap
    seen_waiting: bool = False  # the registry showed the session waiting during this request
    card_failed: bool = False
    card_permanent_failure: bool = False
    attempts: int = 0
    next_send_at: float = 0.0
    last_error_code: int | None = None
    maybe_duplicate: bool = False  # a read failure may have hidden a successful Telegram send
    why: str = ""  # why the registry or presence says the PC decided


@dataclass
class QuestionCard:
    """A card that answers what an agent asks: Claude AskUserQuestion (kind "ask") and ExitPlanMode
    ("plan") through their PermissionRequest hook, Codex request_user_input ("codex") through the
    Codex Desktop IPC, and async Codex questions ("codex_async") through IPC or a raw send.

    Taps only change what the card holds; the answers leave on [Send answers] (or [Approve plan] /
    [Keep planning]), a tap on the card that shows them, bound to the card's nonce.
    """

    nonce: str
    agent: str
    session_id: str
    key: str
    kind: str  # ask | plan | codex | codex_async
    title: str  # redacted
    user_ids: tuple[int, ...]
    questions: list[Question] = field(default_factory=list)
    shown: list[Question] = field(default_factory=list)  # the questions as the card shows them (redacted)
    answers: list[list[str]] = field(default_factory=list)  # per question: chosen labels, or one typed text
    typed: list[bool] = field(default_factory=list)  # per question: the answer was typed
    index: int = 0  # the question being answered
    review: bool = False  # every question visited: [Send answers] is offered
    plan: str = ""  # kind plan: the plan (redacted)
    feedback: str = ""  # kind plan: typed change requests that go with [Keep planning]
    tool_input: dict[str, Any] = field(default_factory=dict)  # Claude: the hook's input (never logged)
    request_id: str = ""  # Codex: the request_user_input call id
    turn_id: str = ""
    mode: str = "blocking"  # blocking | async
    client_id: str = ""  # async IPC reply identity; retained across an unconfirmed retry
    marker: str = ""  # tg:XXXXXX (without brackets), as the source attributes a raw send
    sent_at: float = 0.0
    via: str = ""
    job_id: str = ""
    wait: PermissionWait | None = None  # Claude: the hook request the card answers
    message_id: int | None = None
    thread_id: int | None = None
    # open | sending (Codex: the answer is on its way) | sent (Codex: delivered; Claude: decided)
    state: str = "open"
    # Codex: [Send answers] went out at least once. A failed IPC call may still have delivered it, so
    # the answer the rollout shows later is not necessarily one given at the PC.
    tried: bool = False
    footer: str = ""  # status line under the questions
    truncated: bool = False  # the card could not show everything: [View full text]
    viewed: bool = False  # [View full text] sent the whole text (a truncated plan is approved only after that)
    # Bumped whenever the buttons start to mean something else (another question, back to the
    # list): taps carry it, so one on buttons the card no longer shows cannot hit another question.
    version: int = 0
    reminded: bool = False
    closed: bool = False  # _finish_card ran: no edit but that one
    # Orders one card's changes and edits (update workers, the Codex delivery, the send thread).
    edit_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def owned(self) -> bool:
        return self.wait is not None and self.wait.owned


@dataclass
class CodexApprovalCard:
    """One snapshot-bound approval; only accept/decline may leave this single-use card."""

    nonce: str
    approval: ApprovalEvent
    title: str
    user_ids: tuple[int, ...]
    html: str
    full: str
    needs_view: bool = False
    viewed: bool = False
    message_id: int | None = None
    thread_id: int | None = None
    state: str = "open"  # open | sending | sent (outcome unknown) | closed
    invalidated: bool = False  # a lock revokes the nonce immediately, before its queued Telegram edit
    write_started: bool = False  # reserved confirmations can still be revoked before the worker starts IPC
    version: int = 0
    reminded: bool = False
    footer: str = ""
    pending_by: str = ""  # a successful snapshot closed it while the write was in flight
    edit_lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    @property
    def key(self) -> str:
        return self.approval.key


@dataclass
class PlanOffer:
    """[Implement this plan] under a Codex turn that ended with a plan."""

    key: str
    turn_id: str
    title: str
    created: float


@dataclass
class ProgressCard:
    """The message showing what a running turn has said so far."""

    message_id: int
    thread_id: int | None
    created: float
    header: str
    texts: list[str]
    edited: float = 0.0
    pending: bool = False  # texts added since the last edit
    dropped: bool = False  # older texts no longer fit and were dropped
    flush_queued: bool = False  # an edit is waiting in the outbox
    cwd: str = ""


@dataclass
class AllowAll:
    """[Allow all]: the bridge allows every permission request of the session until revoked or expired."""

    session_id: str
    key: str
    user_id: int
    since: float
    until: float
    allowed: int = 0
    allowed_at: list[float] = field(default_factory=list)  # automatic allows not yet counted on a turn push


@dataclass
class Delivery:
    """A send that is followed until it is confirmed (or given up)."""

    kind: str  # codex_job | codex_async | claude_inbox | headless
    key: str
    started: float
    message_id: int | None
    thread_id: int | None
    marker: str = ""
    job_id: str = ""
    info: dict[str, Any] = field(default_factory=dict)
    run: headless.HeadlessRun | None = None
    text: str = ""
    last_phase: str = ""
    queued: bool = False  # [Queue]: waits for the running turn to end; otherwise sent at once (or steered)


@dataclass
class Wizard:
    """/new: agent -> target -> prompt (a reply to the prompt message) -> confirmation."""

    wid: str
    user_id: int
    created: float
    thread_id: int | None
    agent: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)  # (label, value) of the current step
    target: str = ""  # Codex project id ("" = none) or Claude cwd
    target_label: str = ""
    prompt_message_id: int | None = None


class Outbox:
    """Bounded FIFO of push callables; the oldest is dropped when full."""

    def __init__(self, limit: int = OUTBOX_MAX) -> None:
        self._items: deque[tuple[float, str, Callable[[], None]]] = deque()
        self._cond = threading.Condition()
        self.limit = limit
        self.dropped = 0

    def put(self, label: str, action: Callable[[], None], created: float) -> None:
        with self._cond:
            if len(self._items) >= self.limit:
                self._items.popleft()
                self.dropped += 1
            self._items.append((created, label, action))
            self._cond.notify()

    def put_front(self, item: tuple[float, str, Callable[[], None]]) -> None:
        with self._cond:
            self._items.appendleft(item)
            self._cond.notify()

    def get(self, timeout: float) -> tuple[float, str, Callable[[], None]] | None:
        with self._cond:
            if not self._items:
                self._cond.wait(timeout)
            return self._items.popleft() if self._items else None

    def __len__(self) -> int:
        with self._cond:
            return len(self._items)


# --- the bridge ----------------------------------------------------------------------------


class Bridge:
    def __init__(
        self,
        cfg: Config,
        state: BridgeState,
        api: BotAPI,
        *,
        redact: Callable[[str], str],
        presence: Presence,
        log: BridgeLog,
        clock: Callable[[], float] = _epoch_now,
        poll_s: float = 5.0,
        api_factory: Callable[[str | None], BotAPI] | None = None,
        proxy: str | None = None,
        synchronous: bool = False,
    ) -> None:
        self.cfg = cfg
        self.state = state
        self.api = api
        self.redact = redact
        # Approval cards: exact secrets and token shapes only (see Redactor.light).
        self.redact_light: Callable[[str], str] = getattr(redact, "light", redact)
        self.presence = presence
        self.log = log
        self.clock = clock
        self.poll_s = poll_s
        self.api_factory = api_factory
        self.proxy = proxy
        self.synchronous = synchronous  # tests: handle updates inline
        self.codex: Any = None  # CodexSource, attached by attach_sources
        self.claude: Any = None  # ClaudeSource
        self.antigravity: Any = None  # AntigravitySource (view only)
        self.pi: Any = None  # PiSource (view only)
        self.dsh: Any = None  # DshSource (view only)
        self.me: dict[str, Any] = {}
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.exit_code = 0
        self.pending: dict[str, Pending] = {}
        self.permissions: dict[str, PermissionWait] = {}
        self.questions: dict[str, QuestionCard] = {}  # nonce -> open question card (Claude ones also wait in permissions)
        self.codex_approvals: dict[str, CodexApprovalCard] = {}
        self.plan_offers: OrderedDict[str, PlanOffer] = OrderedDict()  # nonce -> [Implement this plan] under a Codex push
        # (Claude session id, progress.interactive_digest) -> (when, "pending" | "card" | "failed" | "none"): what
        # the PermissionRequest hook of an AskUserQuestion / ExitPlanMode call did. Its progress text
        # gives way to a card, and is pushed when no card goes out.
        self._question_hooks: dict[tuple[str, str], tuple[float, str]] = {}
        self._closed_cards: OrderedDict[int, None] = OrderedDict()  # closed question cards (message ids)
        self._executing = 0  # confirmed writes and question answers under way (status: Install waits)
        self.allow_all: dict[str, AllowAll] = {}  # session id -> grant; in memory only (a restart revokes)
        self.progress_cards: dict[str, ProgressCard] = {}  # session key -> its running turn's message
        self.deliveries: list[Delivery] = []
        self.stop_flags: dict[str, float] = {}  # session -> when /stop was confirmed
        self.stop_hits: dict[str, float] = {}  # session -> first PreToolUse the flag stopped
        self.wizards: dict[str, Wizard] = {}
        self.runs: dict[str, headless.HeadlessRun] = {}
        self._starting: set[str] = set()
        self.last_text: OrderedDict[str, str] = OrderedDict()
        self.hook_events: deque[dict[str, Any]] = deque(maxlen=1000)
        self.outbox = Outbox()
        self.new_times: deque[float] = deque()
        self.started_at = clock()
        self._alerted: dict[str, float] = {}
        self._next_housekeeping = 0.0
        self._next_title_sync = 0.0
        self._member_count_alerted: int | None = None
        self._topic_lock = threading.Lock()
        self._topic_pause_until = 0.0
        self._rename_failed: dict[str, str] = {}
        self._conflict_at: float | None = None  # the last 409 from getUpdates
        self._api_lock = threading.Lock()
        self._auth_cache: tuple[float, str] | None = None  # (checked at, problem)
        self._workers: ThreadPoolExecutor | None = None

    # --- wiring ---

    def attach_sources(self, *, codex: Any = None, claude: Any = None, antigravity: Any = None, pi: Any = None, dsh: Any = None) -> None:
        self.codex, self.claude, self.antigravity, self.pi, self.dsh = codex, claude, antigravity, pi, dsh

    def _source(self, agent: str) -> Any | None:
        """Return the attached source for an agent, or None for an unknown agent."""

        return {CODEX: self.codex, CLAUDE: self.claude, ANTIGRAVITY: self.antigravity, PI: self.pi, DSH: self.dsh}.get(agent)

    def on_event(self, event: Event) -> None:
        """Callback for the sources: queue the push."""

        if isinstance(event, TurnEvent):
            if event.final_text and not self.cfg.excluded(event.cwd):
                self._remember_text(event.key, event.final_text)
            if event.agent == CLAUDE:
                ended = event.completed_at or self.clock()
                with self.lock:
                    grant = self.allow_all.get(event.session_id)
                    allowed = sum(1 for at in grant.allowed_at if at <= ended) if grant is not None else 0
                    if grant is not None:
                        grant.allowed_at = [at for at in grant.allowed_at if at > ended]
                if allowed:
                    event = replace(event, labels=[*event.labels, tr('Automatically allowed {v0} times', v0=allowed)])
            self.outbox.put(f"turn {event.key}", lambda: self._push_turn(event), self.clock())
        elif isinstance(event, AttentionEvent):
            self.outbox.put(f"attention {event.key}", lambda: self._push_attention(event), self.clock())
        elif isinstance(event, ProgressEvent) and getattr(self.cfg, "progress_enabled", True):
            self.outbox.put(f"progress {event.key}", lambda: self._push_progress(event), self.clock())
        elif isinstance(event, QuestionEvent):
            self.outbox.put(f"question {event.key}", lambda: self._push_question(event), self.clock())
        elif isinstance(event, ApprovalEvent):
            self.outbox.put(f"approval {event.key}", lambda: self._push_codex_approval(event), self.clock())

    @property
    def machine(self) -> str:
        return self.cfg.machine

    @property
    def chat_id(self) -> int:
        assert self.cfg.chat_id is not None
        return self.cfg.chat_id

    def _later(self, label: str, action: Callable[[], Any]) -> None:
        """Telegram I/O off the calling thread (poll thread, hook threads)."""

        self.outbox.put(label, action, self.clock())

    def _edit_later(self, message_id: int | None, html: str,
                    buttons: list[list[tuple[str, str]]] | None = None) -> None:
        if message_id:
            self._later("edit", lambda: self._edit(message_id, html, buttons))

    # --- lifecycle ---

    def start(self) -> None:
        """Identify the bot, check the group and adopt headless runs of a previous process."""

        self._adopt_runs()
        self._mark_stale_cards()
        self.me = self._retry_forever(lambda: self.api.get_me(), "getMe")
        if isinstance(self.me.get("id"), int) and self.state.adopt_bot(self.me["id"]):
            self.log.line(tr('Bot changed to @{v0}: reset the message update offset', v0=self.me.get('username')), "WARNING")
        info = self._retry_forever(lambda: self.api.webhook_info(), "getWebhookInfo")
        if info.get("url"):
            self._lock_writes(tr('The bot has a webhook (someone else may have the token)'))
            self._alert("webhook", tr('🚨 <b>[{m}]</b> This bot has a webhook; its token may have leaked. Writes are locked. /revoke the token in @BotFather, then run set-token on this machine.'))
        try:
            chat = self.api.get_chat(self.chat_id)
            if not chat.get("is_forum"):
                self.log.line(tr('Topics are disabled in this group; pushes will go to General.'), "WARNING")
        except TelegramError as exc:
            self.log.line(tr('Failed to read group information: {v0}', v0=exc), "WARNING")
        self._set_commands()
        self.log.line(tr('Bridge started: machine={v0} bot=@{v1} locked={v2}', v0=self.machine, v1=self.me.get('username'), v2=self.state.locked))
        self._later("hello", lambda: self._send(
            tr('🟢 <b>[{v0}]</b> Bridge started{v1}', v0=render.esc(self.machine), v1=tr(' (writes locked)') if self.state.locked else ''),
            silent=True))

    def _set_commands(self) -> None:
        """Resolve descriptions at registration time, including after /lang."""

        try:
            self.api.set_commands(
                [("list", tr('Recent sessions')), ("status", tr('Bridge status')), ("new", tr('New session')), ("stop", tr('Interrupt (in a topic)')),
                 ("read", tr('Recent turns (in a topic)')), ("full", tr('Latest full turn (in a topic)')), ("lock", tr('Lock writes')),
                 ("unlock", tr('Unlock writes')), ("mute", tr('Mute this topic')), ("unmute", tr('Resume pushes')),
                 ("ask", tr('Revoke allow-all; approve individually (in a topic)')),
                 ("lang", tr('Show or change the language')), ("help", tr('Help'))],
                scope={"type": "chat", "chat_id": self.chat_id},
            )
        except TelegramError as exc:
            self.log.line(tr('Failed to set the command menu: {v0}', v0=exc), "WARNING")

    def run(self) -> int:
        from agent_telegram.gate import wait_until_open
        if not wait_until_open(log=self.log.line, stop=self.stopping):
            return self.exit_code
        # Sources and pushes start at once: turns that finish while Telegram is still unreachable
        # at logon are queued instead of lost.
        self._workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="tgbridge-upd")
        loops = [(self._poll_loop, "tgbridge-poll"), (self._send_loop, "tgbridge-send")]
        if getattr(self.codex, "remote", None) is not None:
            loops.append((self._remote_loop, "tgbridge-remote"))
        for target, name in loops:
            threading.Thread(target=self._guard(target, name), name=name, daemon=True).start()
        try:
            self.start()
            self._updates_loop()
        finally:
            self.stopping.set()
            if self.codex is not None and callable(getattr(self.codex, "stop", None)):
                self.codex.stop()
            self._workers.shutdown(wait=False, cancel_futures=True)
        return self.exit_code

    def stop(self) -> None:
        self.stopping.set()
        if self.codex is not None and callable(getattr(self.codex, "stop", None)):
            self.codex.stop()

    def _guard(self, target: Callable[[], None], name: str) -> Callable[[], None]:
        def run() -> None:
            try:
                target()
            except BaseException:  # noqa: BLE001 - pythonw has no stderr; record why the thread ended
                self.log.line(tr('Thread {v0} exited unexpectedly:\n{v1}', v0=name, v1=traceback.format_exc()), "ERROR")
                self.exit_code = self.exit_code or 1
                self.stopping.set()  # the scheduled task restarts the whole bridge
        return run

    def _fatal(self, text: str, code: int) -> None:
        self.log.line(text, "ERROR")
        self.exit_code = code
        self.stopping.set()

    def _network_trouble(self) -> None:
        """After a network error in auto proxy mode, follow Clash coming up or going away."""

        if self.api_factory is None or self.cfg.proxy is not None:
            return
        proxy = resolve_proxy(self.cfg)
        if proxy == self.proxy:
            return
        with self._api_lock:
            if proxy == self.proxy:
                return
            old, self.api, self.proxy = self.api, self.api_factory(proxy), proxy
        self.log.line(tr('Telegram connection changed to {v0}', v0=(tr('via proxy ') + proxy) if proxy else tr('direct')))
        try:
            old.close()
        except Exception:  # noqa: BLE001
            pass

    def _retry_forever(self, call: Callable[[], Any], what: str) -> Any:
        delay = 5.0
        while not self.stopping.is_set():
            try:
                result = call()
                self.log.recovered(what, tr('{v0} recovered', v0=what))
                return result
            except TelegramError as exc:
                if exc.code == 401:
                    self._fatal(tr('Invalid bot token (401; it may have been /revoked in BotFather): run set-token on this machine and restart the bridge.'), 3)
                    break
                if exc.code is None:
                    self._network_trouble()
                hint = tr('{v0} failed ({v1}); is the proxy running (or set proxy in config.json)?', v0=what, v1=exc)
                if _transient(exc):
                    self.log.trouble(what, hint)
                else:
                    self.log.once(what, hint)
                self.stopping.wait(delay)
                delay = min(delay * 2, BACKOFF_MAX_S)
        raise SystemExit(self.exit_code)

    # --- status (GET /status) ---

    def status(self) -> dict[str, Any]:
        self._prune_allow_all()
        with self.lock:
            runs = list(self.runs.values())
            status = {
                "service": "tgbridge",
                "version": VERSION,
                "machine": self.machine,
                "bot": self.me.get("username"),
                "locked": self.state.locked,
                "started_at": self.started_at,
                # Claude question cards wait in permissions too; they count as questions only.
                "pending_permissions": sum(1 for wait in self.permissions.values()
                                           if wait.tool not in progress.INTERACTIVE_TOOLS),
                "pending_questions": len(self.questions),
                "pending_approvals": len(self.codex_approvals),
                "allow_all": len(self.allow_all),
                "pending_confirmations": len(self.pending),
                "executing": self._executing,
                "deliveries": len(self.deliveries),
                "outbox": len(self.outbox),
                "outbox_dropped": self.outbox.dropped,
                "stop_flags": len(self.stop_flags),
                "proxy": self.proxy or "",
            }
        status["headless_runs"] = sum(1 for run in runs if headless.run_alive(run))
        remote = getattr(self.codex, "remote", None)
        if remote is not None:
            status["codex_remote"] = remote.status()
            status["codex_remote"]["last_error"] = self.redact(status["codex_remote"]["last_error"])[:300]
            status["codex_remote"]["list_error"] = self.redact(status["codex_remote"].get("list_error", ""))[:300]
        for name, src in (("antigravity", self.antigravity), ("pi", self.pi), ("dsh", self.dsh)):
            if src is not None:
                try:
                    info = dict(src.status())
                    info["last_error"] = self.redact(info.get("last_error") or "")[:300]
                    status[name] = info
                except Exception as exc:  # noqa: BLE001 - GET /status must keep answering
                    status[name] = {"error": self.redact(f"{type(exc).__name__}: {exc}")[:300]}
        return status

    # --- Telegram polling ---

    def _updates_loop(self) -> None:
        delay = 5.0
        while not self.stopping.is_set():
            wait = self.poll_updates_once(timeout_s=25)
            if wait is None:
                delay = 5.0
                continue
            self.stopping.wait(wait if wait > 0 else delay)
            delay = min(delay * 2, BACKOFF_MAX_S)

    def poll_updates_once(self, *, timeout_s: int = 25) -> float | None:
        """Fetch and handle one batch; None on success, else seconds to wait (0 = use the backoff)."""

        try:
            updates = self.api.get_updates(self.state.update_offset or None, timeout_s=timeout_s,
                                           allowed_updates=ALLOWED_UPDATES)
            self.log.recovered("getUpdates", tr('Telegram message reception recovered'))
        except Conflict:
            return self._on_conflict()
        except TelegramError as exc:
            if exc.code == 401:
                self._fatal(tr('Invalid bot token (401; it may have been /revoked in BotFather): run set-token on this machine and restart the bridge.'), 3)
                return 1.0
            if exc.code is None:
                self._network_trouble()
            if _transient(exc):  # long polls through a proxy drop now and then; only a lasting outage is logged
                self.log.trouble("getUpdates", tr('Failed to receive Telegram messages: {v0}', v0=exc))
            else:
                self.log.once("getUpdates", tr('getUpdates failed: {v0} (retrying later)', v0=exc))
            return 0.0
        for update in updates:
            update_id = update.get("update_id")
            if not isinstance(update_id, int):
                continue
            if self.state.seen_update(update_id):
                try:
                    self.state.confirm_update(update_id)  # handled before: only move the offset past it
                except OSError as exc:
                    self.log.once("state", tr('Failed to write state.json ({v0}); the getUpdates offset was not updated', v0=exc), "ERROR")
                continue
            try:
                self.state.mark_update(update_id)  # before any side effect: at most once
            except OSError as exc:
                # Kept in memory as seen, so it is skipped rather than run twice.
                self.log.once("state", tr('Failed to write state.json ({v0}); skipping update {v1}', v0=exc, v1=update_id), "ERROR")
                continue
            self._dispatch(update_id, update)
        return None

    def _on_conflict(self) -> float:
        """409 from getUpdates: lock writes only when it repeats; seconds to wait before the next poll.

        A second poller or a webhook makes every retry fail again at once. A single 409 can come
        from a long poll cut off by a network drop (seen once right before a laptop went to
        sleep), so the first one only warns and retries soon.
        """

        now = self.clock()
        repeated = self._conflict_at is not None and now - self._conflict_at <= CONFLICT_CONFIRM_S
        self._conflict_at = now
        if not repeated:
            self.log.line(tr('getUpdates 409: is another program receiving messages with this bot’s token? Writes will lock if it recurs within {v0:.0f} minutes', v0=CONFLICT_CONFIRM_S / 60), "WARNING")
            self._alert("conflict-once", tr('⚠️ <b>[{{m}}]</b> Received one 409 Conflict (another program is receiving with this bot’s token, or a disconnected request remains). Writes will lock if it recurs within {v0:.0f} minutes.', v0=CONFLICT_CONFIRM_S / 60),
                        silent=True)
            return CONFLICT_RETRY_S
        if self.state.locked and self.state.locked_reason != OWNER_LOCK:
            self.log.line(tr('getUpdates 409: another program is still receiving with this bot’s token (locked)'), "WARNING")
        self._lock_writes(tr('Another process is polling with the same bot token (409)'))
        self._alert("conflict", tr('🚨 <b>[{m}]</b> Another process is receiving with this bot’s token (repeated 409 Conflict). If you are not running this bridge elsewhere, the token may have leaked: writes are locked.'))
        return 60.0

    def _dispatch(self, update_id: int, update: dict[str, Any]) -> None:
        def handle() -> None:
            try:
                self.handle_update(update)
            except Exception:  # noqa: BLE001 - one bad update must not stop the bridge
                self.log.line(tr('Error handling update {v0}:\n{v1}', v0=update_id, v1=traceback.format_exc()), "ERROR")

        # Handlers may spawn node or claude.exe (busy checks, project lists); a slow one must not
        # hold back the owner's next tap on an approval.
        if self.synchronous or self._workers is None:
            handle()
        else:
            self._workers.submit(handle)

    def handle_update(self, update: dict[str, Any]) -> None:
        if isinstance(update.get("callback_query"), dict):
            self._on_callback(update["callback_query"])
        elif isinstance(update.get("message"), dict):
            self._on_message(update["message"])
        elif isinstance(update.get("my_chat_member"), dict):
            self._on_my_member(update["my_chat_member"])
        elif isinstance(update.get("chat_member"), dict):
            self._on_chat_member(update["chat_member"])

    # --- membership and intruders ---

    def _allowed_user(self, user: dict[str, Any]) -> bool:
        return (not user.get("is_bot")) and user.get("id") in self.cfg.allowed_user_ids

    def _intruder(self, user: dict[str, Any], what: str) -> None:
        uid = user.get("id")
        name = user.get("username") or user.get("first_name") or ""
        self.log.audit("intruder", user_id=uid, what=what)
        self._lock_writes(tr('User {v0} outside the whitelist {v1}', v0=uid, v1=what))
        self._alert(f"intruder:{uid}", tr('🚨 <b>[{{m}}]</b> User outside the whitelist (id {v0} {v1}) {v2}; writes are locked and pushes show titles only. After checking security, send /unlock@this_bot.', v0=uid, v1=render.esc(str(name)), v2=render.esc(what)))

    def _on_my_member(self, update: dict[str, Any]) -> None:
        chat = update.get("chat") or {}
        status = (update.get("new_chat_member") or {}).get("status")
        if chat.get("id") != self.cfg.chat_id:
            if status in ("member", "administrator", "restricted") and chat.get("type") != "private":
                self.log.audit("left_foreign_chat", chat_id=chat.get("id"))
                try:
                    self.api.leave_chat(chat["id"])
                except TelegramError:
                    pass
            return
        self.log.line(tr('This bot’s group status changed to {v0}', v0=status), "WARNING")

    def _on_chat_member(self, update: dict[str, Any]) -> None:
        if (update.get("chat") or {}).get("id") != self.cfg.chat_id:
            return
        member = update.get("new_chat_member") or {}
        user = member.get("user") or {}
        if member.get("status") not in ("member", "administrator", "restricted", "creator"):
            return
        if user.get("id") == self.me.get("id") or self._allowed_user(user):
            return
        if user.get("is_bot"):
            self._alert(f"bot:{user.get('id')}", tr('ℹ️ <b>[{{m}}]</b> A bot joined the group: @{v0} (remove it unless it is another machine’s bridge bot)', v0=render.esc(str(user.get('username')))), silent=True)
            return
        self._intruder(user, tr('joined the group'))

    def _expected_members(self) -> int:
        return len(self.cfg.allowed_user_ids) + self.cfg.expected_bots  # the owner(s) and one bot per machine

    def _check_members(self) -> None:
        try:
            count = self.api.member_count(self.chat_id)
        except TelegramError:
            return
        expected = self._expected_members()
        if count > expected and (count != self._member_count_alerted or not self.state.locked):
            self._member_count_alerted = count
            self._lock_writes(tr('Group member count {v0} exceeds the expected {v1}', v0=count, v1=expected))
            self._alert("members", tr('🚨 <b>[{{m}}]</b> The group has {v0} members, exceeding the expected {v1}; writes are locked and pushes show titles only. Check the members, then send /unlock@this_bot when safe.', v0=count, v1=expected))

    def _lock_writes(self, reason: str) -> None:
        if not self.state.locked or self.state.locked_reason == OWNER_LOCK:
            self.state.set_locked(True, reason)
            self.log.line(tr('Writes locked: {v0}', v0=reason), "WARNING")
        self._revoke_allow_all(None, "security lock")
        self._settle_open_cards(reason)
        with self.lock:
            self.progress_cards.clear()  # what waited for an edit is not sent any more

    def _content_blocked(self) -> bool:
        """Locked for a security reason (not the owner's /lock): push titles only, refuse reads."""

        return self.state.locked and self.state.locked_reason != OWNER_LOCK

    def _alert(self, key: str, html: str, *, silent: bool = False, every_s: float = 3600.0) -> None:
        now = self.clock()
        if now - self._alerted.get(key, -every_s - 1) < every_s:
            return
        self._alerted[key] = now
        text = html.replace("{m}", render.esc(self.machine))
        self._later(f"alert {key}", lambda: self._send(text, silent=silent))

    # --- inbound messages ---

    def _on_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        if chat.get("id") != self.cfg.chat_id:
            if chat.get("type") in ("group", "supergroup"):
                try:
                    self.api.leave_chat(chat["id"])
                except TelegramError:
                    pass
            return  # private chats and foreign groups get no answer at all
        sender = message.get("from") or {}
        for member in message.get("new_chat_members") or []:
            if isinstance(member, dict) and not member.get("is_bot") and not self._allowed_user(member):
                self._intruder(member, tr('was added to the group'))
        if (sender.get("is_bot") or message.get("sender_chat") or message.get("is_automatic_forward")
                or message.get("via_bot") or sender.get("id") in SYSTEM_SENDERS):
            return
        if not self._allowed_user(sender):
            if sender.get("id") is not None:
                self._intruder(sender, tr('posted in the group'))
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return
        command = parse_command(text)
        key, ours = self._route(message)
        if command is not None:
            name, target_bot, args = command
            if target_bot and target_bot.lower() != str(self.me.get("username") or "").lower():
                return
            self._command(name, args, message, key, ours or bool(target_bot))
            return
        wizard = self._wizard_for_reply(message)
        if wizard is not None:
            self._wizard_prompt(wizard, message, text)
            return
        card = self._question_for_reply(message)
        if card is not None:
            self._question_typed(card, message, text)
            return
        if self._replies_to_closed_card(message):
            # Meant as an answer: sending it to the session as a new message would surprise.
            self._reply(message, tr('This question card is closed; your reply was not sent. To send to the session, type directly in its topic (do not reply to the card).'),
                        silent=True)
            return
        if not ours or key is None:
            return
        if message.get("forward_origin") or message.get("external_reply") or message.get("quote"):
            self._reply(message, tr('Forwarded or quoted messages are not accepted. Type the text you want to send directly.'))
            return
        self._request_send(message, key, text)

    def _route(self, message: dict[str, Any]) -> tuple[str | None, bool]:
        """(session key, whether the message is addressed to this bot)."""

        thread_id = message.get("message_thread_id") if message.get("is_topic_message") else None
        if isinstance(thread_id, int):
            key = self.state.key_for_thread(thread_id)
            if key:
                return key, True
        reply = message.get("reply_to_message")
        if isinstance(reply, dict) and (reply.get("from") or {}).get("id") == self.me.get("id"):
            # Inside a topic this is the topic's creation message; our topics matched above.
            return self.state.key_for_message(reply.get("message_id")), True
        return None, False

    def _thread_of(self, message: dict[str, Any]) -> int | None:
        thread_id = message.get("message_thread_id")
        return thread_id if message.get("is_topic_message") and isinstance(thread_id, int) else None

    def _fresh(self, message: dict[str, Any]) -> bool:
        date = message.get("date")
        return not isinstance(date, int) or self.clock() - date <= self.cfg.write_max_age_s

    # --- commands ---

    def _command(self, name: str, args: str, message: dict[str, Any], key: str | None, ours: bool) -> None:
        self.log.audit("command", command=name, user_id=(message.get("from") or {}).get("id"), key=key,
                       args_len=len(args))
        if not ours and name not in BROADCAST_COMMANDS:
            if name in ("new", "unlock") and self._thread_of(message) is None:
                verb = tr('New session') if name == "new" else tr('Unlock')
                self._reply(message, f"👉 <b>[{render.esc(self.machine)}]</b>",
                            buttons=[[(tr('{v1} on {v0}', v0=self.machine, v1=verb), f"mach:{name}")]], silent=True)
            return
        if name in ("help", "start"):
            self._reply(message, _help(), silent=True)
        elif name == "list":
            self._cmd_list(message, args)
        elif name == "status":
            self._cmd_status(message)
        elif name == "lang":
            self._cmd_lang(message, args)
        elif name == "lock":
            self.state.set_locked(True, OWNER_LOCK)
            revoked = self._revoke_allow_all(None, "lock")
            self._settle_open_cards("/lock")
            note = tr(', and revoked allow-all in {v0} sessions', v0=revoked) if revoked else ""
            self._reply(message, tr('🔒 <b>[{v0}]</b> Writes locked{v1}.', v0=render.esc(self.machine), v1=note), silent=True)
        elif name == "unlock":
            self._confirm_card(message, action="unlock", key="", text="", thread_id=self._thread_of(message))
        elif name == "new":
            self._wizard_start(message, args)
        elif name in SESSION_COMMANDS:
            if key is None:
                self._reply(message, tr('Use this command in a session topic or reply to a push.'))
                return
            if name == "read":
                count = int(args) if args.isdigit() else 3
                self._cmd_read(message, key, max(1, min(count, 10)))
            elif name == "full":
                self._cmd_full(message, key)
            elif name == "stop":
                self._request_stop(message, key)
            elif name in ("mute", "unmute"):
                self.state.set_muted(key, name == "mute")
                self._reply(message, tr('🔕 Pushes muted for this session.') if name == "mute" else tr('🔔 Pushes resumed.'), silent=True)
            elif name == "ask":
                agent, session_id = split_key(key)
                dropped = self._revoke_allow_all(session_id, "/ask") if agent == CLAUDE else 0
                self._reply(message, tr('🔒 Individual approvals restored.') if dropped else tr('Allow-all is not enabled for this session.'), silent=True)
        else:
            self._reply(message, tr('Unknown command. Send /help for usage.'), silent=True)

    def _cmd_lang(self, message: dict[str, Any], value: str) -> None:
        """A local preference, not a session write; available under either lock."""

        if not value:
            user_id = (message.get("from") or {}).get("id")
            self._reply(message, tr('<b>[{machine}]</b> Language: {language}',
                                    machine=render.esc(self.machine), language=current()),
                        buttons=[[("English", f"lang:en:{user_id}"),
                                  ("中文", f"lang:zh:{user_id}")]], silent=True)  # i18n: data - language self-names
            return
        language = normalize(value)
        if language is None:
            self._reply(message, tr('Usage: /lang [en|zh]'), silent=True)
            return
        try:
            with self.lock:
                path = config_path()
                try:
                    saved = json.loads(path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    saved = {}
                if not isinstance(saved, dict):
                    raise ValueError("config.json must contain an object")
                # Re-read the disk copy; never write stale binding/tunables from self.cfg.
                saved["language"] = language
                atomic_write_json(path, saved)
                self.cfg.language = language
                set_language(language)
        except (OSError, ValueError) as exc:
            self.log.line(tr('Failed to save the language: {error}', error=exc), "WARNING")
            self._reply(message, tr('Could not save the language. Check the bridge log.'), silent=True)
            return
        self.log.audit("language_changed", user_id=(message.get("from") or {}).get("id"),
                       machine=self.machine, language=language)
        self._set_commands()
        self._reply(message, tr('<b>[{machine}]</b> Language changed to {language}.',
                                machine=render.esc(self.machine), language=language), silent=True)

    def _sessions(self, limit: int) -> list[SessionView]:
        views: list[SessionView] = []
        for source in (self.codex, self.claude, self.antigravity, self.pi, self.dsh):
            if source is None:
                continue
            try:
                views.extend(source.sessions(limit))
            except Exception as exc:  # noqa: BLE001 - one source failing must not hide the other
                self.log.line(tr('Failed to read the session list: {v0}: {v1}', v0=type(exc).__name__, v1=exc), "WARNING")
        rank = {"running": 0, "waiting": 0}
        views.sort(key=lambda view: (rank.get(view.state, 1), -view.last_activity))
        return views[:limit]

    def _view(self, key: str) -> SessionView | None:
        agent, session_id = split_key(key)
        source = self._source(agent)
        if source is None:
            return None
        try:
            return source.view(session_id)
        except Exception:  # noqa: BLE001
            return None

    def _topic_id(self, key: str) -> int | None:
        topic = self.state.topic_for(key)
        if not topic or topic.get("closed"):
            return None
        thread_id = int(topic.get("thread_id") or 0)
        return thread_id if thread_id > 0 else None

    def _cmd_list(self, message: dict[str, Any], args: str) -> None:
        limit = int(args) if args.isdigit() else self.cfg.recent_n
        views = self._sessions(max(1, min(limit, 30)))
        now = self.clock()
        head = tr('<b>[{v0}] {v1} recent sessions</b>', v0=render.esc(self.machine), v1=len(views))
        if not views:
            self._reply(message, head + tr('\n(none)'), silent=True)
            return
        lines = [head] + [f"{index}. {render.session_line(view, now, redact=self.redact)}"
                          for index, view in enumerate(views, 1)]
        buttons: list[list[tuple[str, str]]] = []
        for index, view in enumerate(views[:10], 1):
            label = f"{index}. {self.redact(view.title)}"[:40]
            thread_id = self._topic_id(view.key)
            if thread_id:
                buttons.append([(label, "url:" + topic_link(self.chat_id, thread_id))])
            else:
                buttons.append([(label, f"open:{view.key}")])
        self._reply(message, "\n".join(lines), buttons=buttons, silent=True)

    def _cmd_status(self, message: dict[str, Any]) -> None:
        status = self.status()
        claude_live = 0
        if self.claude is not None:
            try:
                claude_live = sum(1 for view in self.claude.sessions(50) if view.live)
            except Exception:  # noqa: BLE001
                claude_live = -1
        codex_running = "?"
        if self.codex is not None:
            try:
                codex_running = tr('running') if self.codex.desktop_running() else tr('stopped')
            except Exception:  # noqa: BLE001
                pass
        lock = tr('🔓 Available')
        if status["locked"]:
            lock = tr('🔒 Locked ({v0})', v0=render.esc(self.redact(self.state.locked_reason)))
        lines = [
            tr('<b>[{v0}] Status</b>', v0=render.esc(self.machine)),
            tr('Writes: {v0}', v0=lock),
            tr('Codex Desktop: {status}', status=codex_running),
        ]
        remote = status.get("codex_remote")
        if remote is not None:
            line = tr('Remote Codex (view-only): {v0} conversations, hosts {v1}', v0=remote['threads'], v1=render.esc(tr(', ').join(remote['hosts']) or tr('none')))
            if remote["unavailable"]:
                line += tr('; unreachable: {v0}', v0=render.esc(tr(', ').join(remote['unavailable'])))
            if remote["last_error"]:
                line += tr('; latest error: {v0}', v0=render.esc(self.redact(remote['last_error'])[:200]))
            lines.append(line)
        lines += [
            tr('Active Claude sessions: {v0}', v0=claude_live),
            tr('Pending approvals: {v0} · Questions awaiting your answer: {v1} · Allow-all sessions: {v2} · Tracked deliveries: {v3} · Background Claude runs: {v4}', v0=status['pending_permissions'], v1=status['pending_questions'], v2=status['allow_all'], v3=status['deliveries'], v4=status['headless_runs']),
            tr('Queued pushes: {v0} · At PC: {v1}', v0=status['outbox'], v1=tr('yes') if self.presence.at_pc() else tr('no')),
            tr('Started: {v0:%m-%d %H:%M}', v0=datetime.fromtimestamp(self.started_at)),
        ]
        if self.antigravity is not None:
            try:
                info = self.antigravity.status()
                stores = tr(', ').join(str(store) for store in info.get("stores") or []) or tr('not found')
                line = tr('Antigravity (view-only): {v0}, {v1} conversations', v0=stores, v1=info.get('conversations', 0))
                if info.get("last_error"):
                    line += tr('; latest error: {v0}', v0=self.redact(str(info['last_error']))[:200])
            except Exception as exc:  # noqa: BLE001 - status must not hide the bridge state
                line = tr('Antigravity: failed to read status: {v0}', v0=self.redact(str(exc))[:200])
            lines.insert(3, line)
        for label, src in (("DSH", self.dsh), ("Pi", self.pi)):
            if src is not None:
                try:
                    info = src.status()
                    line = tr('{v0} (view-only): {v1} conversations', v0=label, v1=info.get('conversations', 0))
                    if info.get("last_error"):
                        line += tr('; latest error: {v0}', v0=self.redact(str(info['last_error']))[:200])
                except Exception as exc:  # noqa: BLE001 - status must not hide the bridge state
                    line = tr('{v0}: failed to read status: {v1}', v0=label, v1=self.redact(str(exc))[:200])
                lines.insert(4, line)
        if self._quota_shown():
            lines += self._quota_lines() or [tr('📊 Quota: no data yet (available after a Codex turn or a Claude Desktop quota record)')]
        self._reply(message, "\n".join(lines), silent=True)

    def _excluded_session(self, key: str, *cwds: str) -> bool:
        view = self._view(key)
        paths = [path for path in (*cwds, view.cwd if view else "") if path]
        return any(self.cfg.excluded(path) for path in paths)

    def _cmd_read(self, message: dict[str, Any], key: str, count: int) -> None:
        agent, session_id = split_key(key)
        source = self._source(agent)
        if source is None:
            self._reply(message, tr('Unknown session.'), silent=True)
            return
        if self._content_blocked():
            self._reply(message, tr('🔒 The bridge is security-locked; session content is not being sent.'), silent=True)
            return
        if self._excluded_session(key):
            self._reply(message, tr('This session’s directory is in content_exclude; its content is not sent to Telegram.'), silent=True)
            return
        try:
            turns = source.read_turns(session_id, count)
        except Exception as exc:  # noqa: BLE001
            self._reply(message, tr('Failed to read: {v0}', v0=render.esc(self.redact(str(exc)))[:800]), silent=True)
            return
        if not turns:
            self._reply(message, tr('No recent turns found.'), silent=True)
            return
        blocks = []
        for index, turn in enumerate(turns, 1):
            data = turn if isinstance(turn, dict) else vars(turn)
            status = str(data.get("status") or "")
            user = self.redact(str(data.get("user_text") or ""))
            final = data.get("final_message") if "final_message" in data else data.get("final_text")
            body = self.redact(str(final or data.get("error") or ""))
            commentary = data.get("commentary") or []
            lines = [tr('{v0} Turn {v1}', v0=render.status_icon(status), v1=index), tr('[User] {v0}', v0=user[:600])]
            if commentary and not final:
                lines.append(tr('[Progress]') + self.redact(" / ".join(str(item) for item in commentary[-3:]))[:600])
            lines.append(tr('[Reply] {v0}', v0=body))
            blocks.append("\n".join(lines))
        text = "\n\n".join(blocks)
        if render.utf16_len(text) <= 3500:
            self._reply(message, render.esc(text), silent=True)
        else:
            self._reply_document(message, f"{session_id[:8]}-recent.md", text)

    def _full_text(self, key: str) -> str | None:
        with self.lock:
            if key in self.last_text:
                return self.last_text[key]
        agent, session_id = split_key(key)
        source = self._source(agent)
        if source is None:
            return None
        try:
            return source.full_text(session_id)
        except Exception:  # noqa: BLE001
            return None

    def _cmd_full(self, message: dict[str, Any], key: str) -> None:
        agent, _session_id = split_key(key)
        if self._source(agent) is None:
            self._reply(message, tr('Unknown session.'), silent=True)
            return
        if self._content_blocked():
            self._reply(message, tr('🔒 The bridge is security-locked; session content is not being sent.'), silent=True)
            return
        if self._excluded_session(key):
            self._reply(message, tr('This session’s directory is in content_exclude; its content is not sent to Telegram.'), silent=True)
            return
        text = self._full_text(key)
        if not text:
            self._reply(message, tr('No reply found for the latest turn.'), silent=True)
            return
        text = self.redact(text)
        if render.utf16_len(text) <= 3500:
            self._reply(message, render.esc(text), silent=True)
        else:
            self._reply_document(message, f"{split_key(key)[1][:8]}-reply.md", text)

    def _remember_text(self, key: str, text: str) -> None:
        with self.lock:
            self.last_text[key] = text
            self.last_text.move_to_end(key)
            while len(self.last_text) > 300:
                self.last_text.popitem(last=False)

    # --- sends (write, confirmed) ---

    def _write_in_flight(self, key: str) -> bool:
        """One unfinished write per session: a card waiting for a tap or a send being followed."""

        with self.lock:
            return (any(item.key == key and item.action in ("send", "stop", "implement")
                        for item in self.pending.values())
                    or any(item.key == key for item in self.deliveries))

    def _request_send(self, message: dict[str, Any], key: str, text: str) -> None:
        agent, session_id = split_key(key)
        if agent in READONLY_AGENTS:
            label = render.agent_label(agent)
            self._reply(message, tr('{v0} conversations are view-only; reply in {v1}.', v0=label, v1=label), silent=True)
            return
        if self._source(agent) is None:
            self._reply(message, tr('Unknown session.'), silent=True)
            return
        if self.state.locked:
            self._reply(message, tr('🔒 Writes are locked. Send /unlock@this_bot to unlock.'))
            return
        if not self._fresh(message):
            self._reply(message, tr('⌛ This message is too old (sent while the bridge was offline). Send it again.'))
            return
        if self._write_in_flight(key):
            self._reply(message, tr('The previous message to this session is still pending confirmation or delivery. Try again later.'))
            return
        view = self._view(key)
        title = view.title if view else session_id[:8]
        note = ""
        extra: dict[str, Any] = {}
        if agent == CODEX:
            if self.codex is None:
                self._reply(message, tr('Codex is not enabled on this machine.'))
                return
            if view is not None and view.kind.startswith("remote:"):
                self._reply(message, tr('This Codex conversation is on a remote host; it is view-only and cannot receive messages from Telegram.'))
                return
            if view is not None and not view.live:
                self._reply(message, tr('This conversation cannot receive messages (it is not a local Codex Desktop conversation, or Codex Desktop is not running).'))
                return
            busy = self.codex.busy(session_id)
            extra["busy"] = busy is not False  # unknown counts as busy: offer both buttons
            if extra["busy"]:
                note = (tr('The conversation is running this turn.') if busy else tr('Could not confirm whether the conversation is idle.')) + (
                    tr('Queue: the bridge sends after this turn ends (up to 4 hours; invisible in Codex’s own queue). Steer: insert immediately into the running turn for it to handle.'))
        elif agent == CLAUDE:
            if self.claude is None:
                self._reply(message, tr('Claude is not enabled on this machine.'))
                return
            if self.claude.live(session_id) is not None:
                if self.claude.inbox_for(session_id) is None:
                    self._reply(message, tr('This session has not reported its inbox to the bridge. It must start or end at least one turn after installing hooks before messages can be sent.'))
                    return
                extra["mode"] = "inbox"
                if view is not None and view.state in ("running", "waiting"):
                    extra["busy"] = True
                    note = (tr('The session is running this turn. Queue: run as a new turn after this one ends. Steer: insert after the next tool call (Claude sees a message from another session and usually finishes its current work first).'))
            else:
                problem = self._headless_problem()
                if problem:
                    self._reply(message, tr('The session is not running. {v0}', v0=problem))
                    return
                extra["mode"] = "headless"
                note = tr('The session is not running; continue it in the background with claude -p --resume (reopen the session in Desktop to see this turn).')
        else:
            return
        if self._open_question(key, blocking_only=True) is not None:
            note = (tr('This session is waiting for your answer: tap the question card’s buttons or reply to that card. This message will be handled after the question is answered.') + (note and "\n" + note))
        pending = self._confirm_card(message, action="send", key=key, text=text, title=title, note=note,
                                     thread_id=self._thread_of(message), extra=extra, busy=bool(extra.get("busy")))
        if pending is not None:
            self.log.audit("send_requested", key=key, text_sha=text_digest(text), text_len=len(text))

    def _owned_run(self, session_id: str) -> headless.HeadlessRun | None:
        with self.lock:
            run = self.runs.get(session_id)
        return run if run is not None and headless.run_alive(run) else None

    def _request_stop(self, message: dict[str, Any], key: str) -> None:
        agent, session_id = split_key(key)
        if agent in READONLY_AGENTS:
            label = render.agent_label(agent)
            self._reply(message, tr('{v0} conversations are view-only; reply in {v1}.', v0=label, v1=label), silent=True)
            return
        if self._source(agent) is None:
            self._reply(message, tr('Unknown session.'), silent=True)
            return
        if agent == CODEX:
            self._reply(message, tr('Codex Desktop does not support remote interruption. Click Stop on the PC.'), silent=True)
            return
        if self.state.locked:
            self._reply(message, tr('🔒 Writes are locked. Send /unlock@this_bot to unlock.'))
            return
        view = self._view(key)
        if self._owned_run(session_id) is None and (view is None or view.state != "running"):
            self._reply(message, tr('The session is not running; no interruption is needed.'), silent=True)
            return
        self._confirm_card(message, action="stop", key=key, text="", title=view.title if view else session_id[:8],
                           thread_id=self._thread_of(message))

    def _headless_problem(self) -> str:
        with self.lock:
            runs = list(self.runs.values())
            cached = self._auth_cache
        alive = sum(1 for run in runs if headless.run_alive(run))
        if alive >= self.cfg.claude_max_headless:
            return tr('There are already {v0} background Claude runs (limit {v1}).', v0=alive, v1=self.cfg.claude_max_headless)
        now = self.clock()
        if cached is not None and now - cached[0] < (AUTH_CACHE_S if not cached[1] else 60.0):
            return cached[1]
        exe = headless.find_claude_exe(self.cfg.claude_exe)
        if exe is None:
            problem = tr('claude.exe was not found on this machine; cannot run in the background.')
        elif not headless.auth_status(exe).get("loggedIn"):
            problem = tr('The local Claude CLI is not logged in (run claude in a terminal, then /login); cannot run in the background.')
        else:
            problem = ""
        with self.lock:
            self._auth_cache = (now, problem)
        return problem

    def _confirm_card(self, message: dict[str, Any], *, action: str, key: str, text: str, title: str = "",
                      note: str = "", thread_id: int | None, extra: dict[str, Any] | None = None,
                      busy: bool = False) -> Pending | None:
        user_id = int((message.get("from") or {}).get("id") or 0)
        nonce = secrets.token_urlsafe(9)
        agent = split_key(key)[0] if key else ""
        if action == "unlock":
            html = tr('🔓 Confirm unlocking writes on <b>[{v0}]</b>?', v0=render.esc(self.machine))
        else:
            html = render.confirm_card(machine=self.machine, agent=agent or CODEX, title=self.redact(title),
                                       action=action, text=self.redact(text))
        if note:
            html += f"\n<i>{render.esc(note)}</i>"
        if busy:  # a running turn: queue after it ("ok") or steer into it ("now")
            buttons = [[(tr('📥 Queue: send after this turn'), f"ok:{nonce}"), (tr('↪️ Steer: insert into this turn'), f"now:{nonce}")],
                       [(tr('✖ Cancel'), f"no:{nonce}")]]
        else:
            label = {"send": tr('✅ Send'), "stop": tr('⏹ Interrupt'), "new": tr('✅ Create'), "unlock": tr('🔓 Unlock')}.get(action, tr('✅ Confirm'))
            buttons = [[(label, f"ok:{nonce}"), (tr('✖ Cancel'), f"no:{nonce}")]]
        pending = Pending(nonce, action, key, user_id, self.clock(), text=text, thread_id=thread_id, extra=extra or {})
        with self.lock:
            self._expire_pending()
            self.pending[nonce] = pending  # before sending: a quick tap must find it
        try:
            sent = self.api.send_message(self.chat_id, html, thread_id=thread_id, reply_to=message.get("message_id"),
                                         buttons=buttons)
        except TelegramError as exc:
            with self.lock:
                self.pending.pop(nonce, None)
            self.log.line(tr('Failed to send confirmation card: {v0}', v0=exc), "WARNING")
            return None
        pending.message_id = sent.get("message_id")
        return pending

    def _expire_pending(self) -> None:
        now = self.clock()
        for nonce, item in list(self.pending.items()):
            if now - item.created > CONFIRM_TTL_S:
                del self.pending[nonce]
                self._edit_later(item.message_id, tr('⌛ Confirmation expired; nothing executed.'))

    # --- callbacks ---

    def _on_callback(self, query: dict[str, Any]) -> None:
        data = str(query.get("data") or "")
        message = query.get("message") or {}
        sender = query.get("from") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if chat_id != self.cfg.chat_id or not self._allowed_user(sender):
            self._answer(query, tr('Not authorized'))
            if chat_id == self.cfg.chat_id and sender.get("id") is not None and not sender.get("is_bot"):
                self._intruder(sender, tr('tapped a button'))
            return
        action, _, argument = data.partition(":")
        self.log.audit("callback", action=action, user_id=sender.get("id"))
        if action in ("ok", "now", "no"):
            self._answer(query, self._on_confirm(argument, action, message, sender))
        elif action == "lang":
            language, _, owner = argument.partition(":")
            if (owner != str(sender.get("id"))
                    or (message.get("from") or {}).get("id") != self.me.get("id")):
                self._answer(query, tr('Not authorized'))
                return
            self._answer(query)
            self._cmd_lang(self._as_message(message, sender), language)
        elif action == "perm":
            choice, _, nonce = argument.partition(":")
            self._answer(query, self._on_permission_choice(nonce, choice, sender))
        elif action == "ca":
            choice, _, nonce = argument.partition(":")
            self._answer(query, self._on_codex_approval_choice(nonce, choice, sender, message))
        elif action == "q":
            choice, _, nonce = argument.partition(":")
            self._answer(query, self._on_question_choice(nonce, choice, sender, message))
        elif action == "pl":
            self._answer(query, self._request_implement(argument, self._as_message(message, sender)))
        elif action == "aa":
            _, _, session_id = argument.partition(":")
            dropped = self._revoke_allow_all(session_id, "button")
            self._answer(query, tr('Individual approvals restored') if dropped else tr('Allow-all is no longer active'))
            if message.get("message_id"):
                self._edit(message["message_id"], tr('🔒 Allow-all revoked; this session now uses individual approvals.'))
        elif action in ("full", "read"):
            self._answer(query)
            if action == "full":
                self._cmd_full(self._as_message(message), argument)
            else:
                self._cmd_read(self._as_message(message), argument, 3)
        elif action == "open":
            self._answer(query)
            self._open_topic(argument, message)
        elif action == "mach":
            self._answer(query)
            fake = self._as_message(message, sender)
            if argument == "new":
                self._wizard_start(fake, "")
            elif argument == "unlock":
                self._confirm_card(fake, action="unlock", key="", text="", thread_id=self._thread_of(message))
        elif action == "nw":
            self._answer(query)
            wid, _, index = argument.partition(":")
            self._wizard_choice(wid, index, sender, message)
        else:
            self._answer(query, tr('Button expired'))

    def _as_message(self, message: dict[str, Any], sender: dict[str, Any] | None = None) -> dict[str, Any]:
        """A button's message as an incoming one from ``sender``: replies go under it, in its topic."""

        fake = {"chat": {"id": self.chat_id}, "message_id": message.get("message_id"),
                "message_thread_id": message.get("message_thread_id"), "is_topic_message": message.get("is_topic_message")}
        if sender is not None:
            fake["from"] = sender
        return fake

    def _answer(self, query: dict[str, Any], text: str = "") -> None:
        try:
            self.api.answer_callback(str(query.get("id")), text)
        except TelegramError:
            pass

    def _edit(self, message_id: int | None, html: str, buttons: list[list[tuple[str, str]]] | None = None) -> None:
        if not message_id:
            return
        try:
            self.api.edit_message(self.chat_id, message_id, html, buttons=buttons)
        except TelegramError as exc:
            self.log.line(tr('Failed to edit message: {v0}', v0=exc), "WARNING")

    def _on_confirm(self, nonce: str, choice: str, message: dict[str, Any], sender: dict[str, Any]) -> str:
        card = message.get("message_id")
        with self.lock:
            pending = self.pending.get(nonce)
            if pending is not None and (pending.user_id != sender.get("id")
                                        or (pending.message_id is not None and card != pending.message_id)):
                return tr('Only the requester can confirm on the original card')
            self.pending.pop(nonce, None)
        if pending is None:
            self._edit(card, tr('Button expired (already handled, timed out, or bridge restarted).'))
            return ""
        if self.clock() - pending.created > CONFIRM_TTL_S:
            self._edit(card, tr('⌛ Confirmation expired; nothing executed.'))
            return ""
        if choice == "no":
            self._edit(card, tr('✖ Canceled.'))
            self.log.audit("cancelled", key=pending.key, action=pending.action)
            return ""
        if self.state.locked and pending.action != "unlock":
            self._edit(card, tr('🔒 Writes locked; nothing executed.'))
            return ""
        self._edit(card, tr('⏳ Processing…'))
        self.log.audit("confirmed", key=pending.key, action=pending.action, choice=choice,
                       text_sha=text_digest(pending.text) if pending.text else None)
        self._start_exec(self._execute_safely, pending, choice)
        return ""

    def _start_exec(self, target: Callable[..., None], *args: Any, _reserved: bool = False) -> None:
        """Run a write on its own tgbridge-exec thread, counted in status()["executing"] meanwhile.

        Between the tap and the delivery being tracked nothing else shows the write, and an
        Install restarting the bridge then would cut it off halfway (a plan's mode switched
        without the plan sent). Question confirmations reserve the count before their
        Telegram status edit and pass _reserved=True when starting the worker.
        """

        def run() -> None:
            try:
                target(*args)
            finally:
                with self.lock:
                    self._executing -= 1

        if not _reserved:
            with self.lock:
                self._executing += 1
        try:
            threading.Thread(target=run, name="tgbridge-exec", daemon=True).start()
        except BaseException:
            with self.lock:
                self._executing -= 1
            raise

    def _execute_safely(self, pending: Pending, choice: str) -> None:
        try:
            self._execute(pending, choice)
        except Exception as exc:  # noqa: BLE001
            self.log.line(tr('Failed to execute {v0}:\n{v1}', v0=pending.action, v1=traceback.format_exc()), "ERROR")
            self._edit(pending.message_id, tr('❌ Failed: {v0}', v0=render.esc(self.redact(str(exc)))[:800]))

    def _execute(self, pending: Pending, choice: str) -> None:
        if pending.action == "unlock":
            self._execute_unlock(pending)
            return
        if pending.action == "new":
            self._execute_new(pending)
            return
        agent, session_id = split_key(pending.key)
        if agent in READONLY_AGENTS:
            label = render.agent_label(agent)
            self._edit(pending.message_id, tr('{v0} conversations are view-only; reply in {v1}.', v0=label, v1=label))
            return
        if self._source(agent) is None:
            self._edit(pending.message_id, tr('Unknown session.'))
            return
        if pending.action == "stop":
            self._execute_stop(pending, session_id)
            return
        if pending.action == "implement":
            self._execute_implement(pending, session_id)
            return
        marker = f"[tg:{pending.nonce[:6]}]"
        # On a running turn "ok" is [Queue] (after the turn) and "now" is [Steer] (into the turn).
        queued = bool(pending.extra.get("busy")) and choice != "now"
        if agent == CODEX:
            job_id = self.codex.submit_send(session_id, pending.text, nonce=marker,
                                            if_busy="send" if choice == "now" else "wait")
            self._track(Delivery("codex_job", pending.key, self.clock(), pending.message_id, pending.thread_id,
                                 marker=marker, job_id=job_id, text=pending.text, queued=queued))
            self._edit(pending.message_id, tr('📥 Queued: the bridge will send to Codex after this turn ends…') if queued else
                       tr('↪️ Submitted to Codex, inserting into the running turn…') if choice == "now" else tr('📨 Submitted to Codex, awaiting delivery…'))
        elif pending.extra.get("mode") == "headless":
            run = self._start_headless(session_id, pending.text, resume=True, cwd=None)
            self._track(Delivery("headless", pending.key, self.clock(), pending.message_id, pending.thread_id,
                                 run=run, text=pending.text))
            self._edit(pending.message_id, tr('🚀 Continued this session in the background with claude -p --resume…'))
        else:
            if queued:
                info = self.claude.deliver(session_id, self._wrap_claude(marker, pending.text), priority="later")
            else:
                info = self.claude.deliver(session_id, self._wrap_claude(marker, pending.text))
            self._track(Delivery("claude_inbox", pending.key, self.clock(), pending.message_id, pending.thread_id,
                                 marker=marker, info=info, text=pending.text, queued=queued))
            self._edit(pending.message_id, tr('📥 Added to the session queue, awaiting confirmation…') if queued else tr('📨 Delivered to the session inbox, awaiting confirmation…'))

    @staticmethod
    def _wrap_claude(marker: str, text: str) -> str:
        # Only a neutral marker: any local session can post to this inbox, so an in-band claim of
        # owner authority would be copied by exactly the messages it should not cover.
        return f"{marker}（经 Telegram 发送）\n{text}"  # i18n: data - model protocol text; preserve verbatim

    def _execute_unlock(self, pending: Pending) -> None:
        try:
            count = self.api.member_count(self.chat_id)
        except TelegramError:
            count = None
        if count is not None and count > self._expected_members():
            self._edit(pending.message_id, tr('🔒 The group still has {v0} members (expected {v1}); remove the extra members before unlocking.', v0=count, v1=self._expected_members()))
            return
        self.state.set_locked(False, "owner /unlock")
        self._edit(pending.message_id, tr('🔓 <b>[{v0}]</b> Writes unlocked.', v0=render.esc(self.machine)))

    def _execute_stop(self, pending: Pending, session_id: str) -> None:
        run = self._owned_run(session_id)
        if run is not None:
            stopped = headless.stop_run(run)
            self._edit(pending.message_id, tr('⏹ Background run ended.') if stopped else tr('⚠️ Could not end the background run.'))
            return
        view = self._view(pending.key)
        if view is None or view.state != "running":
            self._edit(pending.message_id, tr('The session is no longer running.'))
            return
        with self.lock:
            self.stop_flags[session_id] = self.clock()
            self.stop_hits.pop(session_id, None)
        self._edit(pending.message_id, tr('⏹ Interruption requested: the session will stop at its next tool call (the current command will finish first).'))

    def _execute_implement(self, pending: Pending, session_id: str) -> None:
        """Codex Desktop's "implement this plan": Default mode, then the plan as a new turn."""

        marker = f"[tg:{pending.nonce[:6]}]"
        job_id = self.codex.implement_plan(session_id, str(pending.extra.get("turn_id") or ""), nonce=marker)
        self._track(Delivery("codex_job", pending.key, self.clock(), pending.message_id, pending.thread_id,
                             marker=marker, job_id=job_id, text=tr('(implement plan)')))
        self.log.audit("plan_implement_sent", key=pending.key, job_id=job_id)
        self._edit(pending.message_id, tr('🚀 Switched back to default mode and sent the plan to Codex for implementation…'))

    def _track(self, delivery: Delivery) -> None:
        with self.lock:
            self.deliveries.append(delivery)

    # --- /new ---

    def _wizard_start(self, message: dict[str, Any], args: str) -> None:
        if self.state.locked:
            self._reply(message, tr('🔒 Writes are locked. Send /unlock@this_bot to unlock.'))
            return
        user_id = int((message.get("from") or {}).get("id") or 0)
        wizard = Wizard(secrets.token_hex(4), user_id, self.clock(), self._thread_of(message))
        with self.lock:
            self._expire_wizards()
            self.wizards[wizard.wid] = wizard
        agent = args.strip().lower()
        if agent in (CODEX, CLAUDE):
            self._wizard_agent(wizard, agent, message)
            return
        wizard.options = [("Codex", CODEX), ("Claude", CLAUDE)]
        self._reply(message, tr('🆕 <b>[{v0}]</b> Which type of session do you want to create?', v0=render.esc(self.machine)),
                    buttons=[[(label, f"nw:{wizard.wid}:{index}") for index, (label, _) in enumerate(wizard.options)]])

    def _expire_wizards(self) -> None:
        now = self.clock()
        for wid, wizard in list(self.wizards.items()):
            if now - wizard.created > CONFIRM_TTL_S:
                del self.wizards[wid]

    def _wizard_choice(self, wid: str, index: str, sender: dict[str, Any], message: dict[str, Any]) -> None:
        with self.lock:
            wizard = self.wizards.get(wid)
        if wizard is None or wizard.user_id != sender.get("id") or not index.isdigit() or int(index) >= len(wizard.options):
            self._edit(message.get("message_id"), tr('This wizard expired. Send /new again.'))
            return
        label, value = wizard.options[int(index)]
        self._edit(message.get("message_id"), f"🆕 <b>[{render.esc(self.machine)}]</b> {render.esc(label)}")
        if not wizard.agent:
            self._wizard_agent(wizard, value, message)
        else:
            wizard.target, wizard.target_label = value, label
            self._wizard_ask_prompt(wizard, message)

    def _wizard_agent(self, wizard: Wizard, agent: str, message: dict[str, Any]) -> None:
        if agent not in (CODEX, CLAUDE):
            self.log.line(tr('Refusing to add an unknown agent to the wizard: {v0}', v0=agent), "WARNING")
            with self.lock:
                self.wizards.pop(wizard.wid, None)
            self._reply(message, tr('Unknown session.'))
            return
        wizard.agent = agent
        if agent == CODEX:
            if self.codex is None:
                self._reply(message, tr('Codex is not enabled on this machine.'))
                return
            if not self.codex.desktop_running():
                self._reply(message, tr('Codex Desktop is not running; cannot create a task.'))
                return
            try:
                projects = self.codex.list_projects()
            except Exception as exc:  # noqa: BLE001
                self.log.line(tr('list_projects failed: {v0}', v0=exc), "WARNING")
                projects = []
            options = []
            for project in projects[:8]:
                pid = str(project.get("projectId") or project.get("id") or "")
                name = str(project.get("name") or project.get("title") or pid)
                if pid:
                    options.append((self.redact(name)[:40], pid))
            options.append((tr('No project'), ""))
            wizard.options = options
            question = tr('Which project should the new Codex task use?')
        else:
            if self.claude is None:
                self._reply(message, tr('Claude is not enabled on this machine.'))
                return
            problem = self._headless_problem()
            if problem:
                self._reply(message, problem)
                return
            wizard.options = [(self.redact(Path(path).name or path)[:40], path) for path in self._claude_dirs()]
            if not wizard.options:
                self._reply(message, tr('No available directories (roots) in the configuration.'))
                return
            question = tr('Which directory should Claude start in?')
        rows = [[(label, f"nw:{wizard.wid}:{index}")] for index, (label, _) in enumerate(wizard.options)]
        self._reply(message, f"🆕 <b>[{render.esc(self.machine)}]</b> {question}", buttons=rows)

    def _claude_dirs(self) -> list[str]:
        """Allowed working directories: the configured roots and recently used ones below them."""

        roots = [Path(root) for root in self.cfg.roots if Path(root).is_dir()]
        found: list[str] = [str(root) for root in roots]

        def inside(path: Path) -> bool:
            return any(path == root or root in path.parents for root in roots)

        if self.claude is not None:
            try:
                for view in self.claude.sessions(30):
                    path = Path(view.cwd) if view.cwd else None
                    if path and path.is_dir() and inside(path) and str(path) not in found:
                        found.append(str(path))
            except Exception:  # noqa: BLE001
                pass
        return found[:10]

    def _wizard_ask_prompt(self, wizard: Wizard, message: dict[str, Any]) -> None:
        where = wizard.target_label or (tr('No project') if wizard.agent == CODEX else wizard.target)
        sent = self._reply(message, tr('✍️ Reply to this message with the new {v0} session’s task (<b>[{v1}]</b> {v2}).', v0=render.agent_label(wizard.agent), v1=render.esc(self.machine), v2=render.esc(self.redact(where))))
        if sent:
            wizard.prompt_message_id = sent.get("message_id")

    def _wizard_for_reply(self, message: dict[str, Any]) -> Wizard | None:
        reply = message.get("reply_to_message")
        if not isinstance(reply, dict) or (reply.get("from") or {}).get("id") != self.me.get("id"):
            return None
        with self.lock:
            for wizard in self.wizards.values():
                if wizard.prompt_message_id and wizard.prompt_message_id == reply.get("message_id"):
                    return wizard
        return None

    def _wizard_prompt(self, wizard: Wizard, message: dict[str, Any], text: str) -> None:
        if (message.get("from") or {}).get("id") != wizard.user_id:
            return  # another member's reply must not consume the owner's wizard
        with self.lock:
            self.wizards.pop(wizard.wid, None)
        if self.state.locked:
            self._reply(message, tr('🔒 Writes are locked. Send /unlock@this_bot to unlock.'))
            return
        if not self._fresh(message):
            self._reply(message, tr('⌛ This message is too old. Send /new again.'))
            return
        title = wizard.target_label or wizard.target or tr('New conversation')
        self._confirm_card(message, action="new", key=session_key(wizard.agent, ""), text=text, title=title,
                           thread_id=self._thread_of(message),
                           extra={"agent": wizard.agent, "target": wizard.target, "target_label": wizard.target_label})

    def _execute_new(self, pending: Pending) -> None:
        now = self.clock()
        with self.lock:
            while self.new_times and now - self.new_times[0] > 3600:
                self.new_times.popleft()
            limited = len(self.new_times) >= NEW_PER_HOUR
            if not limited:
                self.new_times.append(now)
        if limited:
            self._edit(pending.message_id, tr('At most {v0} new sessions per hour. Try again later.', v0=NEW_PER_HOUR))
            return
        agent = pending.extra.get("agent")
        target = str(pending.extra.get("target") or "")
        if agent in READONLY_AGENTS:
            label = render.agent_label(str(agent))
            self._edit(pending.message_id, tr('{v0} conversations are view-only; reply in {v1}.', v0=label, v1=label))
            return
        if agent not in (CODEX, CLAUDE):
            self._edit(pending.message_id, tr('Unknown session.'))
            return
        if agent == CODEX:
            result = self.codex.create_thread(pending.text, project_id=target or None, title=None)
            thread = result.get("thread_id") or result.get("threadId") or result.get("id") or ""
            self._edit(pending.message_id, tr('✅ Created a task in Codex Desktop') + (tr(' ({thread})', thread=render.esc(str(thread))) if thread else "")
                       + tr('; it will be pushed to its topic after this turn ends.'))
            return
        if target not in self._claude_dirs():
            self._edit(pending.message_id, tr('The directory is outside the allowed roots.'))
            return
        session_id = str(uuid.uuid4())
        run = self._start_headless(session_id, pending.text, resume=False, cwd=target)
        key = session_key(CLAUDE, session_id)
        title = pending.text.strip().splitlines()[0][:40] if pending.text.strip() else session_id[:8]
        thread_id = self._topic(key, self.redact(title), CLAUDE)
        self._track(Delivery("headless", key, now, pending.message_id, thread_id, run=run, text=pending.text))
        link = tr(': {detail}', detail=topic_link(self.chat_id, thread_id)) if thread_id else ""
        self._edit(pending.message_id, tr('🚀 Started Claude session {v0}{v1} in the background', v0=session_id[:8], v1=render.esc(link)))

    @staticmethod
    def _remote_mode(mode: str, *, bypass_allowed: bool) -> str:
        """Permission mode for a run started from Telegram: at most acceptEdits unless configured."""

        if mode == "bypassPermissions" and bypass_allowed:
            return mode
        if mode == "manual":
            return "default"
        return mode if mode in _REMOTE_MODES else "acceptEdits"

    def _start_headless(self, session_id: str, prompt: str, *, resume: bool, cwd: str | None) -> headless.HeadlessRun:
        with self.lock:
            previous = self.runs.get(session_id)
            if session_id in self._starting or (previous is not None and headless.run_alive(previous)):
                raise RuntimeError(tr('This session already has a background run. Wait for it to finish before sending again.'))
            self._starting.add(session_id)
        try:
            if resume and self.claude.live(session_id) is not None:
                # Opened on the PC since the card was shown: a second writer would fork the transcript.
                raise RuntimeError(tr('The session is already open on the PC. Send again to deliver directly to its inbox.'))
            problem = self._headless_problem()
            if problem:
                raise RuntimeError(problem)
            exe = headless.find_claude_exe(self.cfg.claude_exe)
            if exe is None:
                raise RuntimeError(tr('claude.exe was not found on this machine.'))
            if resume:
                view = self.claude.view(session_id)
                cwd = view.cwd if view is not None and view.cwd else cwd
                mode = self.claude.permission_mode(session_id)
            else:
                mode = "acceptEdits"
            mode = self._remote_mode(mode, bypass_allowed=self.cfg.claude_remote_bypass_allowed)
            if not cwd or not Path(cwd).is_dir():
                raise RuntimeError(tr('The session’s working directory was not found; cannot run in the background.'))
            log_dir = tg_dir() / "headless"
            log_dir.mkdir(parents=True, exist_ok=True)
            run = headless.start_run(exe=exe, cwd=Path(cwd), prompt=prompt, session_id=session_id, resume=resume,
                                     permission_mode=mode, log_path=log_dir / f"{session_id}.log")
            with self.lock:
                self.runs[session_id] = run
                self._save_runs()
        finally:
            with self.lock:
                self._starting.discard(session_id)
        self.log.audit("headless_started", session_id=session_id, resume=resume, mode=mode, pid=run.pid)
        return run

    def _save_runs(self) -> None:
        self.state.put("headless_runs", {sid: run.to_dict() for sid, run in self.runs.items()})

    def _adopt_runs(self) -> None:
        saved = self.state.get("headless_runs", {}) or {}
        for session_id, data in saved.items():
            try:
                run = headless.HeadlessRun.from_dict({**data, "session_id": session_id})
            except (KeyError, TypeError, ValueError):
                continue
            if headless.run_alive(run):
                with self.lock:
                    self.runs[session_id] = run
                # Followed like a fresh run, so the entry is released (and reported) when it exits.
                self._track(Delivery("headless", session_key(CLAUDE, session_id), self.clock(), None, None, run=run))
        with self.lock:
            self._save_runs()

    # --- permissions (PermissionRequest hook, blocking) ---

    @staticmethod
    def _permission_wait_s(hook_timeout_s: float | None) -> float:
        """How long a request may wait for Telegram: up to the hook's own timeout minus a margin."""

        timeout = hook_timeout_s if hook_timeout_s and hook_timeout_s > 0 else LEGACY_HOOK_TIMEOUT_S
        return max(10.0, min(PERMISSION_WAIT_MAX_S, float(timeout) - PERMISSION_MARGIN_S))

    def _registry_entry(self, session_id: str) -> Any:
        try:
            return self.claude.live(session_id) if self.claude is not None else None
        except Exception:  # noqa: BLE001 - a registry caught mid-write reads as unknown
            return None

    def claude_permission(self, payload: dict[str, Any], *, gone: threading.Event | None = None,
                          hook_timeout_s: float | None = None) -> dict[str, Any]:
        """Answer a PermissionRequest hook: allow / deny from Telegram, or {} to leave it to the dialog.

        In interactive sessions (Desktop, CLI) Claude Code shows its own dialog while this hook
        runs and takes whichever answers first, so the card goes out even when someone is at
        the PC (silently) and stays open until shortly before the hook's timeout; an answer in
        the dialog is noticed through the session registry. Where nothing races the hook (some
        sub-agent dialogs wait for it, and print-mode runs have no dialog) a person at the PC
        decides at once and Telegram gets the old 540 s. ``gone`` is set by the hook server
        when Claude Code hung up.
        """

        session_id = str(payload.get("session_id") or "")
        tool_name = str(payload.get("tool_name") or "")
        tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        owned = self._owned_run(session_id) is not None
        if not self.cfg.bound or not session_id:
            self.log.audit("card_skipped", session_id=session_id, tool=tool_name, reason="unbound")
            return {}
        if self.state.locked:
            self.log.audit("card_skipped", session_id=session_id, tool=tool_name, reason="locked")
            return {}
        if tool_name in progress.INTERACTIVE_TOOLS:
            # Claude Code ignores a plain "allow" for these; the answers travel in updatedInput.
            return self._claude_question(payload, tool_name, tool_input, gone=gone, hook_timeout_s=hook_timeout_s)
        canonical = json.dumps(tool_input, ensure_ascii=False, sort_keys=True, default=str)
        key = session_key(CLAUDE, session_id)
        if self._auto_allow(session_id):
            self.log.audit("permission_auto_allowed", session_id=session_id, tool=tool_name,
                           input_sha=text_digest(canonical), agent_id=payload.get("agent_id"))
            return self._permission_output("allow", "")
        subagent = bool(payload.get("agent_id"))
        entry = self._registry_entry(session_id)
        # Claude Code's own dialog is open meanwhile (it races this hook) only in interactive sessions.
        dialog = not owned and not subagent and getattr(entry, "kind", "") == "interactive"
        at_pc = self.presence.at_pc()
        if not owned and not dialog and at_pc:
            self.log.audit("card_skipped", session_id=session_id, tool=tool_name,
                           reason="at_pc_subagent" if subagent else "at_pc_no_dialog")
            return {}  # the dialog may wait for this hook, or there is none: the PC decides at once
        view = self.claude.view(session_id) if self.claude is not None else None
        title = view.title if view else session_id[:8]
        cwd = str(payload.get("cwd") or (view.cwd if view else ""))
        excluded = self._excluded_session(key, cwd)
        if excluded and not owned:
            self.log.audit("card_skipped", session_id=session_id, tool=tool_name, reason="excluded")
            return {}  # content of excluded folders never goes to Telegram; the PC decides
        shown_input = {tr('Note'): tr('The directory is in content_exclude; tool input is hidden')} if excluded else tool_input
        light = self.redact_light
        html, truncated, full = render.permission_card(
            machine=self.machine, title=self.redact(title), cwd=self.redact(cwd), tool_name=tool_name,
            tool_input=shown_input, permission_mode=str(payload.get("permission_mode") or ""),
            agent_id=payload.get("agent_id"), redact=light)
        changed = not excluded and light(canonical) != canonical
        wait_s = self._permission_wait_s(hook_timeout_s)
        if not dialog:
            wait_s = min(wait_s, PERMISSION_WAIT_S)  # a run without a dialog stays blocked meanwhile
        now = self.clock()
        wait = PermissionWait(secrets.token_urlsafe(9), session_id, text_digest(canonical), now + wait_s, owned,
                              tuple(self.cfg.allowed_user_ids), html=html, full="" if excluded else light(full),
                              needs_view=(truncated or changed) and not excluded, key=key,
                              title=self.redact(title), tool=tool_name, subagent=subagent, dialog=dialog, arrived=now)
        if changed:
            wait.html = html = html + tr('\n<i>⚠️ Parts of the input are hidden. View the full text before approving.</i>')
        with self.lock:
            # Before the topic and the card: a quick tap must find it, and so must the waiting alert.
            self.permissions[wait.nonce] = wait
        try:
            if not self._send_claude_card(wait):
                return self._permission_output("deny", "无法联系 Telegram 审批") if owned else {}  # i18n: data - model protocol text; preserve verbatim
            self._await_permission(wait, gone)
        finally:
            with self.lock:
                self.permissions.pop(wait.nonce, None)
            self._audit_card_failure(wait)
        decision = wait.decision
        outcome = _permission_outcomes().get(decision, _permission_outcomes()["local"])
        self._edit_later(wait.message_id, wait.html + f"\n\n<b>{render.esc(outcome)}</b>")
        self._remember_card(wait.message_id, False)
        self.log.audit("permission_decided", session_id=session_id, tool=tool_name, decision=decision, why=wait.why)
        if decision in ("allow", "allow_all"):
            return self._permission_output("allow", "")
        if decision == "deny":
            message = "无法联系 Telegram 审批" if wait.card_permanent_failure else "主人在 Telegram 上拒绝了这个操作。"  # i18n: data - model protocol text; preserve verbatim
            return self._permission_output("deny", message)
        return {}

    def _send_claude_card(self, wait: PermissionWait, card: QuestionCard | None = None) -> bool:
        """Try a hook's card once, outside Bridge.lock; False means immediate local/owned fallback."""

        if wait.event.is_set():
            return True
        silent = self.presence.at_pc() and not wait.owned
        wait.reminded = not silent
        wait.attempts += 1
        try:
            if wait.thread_id is None:
                wait.thread_id = self._topic(wait.key, wait.title, CLAUDE)
            if card is None:
                html, buttons = wait.html, self._permission_buttons(wait)
            else:
                card.thread_id = wait.thread_id
                with self.lock:
                    html, buttons = self._question_view(card)
            if wait.maybe_duplicate:
                note = tr('\n<i>If a duplicate card appears above, use this one.</i>')
                html += note
                self.log.audit("card_resent_maybe_duplicate", session_id=wait.session_id, tool=wait.tool,
                               attempts=wait.attempts)
            sent = self._send(html, thread_id=wait.thread_id, buttons=buttons, key=wait.key, silent=silent)
        except Exception as exc:  # noqa: BLE001 - a permanent failure leaves the dialog (or denies an owned run)
            if not wait.card_failed and card is not None:
                digest = progress.interactive_digest(wait.tool, card.tool_input)
                self._note_question_hook(wait.session_id, digest, "failed")
            wait.card_failed = True
            wait.last_error_code = exc.code if isinstance(exc, TelegramError) else None
            wait.maybe_duplicate |= any(name in str(exc).lower()
                                        for name in ("readtimeout", "readerror", "remoteprotocolerror"))
            self.log.line(tr('Failed to send card: {v0}: {v1}', v0=type(exc).__name__, v1=exc), "WARNING")
            if isinstance(exc, TelegramError) and _transient(exc):
                if exc.code is None:
                    self._network_trouble()
                wait.next_send_at = self.clock() + min(2.0 ** min(wait.attempts, 5), 30.0)
                return True
            wait.card_permanent_failure = True
            self._settle(wait, "deny" if wait.owned else "local")
            return False
        wait.message_id = sent.get("message_id")
        if card is None:
            wait.html = html
            fields: dict[str, Any] = {"input_sha": wait.digest}
        else:
            with self.lock:
                card.message_id = wait.message_id
                # Unsent Claude questions are never open cards: topic text must not answer them.
                self.questions[card.nonce] = card
            self._note_question_hook(wait.session_id, progress.interactive_digest(wait.tool, card.tool_input), "card")
            fields = {"questions": len(card.questions)}
        self._remember_card(wait.message_id, True)
        self.log.audit("question_asked" if card is not None else "permission_asked", session_id=wait.session_id,
                       tool=wait.tool, silent=silent, subagent=wait.subagent, dialog=wait.dialog,
                       attempts=wait.attempts, **fields)
        return True

    def _audit_card_failure(self, wait: PermissionWait) -> None:
        if wait.card_failed and wait.message_id is None:
            self.log.audit("card_send_failed", session_id=wait.session_id, tool=wait.tool, attempts=wait.attempts,
                           code=wait.last_error_code, outcome=wait.decision)

    def _await_permission(self, wait: PermissionWait, gone: threading.Event | None,
                          card: QuestionCard | None = None) -> None:
        """Block until the request is settled (a button, the PC, the hook going away, the deadline)."""

        misses = 0
        while not wait.event.wait(PERMISSION_POLL_S):
            now = self.clock()
            if now >= wait.deadline:
                self._settle(wait, "deny" if wait.owned else "expired")
            elif self.stopping.is_set():
                self._settle(wait, "deny" if wait.owned else "restart")
            elif gone is not None and gone.is_set():
                self._settle(wait, "gone")
            elif not wait.owned and not wait.dialog:
                # The registry would mislead here (another dialog of the session sets "waiting"); a
                # person back at the PC takes over instead.
                if self.presence.at_pc():
                    self._settle(wait, "local", why="back_at_pc")
            elif not wait.owned:
                entry = self._registry_entry(wait.session_id)
                status = getattr(entry, "status", None)
                changed_at = float(getattr(entry, "status_updated_at", 0.0) or 0.0)
                if status == "waiting":
                    wait.seen_waiting, misses = True, 0
                elif status is not None and (wait.seen_waiting or changed_at > wait.arrived - 1.0
                                             or now - wait.arrived >= PERMISSION_NO_DIALOG_S):
                    # The status left "waiting" (or never showed it): the dialog was answered on the PC,
                    # perhaps before the first look, or something else decided the request.
                    misses += 1
                    if misses >= PERMISSION_LOCAL_MISSES:
                        why = ("left_waiting" if wait.seen_waiting else
                               "changed_after_arrival" if changed_at > wait.arrived - 1.0 else "no_dialog_15s")
                        self._settle(wait, "answered", why=why)
                        continue
                if wait.message_id is not None and wait.seen_waiting and not wait.reminded and not self.presence.at_pc():
                    wait.reminded = True
                    self._later("permission reminder", lambda: self._permission_reminder(wait))

            # Local answers, disconnects and expiry take precedence over another network attempt.
            if not wait.event.is_set() and wait.message_id is None:
                if not wait.owned and now - wait.arrived >= CARD_RETRY_S:
                    self._settle(wait, "local")
                elif now >= wait.next_send_at:
                    self._send_claude_card(wait, card)

    def _settle(self, wait: PermissionWait, decision: str, *, why: str = "") -> bool:
        """Record the first decision only (buttons, the PC and the deadline race); True if this one won."""

        with self.lock:
            if wait.event.is_set():
                return False
            wait.decision = decision
            wait.why = why
            wait.event.set()
            return True

    def _settle_open_cards(self, reason: str) -> None:
        """Writes got locked: open cards go to the PC (a run the bridge started is denied)."""

        with self.lock:
            waits = list(self.permissions.values())
        for wait in waits:
            self._settle(wait, "deny" if wait.owned else "locked")
        codex_cards = self._close_codex_cards()
        codex_cards += self._close_codex_approvals()
        if waits or codex_cards:
            self.log.line(tr('Writes locked; handed {v0} approval or question cards over to the PC ({v1})', v0=len(waits) + codex_cards, v1=reason))

    def _remember_card(self, message_id: int | None, open_: bool) -> None:
        """Open approval cards survive in state, so a restart can mark the ones it cut off.

        Best effort: a state.json that cannot be written must not leave a card or its wait behind.
        """

        if not message_id:
            return
        with self.lock:
            cards = [item for item in self.state.get("open_permission_cards", []) if isinstance(item, int)]
            if open_ and message_id not in cards:
                cards = (cards + [message_id])[-200:]
            elif not open_ and message_id in cards:
                cards.remove(message_id)
            else:
                return
            try:
                self.state.put("open_permission_cards", cards)
            except OSError as exc:
                self.log.once("state", tr('Failed to write state.json ({v0}); this card may not be recognized after a restart', v0=exc), "ERROR")

    def _mark_stale_cards(self) -> None:
        """At start: cards a previous process left open can no longer answer anything."""

        with self.lock:
            # Cards this process sent meanwhile (the sources start polling first) stay open.
            live = {card.message_id for card in self.questions.values()}
            live |= {wait.message_id for wait in self.permissions.values()}
            live |= {card.message_id for card in self.codex_approvals.values()}
            saved = [item for item in self.state.get("open_permission_cards", []) if isinstance(item, int)]
            cards = [item for item in saved if item not in live]
            if live & set(saved):
                self.state.put("open_permission_cards", [item for item in saved if item in live])
            else:
                self.state.drop("open_permission_cards")
        for message_id in cards:
            self._edit_later(message_id, tr('🔄 This card expired (the bridge restarted). Handle it on the PC; new requests will get new cards.'))

    def _saved_async_answer(self, session_id: str, call_id: str) -> dict[str, Any] | None:
        with self.lock:
            return next((item for item in reversed(self.state.get("async_answers", []))
                         if isinstance(item, dict) and item.get("session_id") == session_id
                         and item.get("call_id") == call_id), None)

    def _remember_async_answer(self, card: QuestionCard, indices: list[int]) -> None:
        """Save only delivery identities, never answer text; best effort as for open cards."""

        with self.lock:
            saved = [item for item in self.state.get("async_answers", []) if isinstance(item, dict)
                     and (item.get("session_id"), item.get("call_id")) != (card.session_id, card.request_id)]
            saved.append({"session_id": card.session_id, "call_id": card.request_id, "indices": indices,
                          "via": card.via, "client_id": card.client_id, "marker": card.marker,
                          "job_id": card.job_id, "at": card.sent_at})
            try:
                self.state.put("async_answers", saved[-200:])
            except OSError as exc:
                self.log.once("state", tr('Failed to write state.json ({v0}); sent answers may not be recognized after a restart', v0=exc), "ERROR")

    def _forget_async_answer(self, session_id: str, call_id: str) -> None:
        with self.lock:
            saved = self.state.get("async_answers", [])
            remaining = [item for item in saved if not isinstance(item, dict)
                         or (item.get("session_id"), item.get("call_id")) != (session_id, call_id)]
            if remaining == saved:
                return
            try:
                self.state.put("async_answers", remaining)
            except OSError as exc:
                self.log.once("state", tr('Failed to write state.json ({v0}); closed questions may not be recognized after a restart', v0=exc), "ERROR")

    def _permission_reminder(self, wait: PermissionWait) -> None:
        """A loud note under a card that was sent silently, once nobody is at the PC any more."""

        if wait.event.is_set() or wait.message_id is None:
            return
        what = {progress.QUESTION_TOOL: tr('is still waiting for your answer'), progress.PLAN_TOOL: tr('’s plan is still waiting for your approval')}.get(
            wait.tool, tr('is still waiting for approval: {v0}', v0=render.esc(wait.tool)))
        self._send(tr('⏰ <b>{v0}</b> {v1} (buttons are on the card above)', v0=render.esc(wait.title), v1=what),
                   thread_id=wait.thread_id, reply_to=wait.message_id, key=wait.key)

    @staticmethod
    def _permission_output(decision: str, message: str = "", *,
                           updated_input: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"behavior": decision}
        if decision == "deny":
            body["message"] = message
        if updated_input is not None:
            body["updatedInput"] = updated_input
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": body}}

    @staticmethod
    def _permission_buttons(wait: PermissionWait) -> list[list[tuple[str, str]]]:
        if wait.confirming_all:
            return [[(tr('⚠️ Confirm allow-all'), f"perm:Y:{wait.nonce}"), (tr('↩️ Back'), f"perm:b:{wait.nonce}")]]
        if wait.needs_view and not wait.viewed:
            rows = [[(tr('📄 View full text'), f"perm:v:{wait.nonce}"), (tr('⛔ Deny'), f"perm:d:{wait.nonce}")]]
        else:
            rows = [[(tr('✅ Allow'), f"perm:a:{wait.nonce}"), (tr('⛔ Deny'), f"perm:d:{wait.nonce}")],
                    [(tr('⚡ Allow all (this session)'), f"perm:A:{wait.nonce}")]]
        if not wait.owned:
            rows.append([(tr('💻 Handle on the PC'), f"perm:l:{wait.nonce}")])
        return rows

    def _on_permission_choice(self, nonce: str, choice: str, sender: dict[str, Any]) -> str:
        with self.lock:
            wait = self.permissions.get(nonce)
        if wait is None or wait.event.is_set() or wait.tool in progress.INTERACTIVE_TOOLS:
            return tr('This request was already handled or expired')  # questions and plans are answered on their own card
        if sender.get("id") not in wait.user_ids:
            return tr('Not authorized')
        if choice == "v":
            if self._content_blocked():
                return tr('The bridge is security-locked; content is not being sent')
            if wait.needs_view and not wait.viewed:
                note = self._send_full_text("tool-input.md", wait.full, tr('Full tool input'), thread_id=wait.thread_id)
                if note != tr('Full text sent'):
                    return note
                wait.viewed = True
                self._edit(wait.message_id, wait.html, self._permission_buttons(wait))
            return tr('Full text sent')
        if choice in ("a", "A", "Y"):
            if self.state.locked:
                return tr('Writes are locked; cannot approve')
            if wait.needs_view and not wait.viewed:
                return tr('View the full text first')
        if choice in ("A", "b"):
            # [Allow all] takes a second tap on a card that says what it does.
            wait.confirming_all = choice == "A"
            html = wait.html
            if wait.confirming_all:
                html += (tr('\n\n<b>⚡ Allow all: allow this request and automatically allow every subsequent permission request in this session (for {v0:.0f} hours; /ask in the topic, /lock, or a bridge restart restores individual approvals).</b>', v0=ALLOW_ALL_TTL_S / 3600))
            self._edit(wait.message_id, html, self._permission_buttons(wait))
            return tr('Tap again to confirm') if wait.confirming_all else ""
        if choice == "Y" and not wait.confirming_all:
            return tr('Button expired')
        decision = {"a": "allow", "Y": "allow_all", "d": "deny", "l": "local"}.get(choice)
        if decision is None:
            return tr('Button expired')
        if decision == "local" and wait.owned:
            decision = "deny"  # a bridge-owned run has no local dialog
        others: list[PermissionWait] = []
        with self.lock:
            # One step: a /lock racing this tap either refuses it here or revokes the grant afterwards.
            if wait.event.is_set():
                return tr('This request was already handled or expired')  # e.g. answered on the PC a moment earlier
            if decision == "allow_all":
                if self.state.locked:
                    return tr('Writes are locked; cannot approve')
                now = self.clock()
                self.allow_all[wait.session_id] = AllowAll(wait.session_id, wait.key, int(sender["id"]), now,
                                                           now + ALLOW_ALL_TTL_S)
                others = [item for item in self.permissions.values()
                          if item.session_id == wait.session_id and item is not wait
                          and item.tool not in progress.INTERACTIVE_TOOLS]  # questions still need answers
            self._settle(wait, decision)
        if decision == "allow_all":
            self._after_allow_all(wait, others, int(sender["id"]))
        return {"allow": tr('Allowed'), "allow_all": tr('Allow-all enabled'), "deny": tr('Denied'), "local": tr('Handed over to the PC')}[decision]

    # --- Codex command / file approval cards ---

    @staticmethod
    def _same_approval(card: CodexApprovalCard, event: ApprovalEvent) -> bool:
        current = card.approval
        return (current.session_id == event.session_id and current.item_id == event.item_id
                and type(current.request_id) is type(event.request_id) and current.request_id == event.request_id)

    @staticmethod
    def _codex_approval_buttons(card: CodexApprovalCard) -> list[list[tuple[str, str]]]:
        if card.state != "open" or card.invalidated:
            return []

        def tap(choice: str) -> str:
            return f"ca:{choice}.{card.version}:{card.nonce}"

        first = [(tr('📄 View full text'), tap("v"))] if card.needs_view and not card.viewed else [(tr('✅ Allow'), tap("a"))]
        return [first + [(tr('⛔ Deny'), tap("d"))], [(tr('💻 Hand over to PC'), tap("l"))]]

    def _codex_approval_view(self, card: CodexApprovalCard) -> tuple[str, list[list[tuple[str, str]]]]:
        html = card.html
        if card.footer:
            html += f"\n\n<b>{render.esc(card.footer)}</b>"
        return html, self._codex_approval_buttons(card)

    def _push_codex_approval(self, event: ApprovalEvent) -> None:
        """Snapshot events arrive on the send thread; source polling never performs Telegram I/O."""

        if event.agent != CODEX:
            return
        with self.lock:
            existing = next((card for card in self.codex_approvals.values() if self._same_approval(card, event)), None)
        if event.closed:
            if existing is not None:
                self._finish_codex_approval(existing, by=event.by or "pc")
            return
        if existing is not None:
            return
        title = self.redact(event.title) or event.session_id[:8]
        at_pc, muted = self.presence.at_pc(), self.state.muted(event.key)
        silent = at_pc or muted
        thread_id = None
        if event.excluded or self.state.locked:
            thread_id = self._topic(event.key, title, CODEX)
            reason = tr('directory is in content_exclude') if event.excluded else tr('writes locked')
            self._send(tr('⚠️ <b>{v0}</b> is waiting for approval ({v1}; handle on the PC).', v0=render.esc(render.topic_name(self.machine, CODEX, title)), v1=reason), thread_id=thread_id, silent=silent, key=event.key)
            return
        tool_input = render.codex_approval_input(event)
        canonical = json.dumps(tool_input, ensure_ascii=False, sort_keys=True, default=str)
        html, truncated, full = render.codex_approval_card(
            machine=self.machine, title=title, cwd=self.redact(event.cwd), kind=event.kind,
            tool_input=tool_input, redact=self.redact_light)
        # Compare the rendered input as well: JSON escaping can hide an exact secret
        # containing quotes/backslashes from a redactor applied only after serialization.
        _raw_html, _raw_truncated, raw_full = render.codex_approval_card(
            machine=self.machine, title=title, cwd=self.redact(event.cwd), kind=event.kind,
            tool_input=tool_input)
        changed = full != raw_full or title != event.title or self.redact(event.cwd) != event.cwd
        if changed:
            html += tr('\n<i>⚠️ Parts of the input are hidden. View the full text before approving.</i>')
        card = CodexApprovalCard(secrets.token_urlsafe(9), event, title, tuple(self.cfg.allowed_user_ids),
                                 html, self.redact_light(full), needs_view=truncated or changed,
                                 reminded=not at_pc or muted)
        try:
            card.thread_id = thread_id = self._topic(event.key, title, CODEX)
            with self.lock:
                # A lock that raced topic/render work must not publish a usable approval card.
                locked = self.state.locked
                if not locked:
                    self.codex_approvals[card.nonce] = card
            if locked:
                self._send(tr('⚠️ <b>{v0}</b> is waiting for approval (writes locked; handle on the PC).', v0=render.esc(render.topic_name(self.machine, CODEX, title))), thread_id=thread_id, silent=silent, key=event.key)
                return
            with card.edit_lock:
                with self.lock:
                    view = self._codex_approval_view(card)
                sent = self._send(view[0], thread_id=thread_id, buttons=view[1], silent=silent, key=event.key)
                card.message_id = sent.get("message_id")
        except Exception as exc:  # noqa: BLE001 - a permanent failure leaves one plain notice
            with self.lock:
                self.codex_approvals.pop(card.nonce, None)
            if isinstance(exc, TelegramError) and _transient(exc):
                raise  # outbox retries the event, just as it does a Codex question card
            self.log.audit("approval_push_failed", key=event.key, kind=event.kind,
                           error=type(exc).__name__, input_sha=text_digest(canonical),
                           code=exc.code if isinstance(exc, TelegramError) else None)
            self.log.line(tr('Failed to send Codex approval card: {v0}', v0=type(exc).__name__), "WARNING")
            try:
                self._send(tr('⚠️ <b>{v0}</b> is waiting for approval (card could not be sent; handle on the PC).', v0=render.esc(render.topic_name(self.machine, CODEX, title))), thread_id=thread_id, silent=silent, key=event.key)
            except Exception as fallback_exc:  # noqa: BLE001 - attempted only once
                self.log.line(tr('Failed to send Codex approval notice: {v0}', v0=type(fallback_exc).__name__), "WARNING")
            return
        self._remember_card(card.message_id, True)
        self.log.audit("codex_approval_pushed", key=event.key, kind=event.kind, silent=silent,
                       input_sha=text_digest(canonical))

    def _on_codex_approval_choice(self, nonce: str, choice: str, sender: dict[str, Any],
                                   message: dict[str, Any]) -> str:
        op, _, version = choice.partition(".")
        with self.lock:
            card = self.codex_approvals.get(nonce)
        if card is None:
            return tr('This request was already handled or expired')
        if sender.get("id") not in card.user_ids:
            return tr('Not authorized')
        if card.message_id is not None and message.get("message_id") not in (None, card.message_id):
            return tr('Button expired')
        deliver = False
        decision = ""
        try:
            with card.edit_lock:
                with self.lock:
                    if self.codex_approvals.get(nonce) is not card or card.state != "open":
                        return tr('This request was already handled, or the decision is being sent')
                    if card.invalidated:
                        return tr('Writes are locked; handle on the PC')
                    if version != str(card.version):
                        return tr('Button expired: use the buttons currently on the card')
                    if self.state.locked:
                        return tr('Writes are locked; handle on the PC')
                    if op == "a" and card.needs_view and not card.viewed:
                        return tr('View the full text first')
                    if op not in ("a", "d", "l", "v"):
                        return tr('Button expired')
                    if op in ("a", "d"):
                        card.state, card.footer = "sending", tr('⏳ Sending the approval decision to Codex…')
                        card.write_started = False
                        decision, deliver = ("accept" if op == "a" else "decline"), True
                        self._executing += 1  # reserve before any Telegram I/O
                    elif op == "l":
                        card.state = "sent"  # consume the tap before its send-thread close
                if op == "v":
                    note = self._send_full_text("codex-approval.md", card.full, tr('Full approval input'),
                                                thread_id=card.thread_id)
                    if note != tr('Full text sent'):
                        return note
                    with self.lock:
                        if self.codex_approvals.get(nonce) is not card or self.state.locked or card.invalidated:
                            return tr('Writes are locked; handle on the PC')
                        card.viewed = True
                        card.version += 1
                if op == "l":
                    self._later("approval close", lambda: self._finish_codex_approval(card, by="local"))
                    return tr('Handed over to the PC')
                self._edit(card.message_id, *self._codex_approval_view(card))
        finally:
            if deliver:
                self._start_exec(self._deliver_codex_approval, card, decision, _reserved=True)
        return tr('Full text sent') if op == "v" else tr('Sending')

    def _deliver_codex_approval(self, card: CodexApprovalCard, decision: str) -> None:
        """Only confirmed taps use the executing worker; completion edits return to the send thread."""

        event = card.approval
        try:
            # A security lock before the worker actually starts still prevents the write.
            with self.lock:
                if self.state.locked or card.invalidated or self.codex_approvals.get(card.nonce) is not card:
                    return
                card.write_started = True
            self.codex.decide_approval(event.session_id, method=event.method, item_id=event.item_id,
                                      turn_id=event.turn_id, approval_id=event.approval_id, digest=event.digest,
                                      decision=decision, request_id=event.request_id)
        except codex_ipc.AlreadyAnswered:
            self._later("approval close", lambda: self._finish_codex_approval(card, by="pc", resolved=True))
        except codex_ipc.OutcomeUnknown:
            self.log.audit("codex_approval_failed", key=card.key, error="OutcomeUnknown")
            self._later("approval uncertain", lambda: self._codex_approval_result(
                card, "sent", tr('⏳ Possibly delivered to Codex; confirming…')))
        except Exception as exc:  # noqa: BLE001 - refuse further taps until the failed write returns
            self.log.audit("codex_approval_failed", key=card.key, error=type(exc).__name__)
            footer = "❌ " + self.redact(str(exc))[:600]
            self._later("approval failed", lambda: self._codex_approval_result(card, "open", footer))
        else:
            self.log.audit("codex_approval_decided", key=card.key, decision=decision, by="telegram")
            self._later("approval decided", lambda: self._finish_codex_approval(card, by="telegram", decision=decision))

    def _codex_approval_result(self, card: CodexApprovalCard, state: str, footer: str) -> None:
        with card.edit_lock:
            with self.lock:
                if self.codex_approvals.get(card.nonce) is not card or card.state == "closed" or card.invalidated:
                    return
                pending_by = card.pending_by
                card.state, card.footer = state, footer
                card.version += 1
                view = self._codex_approval_view(card)
            if not pending_by:
                self._edit(card.message_id, *view)
        if pending_by:
            self._finish_codex_approval(card, by=pending_by)

    def _finish_codex_approval(self, card: CodexApprovalCard, *, by: str, decision: str = "",
                               resolved: bool = False) -> None:
        """Close on the send thread, ordered after any card edit or tap (edit_lock before lock)."""

        outcome = {
            "pc": tr('Handled on the PC'),
            "turn_ended": tr('This turn ended; approval is no longer valid'),
            "desktop_gone": tr('Codex Desktop restarted or exited; approval is no longer valid'),
            "gone": tr('Conversation archived or deleted'),
            "local": tr('💻 Handed over to the PC'),
            "locked": tr('🔒 Writes locked; please handle on the PC'),
            "telegram": tr('✅ Allowed from Telegram') if decision == "accept" else tr('⛔ Denied from Telegram'),
        }.get(by, tr('Approval is no longer pending'))
        with card.edit_lock:
            with self.lock:
                if self.codex_approvals.get(card.nonce) is not card or card.state == "closed":
                    return
                if card.state == "sending" and card.write_started and not resolved and by not in ("telegram", "locked"):
                    card.pending_by = by
                    return  # the worker's confirmed result decides whose outcome to show
                card.state, card.footer = "closed", outcome
                self.codex_approvals.pop(card.nonce, None)
                view = self._codex_approval_view(card)
            self._edit(card.message_id, *view)
            self._remember_card(card.message_id, False)
        self.log.audit("codex_approval_closed", key=card.key, by=by)

    def _close_codex_approvals(self) -> int:
        with self.lock:
            cards = list(self.codex_approvals.values())
            for card in cards:
                card.invalidated = True  # unlocking before the send-thread close never revives this card
        for card in cards:
            self._later("approval lock", lambda card=card: self._finish_codex_approval(card, by="locked"))
        return len(cards)

    def _remind_codex_approvals(self) -> None:
        with self.lock:
            due = [card for card in self.codex_approvals.values() if not card.reminded and card.state == "open"
                   and card.message_id and not self.state.muted(card.key)]
        if not due or self.presence.at_pc():
            return
        for card in due:
            card.reminded = True
            self._later("approval reminder", lambda card=card: self._codex_approval_reminder(card))

    def _codex_approval_reminder(self, card: CodexApprovalCard) -> None:
        with self.lock:
            if (self.codex_approvals.get(card.nonce) is not card or card.state != "open"
                    or self.state.muted(card.key)):
                return
        self._send(tr('⏰ <b>{v0}</b> is still waiting for approval (buttons are on the card above)', v0=render.esc(card.title)),
                   thread_id=card.thread_id, reply_to=card.message_id, key=card.key)

    # --- question cards (Claude AskUserQuestion / ExitPlanMode, Codex request_user_input) ---
    #
    # Locks: card.edit_lock (one card's taps, edits and close, in order) before self.lock, never
    # the other way round. Closing happens on the send thread (_finish_card through _later), so a
    # card is never closed halfway through being sent, and the close is the card's last edit.

    def _claude_question(self, payload: dict[str, Any], tool_name: str, tool_input: dict[str, Any], *,
                         gone: threading.Event | None, hook_timeout_s: float | None) -> dict[str, Any]:
        """Answer AskUserQuestion or ExitPlanMode from a question card.

        Claude Code takes a hook's "allow" for these only together with updatedInput
        ("Hook satisfied user interaction ... via updatedInput", 2.1.286): AskUserQuestion gets
        ``answers`` (question text -> label, labels joined by ", ", or the typed text), a plan
        is approved with its input. [Keep planning] denies with the owner's change requests as the
        message. Meanwhile the card races Claude's own dialog exactly like a permission card.
        What the hook did (a card or none) is noted for the call's progress text.
        """

        session_id = str(payload.get("session_id") or "")
        digest = progress.interactive_digest(tool_name, tool_input)
        self._note_question_hook(session_id, digest, "pending")
        carded = False
        try:
            if not self.cfg.bound or not session_id:
                self.log.audit("card_skipped", session_id=session_id, tool=tool_name, reason="unbound")
                return {}
            if self.state.locked:
                self.log.audit("card_skipped", session_id=session_id, tool=tool_name, reason="locked")
                return {}
            kind = "plan" if tool_name == progress.PLAN_TOOL else "ask"
            questions = progress.ask_questions(tool_input) if kind == "ask" else []
            plan = ""
            if kind == "plan":
                plan = str(tool_input.get("plan") or "").strip() or self._plan_file(tool_input.get("planFilePath"))
            if not (questions or plan):
                self.log.audit("card_skipped", session_id=session_id, tool=tool_name, reason="no_questions")
                return {}  # nothing a card could show: the dialog decides
            owned = self._owned_run(session_id) is not None
            subagent = bool(payload.get("agent_id"))
            entry = self._registry_entry(session_id)
            dialog = not owned and not subagent and getattr(entry, "kind", "") == "interactive"
            at_pc = self.presence.at_pc()
            if not owned and not dialog and at_pc:
                self.log.audit("card_skipped", session_id=session_id, tool=tool_name,
                               reason="at_pc_subagent" if subagent else "at_pc_no_dialog")
                return {}  # as for permissions: the dialog may wait for this hook, or there is none
            view = self.claude.view(session_id) if self.claude is not None else None
            title = self.redact(view.title if view else session_id[:8])
            cwd = str(payload.get("cwd") or (view.cwd if view else ""))
            key = session_key(CLAUDE, session_id)
            if self._excluded_session(key, cwd):
                self.log.audit("card_skipped", session_id=session_id, tool=tool_name, reason="excluded")
                return {}  # questions and plans of excluded folders never go to Telegram
            now = self.clock()
            wait_s = self._permission_wait_s(hook_timeout_s)
            if not dialog:
                wait_s = min(wait_s, PERMISSION_WAIT_S)  # a run without a dialog stays blocked meanwhile
            nonce = secrets.token_urlsafe(9)
            users = tuple(self.cfg.allowed_user_ids)
            canonical = json.dumps(tool_input, ensure_ascii=False, sort_keys=True, default=str)
            wait = PermissionWait(nonce, session_id, text_digest(canonical), now + wait_s, owned, users, key=key,
                                  title=title, tool=tool_name, subagent=subagent, dialog=dialog, arrived=now)
            card = QuestionCard(nonce, CLAUDE, session_id, key, kind, title, users, questions=questions,
                                shown=self._shown_questions(questions), answers=[[] for _ in questions],
                                typed=[False for _ in questions], tool_input=tool_input, wait=wait,
                                plan=self.redact(plan))
            with self.lock:
                self.permissions[nonce] = wait  # waiting alerts see the hook, even while its card is unsent
            try:
                if not self._send_claude_card(wait, card):
                    return self._question_output(card, "deny") if owned else {}
                self._await_permission(wait, gone, card)
            finally:
                with self.lock:
                    self.permissions.pop(nonce, None)
                    self.questions.pop(nonce, None)
                carded = wait.message_id is not None
                self._audit_card_failure(wait)
            decision = wait.decision
            output = self._question_output(card, decision)
            # Only what left with the decision stays on the closed card (a plan's notes go with [Keep planning]).
            sent_back = decision == "keep_planning" or (decision == "answer" and kind == "ask")
            outcome = self._question_outcome(card, decision)
            self._later("question close", lambda: self._finish_card(card, outcome, sent=sent_back))
            self.log.audit("question_decided", session_id=session_id, tool=tool_name, decision=decision, why=wait.why)
            return output
        finally:
            if not carded:
                self._note_question_hook(session_id, digest, "none")

    @staticmethod
    def _plan_file(path: Any) -> str:
        """The plan file Claude Code keeps (``plans/*.md``), for a plan whose text the input lacks."""

        if not isinstance(path, str) or not path.lower().endswith(".md") or Path(path).parent.name != "plans":
            return ""
        try:
            with open(path, "rb") as stream:
                return stream.read(PLAN_FILE_MAX).decode("utf-8", "replace").strip()
        except OSError:
            return ""

    @staticmethod
    def _question_outcome(card: QuestionCard, decision: str) -> str:
        if decision == "answer":
            return _question_outcomes()["approve" if card.kind == "plan" else "answer"]
        if decision in _question_outcomes():
            return _question_outcomes()[decision]
        return _permission_outcomes().get(decision, _permission_outcomes()["local"])

    @staticmethod
    def _answer_value(card: QuestionCard, index: int) -> str:
        chosen = card.answers[index]
        return chosen[0] if card.typed[index] else ", ".join(chosen)

    def _question_output(self, card: QuestionCard, decision: str) -> dict[str, Any]:
        """The PermissionRequest answer for a Claude question card's decision ({} leaves it to the PC)."""

        if decision == "answer":
            if card.kind == "plan":
                updated = {name: value for name, value in card.tool_input.items() if name not in PLAN_SHOWN_FIELDS}
            else:
                answers = {question.id: self._answer_value(card, index)
                           for index, question in enumerate(card.questions) if card.answers[index]}
                updated = {**card.tool_input, "answers": answers}
            return self._permission_output("allow", updated_input=updated)
        if decision == "keep_planning":
            message = "用户在 Telegram 上没有批准这个计划：请继续完善计划（仍在计划模式），改好后再提交。"  # i18n: data - model protocol text; preserve verbatim
            if card.feedback:
                message += f"\n用户的修改意见：{card.feedback}"  # i18n: data - model protocol text; preserve verbatim
            return self._permission_output("deny", message)
        if decision == "deny":
            return self._permission_output("deny", "没有在 Telegram 上收到回答。")  # i18n: data - model protocol text; preserve verbatim
        return {}

    def _note_question_hook(self, session_id: str, digest: str, what: str) -> None:
        if digest:
            with self.lock:
                self._question_hooks[(session_id, digest)] = (self.clock(), what)

    def _card_possible(self, session_id: str) -> bool:
        """A question card or approval card of this Claude session can still arrive (its hooks report)."""

        if not self.cfg.bound or self.state.locked or self.claude is None:
            return False
        try:
            return bool(self.claude.hooked(session_id))
        except Exception:  # noqa: BLE001 - unknown: assume the hooks run
            return True

    def _card_shows(self, session_id: str, digest: str) -> bool:
        """The question card of this AskUserQuestion / ExitPlanMode call shows its text (send thread).

        The hook and the transcript record reach the bridge at about the same time: a text waits
        a moment for its hook, but only while a card can come at all. A hook still busy sending
        its card after that, or one that never came, leaves the text to the progress message.
        """

        if not digest:
            return False
        steps = ATTENTION_CARD_WAIT_STEPS if self._card_possible(session_id) else 0
        waited = 0
        while True:
            with self.lock:
                record = self._question_hooks.get((session_id, digest))
            if record is not None and record[1] != "pending":
                return record[1] == "card"
            # No hook yet: a few half-seconds; a hook still sending its card: twice as long.
            if waited >= (2 * steps if record is not None else steps) or self.stopping.wait(0.5):
                return False
            waited += 1

    def _shown_questions(self, questions: list[Question]) -> list[Question]:
        """The questions as cards and documents show them, redacted like every pushed text."""

        red = self.redact
        return [replace(question, text=red(question.text), header=red(question.header),
                        options=[(red(label), red(description)) for label, description in question.options])
                for question in questions]

    def _question_help(self, card: QuestionCard) -> str:
        hint = (tr(' Questions without options must be answered by replying to this card.')
                if card.mode == "async" and any(not question.options for question in card.questions) else "")
        if card.review:
            return tr('After reviewing, tap [📨 Send answers]; to change them, tap [⬅️ Previous question] or [↩️ Start over].') + hint
        question = card.questions[card.index]
        if question.multi:
            return tr('Multiple selections: tap options to select, then [✅ Done]; you can also reply to this card with your own answer.')
        if question.options:
            if card.mode == "async":
                return tr('Choose an option or reply to this card with your own answer; answer every question before sending.') + hint
            return tr('Choose an option or reply to this card with your own answer; tap [Skip] to leave it unanswered.') + hint
        if card.mode == "async":
            return tr('Reply to this card with your answer; answer every question before sending.') + hint
        return tr('Reply to this card with your answer; tap [Skip] to leave it unanswered.') + hint

    def _question_view(self, card: QuestionCard, *, final: str = "",
                       sent: bool = False) -> tuple[str, list[list[tuple[str, str]]]]:
        """(html, buttons) of a question card, under self.lock.

        ``final`` closes it: no buttons, that line instead of the help, and what was picked or
        typed only when it was ``sent`` (otherwise it would look like the answer).
        """

        keep = not final or sent
        if card.kind == "plan":
            for _ in range(2):  # a first render may find the plan too long: the help then says so
                gated = card.truncated and not card.viewed
                footer = final or card.footer or (
                    tr('This plan is too long for the card: tap [📄 View full text] first to reveal [✅ Approve plan]; tap [✏️ Keep planning] to have Claude revise it (you can reply to this card with change requests first).') if gated else
                    tr('Tap [✅ Approve plan] to have Claude implement it; tap [✏️ Keep planning] to revise it (you can reply to this card with change requests first).'))
                html, truncated = render.plan_card(machine=self.machine, title=card.title, plan=card.plan,
                                                   feedback=card.feedback if keep else "", footer=footer)
                if final or card.truncated or not truncated:
                    break
                card.truncated = True
        else:
            footer = final or card.footer or self._question_help(card)
            if keep:
                answers = [[self.redact(value) for value in chosen] for chosen in card.answers]
                typed, index, review = list(card.typed), card.index, card.review or bool(final)
            else:
                answers, typed, index, review = [[] for _ in card.shown], [], -1, False
            html, truncated = render.question_card(
                machine=self.machine, agent=card.agent, title=card.title, questions=card.shown,
                answers=answers, typed=typed, index=index, review=review, footer=footer)
        if final:
            return html, []
        card.truncated = card.truncated or truncated
        return html, self._question_buttons(card)

    def _question_buttons(self, card: QuestionCard) -> list[list[tuple[str, str]]]:
        if card.state != "open":
            return []
        nonce, version = card.nonce, card.version

        def tap(op: str) -> str:
            return f"q:{op}.{version}:{nonce}"

        rows: list[list[tuple[str, str]]] = []
        if card.kind == "plan":
            keep = (tr('✏️ Keep planning with feedback') if card.feedback else tr('✏️ Keep planning'), tap("k"))
            rows.append([(tr('✅ Approve plan'), tap("a")), keep] if card.viewed or not card.truncated else [keep])
        elif card.review:
            rows.append([(tr('📨 Send answers'), tap("s")), (tr('↩️ Start over'), tap("r"))])
            rows.append([(tr('⬅️ Previous question'), tap("p"))])
        else:
            question, shown = card.questions[card.index], card.shown[card.index]
            chosen = card.answers[card.index]
            for position, (label, _description) in enumerate(shown.options):
                mark = ""
                if question.multi:
                    mark = "☑️ " if question.options[position][0] in chosen and not card.typed[card.index] else "⬜ "
                text = render.button_label(f"{mark}{render.option_letter(position)}. {label}")
                rows.append([(text, tap(f"o{position}"))])
            nav = [(tr('⬅️ Previous question'), tap("p"))] if card.index > 0 else []
            done = question.multi and chosen and not card.typed[card.index]
            if card.mode != "async" or chosen:
                nav.append((tr('✅ Done ➡️') if done else tr('Skip ➡️'), tap("n")))
            if nav:
                rows.append(nav)
        extra = [(tr('📄 View full text'), f"q:v:{nonce}")] if card.truncated else []
        if card.agent == CLAUDE and not card.owned:
            extra.append((tr('💻 Hand over to PC'), f"q:l:{nonce}"))
        if extra:
            rows.append(extra)
        return rows

    @staticmethod
    def _advance(card: QuestionCard) -> None:
        card.version += 1
        if card.index + 1 >= len(card.questions):
            card.index, card.review = max(0, len(card.questions) - 1), True
        else:
            card.index += 1

    def _question_step(self, card: QuestionCard, op: str) -> str | None:
        """Apply a tap that only changes what the card holds (under self.lock); None = not such a tap."""

        if card.kind == "plan":
            return None
        if op.startswith("o") and op[1:].isdigit() and not card.review:
            position = int(op[1:])
            question = card.questions[card.index]
            if position >= len(question.options):
                return tr('Button expired')
            label = question.options[position][0]
            if question.multi:
                chosen = [] if card.typed[card.index] else list(card.answers[card.index])
                if label in chosen:
                    chosen.remove(label)
                else:
                    chosen.append(label)
                card.answers[card.index], card.typed[card.index] = chosen, False
                return ""
            card.answers[card.index], card.typed[card.index] = [label], False
            self._advance(card)
            return tr('Selected {v0}', v0=render.option_letter(position))
        if op == "n" and not card.review:
            if card.mode == "async" and not card.answers[card.index]:
                return tr('Answer every question before sending')
            self._advance(card)
            return ""
        if op == "p":
            card.version += 1
            if card.review:
                card.review = False
            elif card.index > 0:
                card.index -= 1
            return ""
        if op == "r":
            card.version += 1
            card.answers = [[] for _ in card.questions]
            card.typed = [False for _ in card.questions]
            card.index, card.review, card.footer = 0, False, ""
            return tr('Cleared; choose again')
        return None

    def _on_question_choice(self, nonce: str, choice: str, sender: dict[str, Any], message: dict[str, Any]) -> str:
        op, _, version = choice.partition(".")
        with self.lock:
            card = self.questions.get(nonce)
        if card is None or (card.wait is not None and card.wait.event.is_set()):
            return tr('This question was already handled or expired')
        if card.agent not in (CODEX, CLAUDE):
            self.log.line(tr('Ignoring question card from unknown agent: {v0}', v0=card.agent), "WARNING")
            return tr('This question cannot be answered on Telegram')
        if sender.get("id") not in card.user_ids:
            return tr('Not authorized')
        if card.message_id is not None and message.get("message_id") not in (None, card.message_id):
            return tr('Button expired')
        if op == "v":
            return self._question_full(card)
        if op == "l":
            if card.wait is None or card.owned:
                return tr('Button expired')
            return tr('Handed over to the PC') if self._settle(card.wait, "local") else tr('This question was already handled')
        deliver = False
        try:
            with card.edit_lock:
                with self.lock:
                    # Checked again with the change: an answer that left the card stays as it was sent.
                    if (self.questions.get(nonce) is not card or card.state != "open"
                            or (card.wait is not None and card.wait.event.is_set())):
                        return tr('This question was already handled, or its answers are being sent')
                    if version != str(card.version):
                        return tr('Button expired: use the buttons currently on the card')
                    note = self._question_step(card, op)
                    view: tuple[str, list[list[tuple[str, str]]]] | None = None
                    if note is not None:
                        view = self._question_view(card)
                    else:
                        note, deliver = self._question_submit(card, op)
                        if deliver:
                            # Reserve before Telegram I/O: the consumed confirmation already
                            # counts as executing even if editing its status is slow or fails.
                            self._executing += 1
                            view = self._question_view(card, final=tr('⏳ Sending answers to Codex…'), sent=True)
                if view is not None:
                    self._edit_open_card(card, *view)
        finally:
            if deliver:
                self.log.audit("question_submitted", key=card.key, questions=len(card.questions),
                               answered=sum(1 for chosen in card.answers if chosen), mode=card.mode)
                self._start_exec(self._deliver_codex_answer, card, _reserved=True)
        return note

    def _question_submit(self, card: QuestionCard, op: str) -> tuple[str, bool]:
        """[Send answers], [Approve plan], [Keep planning] (under self.lock, the card still open): the answers leave the card.

        A Claude card is decided here (the hook thread returns the output); a Codex card turns
        "sending" and the caller delivers it. Returns the tap's note and whether to deliver.
        """

        if self.state.locked:
            return tr('Writes are locked; answer on the PC'), False
        if card.kind == "plan":
            if op not in ("a", "k") or card.wait is None:
                return tr('Button expired'), False
            if op == "a" and card.truncated and not card.viewed:
                return tr('The full plan is not shown: tap [📄 View full text] first'), False
            self._settle(card.wait, "answer" if op == "a" else "keep_planning")
            card.state = "sent"
            return (tr('Plan approved') if op == "a" else tr('Asked Claude to keep planning')), False
        if op != "s" or not card.review:
            return tr('Button expired'), False
        if card.mode == "async" and not all(card.answers):
            return tr('Answer every question before sending'), False
        if card.wait is not None:
            self._settle(card.wait, "answer")
            card.state = "sent"
            return tr('Answers sent'), False
        card.state, card.footer, card.tried = "sending", "", True
        if card.mode == "async":
            card.client_id = card.client_id or str(uuid.uuid4())
            card.marker = card.marker or "tg:" + card.nonce[:6]
        return tr('Sending'), True

    def _edit_open_card(self, card: QuestionCard, html: str,
                        buttons: list[list[tuple[str, str]]] | None = None) -> None:
        """Edit a question card that is not closed (the caller holds card.edit_lock)."""

        if not card.closed:
            self._edit(card.message_id, html, buttons)

    def _deliver_codex_answer(self, card: QuestionCard) -> None:
        """Send a Codex card's answers through the Codex Desktop IPC (tgbridge-exec thread)."""

        if card.agent != CODEX:
            self.log.line(tr('Refusing to send question answers to {v0}', v0=card.agent), "WARNING")
            return
        if card.mode == "async":
            self._deliver_codex_async_answer(card)
            return
        # "sending": no tap changes the answers meanwhile.
        answers = {question.id: list(card.answers[index])
                   for index, question in enumerate(card.questions) if card.answers[index]}
        try:
            self.codex.answer_question(card.session_id, card.request_id, answers)
        except Exception as exc:  # noqa: BLE001 - the card offers another try
            reason = self.redact(str(exc))[:300]
            self.log.line(tr('Failed to answer a Codex question: {v0}: {v1}', v0=type(exc).__name__, v1=reason), "WARNING")
            self.log.audit("question_failed", key=card.key, error=type(exc).__name__, mode=card.mode)
            with card.edit_lock:
                with self.lock:
                    if self.questions.get(card.nonce) is not card:
                        return  # closed meanwhile (answered on the PC, turn ended, locked)
                    card.state = "open"
                    card.footer = tr('❌ Could not send to Codex: {v0}. Tap [📨 Send answers] again or answer on the PC.', v0=reason)
                    view = self._question_view(card)
                self._edit_open_card(card, *view)
            return
        self.log.audit("question_answered", key=card.key, mode=card.mode)
        with card.edit_lock:
            with self.lock:
                card.state = "sent"
                view = self._question_view(card, final=tr('📨 Sent to Codex; awaiting receipt confirmation…'), sent=True)
            self._edit_open_card(card, *view)

    def _deliver_codex_async_answer(self, card: QuestionCard) -> None:
        """Send a confirmed async reply, retaining its identity until the rollout records it."""

        answers = {int(question.id): (question.text, self._answer_value(card, index))
                   for index, question in enumerate(card.questions) if card.answers[index]}
        envelope = codex_threads.build_question_reply(card.request_id, answers)
        job_id = ""
        record = True
        try:
            result = self.codex.answer_async_question(card.session_id, card.request_id, card.turn_id, answers,
                                                      client_id=card.client_id, marker=f"[{card.marker}]")
            via = result["via"]
            job_id = str(result.get("job_id") or "")
            if via == "app_tools":
                footer = tr('📨 This turn ended; answers sent as a new message, awaiting the Codex record…')
            elif result.get("other_turn"):
                footer = tr('📨 Answers entered a new Codex turn; awaiting its record…')
            else:
                footer = tr('📨 Inserted into the running turn; awaiting the Codex record…')
        except codex_ipc.OutcomeUnknown:
            via, footer = "ipc_unconfirmed", tr('⏳ Possibly delivered to Codex; confirming…')
        except codex_ipc.AlreadyAnswered as exc:
            via = "ipc"
            if exc.mine:
                footer = tr('📨 Previous answers delivered; awaiting the Codex record…')
            else:
                footer, record = tr('Someone already answered this question'), False
        except Exception as exc:  # noqa: BLE001 - known failures offer an explicit retry
            reason = self.redact(str(exc))[:300]
            self.log.line(tr('Failed to answer a Codex question: {v0}: {v1}', v0=type(exc).__name__, v1=reason), "WARNING")
            self.log.audit("question_failed", key=card.key, mode=card.mode, error=type(exc).__name__)
            self._reopen_async_card(card, tr('❌ Could not send to Codex: {v0}. Tap [📨 Send answers] again or answer on the PC.', v0=reason))
            return
        with card.edit_lock:
            with self.lock:
                if self.questions.get(card.nonce) is not card or card.closed:
                    return  # the source closed it while the write was in progress
                card.state, card.tried, card.sent_at = "sent", True, self.clock()
                card.via, card.job_id, card.footer = via, job_id, footer
                if record:
                    self._remember_async_answer(card, list(answers))
                    self.log.audit("question_async_sent", key=card.key, mode=card.mode, via=via,
                                   count=len(answers), text_sha=text_digest(envelope))
                if via == "app_tools" and job_id:
                    self._track(Delivery("codex_async", card.key, card.sent_at, card.message_id, card.thread_id,
                                         marker=card.marker, job_id=job_id, info={"nonce": card.nonce}))
                view = self._question_view(card)
            self._edit_open_card(card, *view)

    def _reopen_async_card(self, card: QuestionCard, footer: str, *, job_id: str = "",
                           unconfirmed: bool = False) -> None:
        """Update a still-pending card (exec/send thread); never called directly by polling."""

        with card.edit_lock:
            with self.lock:
                if (self.questions.get(card.nonce) is not card or card.closed
                        or card.state not in ("sending", "sent")
                        or (job_id and card.job_id != job_id)):
                    return
                if unconfirmed and (self.clock() - card.sent_at <= ASYNC_ANSWER_CONFIRM_S
                                    or self._async_delivery_active(card)):
                    return
                card.state, card.footer, card.job_id = "open", footer, ""
                card.version += 1
                self._forget_async_answer(card.session_id, card.request_id)
                if unconfirmed:
                    self.log.audit("question_async_unconfirmed", key=card.key, mode=card.mode, via=card.via)
                view = self._question_view(card)
            self._edit_open_card(card, *view)

    def _async_delivery_active(self, card: QuestionCard) -> bool:
        """Caller holds self.lock."""

        return any(item.kind == "codex_async" and item.job_id == card.job_id and item.key == card.key
                   for item in self.deliveries) if card.job_id else False

    def _recheck_async_answers(self) -> None:
        """Queue reconciliation; a running turn may not record steered input until it samples again."""

        with self.lock:
            due = [card for card in self.questions.values() if card.mode == "async" and card.state == "sent"
                   and self.clock() - card.sent_at > ASYNC_ANSWER_CONFIRM_S and not self._async_delivery_active(card)]
        for card in due:
            self._later("async question confirm", lambda card=card: self._confirm_async_answer(card))

    def _confirm_async_answer(self, card: QuestionCard) -> None:
        with self.lock:
            if self.questions.get(card.nonce) is not card or card.state != "sent":
                return
        state = self.codex.async_question_state(card.session_id, card.request_id)
        if state is None or not state["waiting"] or state["turn_running"]:
            return  # the close event follows, or the running turn still has to sample the input
        self._reopen_async_card(card, tr('⚠️ Delivery unconfirmed; you can tap [📨 Send answers] again'), unconfirmed=True)

    def _send_full_text(self, filename: str, text: str, caption: str, *, thread_id: int | None,
                        reply_to: int | None = None) -> str:
        """[View full text]: the whole text as a document; the tap’s note."""

        if self._content_blocked():
            return tr('The bridge is security-locked; content is not being sent')
        options: dict[str, Any] = {"reply_to": reply_to} if reply_to else {}
        try:
            self.api.send_document(self.chat_id, filename, _utf8(text), caption=caption, thread_id=thread_id,
                                   **options)
        except TelegramError as exc:
            return tr('Failed to send full text: {v0}', v0=exc)[:180]
        return tr('Full text sent')

    def _question_full(self, card: QuestionCard) -> str:
        """The whole questions or plan as a document; a long plan may be approved after that."""

        plan = card.kind == "plan"
        text = render.question_markdown(machine=self.machine, agent=card.agent, title=card.title,
                                        questions=card.shown, plan=card.plan)
        note = self._send_full_text("plan.md" if plan else "question.md", text, tr('Full plan') if plan else tr('Full questions'),
                                    thread_id=card.thread_id, reply_to=card.message_id)
        if note == tr('Full text sent') and plan and not card.viewed:
            with card.edit_lock:
                with self.lock:
                    if self.questions.get(card.nonce) is not card or card.state != "open":
                        return note
                    card.viewed = True
                    view = self._question_view(card)
                self._edit_open_card(card, *view)
        return note

    def _replied_card_id(self, message: dict[str, Any]) -> int | None:
        """The id of this bot's message that ``message`` replies to; None for anything else."""

        reply = message.get("reply_to_message")
        if not isinstance(reply, dict) or (reply.get("from") or {}).get("id") != self.me.get("id"):
            return None
        message_id = reply.get("message_id")
        return message_id if isinstance(message_id, int) and message_id else None

    def _question_for_reply(self, message: dict[str, Any]) -> QuestionCard | None:
        message_id = self._replied_card_id(message)
        if message_id is None:
            return None
        with self.lock:
            return next((card for card in self.questions.values() if card.message_id == message_id), None)

    def _replies_to_closed_card(self, message: dict[str, Any]) -> bool:
        message_id = self._replied_card_id(message)
        with self.lock:
            return message_id is not None and message_id in self._closed_cards

    def _question_typed(self, card: QuestionCard, message: dict[str, Any], text: str) -> None:
        """A reply to a question card: the typed answer of the question shown (a plan: change requests)."""

        if (message.get("from") or {}).get("id") not in card.user_ids:
            return
        if message.get("forward_origin") or message.get("external_reply"):
            self._reply(message, tr('Forwarded messages are not accepted. Reply to this card directly with your answer.'), silent=True)
            return
        if not self._fresh(message):
            self._reply(message, tr('⌛ This message is too old (sent while the bridge was offline). Reply again.'), silent=True)
            return
        text = text.strip()[:QUESTION_TYPED_MAX]
        changed = False
        with card.edit_lock:
            with self.lock:
                if (self.questions.get(card.nonce) is not card or card.state != "open"
                        or (card.wait is not None and card.wait.event.is_set())):
                    note = tr('This question was already handled.')
                elif card.kind == "plan":
                    card.feedback, changed = text, True
                    note = tr('✍️ Feedback added to the card: tap [✏️ Keep planning with feedback] to send it to Claude.')
                elif card.review and len(card.questions) > 1:
                    note = tr('You are at the review step: tap [⬅️ Previous question] to return to the question you want to change, then reply to the card.')
                else:
                    card.answers[card.index], card.typed[card.index] = [text], True
                    self._advance(card)
                    changed = True
                    note = tr('✍️ Added to the card') + (tr('; review and tap [📨 Send answers].') if card.review else tr('; continue to the next question.'))
                view = self._question_view(card) if changed else None
            if view is not None:
                self._edit_open_card(card, *view)
        if changed:
            self.log.audit("question_typed", key=card.key, chars=len(text), mode=card.mode)
        self._reply(message, render.esc(note), silent=True)

    def _finish_card(self, card: QuestionCard, outcome: str, *, sent: bool = False) -> None:
        """Close a question card (send thread): no buttons, ``outcome`` at the bottom.

        Callers removed it from self.questions already. Once closed no other edit touches it.
        """

        with card.edit_lock:
            with self.lock:
                card.closed = True
                html, _ = self._question_view(card, final=outcome, sent=sent)
            message_id = card.message_id
        if not message_id:
            return  # never sent
        self._edit(message_id, html)
        self._remember_card(message_id, False)
        with self.lock:
            self._closed_cards[message_id] = None
            self._closed_cards.move_to_end(message_id)
            while len(self._closed_cards) > CLOSED_CARDS_MAX:
                self._closed_cards.popitem(last=False)

    def _open_question(self, key: str, *, blocking_only: bool = False) -> QuestionCard | None:
        with self.lock:
            return next((card for card in self.questions.values() if card.key == key
                         and (not blocking_only or card.mode == "blocking")), None)

    def _push_question(self, event: QuestionEvent) -> None:
        """A Codex question card (send thread); a closed question closes its card."""

        if event.agent not in (CODEX, CLAUDE):
            self.log.line(tr('Ignoring question event from {v0} (view-only sources do not accept answers)', v0=event.agent), "WARNING")
            return
        if event.closed:
            self._close_codex_question(event)
            return
        with self.lock:
            if any(card.session_id == event.session_id and card.request_id == event.request_id
                   for card in self.questions.values()):
                return
        key = event.key
        title = self.redact(event.title) or event.session_id[:8]
        thread_id = None
        at_pc = self.presence.at_pc()
        muted_async = event.mode == "async" and self.state.muted(key)
        silent = at_pc or muted_async
        if event.excluded or self.state.locked or not event.questions:
            thread_id = self._topic(key, title, event.agent)
            reason = (tr('directory is in content_exclude') if event.excluded else
                      tr('writes locked') if self.state.locked else tr('unrecognized question format'))
            self._send(tr('❓ <b>{v0}</b> is waiting for your answer ({v1}; answer on the PC).', v0=render.esc(render.topic_name(self.machine, event.agent, title)), v1=reason), thread_id=thread_id, silent=silent, key=key)
            return
        card = QuestionCard(secrets.token_urlsafe(9), event.agent, event.session_id, key,
                            "codex_async" if event.mode == "async" else "codex", title,
                            tuple(self.cfg.allowed_user_ids), questions=list(event.questions),
                            shown=self._shown_questions(list(event.questions)),
                            answers=[[] for _ in event.questions], typed=[False for _ in event.questions],
                            request_id=event.request_id, turn_id=event.turn_id, thread_id=thread_id, mode=event.mode,
                            reminded=not at_pc or muted_async)
        saved = self._saved_async_answer(event.session_id, event.request_id) if event.mode == "async" else None
        if saved is not None:
            card.state, card.tried = "sent", True
            card.client_id, card.marker = str(saved.get("client_id") or ""), str(saved.get("marker") or "")
            card.via, card.job_id = str(saved.get("via") or ""), str(saved.get("job_id") or "")
            card.sent_at = float(saved.get("at") or self.clock())
            card.footer = tr('📨 Answers sent; awaiting the Codex record…')
        try:
            card.thread_id = thread_id = self._topic(key, title, event.agent)
            with self.lock:
                self.questions[card.nonce] = card  # before the card: a quick tap must find it
                html, buttons = self._question_view(card)
            sent = self._send(html, thread_id=thread_id, buttons=buttons, silent=silent, key=key)
        except Exception as exc:  # noqa: BLE001 - permanent errors need a plain PC fallback
            with self.lock:
                if self.questions.get(card.nonce) is card:
                    del self.questions[card.nonce]  # the send loop tries the whole push again
            if isinstance(exc, TelegramError) and _transient(exc):
                raise
            self.log.audit("question_push_failed", key=key, mode=card.mode,
                           code=exc.code if isinstance(exc, TelegramError) else None)
            self.log.line(tr('Failed to send question card: {v0}: {v1}', v0=type(exc).__name__, v1=exc), "WARNING")
            try:
                self._send(tr('❓ <b>{v0}</b> is waiting for your answer (card could not be sent; answer on the PC).', v0=render.esc(render.topic_name(self.machine, event.agent, title))), thread_id=thread_id, silent=silent, key=key)
            except Exception as fallback_exc:  # noqa: BLE001 - the fallback is attempted only once
                self.log.line(tr('Failed to send question notice: {v0}: {v1}', v0=type(fallback_exc).__name__, v1=fallback_exc), "WARNING")
            return
        # A close queued meanwhile runs after this push (same thread), with the message id set.
        card.message_id = sent.get("message_id")
        self._remember_card(card.message_id, True)
        if card.mode == "async" and card.via == "app_tools" and card.job_id:
            self._track(Delivery("codex_async", card.key, card.sent_at, card.message_id, card.thread_id,
                                 marker=card.marker, job_id=card.job_id, info={"nonce": card.nonce}))
        self.log.audit("question_pushed", key=key, mode=card.mode, questions=len(card.questions), silent=silent)

    def _close_codex_question(self, event: QuestionEvent) -> None:
        """The rollout says the question no longer waits (send thread): close its card."""

        with self.lock:
            card = next((card for card in self.questions.values()
                         if card.agent == event.agent and card.session_id == event.session_id
                         and card.request_id == event.request_id), None)
            # The source no longer waits: the saved delivery identity is done with. A lock
            # close keeps it, so a card pushed again after a restart still starts as sent.
            if event.mode == "async":
                self._forget_async_answer(event.session_id, event.request_id)
            if card is None:
                return
            del self.questions[card.nonce]
        answer = render.clip(self.redact(event.answer), QUESTION_ANSWER_SHOWN)[0] if event.answer else ""
        if card.mode == "async":
            mine = any((card.client_id and source == f"client:{card.client_id}")
                       or (card.marker and source == card.marker) for source in event.answer_sources.values())
            outcome = {
                "answered": tr('✅ Answered from Telegram') if mine else tr('Answered on the PC'),
                "superseded": tr('The conversation continued; this question is no longer valid'),
                "gone": tr('Conversation archived or deleted'),
                "restart": tr('Codex Desktop restarted or exited; this question is no longer valid'),
                "desktop_gone": tr('Codex Desktop restarted or exited; this question is no longer valid'),
                "expired": tr('Unanswered for over 6 hours; this question is no longer valid'),
            }.get(event.by, tr('This question is no longer awaiting an answer'))
            if answer:
                outcome += tr(':\n{answer}', answer=answer)
        elif (card.state != "open" or card.tried) and answer:
            outcome = tr('✅ Codex received these answers:\n{v0}', v0=answer)  # the card's or the PC's, whichever came first
        elif card.state == "sent":
            outcome = tr('✅ Codex received the answers.')
        elif answer:
            outcome = tr('💻 Answered on the PC:\n{v0}', v0=answer)
        else:
            outcome = tr('⏹ This question is no longer awaiting an answer (the turn ended or Codex Desktop closed).')
        self._finish_card(card, outcome, sent=card.state == "sent" and not answer
                          and (card.mode != "async" or (event.by == "answered" and mine)))
        self.log.audit("question_closed", key=card.key, mode=card.mode, by=event.by,
                       sent=card.state != "open", answered=bool(event.answer))

    def _close_codex_cards(self) -> int:
        """Writes got locked: open Codex question cards go to the PC; returns how many there were."""

        with self.lock:
            cards = [card for card in self.questions.values() if card.wait is None]
            for card in cards:
                del self.questions[card.nonce]
        for card in cards:
            if card.state == "sent":
                outcome = tr('✅ Answers sent to Codex.')
            elif card.state == "sending":
                outcome = tr('🔒 Writes locked; answers are being sent. Check delivery on the PC.')
            else:
                outcome = tr('🔒 Writes locked; answer on the PC.')
            sent = card.state == "sent"
            self._later("question close", lambda card=card, outcome=outcome, sent=sent:
                        self._finish_card(card, outcome, sent=sent))
        return len(cards)

    def _remind_questions(self) -> None:
        """A loud note under Codex question cards sent silently, once nobody is at the PC any more."""

        with self.lock:
            due = [card for card in self.questions.values()
                   if card.wait is None and not card.reminded and card.state == "open" and card.message_id
                   and not (card.mode == "async" and self.state.muted(card.key))]
        if not due or self.presence.at_pc():
            return
        for card in due:
            card.reminded = True
            self._later("question reminder", lambda card=card: self._question_reminder(card))

    def _question_reminder(self, card: QuestionCard) -> None:
        with self.lock:
            if self.questions.get(card.nonce) is not card or card.state != "open":
                return  # answered or closed while the reminder waited
            if card.mode == "async" and self.state.muted(card.key):
                return
        self._send(tr('⏰ <b>{v0}</b> is still waiting for your answer (buttons are on the card above)', v0=render.esc(card.title)),
                   thread_id=card.thread_id, reply_to=card.message_id, key=card.key)

    def _request_implement(self, nonce: str, message: dict[str, Any]) -> str:
        """[Implement this plan] under a Codex push: a confirmation card first, as for every write."""

        with self.lock:
            offer = self.plan_offers.get(nonce)
        if offer is None or self.clock() - offer.created > PLAN_OFFER_TTL_S:
            return tr('This button expired (too old, or the bridge restarted)')
        if split_key(offer.key)[0] != CODEX:
            self.log.line(tr('Refusing to implement a plan for {v0}', v0=split_key(offer.key)[0]), "WARNING")
            return tr('This conversation is view-only')
        if self.state.locked:
            return tr('writes locked')
        if self._write_in_flight(offer.key):
            return tr('This session still has a pending delivery; try again later')
        pending = self._confirm_card(
            message, action="implement", key=offer.key, text="", title=offer.title,
            note=tr('Codex will switch back to default mode and start implementing this plan (as when clicking “Yes, implement this plan” in Codex Desktop).'),
            thread_id=self._thread_of(message), extra={"turn_id": offer.turn_id})
        if pending is not None:
            self.log.audit("plan_implement_requested", key=offer.key)
        return ""

    # --- [Allow all] grants ---

    def _prune_allow_all(self) -> None:
        now = self.clock()
        with self.lock:
            expired = [grant for grant in self.allow_all.values() if now >= grant.until]
            for grant in expired:
                del self.allow_all[grant.session_id]
        for grant in expired:
            self.log.audit("allow_all_expired", session_id=grant.session_id, allowed=grant.allowed)

    def _allow_all_for(self, session_id: str) -> AllowAll | None:
        """The session's live grant (expired ones are dropped); None while writes are locked."""

        if self.state.locked:
            return None
        self._prune_allow_all()
        with self.lock:
            return self.allow_all.get(session_id)

    def _auto_allow(self, session_id: str) -> bool:
        """Count one automatic allow under the session's grant; False when it has none."""

        if self._allow_all_for(session_id) is None:
            return False
        with self.lock:
            grant = self.allow_all.get(session_id)
            if grant is None:
                return False
            grant.allowed += 1
            grant.allowed_at.append(self.clock())
            del grant.allowed_at[:-1000]
            return True

    def _after_allow_all(self, wait: PermissionWait, others: list[PermissionWait], user_id: int) -> None:
        for item in others:  # other requests of the session that wait at the same time
            self._settle(item, "allow")
        self.log.audit("allow_all_granted", session_id=wait.session_id, user_id=user_id, tool=wait.tool)
        self._later("allow-all notice", lambda: self._send(
            tr('⚡ <b>{v0}</b>: allow-all enabled; future permission requests are automatically allowed for {v1:.0f} hours. Send /ask in the topic or tap the button below to restore individual approvals.', v0=render.esc(wait.title), v1=ALLOW_ALL_TTL_S / 3600),
            thread_id=wait.thread_id, buttons=[[(tr('🔒 Restore individual approvals'), f"aa:off:{wait.session_id}")]], key=wait.key))

    def _revoke_allow_all(self, session_id: str | None, reason: str) -> int:
        """Drop the live grant of one session (or of all sessions); returns how many were dropped."""

        self._prune_allow_all()
        with self.lock:
            if session_id is None:
                dropped = list(self.allow_all.values())
                self.allow_all.clear()
            else:
                grant = self.allow_all.pop(session_id, None)
                dropped = [grant] if grant is not None else []
        for grant in dropped:
            self.log.audit("allow_all_revoked", session_id=grant.session_id, reason=reason, allowed=grant.allowed)
        return len(dropped)

    # --- stop flags (PreToolUse hook) ---

    def claude_pretooluse(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload.get("session_id") or "")
        now = self.clock()
        with self.lock:
            since = self.stop_flags.get(session_id)
            if since is None:
                return {}
            first = self.stop_hits.setdefault(session_id, now)
            if now - since > STOP_FLAG_TTL_S or now - first > STOP_HIT_GRACE_S:
                # Used up: the stopped turn's parallel calls had their grace; the next turn is not stopped.
                self.stop_flags.pop(session_id, None)
                self.stop_hits.pop(session_id, None)
                return {}
        self.log.audit("stop_hit", session_id=session_id, agent_id=payload.get("agent_id"),
                       tool=payload.get("tool_name"))
        return {"continue": False, "stopReason": "已从 Telegram 停止"}  # i18n: data - model protocol text; preserve verbatim

    # --- hook events ---

    def claude_event(self, payload: dict[str, Any]) -> None:
        self.hook_events.append(payload)

    def _drain_hook_events(self) -> None:
        while self.hook_events:
            payload = self.hook_events.popleft()
            event = payload.get("event")
            session_id = str(payload.get("session_id") or "")
            if event in ("Stop", "StopFailure") and not payload.get("agent_id"):
                with self.lock:
                    self.stop_flags.pop(session_id, None)
                    self.stop_hits.pop(session_id, None)
            if self.claude is not None:
                try:
                    self.claude.on_hook(payload)
                except Exception:  # noqa: BLE001
                    self.log.line(tr('Error handling hook event:\n{v0}', v0=traceback.format_exc()), "ERROR")

    # --- polling thread ---

    def _poll_loop(self) -> None:
        while not self.stopping.is_set():
            started = time.monotonic()
            for name, step in (("hooks", self._drain_hook_events), ("codex", self._poll_codex),
                               ("claude", self._poll_claude), ("antigravity", self._poll_antigravity),
                               ("pi", self._poll_pi), ("dsh", self._poll_dsh),
                               ("deliveries", self._check_deliveries),
                               ("housekeeping", self._housekeeping)):
                try:
                    step()
                    self.log.clear(f"poll:{name}")
                except Exception as exc:  # noqa: BLE001 - one bad poll must not stop the bridge
                    self.log.once(f"poll:{name}", tr('Error polling {v0}: {v1}: {v2}\n{v3}', v0=name, v1=type(exc).__name__, v2=exc, v3=traceback.format_exc()),
                                  "ERROR")
            self.stopping.wait(max(0.5, self.poll_s - (time.monotonic() - started)))

    def _poll_codex(self) -> None:
        if self.codex is not None:
            self.codex.poll()

    def _remote_loop(self) -> None:
        """Poll remote-host Codex threads; a failing Codex app is retried with backoff (up to 10 min)."""

        pace = max(REMOTE_MIN_POLL_S, float(self.cfg.codex_remote_poll_s))
        delay = REMOTE_FIRST_POLL_S
        try:
            while not self.stopping.wait(delay):
                try:
                    self.codex.poll_remote()
                    self.log.recovered("poll:codex-remote", tr('Reading remote Codex conversations recovered'))
                    self.log.recovered("poll:codex-remote-list", tr('Listing remote Codex conversations recovered'))
                    delay = pace
                except codex_remote.ListUnavailable as exc:
                    # The known threads were read; only threads new since the listing broke wait for it.
                    self.log.recovered("poll:codex-remote", tr('Reading remote Codex conversations recovered'))
                    self.log.trouble("poll:codex-remote-list",
                                     tr('Failed to list remote Codex conversations; known conversations will still be read directly: {v0}', v0=exc), after_s=REMOTE_QUIET_S)
                    delay = pace
                except Exception as exc:  # noqa: BLE001 - Codex may be closed, restarting or slow
                    # Codex Desktop restarting or still connecting its hosts: only a lasting failure is logged.
                    self.log.trouble("poll:codex-remote", tr('Error reading remote Codex conversations: {v0}: {v1}', v0=type(exc).__name__, v1=exc),
                                     after_s=REMOTE_QUIET_S)
                    delay = min(max(delay, pace) * 2, REMOTE_BACKOFF_MAX_S)
        finally:
            self.codex.remote.close()

    def _poll_claude(self) -> None:
        if self.claude is not None:
            self.claude.poll()
        with self.lock:
            flags = list(self.stop_flags.items())
        for session_id, since in flags:
            view = self.claude.view(session_id) if self.claude is not None else None
            if self.clock() - since > STOP_FLAG_TTL_S or (self.clock() - since > 15 and (view is None or view.state != "running")):
                with self.lock:
                    self.stop_flags.pop(session_id, None)
                    self.stop_hits.pop(session_id, None)

    def _poll_antigravity(self) -> None:
        if self.antigravity is not None:
            self.antigravity.poll()

    def _poll_pi(self) -> None:
        if self.pi is not None:
            self.pi.poll()

    def _poll_dsh(self) -> None:
        if self.dsh is not None:
            self.dsh.poll()

    def _check_deliveries(self) -> None:
        with self.lock:
            items = list(self.deliveries)
        for delivery in items:
            done = False
            try:
                done = self._check_delivery(delivery)
            except Exception as exc:  # noqa: BLE001
                self.log.line(tr('Failed to track delivery: {v0}: {v1}', v0=type(exc).__name__, v1=exc), "WARNING")
                done = self.clock() - delivery.started > 6 * 3600
            if done:
                with self.lock:
                    if delivery in self.deliveries:
                        self.deliveries.remove(delivery)

    def _check_delivery(self, delivery: Delivery) -> bool:
        now = self.clock()
        if delivery.kind == "codex_async":
            with self.lock:
                card = next((card for card in self.questions.values()
                             if card.mode == "async" and card.key == delivery.key
                             and card.job_id == delivery.job_id and card.message_id == delivery.message_id), None)
                if card is None or card.closed:
                    return True
            job = self.codex.job(delivery.job_id)
            state = job.get("state") or {}
            status, phase = state.get("status"), state.get("phase")
            delivery.last_phase = str(phase or "")
            if status in ("failed", "orphaned", "cancelled"):
                error = (job.get("result") or {}).get("error") or state.get("error") or status
                footer = tr('❌ Send failed: {v0}. Tap [📨 Send answers] again or answer on the PC.', v0=self.redact(str(error))[:800])
                self._later("async question failed", lambda card=card, footer=footer, job_id=delivery.job_id:
                            self._reopen_async_card(card, footer, job_id=job_id))
                return True
            # Delivered (the job follows the turn the answer started) or done: the source's
            # attribution closes the card. Stop tracking so a long turn, or a reply the source
            # cannot attribute, never keeps the bridge busy for Install/Restart; the 120 s
            # unconfirmed check covers the rest.
            return status == "succeeded" or phase == "running_turn"
        if delivery.kind == "codex_job":
            job = self.codex.job(delivery.job_id)
            state = job.get("state") or {}
            status, phase = state.get("status"), state.get("phase")
            if status in ("succeeded",):
                self._edit_later(delivery.message_id, tr('✅ Codex finished this turn (see the push for results).'))
                return True
            if status in ("failed", "orphaned", "cancelled"):
                error = (job.get("result") or {}).get("error") or state.get("error") or status
                self._edit_later(delivery.message_id, tr('❌ Send failed: {v0}', v0=render.esc(self.redact(str(error)))[:800]))
                return True
            if phase != delivery.last_phase:
                delivery.last_phase = str(phase or "")
                text = {"waiting_idle": tr('⏳ The conversation is busy; sending after this turn ends…'), "running_turn": tr('✅ Delivered; Codex is running…'),
                        "waiting_retry": tr('⏳ This turn failed; waiting for automatic retry…')}.get(delivery.last_phase)
                if text:
                    self._edit_later(delivery.message_id, text)
            return False
        if delivery.kind == "claude_inbox":
            _, session_id = split_key(delivery.key)
            if self.claude.check_delivered(session_id, delivery.marker, delivery.info):
                self._edit_later(delivery.message_id, tr('✅ Added to the session queue; runs after this turn ends.') if delivery.queued
                                 else tr('✅ Delivered to the session.'))
                self.log.audit("delivered", key=delivery.key)
                return True
            if now - delivery.started > DELIVERY_CONFIRM_S:
                self._edit_later(delivery.message_id, tr('⚠️ Delivery unconfirmed after 90 seconds: the session may have closed, or the message awaits confirmation on the PC. It will not be resent automatically; send again if needed.'))
                return True
            return False
        if delivery.kind == "headless" and delivery.run is not None:
            if headless.run_alive(delivery.run):
                return False
            result = headless.parse_result(delivery.run.log_path, offset=delivery.run.log_offset) or {}
            with self.lock:
                if self.runs.get(delivery.run.session_id) is delivery.run:
                    self.runs.pop(delivery.run.session_id, None)
                self._save_runs()
            if result.get("is_error") or not result:
                reason = self.redact(str(result.get("result") or tr('No result (process exited)')))
                self._edit_later(delivery.message_id, tr('❌ Background Claude run failed: {v0}', v0=render.esc(reason)[:800]))
            else:
                self._edit_later(delivery.message_id, tr('✅ Background Claude run finished (see the push for results).'))
            return True
        return True

    def _housekeeping(self) -> None:
        now = self.clock()
        with self.lock:
            self._expire_pending()
            self._expire_wizards()
            for key in [key for key, (at, _) in self._question_hooks.items() if now - at > QUESTION_HOOK_TTL_S]:
                del self._question_hooks[key]
            for nonce in [key for key, offer in self.plan_offers.items() if now - offer.created > PLAN_OFFER_TTL_S]:
                del self.plan_offers[nonce]
        self._prune_allow_all()
        self._flush_progress()
        self._remind_questions()
        self._remind_codex_approvals()
        self._recheck_async_answers()
        if now >= self._next_title_sync:
            self._next_title_sync = now + TOPIC_TITLE_SYNC_S
            self._sync_topic_titles(now)
        if now < self._next_housekeeping:
            return
        self._next_housekeeping = now + 3600.0
        self._later("members", self._check_members)
        idle_s = self.cfg.topic_idle_close_h * 3600.0
        closes = 0
        for key, topic in self.state.topics().items():
            thread_id = int(topic.get("thread_id") or 0)
            if thread_id <= 0:
                self.state.drop_topic(key)
                continue
            active = float(topic.get("active_at") or topic.get("created_at") or 0)
            if not topic.get("closed") and now - active > idle_s and closes < CLOSES_PER_PASS:
                closes += 1
                self._later("close-topic", lambda key=key, thread_id=thread_id: self._close_topic(key, thread_id))

    def _sync_topic_titles(self, now: float) -> None:
        """Apps can rename a conversation without a new turn to trigger a push."""

        active_s = self.cfg.active_window_h * 3600.0
        candidates = {
            key: topic for key, topic in self.state.topics().items()
            if not topic.get("closed") and int(topic.get("thread_id") or 0) > 0
            and now - float(topic.get("active_at") or topic.get("created_at") or 0) <= active_s
        }
        if not candidates:
            return
        views = {view.key: view for view in self._sessions(TOPIC_TITLE_SYNC_LIMIT)}
        for key, topic in candidates.items():
            view = views.get(key)
            if view is None or not view.title:
                continue
            title = self.redact(view.title)
            name = render.topic_name(self.machine, view.agent, title)
            if name != topic.get("name") and name != self._rename_failed.get(key):
                self._later("rename-topic", lambda key=key, agent=view.agent, title=title:
                            self._rename_topic(key, agent, title))

    def _rename_topic(self, key: str, agent: str, title: str) -> None:
        with self._topic_lock:
            topic = self.state.topic_for(key)
            thread_id = int(topic.get("thread_id") or 0) if topic else 0
            name = render.topic_name(self.machine, agent, title)
            if not topic or topic.get("closed") or thread_id <= 0 or name == topic.get("name"):
                return
            try:
                self.api.edit_topic(self.chat_id, thread_id, name)
            except TelegramError as exc:
                if exc.code != 400 or "not modified" not in str(exc).lower().replace("_", " "):
                    if exc.code is not None:  # refused by Telegram (a network error is retried next pass)
                        self._rename_failed[key] = name
                    self.log.once(f"rename:{key}", tr('Failed to rename topic: {v0}', v0=self.redact(str(exc))), "WARNING")
                    return
            self.state.set_topic(key, thread_id, name)

    def _close_topic(self, key: str, thread_id: int) -> None:
        try:
            self.api.close_topic(self.chat_id, thread_id)
            self.state.set_topic_closed(key, True)
        except TelegramError as exc:
            if exc.code == 400:
                self.state.drop_topic(key)  # deleted in Telegram
            else:
                raise

    # --- outbound pushes ---

    def _send_loop(self) -> None:
        delay = 5.0
        while not self.stopping.is_set():
            item = self.outbox.get(1.0)
            if item is None:
                continue
            created, label, action = item
            if self.clock() - created > PUSH_MAX_AGE_S:
                self.log.line(tr('Discarding expired push: {v0}', v0=label), "WARNING")
                continue
            try:
                action()
                delay = 5.0
                self.log.recovered("send", tr('Telegram pushes recovered'))
            except TelegramError as exc:
                if _transient(exc):
                    if exc.code is None:
                        self._network_trouble()
                    self.log.trouble("send", tr('Telegram push failed: {v0}', v0=exc))
                    self.outbox.put_front(item)
                    self.stopping.wait(delay)
                    delay = min(delay * 2, BACKOFF_MAX_S)
                else:
                    self.log.line(tr('Push rejected by Telegram ({v0}): {v1}', v0=label, v1=exc), "WARNING")
            except Exception:  # noqa: BLE001
                self.log.line(tr('Push error ({v0}):\n{v1}', v0=label, v1=traceback.format_exc()), "ERROR")

    def _send(self, html: str, *, thread_id: int | None = None, buttons: list[list[tuple[str, str]]] | None = None,
              silent: bool = False, key: str | None = None, reply_to: int | None = None) -> dict[str, Any]:
        options: dict[str, Any] = {"reply_to": reply_to} if reply_to else {}
        try:
            sent = self.api.send_message(self.chat_id, html, thread_id=thread_id, buttons=buttons, silent=silent,
                                         **options)
        except TelegramError as exc:
            reason = str(exc).lower()
            if thread_id is None or exc.code != 400:
                raise
            if "topic_closed" in reason or "topic closed" in reason:
                # Closed in Telegram (by hand or by housekeeping): reopen and send again.
                self.api.reopen_topic(self.chat_id, thread_id)
                if key:
                    self.state.set_topic_closed(key, False)
                sent = self.api.send_message(self.chat_id, html, thread_id=thread_id, buttons=buttons, silent=silent,
                                             **options)
            elif "thread" in reason:
                # The topic was deleted in Telegram: fall back to General; the next push creates a new one.
                if key:
                    self.state.drop_topic(key)
                sent = self.api.send_message(self.chat_id, html, buttons=buttons, silent=silent)
            else:
                raise
        if key and sent.get("message_id"):
            self.state.remember_message(int(sent["message_id"]), key)
        return sent

    def _reply(self, message: dict[str, Any], html: str, *, buttons: list[list[tuple[str, str]]] | None = None,
               silent: bool = False) -> dict[str, Any] | None:
        try:
            return self.api.send_message(self.chat_id, html, thread_id=self._thread_of(message),
                                         reply_to=message.get("message_id"), buttons=buttons, silent=silent)
        except TelegramError as exc:
            self.log.line(tr('Reply failed: {v0}', v0=exc), "WARNING")
            return None

    def _reply_document(self, message: dict[str, Any], filename: str, text: str) -> None:
        try:
            self.api.send_document(self.chat_id, filename, _utf8(text), thread_id=self._thread_of(message),
                                   reply_to=message.get("message_id"), silent=True)
        except TelegramError as exc:
            self.log.line(tr('Failed to send file: {v0}', v0=exc), "WARNING")

    def _topic(self, key: str, title: str, agent: str) -> int | None:
        """Thread id of the session's topic, created (or reopened) on demand; None -> General.

        Serialized: a permission card on a hook thread and a push on the send thread for the same
        new session must not create two topics.
        """

        with self._topic_lock:
            topic = self.state.topic_for(key)
            now = self.clock()
            thread_id = int(topic.get("thread_id") or 0) if topic else 0
            if topic and thread_id > 0:
                if topic.get("closed"):
                    try:
                        self.api.reopen_topic(self.chat_id, thread_id)
                        self.state.set_topic_closed(key, False)
                    except TelegramError as exc:
                        self.log.line(tr('Failed to reopen topic: {v0}', v0=exc), "WARNING")
                        if exc.code != 400:
                            return thread_id  # transient: the send itself will retry or fall back
                        self.state.drop_topic(key)
                        topic = None
                if topic:
                    self.state.touch_topic(key, now)
                    name = render.topic_name(self.machine, agent, title)
                    if title and name != topic.get("name"):
                        try:
                            self.api.edit_topic(self.chat_id, thread_id, name)
                            self.state.set_topic(key, thread_id, name)
                        except TelegramError:
                            pass
                    return thread_id
            if now < self._topic_pause_until:
                return None
            name = render.topic_name(self.machine, agent, title or split_key(key)[1][:8])
            try:
                thread_id = self.api.create_topic(self.chat_id, name)
            except TelegramError as exc:
                if exc.code in (400, 403):
                    # Not a forum, or no right to manage topics: stop trying for a while.
                    self._topic_pause_until = now + TOPIC_PAUSE_S
                self.log.line(tr('Failed to create topic: {v0}', v0=exc), "WARNING")
                return None
            self.state.set_topic(key, thread_id, name)
            self.state.touch_topic(key, now)
            return thread_id

    def _open_topic(self, key: str, message: dict[str, Any]) -> None:
        view = self._view(key)
        if view is None:
            self._reply(message, tr('Session not found.'), silent=True)
            return
        thread_id = self._topic(key, self.redact(view.title), view.agent)
        readonly = view.agent in READONLY_AGENTS or view.kind.startswith("remote:")
        instruction = (tr('This conversation is view-only. Reply in its desktop application; /read shows recent turns.') if readonly else
                       tr('Type directly in this topic to send to the session; /read shows recent turns.'))
        html = (f"{render.status_icon(view.state)} <b>{render.esc(render.topic_name(self.machine, view.agent, self.redact(view.title)))}</b>\n"
                f"<i>{render.esc(self.redact(view.cwd))}</i>\n{instruction}")
        self._send(html, thread_id=thread_id, silent=True, key=key)

    # --- mid-turn messages ---

    def _progress_hidden(self, key: str, cwd: str = "") -> bool:
        return self.state.muted(key) or self._content_blocked() or bool(cwd and self.cfg.excluded(cwd))

    def _push_progress(self, event: ProgressEvent) -> None:
        """Show what a running turn says: edit its message, or start one (a new message notifies)."""

        key = event.key
        if self._progress_hidden(key, event.cwd):
            self._drop_progress(key)
            return
        if event.agent == CLAUDE and any(digest for _, _, digest in event.items):
            # A question card shows these and takes the answers; the progress message keeps the rest.
            kept = [(kind, text) for kind, text, digest in event.items
                    if not self._card_shows(event.session_id, digest)]
            if not kept:
                return
            if len(kept) < len(event.items):
                kind, asks = progress.summarize(kept)
                event = replace(event, texts=[text for _, text in kept], items=[], kind=kind, asks_user=asks)
        texts = [text for text in (self.redact(item).strip()[:PROGRESS_ITEM_CHARS] for item in event.texts) if text]
        if not texts:
            return
        now = self.clock()
        with self.lock:
            card = self.progress_cards.get(key)
            fresh = card is not None and now - card.created < float(self.cfg.progress_new_message_s)
            if card is not None and fresh and not event.asks_user:
                card.texts.extend(texts)
                card.pending, card.cwd = True, event.cwd or card.cwd
                due = now - card.edited >= PROGRESS_EDIT_MIN_S
                replaced = None
            else:
                replaced, card = card, None
        if card is not None:
            if due:
                self._edit_progress(key)
            return
        if replaced is not None and replaced.pending:
            self._edit_progress(key)  # what waited on the old message goes there before a new one starts
        # A new message: the first of the turn, a long gap, or the agent addresses the user.
        title = self.redact(event.title) or event.session_id[:8]
        icon = "❓" if event.kind == "question" else "💬"
        header = tr('{v0} <b>{v1}</b> In progress', v0=icon, v1=render.esc(render.topic_name(self.machine, event.agent, title)))
        card = ProgressCard(0, None, now, header, texts, edited=now, cwd=event.cwd)
        thread_id = self._topic(key, title, event.agent)
        silent = not event.asks_user or self.presence.at_pc()
        sent = self._send(self._progress_html(card), thread_id=thread_id, silent=silent, key=key)
        card.message_id, card.thread_id = int(sent.get("message_id") or 0), thread_id
        with self.lock:
            self.progress_cards[key] = card
        self.log.audit("progress_pushed", key=key, kind=event.kind, texts=len(texts), asks_user=event.asks_user,
                       silent=silent)

    @staticmethod
    def _progress_html(card: ProgressCard) -> str:
        """Header plus the newest texts that fit; older ones give way (and are dropped from the card)."""

        shown: list[str] = []
        size = render.utf16_len(card.header) + 4
        for text in reversed(card.texts):
            part = render.esc(text)
            if shown and size + render.utf16_len(part) + 2 > PROGRESS_LIMIT:
                break
            shown.append(part)
            size += render.utf16_len(part) + 2
        more = "…\n\n" if len(shown) < len(card.texts) else ""
        if more:
            card.dropped = True
            del card.texts[:-len(shown)]
        elif card.dropped:
            more = "…\n\n"  # dropped by an earlier edit
        return f"{card.header}\n\n{more}" + "\n\n".join(reversed(shown))

    def _drop_progress(self, key: str) -> None:
        with self.lock:
            self.progress_cards.pop(key, None)

    def _edit_progress(self, key: str) -> None:
        """Send the texts waiting on the session's message (send thread)."""

        with self.lock:
            card = self.progress_cards.get(key)
            if card is not None:
                card.flush_queued = False
            if card is None or not card.pending or not card.message_id:
                return
        if self._progress_hidden(key, card.cwd):
            self._drop_progress(key)  # locked, muted or excluded meanwhile: what waited stays unsent
            return
        with self.lock:
            card.pending, card.edited = False, self.clock()
            html = self._progress_html(card)
        try:
            self.api.edit_message(self.chat_id, card.message_id, html)
        except TelegramError as exc:
            if _transient(exc):
                with self.lock:
                    card.pending = True  # network trouble or rate limit: housekeeping tries again
                self.log.trouble(f"progress:{key}", tr('Failed to update in-progress message: {v0}', v0=exc))
            else:
                self.log.line(tr('Failed to update in-progress message: {v0}', v0=exc), "WARNING")
                if exc.code == 400:  # deleted in Telegram (or unchanged): the next text starts a new message
                    with self.lock:
                        if self.progress_cards.get(key) is card:
                            del self.progress_cards[key]
        else:
            self.log.recovered(f"progress:{key}", tr('Updating in-progress messages recovered'))

    def _flush_progress(self) -> None:
        """Poll thread: texts that waited out the edit interval go out (one queued edit per message)."""

        now = self.clock()
        with self.lock:
            due = [key for key, card in self.progress_cards.items()
                   if card.pending and not card.flush_queued and now - card.edited >= PROGRESS_EDIT_MIN_S]
            for key in due:
                self.progress_cards[key].flush_queued = True
        for key in due:
            self._later("progress edit", lambda key=key: self._edit_progress(key))

    def _push_turn(self, event: TurnEvent) -> None:
        key = event.key
        blocked = self._content_blocked()
        excluded = self.cfg.excluded(event.cwd) or tr('Content hidden') in event.labels
        if key in self.progress_cards:
            if not (blocked or excluded or self.state.muted(key)):
                self._edit_progress(key)  # what waited for the edit interval, before the result
            self._drop_progress(key)
        if self.state.muted(key):
            return
        hide = excluded or blocked
        labels = list(event.labels)
        if excluded and tr('Content hidden') not in labels:
            labels.append(tr('Content hidden'))
        if blocked:
            labels.append(tr('Locked; titles only'))
        title = self.redact(event.title) or event.session_id[:8]
        duration = None
        if event.started_at and event.completed_at:
            duration = max(0.0, event.completed_at - event.started_at)
        # A Codex plan (plan mode) can be implemented from here, as Codex Desktop offers after the turn.
        offer = event.agent == CODEX and event.plan and not hide and not self.state.locked
        if offer:
            labels.append(tr('Plan complete: tap [🚀 Implement this plan] or reply to this message to have Codex revise it'))
        # The plan limits left follow every push (account numbers, not the conversation's content).
        footer = quota.quota_line(event.quota, self.clock()) if self._quota_shown() else ""
        html, _truncated = render.turn_card(
            machine=self.machine, agent=event.agent, title=title, status=event.status, duration_s=duration,
            text=None if hide else (self.redact(event.final_text) if event.final_text else None),
            error="" if hide else self.redact(event.error), labels=labels,
            summary="" if hide else self.redact(event.summary), footer=footer, limit=self.cfg.summary_chars)
        buttons = None if hide else [[(tr('📄 Full text'), f"full:{key}"), (tr('🕘 Last 3 turns'), f"read:{key}")]]
        if offer and buttons is not None:
            nonce = secrets.token_urlsafe(9)
            with self.lock:
                self.plan_offers[nonce] = PlanOffer(key, event.turn_id, title, self.clock())
                while len(self.plan_offers) > PLAN_OFFERS_MAX:
                    self.plan_offers.popitem(last=False)
            buttons.append([(tr('🚀 Implement this plan'), f"pl:{nonce}")])
        thread_id = self._topic(key, title, event.agent)
        silent = event.quiet or self.presence.at_pc()
        self._send(html, thread_id=thread_id, buttons=buttons, silent=silent, key=key)
        self.log.audit("pushed", key=key, status=event.status, chars=len(event.final_text or ""), silent=silent)

    def _quota_shown(self) -> bool:
        """Plan limits go out unless switched off or a security lock limits pushes to titles."""

        return bool(getattr(self.cfg, "quota_enabled", True)) and not self._content_blocked()

    def _quota_lines(self) -> list[str]:
        """/status: the plan limits each source last saw (a failing source shows nothing)."""

        now = self.clock()
        lines = []
        for source in (self.codex, self.claude, self.antigravity, self.pi, self.dsh):
            try:
                line = quota.quota_line(source.quota(), now) if source is not None else ""
            except Exception:  # noqa: BLE001 - one unreadable source must not break /status
                line = ""
            if line:
                lines.append(render.esc(line))
        return lines

    def _push_attention(self, event: AttentionEvent) -> None:
        key = event.key
        if self.state.muted(key) and not event.loud:
            return
        # Claude Code 2.1.286 reports "input needed" for AskUserQuestion (and dialogs, elicitations)
        # and "permission prompt" for every other tool, ExitPlanMode included.
        permission = "permission" in event.reason.lower()
        question = "input needed" in event.reason.lower()
        note = ""
        if event.agent == CLAUDE and (permission or question):
            def matches(wait: PermissionWait) -> bool:
                asks = wait.tool == progress.QUESTION_TOOL
                return wait.session_id == event.session_id and not wait.event.is_set() and asks == question

            def covers(wait: PermissionWait) -> bool:
                return matches(wait) and not (wait.message_id is None and wait.card_failed)

            cards: list[PermissionWait] = []
            retrying = False
            # The hook may reach the bridge a moment later, if a card can come at all.
            steps = ATTENTION_CARD_WAIT_STEPS if self._card_possible(event.session_id) else 1
            for step in range(steps):
                with self.lock:
                    cards = [wait for wait in self.permissions.values() if covers(wait)]
                    retrying = any(matches(wait) and wait.message_id is None and wait.card_failed
                                   for wait in self.permissions.values())
                if (cards or retrying or (permission and self._allow_all_for(event.session_id) is not None)
                        or step + 1 >= steps or self.stopping.wait(0.5)):
                    break
            if cards:
                # The approval or question card is the alert; a loud one only adds a nudge under the newest card.
                if event.loud:
                    for wait in cards:
                        wait.reminded = True
                    self._permission_reminder(max(cards, key=lambda wait: wait.arrived))
                return
            if retrying:
                note = tr('The card cannot be sent right now; the bridge is retrying. You can also handle it on the PC.')
            elif not permission:
                pass  # a dialog, or a question without a card: the plain alert below
            elif self._allow_all_for(event.session_id) is not None:
                if not event.loud:
                    return  # the bridge allows it right away; the registry caught the moment in between
                note = tr('Allow-all did not intercept this dialog (it did not pass through the bridge); handle it on the PC.')
            else:
                note = self._no_card_reason(event.cwd)
        title = self.redact(event.title) or event.session_id[:8]
        html = (tr('⚠️ <b>{v0}</b> Waiting for action: {v1}', v0=render.esc(render.topic_name(self.machine, event.agent, title)), v1=render.esc(self.redact(event.reason))))
        if note:
            html += f"\n<i>{render.esc(note)}</i>"
        thread_id = self._topic(key, title, event.agent)
        silent = not event.loud and self.presence.at_pc()
        self._send(html, thread_id=thread_id, silent=silent, key=key)

    def _no_card_reason(self, cwd: str) -> str:
        """Why a waiting permission prompt has no approval card."""

        if self.state.locked:
            return tr('Writes are locked; this request can only be handled on the PC.')
        if cwd and self.cfg.excluded(cwd):
            return tr('The directory is in content_exclude; this request can only be handled on the PC.')
        return (tr('This request was not sent to Telegram (e.g. hooks were not installed, the bridge restarted, or it came from a sub-agent or claude -p while you were at the PC); handle it on the PC.'))

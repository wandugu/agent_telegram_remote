"""Plain data shared by the Codex, Claude and Antigravity sources and the bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CODEX = "codex"
CLAUDE = "claude"
ANTIGRAVITY = "antigravity"  # view only: read from its local files
PI = "pi"  # view only: read from its local files
DSH = "dsh"  # view only: read from its local files


def session_key(agent: str, session_id: str) -> str:
    return f"{agent}:{session_id}"


def split_key(key: str) -> tuple[str, str]:
    agent, _, session_id = key.partition(":")
    return agent, session_id


@dataclass
class SessionView:
    """One conversation as listed by /list and used for routing."""

    agent: str  # CODEX | CLAUDE | ANTIGRAVITY
    id: str  # Codex thread id, Claude session id or Antigravity conversation id
    title: str
    cwd: str
    state: str  # running | waiting | idle | unknown
    last_activity: float  # epoch seconds, 0.0 when unknown
    live: bool = False  # Claude: a live process owns it; Codex: a Desktop conversation Codex can receive messages in
    detail: str = ""  # waitingFor, a one-line summary or an error
    kind: str = ""  # desktop | cli | headless | exec ...

    @property
    def key(self) -> str:
        return session_key(self.agent, self.id)


@dataclass
class QuotaWindow:
    """One plan limit (the 5-hour or the weekly one): how much of it is used, and when it resets."""

    label: str  # e.g. "5h", a week, or several days
    used_percent: float
    resets_at: float | None = None  # epoch seconds; None when the source does not say
    span_s: float | None = None  # the window's length: numbers older than this no longer count


@dataclass
class Quota:
    """An agent's plan limits as last seen: Codex from its rollouts, Claude from Claude Desktop's record."""

    agent: str
    windows: list[QuotaWindow]
    observed_at: float  # epoch seconds the numbers are from
    source: str = ""  # "desktop": Claude Desktop's local usage record, which can be hours old


@dataclass
class TurnEvent:
    """A turn that finished (completed, failed or aborted)."""

    agent: str
    session_id: str
    title: str
    cwd: str
    status: str  # completed | failed | aborted
    final_text: str | None = None
    error: str = ""
    turn_id: str = ""
    started_at: float | None = None  # epoch seconds
    completed_at: float | None = None
    labels: list[str] = field(default_factory=list)  # short tags such as auto-continue
    summary: str = ""  # one-line summary when the agent provides one (Claude Desktop postTurnSummary)
    quiet: bool = False  # push without a notification (for example a failure codex-watch will retry)
    plan: bool = False  # Codex: the turn ended with a proposed plan (plan mode): it may be implemented
    quota: Quota | None = None  # the agent's plan limits when the turn ended, shown under the push

    @property
    def key(self) -> str:
        return session_key(self.agent, self.session_id)


@dataclass
class AttentionEvent:
    """A conversation waits for the user (permission prompt, question, dialog)."""

    agent: str
    session_id: str
    title: str
    cwd: str
    reason: str
    loud: bool = False  # still waiting after the alert delay: notify even at the PC

    @property
    def key(self) -> str:
        return session_key(self.agent, self.session_id)


@dataclass
class ProgressEvent:
    """What the agent told the user while its turn is still running (one or more new messages)."""

    agent: str
    session_id: str
    title: str
    cwd: str
    texts: list[str]
    kind: str = "text"  # text | narration | message (SendUserMessage) | question (AskUserQuestion) | commentary
    asks_user: bool = False  # addressed to the user (a question, a request, SendUserMessage)
    # Claude: ``texts`` as (kind, text, digest); the digest (progress.interactive_digest) marks an
    # AskUserQuestion or ExitPlanMode text, which gives way to the question card of the same call.
    items: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return session_key(self.agent, self.session_id)


@dataclass
class Question:
    """One question an agent asks with options (Codex request_user_input, Claude AskUserQuestion)."""

    id: str  # Codex: the question id; Claude: the question text (the key of its answers)
    text: str
    header: str = ""
    options: list[tuple[str, str]] = field(default_factory=list)  # (label, description); a typed answer is fine too
    multi: bool = False  # several options may be chosen


@dataclass
class QuestionEvent:
    """Codex questions waiting for answers, or closed with their reason and recorded answers.

    Blocking requests wait within their running turn; async requests can outlive the turn.
    Async answers and their sources are keyed by the original question positions.
    """

    agent: str
    session_id: str
    title: str
    cwd: str
    request_id: str  # the blocking or async question call id
    turn_id: str = ""
    questions: list[Question] = field(default_factory=list)
    closed: bool = False  # no longer pending: answered (see answer) or closed for another reason (by)
    answer: str = ""  # closed: the answers as given, one line per question ("" when none)
    excluded: bool = False  # the folder is in content_exclude: no question text may go out
    mode: str = "blocking"  # blocking | async
    by: str = ""  # why a closed request stopped waiting
    answered: dict[int, str] = field(default_factory=dict)
    answer_sources: dict[int, str] = field(default_factory=dict)  # client:<id> | tg:<marker> | ""

    @property
    def key(self) -> str:
        return session_key(self.agent, self.session_id)


@dataclass
class ApprovalEvent:
    """One local Desktop command/file approval, identified by its exact request id and digest."""

    agent: str
    session_id: str
    title: str
    cwd: str
    request_id: int | str
    method: str
    item_id: str
    turn_id: str
    approval_id: str | None
    digest: str
    kind: str  # command | file
    command: str = ""
    command_cwd: str = ""
    reason: str = ""
    network: Any = None
    extra_permissions: Any = None
    files: list[str] = field(default_factory=list)
    available_decisions: list[Any] | None = None
    excluded: bool = False
    closed: bool = False
    by: str = ""

    @property
    def key(self) -> str:
        return session_key(self.agent, self.session_id)


Event = TurnEvent | AttentionEvent | ProgressEvent | QuestionEvent | ApprovalEvent

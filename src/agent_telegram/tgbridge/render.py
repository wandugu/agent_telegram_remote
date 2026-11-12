"""Telegram HTML (parse_mode=HTML) formatting helpers for the bridge.

Callers pass already-redacted text; permission_card and session_line also take
an optional ``redact`` callable, because they read raw values themselves (the
tool input, a SessionView). Every length here is measured in
UTF-16 code units, which is how Telegram counts; that is never less than the
number of Python characters, so ``len(result) <= limit`` holds as well.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Iterable, Sequence

from agent_telegram.i18n import tr
from agent_telegram.tgbridge.model import ANTIGRAVITY, CLAUDE, CODEX, DSH, PI, ApprovalEvent, Question, SessionView

MAX_TEXT = 4096
MAX_TOPIC_NAME = 128
ELLIPSIS = "…"
_SOURCE_NO_REPLY = '(no final reply)'
_SOURCE_NO_TITLE = '(untitled)'
# Longest cwd / tool name / mode shown on a permission card.
_FIELD_LIMIT = 300
# /list lines: 30 of them (the bridge's maximum) must fit one message, so the title gets 40 units
# and the detail 60 (about 130 units per numbered line).
_LIST_TITLE_LIMIT = 40
_LIST_DETAIL_LIMIT = 60
_MAX_TREE_DEPTH = 64

_STATUS_ICONS = {
    "completed": "✅",
    "failed": "❌",
    "aborted": "⏹",
    "running": "⏳",
    "waiting": "⚠️",
    "idle": "💤",
    "unknown": "❔",
}
_AGENT_LABELS = {CODEX: "Codex", CLAUDE: "Claude", ANTIGRAVITY: "Antigravity", PI: "Pi", DSH: "DSH"}
_CONFIRM_ACTIONS = {
    "send": '📨 Confirm sending to {target}?',
    "stop": "⏹ Confirm interrupting {target}'s current turn?",
    "new": '🆕 Confirm creating a session in {target}?',
    "implement": '🚀 Confirm asking {target} to implement this plan?',
}

# C0/C1 control characters except \t and \n, bidi controls (U+061C, U+200E/F, U+202A-202E,
# U+2066-2069), zero-width and other invisible format characters (U+00AD soft hyphen, U+180E,
# U+200B-200D, U+2060-2064, deprecated U+206A-206F, U+FEFF, U+FFF9-FFFB, the U+E0000-E007F tag
# characters used to smuggle hidden text), the U+2028/2029 line and paragraph separators, and lone
# surrogates (which only appear in broken input and cannot be sent as UTF-8).
_INVISIBLE_RE = re.compile(
    r"[\x00-\x08\x0b-\x1f\x7f-\x9f\xad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u206f"
    r"\ufeff\ufff9-\ufffb\ud800-\udfff\U000e0000-\U000e007f]"
)
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_BACKTICKS_RE = re.compile(r"`+")


# --- basic helpers ---------------------------------------------------------------


def esc(text: str) -> str:
    """html.escape(text, quote=False), made safe to send as UTF-8.

    Surrogate pairs are joined and lone surrogates (from broken JSON escapes) become U+FFFD, which
    keeps the UTF-16 length; otherwise the HTTP client fails to encode the whole message.
    """

    if _SURROGATE_RE.search(text):
        text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return html.escape(text, quote=False)


def visible(text: str) -> str:
    """Show control, bidi, zero-width and other invisible characters as ⟨U+XXXX⟩.

    CRLF becomes LF first; a lone CR is shown.
    """

    return _INVISIBLE_RE.sub(lambda match: f"⟨U+{ord(match.group()):04X}⟩", text.replace("\r\n", "\n"))


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _fits(text: str, limit: int) -> bool:
    return len(text) <= limit and utf16_len(text) <= limit


def _prefix_len(text: str, units: int) -> int:
    """Number of leading characters of text that fit into `units` UTF-16 code units."""

    head = text[: max(0, units)]
    if utf16_len(head) <= units:  # no astral characters in the way
        return len(head)
    count = 0
    for index, ch in enumerate(text):
        count += 2 if ord(ch) > 0xFFFF else 1
        if count > units:
            return index
    return len(text)


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def _shorten(text: str, limit: int) -> str:
    """Hard cut to `limit` UTF-16 units with an ellipsis (for one-line fields)."""

    if _fits(text, limit):
        return text
    if limit <= 1:
        return ELLIPSIS[:limit]
    return text[: _prefix_len(text, limit - 1)].rstrip() + ELLIPSIS


def clip(text: str, limit: int) -> tuple[str, bool]:
    """Cut text to at most `limit` units, appending "…"; returns (text, truncated).

    Prefers to cut at a blank line, else at a line end, found in the last 20% of the kept part.
    """

    limit = max(1, int(limit))
    if _fits(text, limit):
        return text, False
    room = limit - 1
    end = _prefix_len(text, room)
    head = text[:end]
    floor = int(end * 0.8)
    cut = head.rfind("\n\n", floor)
    if cut < floor:
        cut = head.rfind("\n", floor)
    if cut >= floor and cut > 0:
        head = head[:cut]
    return head.rstrip() + ELLIPSIS, True


def split_text(text: str, limit: int = 3900) -> list[str]:
    """Split plain text into chunks of at most `limit` units on line boundaries.

    Lines longer than the limit are split hard (at a space in the last 20% when there is one).
    Concatenating the chunks gives the text back, except that whitespace-only chunks are dropped
    (Telegram refuses empty messages).
    """

    limit = max(1, int(limit))
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in (text or "").splitlines(keepends=True):
        size = utf16_len(line)
        if current_len + size <= limit:
            current.append(line)
            current_len += size
            continue
        if current:
            chunks.append("".join(current))
            current, current_len = [], 0
        # Walk the long line by position (re-slicing and re-measuring the rest on every piece
        # would be quadratic on a multi-megabyte single line).
        start = 0
        while size > limit:
            piece = line[start : start + limit]  # `limit` characters are at least `limit` units
            end = max(1, _prefix_len(piece, limit))
            space = piece.rfind(" ", int(end * 0.8), end)
            if space > 0:
                end = space + 1
            chunks.append(piece[:end])
            start += end
            size -= utf16_len(piece[:end])
        line = line[start:]
        if line:
            current, current_len = [line], size
    if current:
        chunks.append("".join(current))
    return [chunk for chunk in chunks if chunk.strip()]


def duration(seconds: float | None) -> str:
    """Compact duration, or an empty string for None."""

    if seconds is None:
        return ""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError, OverflowError):
        return ""
    total = max(0, total)
    if total < 60:
        return tr('{v0}s', v0=total)
    if total < 3600:
        return tr('{v0}m', v0=total // 60)
    hours, minutes = total // 3600, total % 3600 // 60
    return tr('{v0}h{v1}m', v0=hours, v1=minutes) if minutes else tr('{v0}h', v0=hours)


def ago(epoch: float, now: float) -> str:
    """Relative age, or an empty string when the time is unknown."""

    if not epoch or epoch <= 0:
        return ""
    delta = max(0.0, float(now) - float(epoch))
    if delta < 60:
        return tr('just now')
    if delta < 3600:
        return tr('{v0} minutes ago', v0=int(delta // 60))
    if delta < 86400:
        return tr('{v0} hours ago', v0=int(delta // 3600))
    return tr('{v0} days ago', v0=int(delta // 86400))


def status_icon(status: str) -> str:
    return _STATUS_ICONS.get(status, _STATUS_ICONS["unknown"])


def agent_label(agent: str) -> str:
    return _AGENT_LABELS.get((agent or "").lower(), agent or "?")


def topic_name(machine: str, agent: str, title: str) -> str:
    """Forum topic name "{machine}·{Agent}·{title}": one line, no invisible characters, 1..128 units."""

    title = _INVISIBLE_RE.sub("", _one_line(title)).strip() or tr('(untitled)')
    name = _INVISIBLE_RE.sub("", _one_line(f"{machine}·{agent_label(agent)}·{title}")).strip()
    return _shorten(name, MAX_TOPIC_NAME) or tr('(untitled)')


# --- cards -----------------------------------------------------------------------


def turn_card(
    *,
    machine: str,
    agent: str,
    title: str,
    status: str,
    duration_s: float | None,
    text: str | None,
    error: str = "",
    labels: Iterable[str] = (),
    summary: str = "",
    footer: str = "",
    limit: int = 1500,
) -> tuple[str, bool]:
    """Card for a finished turn; returns (html, truncated). ``footer`` (one line, e.g. the plan
    limits left) goes under the reply in italics."""

    header = f"{status_icon(status)} {topic_name(machine, agent, title)}"
    took = duration(duration_s)
    first_plain = header + (f" · {took}" if took else "")
    lines = [f"<b>{esc(header)}</b>" + (f" · {esc(took)}" if took else "")]
    plain = [first_plain]
    label_line = _shorten(" · ".join(filter(None, (_one_line(label) for label in labels or ()))), 300)
    if label_line:
        lines.append(f"<i>{esc(label_line)}</i>")
        plain.append(label_line)
    summary_line = _shorten(_one_line(summary), 500)
    if summary_line:
        lines.append(esc(summary_line))
        plain.append(summary_line)
    body_source = (text or "").strip() or (error or "").strip() or tr('(no final reply)')
    footer_line = _shorten(_one_line(footer), 300)
    tail = f"\n\n<i>{esc(footer_line)}</i>" if footer_line else ""
    body, truncated = clip(body_source, _body_room(limit, plain, extra=utf16_len(tail)))
    return "\n".join(lines) + "\n\n" + esc(body) + tail, truncated


def session_line(view: SessionView, now: float, *, redact: Callable[[str], str] | None = None) -> str:
    """One escaped line for /list: "{icon} {Agent} · {title} · {ago}" (+ " · " + detail).

    The title is shortened to 40 units and the detail to 60, so that 30 numbered lines fit one
    message. ``redact`` (optional) is applied to the title and detail, which come straight from
    the sources.
    """

    red = redact if redact is not None else _same
    parts = [
        f"{status_icon(view.state)} {agent_label(view.agent)}",
        _shorten(_INVISIBLE_RE.sub("", _one_line(red(view.title or ""))).strip() or tr('(untitled)'), _LIST_TITLE_LIMIT),
    ]
    when = ago(view.last_activity, now)
    if when:
        parts.append(when)
    detail = _shorten(_INVISIBLE_RE.sub("", _one_line(red(view.detail or ""))).strip(), _LIST_DETAIL_LIMIT)
    if detail:
        parts.append(detail)
    return esc(" · ".join(parts))


def permission_card(
    *,
    machine: str,
    title: str,
    cwd: str,
    tool_name: str,
    tool_input: dict,
    permission_mode: str,
    agent_id: str | None,
    limit: int = 2500,
    redact: Callable[[str], str] | None = None,
    agent: str = CLAUDE,
) -> tuple[str, bool, str]:
    """Approval card for a Claude Code PermissionRequest; returns (html, truncated, full_plain_text).

    The tool input is shown as indented JSON with invisible characters made visible. ``redact``
    (optional) is applied to the title and cwd, to every string in the tool input before it is
    serialised (where shell quoting is still intact, e.g. ``TOKEN="a b"``) and to the serialised
    text again (for key/value pairs and anything split across strings). The full plain text is
    Markdown, meant to be sent as a .md document when truncated.
    """

    red = redact if redact is not None else _same
    dump = _dump(tool_input)
    size = tr('{v0} bytes / {v1} lines', v0=len(dump.encode('utf-8', 'surrogatepass')), v1=dump.count(chr(10)) + 1)
    body_full = visible(red(_dump(_redact_tree(tool_input, red, 0)) if redact is not None else dump))
    header = tr('🔐 {v0} requests permission', v0=topic_name(machine, agent, red(str(title or ''))))
    fields: list[tuple[str, str]] = [
        (tr('Tool'), visible(_one_line(tool_name)) or "?"),
        (tr('Directory'), visible(red(str(cwd or ""))) or "?"),
        (tr('Mode'), visible(_one_line(permission_mode)) or "default"),
    ]
    if agent_id:
        fields.append((tr('Sub-agent'), visible(_one_line(agent_id))))
    fields.append((tr('Size'), size))
    # The card shows bounded fields; the Markdown text keeps them whole.
    shown = [(name, _shorten(value, _FIELD_LIMIT)) for name, value in fields]

    plain_head = [header, *(tr('{name}: {value}', name=name, value=value) for name, value in shown)]
    body, truncated = clip(body_full, _body_room(limit, plain_head))
    card = (
        f"<b>{esc(header)}</b>\n"
        + "\n".join(
            tr('{name}: {value}', name=name, value=esc(value)) if name == tr('Size') else tr('{name}: <code>{value}</code>', name=name, value=esc(value))
            for name, value in shown
        )
        + f"\n\n<pre>{esc(body)}</pre>"
    )
    fence = "`" * max(3, _longest_backticks(body_full) + 1)
    full = (
        f"# {header}\n\n"
        + "".join(tr('- {name}: {value}\n', name=name, value=value) for name, value in fields)
        + f"\n{fence}json\n{body_full}\n{fence}\n"
    )
    return card, truncated, full


def codex_approval_input(event: ApprovalEvent) -> dict:
    """Display only the approval's reviewable input, never its IPC identifiers."""

    return {
        name: value for name, value in (
            ("command", event.command), ("cwd", event.command_cwd),
            ("reason", event.reason), ("network", event.network),
            ("extra_permissions", event.extra_permissions), ("files", event.files),
        ) if value is not None and value != "" and value != []
    }


def codex_approval_card(
    *,
    machine: str,
    title: str,
    cwd: str,
    kind: str,
    tool_input: dict,
    limit: int = 2500,
    redact: Callable[[str], str] | None = None,
) -> tuple[str, bool, str]:
    """Command/file approval using the same safe JSON display as Claude permissions.

    Callers fully redact title/cwd and lightly redact the input. Any shortened display field
    also requires viewing the complete document before allowing the approval.
    """

    if kind not in ("command", "file"):
        raise ValueError("Only Codex command and file approvals have cards")
    card, truncated, full = permission_card(
        machine=machine, title=title, cwd=cwd,
        tool_name=tr('Codex command') if kind == "command" else tr('Codex file changes'),
        tool_input=tool_input, permission_mode=tr('Approve each action'), agent_id=None,
        limit=limit, redact=redact, agent=CODEX,
    )
    red = redact if redact is not None else _same
    whole_title = _INVISIBLE_RE.sub("", _one_line(red(str(title or "")))).strip() or tr('(untitled)')
    whole_topic = _INVISIBLE_RE.sub("", _one_line(f"{machine}·{agent_label(CODEX)}·{whole_title}")).strip()
    if topic_name(machine, CODEX, red(str(title or ""))) != whole_topic:
        truncated = True
        # permission_card's ordinary title is bounded for display; the review document keeps it.
        full = tr('- Full title: {v0}\n\n', v0=visible(red(str(title or '')))) + full
    if not _fits(visible(red(str(cwd or ""))) or "?", _FIELD_LIMIT):
        truncated = True
    return card, truncated, full


_QUESTION_TEXT_LIMIT = 600
_OPTION_LIMIT = 200
_ANSWER_LIMIT = 300
_FOOTER_LIMIT = 1000


def option_letter(index: int) -> str:
    return chr(ord("A") + index) if 0 <= index < 26 else str(index + 1)


def button_label(text: str, limit: int = 40) -> str:
    """One-line button text without invisible characters, at most ``limit`` units."""

    return _shorten(_INVISIBLE_RE.sub("", _one_line(text)).strip() or "?", limit)


def question_card(
    *,
    machine: str,
    agent: str,
    title: str,
    questions: Sequence[Question],
    answers: Sequence[Sequence[str]],
    typed: Sequence[bool],
    index: int,
    review: bool,
    footer: str = "",
    limit: int = 3800,
) -> tuple[str, bool]:
    """Card for an agent's questions; returns (html, truncated). Callers pass redacted text.

    Every question is listed with its options (letters A, B, ...); the one being answered is
    marked unless the card is in review (all of them visited; index -1 marks none). What was
    picked or typed so far shows under each question. ``footer`` is a status or help line in
    italics and always shows. A card too long for one message keeps the question being
    answered whole and shortens the others; ``truncated`` is set whenever any question text,
    header or option shows shortened or not at all.
    """

    head = tr('❓ {v0} has a question for you', v0=topic_name(machine, agent, title))
    footer = _shorten(footer.strip(), _FOOTER_LIMIT)
    cut = False

    def short(text: str, size: int) -> str:
        nonlocal cut
        shown = _shorten(text, size)
        cut = cut or shown != text
        return shown

    def block(number: int, question: Question, level: str) -> tuple[str, str]:
        current = number == index and not review
        label = question.header or tr('Question {v0}', v0=number + 1)
        mark = "👉 " if current else ""
        line = f"{mark}{number + 1}/{len(questions)} · {short(_one_line(label), 80)}"
        parts = [(f"<b>{esc(line)}</b>", line)]
        if current or level != "bare":  # bare: the others keep their header and answer
            text = short(question.text.strip(), _QUESTION_TEXT_LIMIT if current or level == "full" else 120)
            parts.append((esc(text), text))
        for position, (option, description) in enumerate(question.options if current or level == "full" else ()):
            entry = f"{option_letter(position)}. {short(_one_line(option), 80)}"
            if level != "bare" and description.strip():
                entry += f" — {_one_line(description)}"
            entry = short(entry, _OPTION_LIMIT)
            parts.append((esc(entry), entry))
        chosen = answers[number] if number < len(answers) else []
        if chosen:
            shown = _shorten(_one_line(tr('; ').join(chosen)), _ANSWER_LIMIT)
            prefix = tr('✍️ Entered: ') if number < len(typed) and typed[number] else tr('✅ Selected: ')
            parts.append((f"<i>{esc(prefix + shown)}</i>", prefix + shown))
        elif review or number < index:
            parts.append((tr('<i>(unanswered)</i>'), tr('(unanswered)')))
        return "\n".join(html for html, _ in parts), "\n".join(plain for _, plain in parts)

    room = min(int(limit), MAX_TEXT)
    tail = [(f"<i>{esc(footer)}</i>", footer)] if footer else []
    blocks: list[tuple[str, str]] = []
    for level in ("full", "compact", "bare"):
        blocks = [(f"<b>{esc(head)}</b>", head)]
        blocks += [block(number, question, level) for number, question in enumerate(questions)]
        if sum(utf16_len(html) + 2 for html, _ in blocks + tail) <= room:
            return "\n\n".join(html for html, _ in blocks + tail), cut or level != "full"
    # Still too long: the questions give way, never the footer (it says what happened).
    body, _ = _fit_blocks(blocks, room - sum(utf16_len(html) + 2 for html, _ in tail))
    return "\n\n".join([body, *(html for html, _ in tail)]), True


def plan_card(*, machine: str, title: str, plan: str, feedback: str = "", footer: str = "",
              limit: int = 3800) -> tuple[str, bool]:
    """Card for a plan waiting for approval (Claude ExitPlanMode); returns (html, truncated)."""

    head = tr("📋 {v0}'s plan is waiting for approval", v0=topic_name(machine, CLAUDE, title))
    tail: list[tuple[str, str]] = []
    if feedback.strip():
        note = tr('✍️ Requested changes: ') + _shorten(feedback.strip(), 800)
        tail.append((f"<i>{esc(note)}</i>", note))
    footer = _shorten(footer.strip(), _FOOTER_LIMIT)
    if footer:
        tail.append((f"<i>{esc(footer)}</i>", footer))
    # The rest of the card plus tags and blank lines; the plan gets what is left, measured escaped.
    room = max(100, min(int(limit), MAX_TEXT) - utf16_len(head) - sum(utf16_len(html) + 2 for html, _ in tail) - 60)
    text, size = plan.strip() or tr('(the plan is empty)'), room
    while True:
        body, truncated = clip(text, size)
        overflow = utf16_len(esc(body)) - room
        if overflow <= 0 or size <= 1:
            break
        size = max(1, size - overflow)
    blocks = [(f"<b>{esc(head)}</b>", head), (f"<blockquote>{esc(body)}</blockquote>", body), *tail]
    html, cut = _fit_blocks(blocks, limit)
    return html, truncated or cut


def question_markdown(*, machine: str, agent: str, title: str, questions: Sequence[Question] = (),
                      plan: str = "") -> str:
    """The whole question set or plan as Markdown, for the full-text document."""

    lines = [f"# {topic_name(machine, agent, title)}", ""]
    if plan:
        lines += [plan.strip(), ""]
    for number, question in enumerate(questions, 1):
        lines += [tr('## {v0}. {v1}', v0=number, v1=question.header or tr('Question')), "", question.text.strip(), ""]
        for position, (option, description) in enumerate(question.options):
            lines.append(f"- **{option_letter(position)}. {option}**" + (f" — {description}" if description else ""))
        lines.append("")
    return "\n".join(lines)


def _fit_blocks(blocks: list[tuple[str, str]], limit: int) -> tuple[str, bool]:
    """Join (html, plain) blocks with blank lines, dropping trailing ones that do not fit."""

    limit = min(int(limit), MAX_TEXT)
    kept: list[str] = []
    used = 0
    for position, (html_part, plain) in enumerate(blocks):
        size = utf16_len(html_part) + (2 if kept else 0)
        if used + size > limit - 2:
            if position == 0:
                return esc(_shorten(plain, limit)), True
            kept.append(ELLIPSIS)
            return "\n\n".join(kept), True
        kept.append(html_part)
        used += size
    return "\n\n".join(kept), False


def confirm_card(*, machine: str, agent: str, title: str, action: str, text: str, limit: int = 1500) -> str:
    """Confirmation prompt for a write action ("send", "stop", "new" or "implement") quoting the text."""

    template = _CONFIRM_ACTIONS.get(action)
    if template is None:
        raise ValueError(tr('Unknown confirmation action: {v0}', v0=action))
    target = topic_name(machine, agent, title)
    header = {
        'send': tr('📨 Confirm sending to {target}?', target=target),
        'stop': tr("⏹ Confirm interrupting {target}'s current turn?", target=target),
        'new': tr('🆕 Confirm creating a session in {target}?', target=target),
        'implement': tr('🚀 Confirm asking {target} to implement this plan?', target=target),
    }[action]
    card = f"<b>{esc(header)}</b>"
    quoted = (text or "").strip()
    if not quoted:
        return card
    note_room = 60  # the "only the first N characters" note below
    body, truncated = clip(quoted, _body_room(limit, [header], extra=note_room))
    card += f"\n<blockquote>{esc(body)}</blockquote>"
    if truncated:
        card += tr('\n<i>(Showing the first {v0} of {v1} characters; confirmation sends the full text)</i>', v0=len(body) - 1, v1=len(quoted))
    return card


# --- internals -------------------------------------------------------------------


def _same(text: str) -> str:
    return text


def _dump(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError, RecursionError):  # non-str keys, cycles, absurd nesting
        try:
            return repr(value)
        except Exception:  # noqa: BLE001 - RecursionError or a broken __repr__
            return tr('(Cannot display tool input: {v0})', v0=type(value).__name__)


def _redact_tree(node: object, red: Callable[[str], str], depth: int, path: frozenset[int] = frozenset()) -> object:
    """Copy of a JSON-like value with every string value redacted (keys are kept).

    Nesting deeper than _MAX_TREE_DEPTH and cycles are left as they are: the serialised text is
    redacted as a whole afterwards anyway.
    """

    if isinstance(node, str):
        return red(node)
    if depth > _MAX_TREE_DEPTH or id(node) in path:
        return node
    if isinstance(node, dict):
        inner = path | {id(node)}
        return {key: _redact_tree(value, red, depth + 1, inner) for key, value in node.items()}
    if isinstance(node, (list, tuple)):
        inner = path | {id(node)}
        return [_redact_tree(item, red, depth + 1, inner) for item in node]
    return node


def _body_room(limit: int, head_lines: list[str], *, extra: int = 0) -> int:
    """Body limit so that header lines + separators + body stay within MAX_TEXT."""

    head = sum(utf16_len(line) for line in head_lines) + len(head_lines) + 2 + extra
    return max(1, min(int(limit), MAX_TEXT - head))


def _longest_backticks(text: str) -> int:
    return max((len(match.group()) for match in _BACKTICKS_RE.finditer(text)), default=0)


def __getattr__(name: str) -> str:
    """Resolve legacy message exports at access time, including for the pending bridge."""
    if name == 'NO_REPLY':
        return tr('(no final reply)')
    if name == 'NO_TITLE':
        return tr('(untitled)')
    raise AttributeError(name)

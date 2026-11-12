"""What an agent tells the user while its turn is still running.

Claude Code transcripts hold one content block per record. Between tool calls the user sees
text blocks, thinking blocks that the API tagged as narration (summaries of the prose between
tool calls, shown like text; ordinary thinking stays private), SendUserMessage calls and
AskUserQuestion calls. Records of the final answer end the turn (stop_reason end_turn) and are
pushed with the finished turn instead. AskUserQuestion and ExitPlanMode texts give way to the
question card the bridge sends from the PermissionRequest hook, when it sends one.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from typing import Any

from agent_telegram.i18n import tr
from agent_telegram.tgbridge.model import Question

NARRATION = "narration"
MAX_TEXT = 4000  # one message
USER_MESSAGE_TOOLS = ("SendUserMessage", "Brief")
QUESTION_TOOL = "AskUserQuestion"
PLAN_TOOL = "ExitPlanMode"
# Tools that wait for the person's answer (Claude Code ignores a hook's plain "allow" for them).
INTERACTIVE_TOOLS = frozenset({QUESTION_TOOL, PLAN_TOOL})

# Requests addressed to the user; a message ending in a question
# counts too. Indirect questions and the model checking itself are left out.
_ASKS = re.compile(
    # i18n: data-begin
    r"请(?!求)|麻烦你|需要你|你需要|你来|帮我|你能|你确认|告诉我|"
    r"\bplease\b|\bcan you\b|\bcould you\b|\bwould you\b|\bdo you want\b|\blet me know\b",
    # i18n: data-end
    re.IGNORECASE,
)
# Quoted text (code, UI strings, file names) says nothing about who is addressed.
_QUOTED = re.compile(r"`[^`]*`|“[^”]*”|「[^」]*」|\"[^\"\n]*\"")  # i18n: data


def asks_user(text: str) -> bool:
    text = _QUOTED.sub(" ", text or "").strip()
    return bool(_ASKS.search(text)) or text.endswith(("?", "？"))  # i18n: data


def _varint(data: bytes, at: int) -> tuple[int, int] | None:
    value, shift = 0, 0
    for index in range(at, min(at + 10, len(data))):
        byte = data[index]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, index + 1
        shift += 7
    return None


def _field(data: bytes, wanted: int) -> bytes | None:
    """The last length-delimited protobuf field ``wanted`` of ``data``; None if absent or malformed."""

    found: bytes | None = None
    at = 0
    while at < len(data):
        head = _varint(data, at)
        if head is None:
            return None
        key, at = head
        wire, number = key & 7, key >> 3
        if wire == 0:
            skip = _varint(data, at)
            if skip is None:
                return None
            at = skip[1]
        elif wire == 1:
            at += 8
        elif wire == 2:
            size = _varint(data, at)
            if size is None or size[0] > len(data) - size[1]:
                return None
            start, at = size[1], size[1] + size[0]
            if number == wanted:
                found = data[start:at]
        elif wire == 5:
            at += 4
        else:
            return None
        if at > len(data):
            return None
    return found


def block_kind(signature: Any) -> str | None:
    """The block kind the API put in a thinking signature (field 2 > 1 > 8), as Claude Code reads it.

    Fail-closed: anything that does not parse is None (treat it as ordinary, private thinking).
    """

    if not isinstance(signature, str) or not signature:
        return None
    try:
        raw = base64.b64decode(signature + "=" * (-len(signature) % 4), validate=False)
    except (binascii.Error, ValueError):
        return None
    outer = _field(raw, 2)
    inner = _field(outer, 1) if outer is not None else None
    kind = _field(inner, 8) if inner is not None else None
    if kind is None:
        return None
    try:
        return kind.decode("utf-8")
    except UnicodeDecodeError:
        return None


MAX_QUESTIONS = 4  # AskUserQuestion's own bound (Codex asks at most 3)
MAX_OPTIONS = 8


def parse_options(raw: Any) -> list[tuple[str, str]]:
    """(label, description) of a question's options, whatever shape the agent wrote ([] for none)."""

    if not isinstance(raw, list):
        return []
    return [(str(option["label"]), str(option.get("description") or ""))
            for option in raw if isinstance(option, dict) and option.get("label")][:MAX_OPTIONS]


def ask_questions(tool_input: Any) -> list[Question]:
    """The questions of an AskUserQuestion call ([] when the input holds none).

    Answers are keyed by the question text. "text" and "number" questions (the extended form some
    hosts render) have no options: they take a typed answer.
    """

    items = tool_input.get("questions") if isinstance(tool_input, dict) else None
    found: list[Question] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("question"), str) or not item["question"].strip():
            continue
        options = parse_options(item.get("options"))
        found.append(Question(id=item["question"], text=item["question"].strip(),
                              header=str(item.get("header") or item.get("title") or "").strip(),
                              options=options, multi=bool(item.get("multiSelect")) and bool(options)))
    return found[:MAX_QUESTIONS]


def interactive_digest(tool_name: Any, tool_input: Any) -> str:
    """Identity of what an AskUserQuestion or ExitPlanMode call shows ("" for other calls).

    Computed the same way from the transcript's tool_use and from the PermissionRequest hook's
    input (both carry the question texts or the plan), so a progress text and the question card
    for the same call can be matched.
    """

    if not isinstance(tool_input, dict):
        return ""
    if tool_name == QUESTION_TOOL:
        basis = "\n".join(question.id for question in ask_questions(tool_input))
    elif tool_name == PLAN_TOOL:
        basis = str(tool_input.get("plan") or "").strip()
    else:
        return ""
    if not basis:
        return ""
    return hashlib.sha256(f"{tool_name}\n{basis}".encode("utf-8", "replace")).hexdigest()[:16]


def summarize(items: list[tuple[str, str]]) -> tuple[str, bool]:
    """(kind, asks_user) of a batch of (kind, text) mid-turn messages: the most pressing kind first."""

    kinds = [kind for kind, _ in items]
    kind = next((name for name in ("question", "message", "narration") if name in kinds), "text")
    return kind, any(name in ("question", "message") or asks_user(text) for name, text in items)


def _question_text(tool_input: Any) -> str:
    parts: list[str] = []
    for question in ask_questions(tool_input):
        line = question.text
        if question.options:
            line += "\n" + " / ".join(tr('“{label}”', label=label.strip()) for label, _ in question.options)
        parts.append(line)
    return "\n\n".join(parts)


def claude_items(record: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(kind, text, digest) the user sees from one transcript record while the turn runs.

    ``digest`` is interactive_digest() for AskUserQuestion and ExitPlanMode items ("" otherwise);
    [] for records that show nothing.
    """

    if record.get("type") != "assistant" or record.get("isSidechain") or record.get("isMeta"):
        return []
    message = record.get("message")
    if not isinstance(message, dict) or message.get("stop_reason") != "tool_use":
        return []  # the final answer (end_turn) is pushed with the finished turn
    found: list[tuple[str, str, str]] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            text = str(block.get("text") or "").strip()
            if text:
                found.append(("text", text[:MAX_TEXT], ""))
        elif kind == "thinking":
            text = str(block.get("thinking") or "").strip()
            if text and block_kind(block.get("signature")) == NARRATION:
                found.append(("narration", text[:MAX_TEXT], ""))
        elif kind == "tool_use":
            name = block.get("name")
            tool_input = block.get("input")
            if name in USER_MESSAGE_TOOLS and isinstance(tool_input, dict):
                text = str(tool_input.get("message") or tool_input.get("content") or "").strip()
                if text:
                    found.append(("message", text[:MAX_TEXT], ""))
            elif name == QUESTION_TOOL:
                text = _question_text(tool_input)
                if text:
                    found.append(("question", (text + tr('\n\n(Answer on the PC)'))[:MAX_TEXT],
                                  interactive_digest(name, tool_input)))
            elif name == PLAN_TOOL and isinstance(tool_input, dict):
                plan = str(tool_input.get("plan") or "").strip()
                if plan:
                    found.append(("question", (tr('A plan is waiting for your approval (handle it on the PC):\n') + plan)[:MAX_TEXT],
                                  interactive_digest(name, tool_input)))
    return found

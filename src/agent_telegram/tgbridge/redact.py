"""Conservative redaction of everything the bridge sends to Telegram.

Two layers: exact values read from local configuration (Claude Code settings,
Codex auth, the bot and hook tokens) and credential-shaped patterns. The
patterns cover URI credentials, authorization headers, cookies and secret
assignments, plus well-known token prefixes, Telegram bot tokens, JWTs and Chinese labels. When in
doubt the text is redacted: a lost word in a push is cheaper than a leaked key.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from agent_telegram.i18n import tr
_SOURCE_MARK = '‹redacted›'

# Exact secrets shorter than this would redact ordinary words.
MIN_SECRET_LEN = 8
# load_local_secrets only keeps values at least this long.
MIN_LOCAL_SECRET_LEN = 12
_MAX_CONFIG_BYTES = 8 * 1024 * 1024
_MAX_DEPTH = 32
_SECRET_KEY_RE = re.compile(r"token|key|secret|password|credential|auth", re.IGNORECASE)
# "Bearer <token>" / "Basic <credentials>" / "token <token>" as stored in header settings.
_SCHEME_VALUE_RE = re.compile(r"(?i)(?:bearer|basic|token)\s+(\S{8,})")

# --- general credential patterns ----------------------------------------------
# "PRIVATE KEY" also covers "PGP PRIVATE KEY BLOCK". The block ends at its END line (bounded: a
# key is a few KB, and an unbounded lazy scan per BEGIN would be quadratic); a key cut off before
# its END line loses the base64 lines that follow, separated by whitespace or by JSON-escaped
# line breaks ("\n" as two characters, as in a serialised tool input).
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY[^-\r\n]*-----"
    r"(?:.{0,16384}?-----END [^-\r\n]*PRIVATE KEY[^-\r\n]*-----|(?:(?:\s|\\[rn])+[A-Za-z0-9+/=]{8,})+)",
    re.IGNORECASE | re.DOTALL,
)
# The user part may be empty ("redis://:password@host"), and the password runs to the last "@"
# before the path, so a password containing "@" is not half shown.
_URI_CREDENTIAL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,31}://[^:/@\s]*:)[^/\s]+@")
_AUTHORIZATION = re.compile(r"(?i)\b(authorization\s*:\s*)(?:bearer|basic|token)\s+[^\s,;]+")
_AUTH_SCHEME = re.compile(r"(?i)\b(?:bearer|basic)\s+([^\s,;]+)")
_COOKIE = re.compile(r"(?im)\b(set-cookie|cookie)\s*:\s*[^\r\n]+")
_SECRET_WORD = (
    r"(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|private[_-]?key|credential)"
)
_NAME_CHAR = r"[a-z0-9_-]"
# A whole run of name characters that contains a secret word. The original "[a-z0-9_-]*" affixes
# after "\b" were quadratic on long runs such as "a-a-a-..." or base64url blobs; starting only at
# the beginning of a run, finding the word with a lookahead and taking the run possessively keeps
# the scan linear without limiting the length of the name.
_SECRET_NAME = rf"(?<!{_NAME_CHAR})(?={_NAME_CHAR}*?{_SECRET_WORD}){_NAME_CHAR}++"
# The optional (possibly JSON-escaped) quote after the name also catches JSON keys
# ("api_key": "..."). Values: a double- or single-quoted string, a JSON-escaped quoted string
# (\"...\" inside a serialised JSON string), or a bare word. Names right after "?" or "&" are query
# parameters, handled by _SECRET_QUERY without eating the rest of the URL.
_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)(?<![?&])({_SECRET_NAME}(?:\\?[\"'])?\s*[:=]\s*)"
    r"(\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|\\\"(?:[^\"\\]|\\[^\"])*?\\\"|[^\s,;]+)"
)
_SECRET_QUERY = re.compile(rf"(?i)([?&]{_SECRET_NAME}=)[^&#\s]+")

# --- additional token shapes -----------------------------------------------------
_TOKEN_SHAPES = re.compile(
    "|".join(
        (
            r"\bsk-[A-Za-z0-9_-]{16,}",
            r"\bgh[pousr]_[A-Za-z0-9]{20,}",
            r"\bgithub_pat_[A-Za-z0-9_]{20,}",
            r"\bglpat-[A-Za-z0-9_-]{16,}",
            r"\bhf_[A-Za-z0-9]{20,}",
            r"\bxox[abposr]-[A-Za-z0-9-]{10,}",
            r"\bAIza[0-9A-Za-z_-]{30,}",
            r"\bya29\.[0-9A-Za-z_-]{20,}",  # Google OAuth access token
            r"\bGOCSPX-[0-9A-Za-z_-]{20,}",  # Google OAuth client secret
            r"\bnpm_[A-Za-z0-9]{30,}",
            r"\bAKIA[0-9A-Z]{16}\b",
            # Telegram bot token, also inside https://api.telegram.org/bot<token>/method
            r"(?:\b|(?<=bot))\d{6,12}:[A-Za-z0-9_-]{30,}",
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}",
        )
    )
)
_CHINESE_LABEL = re.compile(r"((?:密码|口令|密钥|验证码|令牌)\s*[:：=＝]\s*)\S+")  # i18n: data

_TRAILING_PUNCT = ".,;:!?)]}'\"。，；：！？）"  # i18n: data


def _looks_like_credential(word: str) -> bool:
    """Whether the word after "Bearer"/"Basic" is a credential rather than prose ("basic idea")."""

    if tr('‹redacted›') in word:
        return True
    core = word.rstrip(_TRAILING_PUNCT)
    if len(core) < 8:
        return False
    if len(core) >= 20:  # prose words are hardly ever this long; all-lowercase tokens can be
        return True
    if any(ch.isdigit() or ch in "_~+/=-." for ch in core):
        return True
    return any(ch.isupper() for ch in core[1:]) and any(ch.islower() for ch in core)


def _auth_scheme(match: re.Match[str]) -> str:
    return tr('‹redacted›') if _looks_like_credential(match.group(1)) else match.group(0)


def _assignment(match: re.Match[str]) -> str:
    """Replace the value, keeping its quotes (plain or JSON-escaped) when it had them."""

    value = match.group(2)
    for quote in ('\\"', '"', "'"):
        if len(value) >= 2 * len(quote) and value.startswith(quote) and value.endswith(quote):
            return f"{match.group(1)}{quote}{tr('‹redacted›')}{quote}"
    return f"{match.group(1)}{tr('‹redacted›')}"


def _patterns() -> tuple[tuple[re.Pattern[str], str | Callable[[re.Match[str]], str]], ...]:
    """Resolve replacement text for the language active during this redaction."""
    return (
        (_PRIVATE_KEY, tr('‹redacted›')),
        (_TOKEN_SHAPES, tr('‹redacted›')),
        (_URI_CREDENTIAL, rf"\g<1>{tr('‹redacted›')}@"),
        (_AUTHORIZATION, rf"\g<1>{tr('‹redacted›')}"),
        (_AUTH_SCHEME, _auth_scheme),
        (_COOKIE, rf"\g<1>: {tr('‹redacted›')}"),
        (_SECRET_QUERY, rf"\g<1>{tr('‹redacted›')}"),
        (_SECRET_ASSIGNMENT, _assignment),
        (_CHINESE_LABEL, rf"\g<1>{tr('‹redacted›')}"),
    )


def redact_patterns(text: str) -> str:
    """Apply only the credential patterns (no exact secrets)."""

    for pattern, replacement in _patterns():
        text = pattern.sub(replacement, text)
    return text


class Redactor:
    """Callable redactor: exact local secrets first (longest first), then patterns.

    Thread-safe: add_secret swaps in a new compiled pattern, callers never lock.
    """

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._lock = threading.Lock()
        self._secrets: set[str] = set()
        self._exact: re.Pattern[str] | None = None
        for value in secrets:
            self._add(value)
        self._compile()

    def add_secret(self, value: str) -> None:
        with self._lock:
            if self._add(value):
                self._compile()

    def _add(self, value: Any) -> bool:
        if not isinstance(value, str):
            return False
        value = value.strip()
        if len(value) < MIN_SECRET_LEN or value in self._secrets:
            return False
        self._secrets.add(value)
        return True

    def _compile(self) -> None:
        ordered = sorted(self._secrets, key=lambda item: (-len(item), item))
        self._exact = re.compile("|".join(map(re.escape, ordered))) if ordered else None

    def __call__(self, text: str) -> str:
        if not isinstance(text, str):
            # Callers pass str; anything else (None, an exception, a number) becomes text rather
            # than raising inside a log or send path.
            text = "" if text is None else str(text)
        if not text:
            return text
        exact = self._exact
        if exact is not None:
            text = exact.sub(tr('‹redacted›'), text)
        return redact_patterns(text)

    def light(self, text: str) -> str:
        """Exact secrets and token shapes only, for text the owner must see as it is.

        Approval cards use this: the line and block patterns (cookies, private keys, secret
        assignments) can swallow the rest of a command, so an approved command could differ
        from the one shown.
        """

        if not isinstance(text, str):
            text = "" if text is None else str(text)
        if not text:
            return text
        exact = self._exact
        if exact is not None:
            text = exact.sub(tr('‹redacted›'), text)
        return _TOKEN_SHAPES.sub(tr('‹redacted›'), text)


def load_local_secrets(claude_home: Path, codex_home: Path) -> list[str]:
    """Secret-looking values from Claude Code settings.json and Codex auth.json.

    Walks the JSON recursively and keeps string values of at least 12 characters
    whose key, or the key of any enclosing object or list, looks secret (so
    ``"credentials": {"github": "..."}`` counts). For values such as
    ``"Bearer <token>"`` the bare token is kept as well. Never raises:
    unreadable or malformed files are skipped.
    """

    found: list[str] = []
    for path in _candidate_files(claude_home, codex_home):
        try:
            with open(path, "rb") as handle:
                raw = handle.read(_MAX_CONFIG_BYTES + 1)
            if len(raw) > _MAX_CONFIG_BYTES:
                continue
            data = json.loads(raw.decode("utf-8-sig"))
        except Exception:  # noqa: BLE001 - missing, locked or malformed: nothing to add
            continue
        try:
            _collect(data, False, found, 0)
        except Exception:  # noqa: BLE001 - keep what was collected so far
            pass
    seen: set[str] = set()
    unique: list[str] = []
    for value in found:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _candidate_files(claude_home: Path, codex_home: Path) -> list[Path]:
    files: list[Path] = []
    for home, name in ((claude_home, "settings.json"), (codex_home, "auth.json")):
        try:
            if home:
                files.append(Path(home) / name)
        except Exception:  # noqa: BLE001 - odd argument types
            continue
    return files


def _collect(node: Any, secret_key: bool, out: list[str], depth: int) -> None:
    if depth > _MAX_DEPTH:
        return
    if isinstance(node, dict):
        for key, value in node.items():
            _collect(value, secret_key or bool(_SECRET_KEY_RE.search(str(key))), out, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _collect(item, secret_key, out, depth + 1)
    elif isinstance(node, str) and secret_key:
        value = node.strip()
        if len(value) >= MIN_LOCAL_SECRET_LEN:
            out.append(value)
            scheme = _SCHEME_VALUE_RE.fullmatch(value)
            if scheme:
                out.append(scheme.group(1))


def __getattr__(name: str) -> str:
    """Resolve legacy message exports at access time, including for the pending bridge."""
    if name == 'MARK':
        return tr('‹redacted›')
    raise AttributeError(name)

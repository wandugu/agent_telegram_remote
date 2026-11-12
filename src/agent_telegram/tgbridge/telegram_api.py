"""Small synchronous Telegram Bot API client used by the bridge.

Everything goes through :meth:`BotAPI.call`: one ``httpx.Client`` (explicit proxy,
``trust_env=False``, certificate checks on), JSON bodies or multipart uploads,
Telegram's ``{"ok": false, ...}`` answers turned into :class:`TelegramError`
(:class:`Conflict` for 409), 429 answers retried after ``retry_after`` and one
shared, thread-safe token bucket for every method that posts into a chat.

The bot token is part of every request URL, so neither the URL nor the token may
reach an exception message or a log line: httpx's own request logging is turned
down to WARNING at import and every error text is scrubbed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import posixpath
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx

from agent_telegram.i18n import tr
# httpx logs "HTTP Request: POST https://api.telegram.org/bot<token>/..." at INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

API_BASE = "https://api.telegram.org"
MAX_RETRY_AFTER_S = 60.0
MAX_429_RETRIES = 3
DEFAULT_RETRY_AFTER_S = 3.0  # a 429 without parameters.retry_after
CALLBACK_DATA_MAX_BYTES = 64
TOPIC_NAME_MAX = 128
CAPTION_MAX = 1024
CALLBACK_TEXT_MAX = 200
# A client replaced after a transport error is closed this long after (requests still running on
# it finish first: none takes longer than its timeout).
RETIRED_CLIENT_CLOSE_S = 600.0
PHOTO_MAX_BYTES = 10 * 1024 * 1024      # sendPhoto's upload limit; larger images must go as documents
DOCUMENT_MAX_BYTES = 50 * 1024 * 1024   # sendDocument's upload limit for bots
_SOURCE_SCRUB_MARK = '‹redacted›'

# Methods that post into (or change something in) the chat share one token bucket.
RATE_LIMITED_METHODS = frozenset(
    {
        "sendMessage",
        "sendDocument",
        "sendPhoto",
        "editMessageText",
        "editMessageReplyMarkup",
        "createForumTopic",
        "editForumTopic",
        "closeForumTopic",
        "reopenForumTopic",
        "deleteMessage",
    }
)

_METHOD_RE = re.compile(r"^[A-Za-z]{1,64}$")
_TOKEN_SHAPE_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")
_TOKEN_TEXT_RE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")
_API_URL_RE = re.compile(r"https?://api\.telegram\.org/\S*", re.IGNORECASE)

# Errors that only say "nothing to do"; the helpers named below return None for them.
_NOT_MODIFIED = ("message is not modified",)
_TOPIC_NOT_MODIFIED = ("topic_not_modified",)
_TOPIC_ALREADY_CLOSED = ("topic_not_modified", "topic_closed", "topic is closed", "topic already closed")
_TOPIC_ALREADY_OPEN = ("topic_not_modified", "topic is not closed", "topic already open", "topic already opened")
_CALLBACK_EXPIRED = ("query is too old", "query id is invalid")

# Fixed table instead of ``mimetypes``: on Windows that module reads HKEY_CLASSES_ROOT, so
# the answer would depend on what is installed on the machine.
_DOCUMENT_TYPES = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".log": "text/plain",
    ".json": "application/json",
    ".jsonl": "application/json",
    ".csv": "text/csv",
    ".html": "text/html",
    ".diff": "text/x-diff",
    ".patch": "text/x-diff",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".zip": "application/zip",
}

Buttons = list[list[tuple[str, str]]]


class TelegramError(RuntimeError):
    """A failed Bot API call. ``code`` is None for network errors (no HTTP answer)."""

    def __init__(
        self,
        description: str,
        *,
        code: int | None = None,
        retry_after: float | None = None,
        method: str = "",
    ) -> None:
        self.description = description
        self.code = code
        self.retry_after = retry_after
        self.method = method
        where = f"Telegram {method}" if method else "Telegram"
        detail = tr(' ({code})', code=code) if code is not None else ""
        super().__init__(tr('{v0} call failed{v1}: {v2}', v0=where, v1=detail, v2=description))


class Conflict(TelegramError):
    """409: another getUpdates consumer is running or a webhook is set (token may have leaked)."""


def _scrub(text: str, token: str) -> str:
    """Remove the bot token and any Bot API URL from text meant for errors or logs."""

    if token:
        text = text.replace(token, tr('‹redacted›'))
        bot_id, _, secret = token.partition(":")
        # Percent-encoded colon (either case), then the secret half on its own.
        text = re.sub(re.escape(bot_id) + "%3a" + re.escape(secret), tr('‹redacted›'), text, flags=re.IGNORECASE)
        if len(secret) >= 16:
            text = text.replace(secret, tr('‹redacted›'))
    text = _API_URL_RE.sub("api.telegram.org/" + tr('‹redacted›'), text)
    return _TOKEN_TEXT_RE.sub(tr('‹redacted›'), text)


def _matches(error: TelegramError, needles: tuple[str, ...]) -> bool:
    text = error.description.lower()
    return any(needle in text for needle in needles)


def inline_keyboard(buttons: Buttons) -> dict[str, Any]:
    """``[[(text, data), ...], ...]`` -> reply_markup; ``data`` "url:<url>" makes a URL button."""

    rows: list[list[dict[str, str]]] = []
    for row in buttons:
        out: list[dict[str, str]] = []
        for text, data in row:
            if not text:
                raise ValueError(tr('Button text cannot be empty'))
            if data.startswith("url:"):
                url = data[4:]
                if not url:
                    raise ValueError(tr('Button “{v0}” has an empty URL', v0=text))
                out.append({"text": text, "url": url})
                continue
            size = len(data.encode("utf-8"))
            if not 1 <= size <= CALLBACK_DATA_MAX_BYTES:
                raise ValueError(
                    tr('Button “{v0}” callback_data must be 1..{v1} bytes ({v2} bytes supplied)', v0=text, v1=CALLBACK_DATA_MAX_BYTES, v2=size)
                )
            out.append({"text": text, "callback_data": data})
        if out:
            rows.append(out)
    return {"inline_keyboard": rows}


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _units(text: str) -> int:
    """Length in UTF-16 code units, the unit Telegram's length limits are counted in."""

    return len(text) + sum(1 for char in text if ord(char) > 0xFFFF)


def _clip(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` UTF-16 units, ending in "…" when something was cut."""

    if _units(text) <= limit:
        return text
    used = 0
    for index, char in enumerate(text):
        used += 2 if ord(char) > 0xFFFF else 1
        if used > limit - 1:  # keep one unit for "…"
            return text[:index].rstrip() + "…"
    return text


def _content_type(filename: str) -> str:
    return _DOCUMENT_TYPES.get(posixpath.splitext(filename.lower())[1], "application/octet-stream")


class RateLimiter:
    """Blocking, thread-safe bucket: at most ``per_minute`` per rolling window, ``min_interval_s`` apart.

    Waiters queue on ``_turn`` (so they are served one at a time); the bucket state has
    its own short lock so :meth:`defer` never waits for a sleeping waiter.
    """

    def __init__(
        self,
        per_minute: int,
        min_interval_s: float,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        window_s: float = 60.0,
    ) -> None:
        if per_minute < 1:
            raise ValueError(tr('per_minute must be at least 1'))
        self.per_minute = per_minute
        self.min_interval_s = max(0.0, min_interval_s)
        self.window_s = window_s
        self._sleep = sleep
        self._clock = clock
        self._stamps: deque[float] = deque()
        self._blocked_until = 0.0
        self._turn = threading.Lock()
        self._state = threading.Lock()

    def _wait_s(self, now: float) -> float:
        while self._stamps and now - self._stamps[0] >= self.window_s:
            self._stamps.popleft()
        wait = self._blocked_until - now
        if self._stamps:
            wait = max(wait, self._stamps[-1] + self.min_interval_s - now)
        if len(self._stamps) >= self.per_minute:
            wait = max(wait, self._stamps[0] + self.window_s - now)
        return wait

    def acquire(self) -> None:
        with self._turn:
            while True:
                with self._state:
                    now = self._clock()
                    wait = self._wait_s(now)
                    if wait <= 0:
                        self._stamps.append(now)
                        return
                self._sleep(wait)

    def defer(self, seconds: float) -> None:
        """Hold every sender back for ``seconds`` (Telegram asked us to slow down)."""

        with self._state:
            self._blocked_until = max(self._blocked_until, self._clock() + seconds)


class BotAPI:
    """One bot's Bot API client. Thread-safe: long polling and sending may run in parallel.

    A transport error replaces the ``httpx.Client``: httpcore (1.0.9) keeps a connection whose
    TLS handshake through the proxy failed (timeout, EOF) in its pool for good, so every such
    failure used up one of the pool's slots until all requests ended in PoolTimeout without a
    single open connection. A new client starts with an empty pool.
    """

    def __init__(
        self,
        token: str,
        *,
        proxy: str | None,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        per_minute: int = 18,
        min_interval_s: float = 1.05,
        limits: httpx.Limits | None = None,
    ) -> None:
        token = (token or "").strip()  # a token read from a file may end in a newline
        if not _TOKEN_SHAPE_RE.fullmatch(token):
            raise ValueError(tr('Invalid bot token format (expected <digits>:<alphanumeric>)'))
        self._token = token
        self.timeout_s = timeout_s
        self._sleep = sleep
        self._clock = clock
        self.limiter = RateLimiter(per_minute, min_interval_s, sleep=sleep, clock=clock)
        options: dict[str, Any] = {"trust_env": False, "verify": True, "timeout": httpx.Timeout(timeout_s)}
        if limits is not None:
            options["limits"] = limits
        if transport is not None:
            # A proxy would mount its own transport over ``transport`` (tests pass a MockTransport).
            options["transport"] = transport
        else:
            options["proxy"] = proxy or None
        self._options = options
        self._client_lock = threading.Lock()
        self._client = httpx.Client(**options)
        self._retired: list[tuple[float, httpx.Client]] = []  # (replaced at, client) still to close
        self._closed = False
        self.renewals = 0  # clients replaced after transport errors

    def __repr__(self) -> str:
        return f"<BotAPI {self._token.partition(':')[0]}>"

    def __enter__(self) -> BotAPI:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        with self._client_lock:
            self._closed = True
            clients = [self._client, *(client for _, client in self._retired)]
            self._retired.clear()
        for client in clients:
            client.close()

    def _renew(self, failed: httpx.Client) -> None:
        """Replace ``failed`` with a client whose pool is empty (once, however many threads saw it fail).

        The old client stays open for requests other threads still run on it and is closed
        RETIRED_CLIENT_CLOSE_S later, at a following renewal or at close().
        """

        now = self._clock()
        with self._client_lock:
            if self._closed or self._client is not failed:
                return
            self._client = httpx.Client(**self._options)
            self._retired.append((now, failed))
            expired = [client for at, client in self._retired if now - at >= RETIRED_CLIENT_CLOSE_S]
            self._retired = [(at, client) for at, client in self._retired if now - at < RETIRED_CLIENT_CLOSE_S]
            self.renewals += 1
        for client in expired:
            client.close()

    # -- core -------------------------------------------------------------------------

    def _error(self, description: str, *, code: int | None, retry_after: float | None, method: str) -> TelegramError:
        cls = Conflict if code == 409 else TelegramError
        return cls(_scrub(description, self._token), code=code, retry_after=retry_after, method=method)

    def _url(self, method: str) -> str:
        return f"{API_BASE}/bot{self._token}/{method}"

    def _post(self, method: str, params: dict[str, Any], files: dict | None, timeout: httpx.Timeout) -> httpx.Response:
        # The URL is built inline (never a local of this frame) so tracebacks that show
        # locals cannot print it.
        client = self._client
        if client.is_closed:
            failure = tr('Client is closed')
        else:
            try:
                if files:
                    data = {key: _form_value(value) for key, value in params.items()}
                    return client.post(self._url(method), data=data, files=files, timeout=timeout)
                return client.post(self._url(method), json=params, timeout=timeout)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                if isinstance(exc, httpx.TransportError):
                    self._renew(client)  # its pool may now hold a connection that never comes back
                detail = str(exc).strip()
                failure = tr('Network error ({v0})', v0=type(exc).__name__) + (tr(': {detail}', detail=detail) if detail else "")
            except RuntimeError:
                if not client.is_closed:
                    raise
                failure = tr('Client is closed')  # closed by another thread while this call started
        # Raised outside the ``except`` blocks: the httpx exception carries the request and so
        # the token URL, and must be neither ``__cause__`` nor ``__context__`` of this error.
        raise self._error(failure, code=None, retry_after=None, method=method)

    def call(
        self,
        method: str,
        params: dict | None = None,
        *,
        files: dict | None = None,
        timeout_s: float | None = None,
    ) -> Any:
        """POST ``method`` and return its ``result``; None values in ``params`` are dropped."""

        if not _METHOD_RE.fullmatch(method):
            raise ValueError(tr('Not a Bot API method name: {v0!r}', v0=method))
        body = {key: value for key, value in (params or {}).items() if value is not None}
        timeout = httpx.Timeout(self.timeout_s if timeout_s is None else timeout_s)
        retries = 0
        while True:
            if method in RATE_LIMITED_METHODS:
                self.limiter.acquire()
            response = self._post(method, body, files, timeout)
            try:
                payload = response.json()
            except ValueError:
                payload = None
            status = response.status_code
            if isinstance(payload, dict) and payload.get("ok") is True:
                return payload.get("result")
            if isinstance(payload, dict) and "ok" in payload:
                code = payload.get("error_code")
                code = code if isinstance(code, int) and not isinstance(code, bool) else status
                description = str(payload.get("description") or f"HTTP {status}")
                parameters = payload.get("parameters")
                raw_retry = parameters.get("retry_after") if isinstance(parameters, dict) else None
            else:
                # Not a Bot API answer (a proxy or captive portal spoke). Without an HTTP error
                # status there is no code: callers treat it like a network error and retry.
                code = status if status >= 400 else None
                description = tr('HTTP {v0} (response is not Bot API JSON)', v0=status)
                raw_retry = None
            retry_after = (
                float(raw_retry)
                if isinstance(raw_retry, (int, float)) and not isinstance(raw_retry, bool) and raw_retry >= 0
                else None
            )
            if code == 429 and retries < MAX_429_RETRIES:
                retries += 1
                wait = min(retry_after if retry_after is not None else DEFAULT_RETRY_AFTER_S, MAX_RETRY_AFTER_S)
                if method in RATE_LIMITED_METHODS:
                    self.limiter.defer(wait)
                self._sleep(wait)
                continue
            raise self._error(description, code=code, retry_after=retry_after, method=method)

    def _call_quiet(self, method: str, params: dict[str, Any], harmless: tuple[str, ...]) -> Any:
        """``call`` that returns None for the "nothing changed" errors listed in ``harmless``."""

        try:
            return self.call(method, params)
        except TelegramError as exc:
            if exc.code is not None and _matches(exc, harmless):
                return None
            raise

    # -- helpers ----------------------------------------------------------------------

    def get_me(self) -> dict:
        return self.call("getMe")

    def get_updates(
        self,
        offset: int | None,
        *,
        timeout_s: int = 25,
        allowed_updates: list[str] | None = None,
    ) -> list[dict]:
        params = {"offset": offset, "timeout": timeout_s, "allowed_updates": allowed_updates}
        result = self.call("getUpdates", params, timeout_s=timeout_s + 10)
        if not isinstance(result, list):
            return []
        return [update for update in result if isinstance(update, dict)]

    @staticmethod
    def _message_params(
        chat_id: int,
        *,
        thread_id: int | None,
        reply_to: int | None,
        silent: bool,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"chat_id": chat_id, "message_thread_id": thread_id}
        if reply_to is not None:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if silent:
            params["disable_notification"] = True
        return params

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
        buttons: Buttons | None = None,
        silent: bool = False,
        parse_mode: str | None = "HTML",
    ) -> dict:
        params = self._message_params(chat_id, thread_id=thread_id, reply_to=reply_to, silent=silent)
        params.update(text=text, parse_mode=parse_mode, link_preview_options={"is_disabled": True})
        markup = inline_keyboard(buttons) if buttons else None
        if markup and markup["inline_keyboard"]:  # rows that are all empty mean no keyboard too
            params["reply_markup"] = markup
        return self.call("sendMessage", params)

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        buttons: Buttons | None = None,
        parse_mode: str | None = "HTML",
    ) -> dict | None:
        """Edit text (and keyboard). ``buttons=None`` lets Telegram drop the keyboard; None when not modified."""

        params: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "link_preview_options": {"is_disabled": True},
        }
        if buttons is not None:
            params["reply_markup"] = inline_keyboard(buttons)
        result = self._call_quiet("editMessageText", params, _NOT_MODIFIED)
        return result if isinstance(result, dict) else None

    def edit_buttons(self, chat_id: int, message_id: int, buttons: Buttons | None = None) -> None:
        """Replace the inline keyboard; None or [] removes it."""

        markup = inline_keyboard(buttons) if buttons else {"inline_keyboard": []}
        params = {"chat_id": chat_id, "message_id": message_id, "reply_markup": markup}
        self._call_quiet("editMessageReplyMarkup", params, _NOT_MODIFIED)

    def send_document(
        self,
        chat_id: int,
        filename: str,
        data: bytes,
        *,
        caption: str = "",
        thread_id: int | None = None,
        reply_to: int | None = None,
        silent: bool = False,
        parse_mode: str | None = None,
    ) -> dict:
        """Upload ``data`` as a file. The caption is plain text unless ``parse_mode`` is given."""

        params = self._upload_params(chat_id, caption, thread_id=thread_id, reply_to=reply_to, silent=silent,
                                     parse_mode=parse_mode)
        document = (filename, bytes(data), _content_type(filename))  # bytes: a 429 retry resends it
        return self.call("sendDocument", params, files={"document": document})

    def send_photo(
        self,
        chat_id: int,
        filename: str,
        data: bytes,
        *,
        caption: str = "",
        thread_id: int | None = None,
        reply_to: int | None = None,
        silent: bool = False,
        parse_mode: str | None = None,
    ) -> dict:
        """Upload ``data`` (at most ``PHOTO_MAX_BYTES``) as a photo; Telegram recompresses it.

        Telegram answers 400 for data it cannot show as a photo (not an image, width plus
        height above 10000, aspect ratio above 20): send such files with :meth:`send_document`.
        The caption is plain text unless ``parse_mode`` is given.
        """

        params = self._upload_params(chat_id, caption, thread_id=thread_id, reply_to=reply_to, silent=silent,
                                     parse_mode=parse_mode)
        photo = (filename, bytes(data), _content_type(filename))  # bytes: a 429 retry resends it
        return self.call("sendPhoto", params, files={"photo": photo})

    def _upload_params(
        self,
        chat_id: int,
        caption: str,
        *,
        thread_id: int | None,
        reply_to: int | None,
        silent: bool,
        parse_mode: str | None,
    ) -> dict[str, Any]:
        params = self._message_params(chat_id, thread_id=thread_id, reply_to=reply_to, silent=silent)
        if caption:
            params["caption"] = caption if parse_mode else _clip(caption, CAPTION_MAX)
            params["parse_mode"] = parse_mode
        return params

    def answer_callback(self, callback_id: str, text: str = "", *, alert: bool = False) -> None:
        """Answer a button press. An expired query is ignored (the action itself already ran)."""

        params: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            params["text"] = _clip(text, CALLBACK_TEXT_MAX)
        if alert:
            params["show_alert"] = True
        self._call_quiet("answerCallbackQuery", params, _CALLBACK_EXPIRED)

    def create_topic(self, chat_id: int, name: str) -> int:
        result = self.call("createForumTopic", {"chat_id": chat_id, "name": self._topic_name(name)})
        if not isinstance(result, dict) or not isinstance(result.get("message_thread_id"), int):
            raise TelegramError(tr('createForumTopic did not return message_thread_id'), method="createForumTopic")
        return result["message_thread_id"]

    def edit_topic(self, chat_id: int, thread_id: int, name: str) -> None:
        params = {"chat_id": chat_id, "message_thread_id": thread_id, "name": self._topic_name(name)}
        self._call_quiet("editForumTopic", params, _TOPIC_NOT_MODIFIED)

    def close_topic(self, chat_id: int, thread_id: int) -> None:
        params = {"chat_id": chat_id, "message_thread_id": thread_id}
        self._call_quiet("closeForumTopic", params, _TOPIC_ALREADY_CLOSED)

    def reopen_topic(self, chat_id: int, thread_id: int) -> None:
        params = {"chat_id": chat_id, "message_thread_id": thread_id}
        self._call_quiet("reopenForumTopic", params, _TOPIC_ALREADY_OPEN)

    @staticmethod
    def _topic_name(name: str) -> str:
        name = " ".join(name.split())
        if not name:
            raise ValueError(tr('Topic name cannot be empty'))
        return _clip(name, TOPIC_NAME_MAX)

    def delete_message(self, chat_id: int, message_id: int) -> None:
        """Best effort: never raises (the message may be gone or too old to delete)."""

        with contextlib.suppress(TelegramError):
            self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def leave_chat(self, chat_id: int) -> None:
        self.call("leaveChat", {"chat_id": chat_id})

    def get_chat(self, chat_id: int) -> dict:
        return self.call("getChat", {"chat_id": chat_id})

    def member_count(self, chat_id: int) -> int:
        return int(self.call("getChatMemberCount", {"chat_id": chat_id}))

    def set_commands(self, commands: list[tuple[str, str]], *, scope: dict | None = None) -> None:
        payload = [{"command": command, "description": description} for command, description in commands]
        self.call("setMyCommands", {"commands": payload, "scope": scope})

    def webhook_info(self) -> dict:
        return self.call("getWebhookInfo")


def __getattr__(name: str) -> str:
    """Resolve legacy message exports at access time, including for the pending bridge."""
    if name == 'SCRUB_MARK':
        return tr('‹redacted›')
    raise AttributeError(name)

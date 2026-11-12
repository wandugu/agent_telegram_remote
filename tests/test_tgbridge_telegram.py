from __future__ import annotations

import json
import logging
import re
import socket
import threading
import traceback
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from agent_telegram.tgbridge import telegram_api
from agent_telegram.tgbridge.telegram_api import BotAPI, Conflict, RateLimiter, TelegramError, inline_keyboard

TOKEN = "123456789:AAH" + "x" * 32
CHAT = -1001234567890
CRLF = bytes([13, 10])


class FakeTime:
    """Deterministic clock whose sleep advances the clock."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Server:
    """MockTransport handler: records requests and answers from a queue or a function."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else (self.answers[0] if self.answers else ok(True))
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, Exception):
            raise answer
        # A fresh Response per request: the client binds and closes the one it receives.
        return httpx.Response(answer.status_code, headers=answer.headers, content=answer.content)

    @property
    def methods(self) -> list[str]:
        return [request.url.path.rsplit("/", 1)[-1] for request in self.requests]

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def fail(code: int, description: str, **parameters: Any) -> httpx.Response:
    payload: dict[str, Any] = {"ok": False, "error_code": code, "description": description}
    if parameters:
        payload["parameters"] = parameters
    return httpx.Response(code, json=payload)


@pytest.fixture
def fake_time() -> FakeTime:
    return FakeTime()


@pytest.fixture
def make_api(fake_time: FakeTime) -> Callable[..., tuple[BotAPI, Server]]:
    created: list[BotAPI] = []

    def factory(*answers: Any, **kwargs: Any) -> tuple[BotAPI, Server]:
        server = Server(*answers)
        options: dict[str, Any] = {
            "proxy": None,
            "transport": httpx.MockTransport(server),
            "sleep": fake_time.sleep,
            "clock": fake_time.clock,
        }
        options.update(kwargs)
        api = BotAPI(TOKEN, **options)
        created.append(api)
        return api, server

    yield factory
    for api in created:
        api.close()


def multipart(request: httpx.Request) -> tuple[dict[str, str], dict[str, tuple[str, bytes, str]]]:
    """Split a multipart/form-data body into text fields and (filename, bytes, content type) files."""

    boundary = request.headers["content-type"].split("boundary=", 1)[1].strip('"').encode()
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes, str]] = {}
    for chunk in request.content.split(b"--" + boundary)[1:]:
        if chunk.startswith(b"--"):
            break
        chunk = chunk.removeprefix(CRLF).removesuffix(CRLF)
        head, _, body = chunk.partition(CRLF + CRLF)
        headers = head.decode("utf-8")
        name = re.search(r'\bname="([^"]*)"', headers).group(1)
        filename = re.search(r'\bfilename="([^"]*)"', headers)
        if filename is None:
            fields[name] = body.decode("utf-8")
        else:
            content_type = re.search(r"(?im)^content-type:\s*(\S+)", headers)
            files[name] = (filename.group(1), body, content_type.group(1) if content_type else "")
    return fields, files


# -- core call ----------------------------------------------------------------------------


def test_success_posts_json_to_method_url(make_api):
    api, server = make_api(ok({"id": 42, "is_bot": True, "username": "pc1_bot"}))

    assert api.get_me() == {"id": 42, "is_bot": True, "username": "pc1_bot"}

    request = server.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"https://api.telegram.org/bot{TOKEN}/getMe"
    assert request.headers["content-type"] == "application/json"
    assert server.body() == {}


def test_call_drops_none_params_and_uses_default_timeout(make_api):
    api, server = make_api(ok([]))

    api.call("getChat", {"chat_id": CHAT, "unused": None})

    assert server.body() == {"chat_id": CHAT}
    assert server.requests[0].extensions["timeout"]["read"] == 30.0


def test_call_rejects_bad_method_names(make_api):
    api, server = make_api()

    for bad in ("", "../getMe", "send message", "getMe?x=1"):
        with pytest.raises(ValueError):
            api.call(bad)
    assert server.requests == []


def test_api_error_raises_telegram_error(make_api):
    api, server = make_api(fail(400, "Bad Request: chat not found"))

    with pytest.raises(TelegramError) as caught:
        api.get_chat(CHAT)

    error = caught.value
    assert type(error) is TelegramError
    assert error.code == 400
    assert error.description == "Bad Request: chat not found"
    assert error.method == "getChat"
    assert error.retry_after is None
    assert "getChat" in str(error) and "chat not found" in str(error) and "400" in str(error)
    assert len(server.requests) == 1


def test_409_raises_conflict(make_api):
    api, server = make_api(
        fail(409, "Conflict: terminated by other getUpdates request; make sure that only one bot instance is running")
    )

    with pytest.raises(Conflict) as caught:
        api.get_updates(0)

    assert isinstance(caught.value, TelegramError)
    assert caught.value.code == 409
    assert len(server.requests) == 1


def test_non_json_answers_become_errors(make_api):
    api, _ = make_api(httpx.Response(502, text="<html>Bad Gateway</html>"), httpx.Response(409, text="conflict"))

    with pytest.raises(TelegramError) as first:
        api.get_me()
    assert type(first.value) is TelegramError
    assert first.value.code == 502

    with pytest.raises(Conflict):
        api.get_me()


def test_ok_false_without_error_code_uses_http_status(make_api):
    api, _ = make_api(httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))

    with pytest.raises(TelegramError) as caught:
        api.get_me()

    assert caught.value.code == 401
    assert caught.value.description == "Unauthorized"


def test_429_sleeps_retry_after_and_retries(make_api, fake_time):
    api, server = make_api(
        fail(429, "Too Many Requests: retry after 5", retry_after=5),
        ok({"message_id": 7}),
    )

    assert api.send_message(CHAT, "hi") == {"message_id": 7}

    assert server.methods == ["sendMessage", "sendMessage"]
    assert fake_time.sleeps == [5.0]


def test_429_retry_after_is_capped_and_retries_are_bounded(make_api, fake_time):
    api, server = make_api(fail(429, "Too Many Requests: retry after 300", retry_after=300))

    with pytest.raises(TelegramError) as caught:
        api.get_updates(5, timeout_s=0)

    assert caught.value.code == 429
    assert caught.value.retry_after == 300.0
    assert len(server.requests) == 4  # first try + 3 retries
    assert fake_time.sleeps == [60.0, 60.0, 60.0]


def test_429_without_retry_after_uses_default_wait(make_api, fake_time):
    api, _ = make_api(httpx.Response(429, text="slow down"), ok(True))

    assert api.call("getMe") is True
    assert fake_time.sleeps == [telegram_api.DEFAULT_RETRY_AFTER_S]


def test_429_on_a_chat_method_holds_back_other_senders(make_api, fake_time):
    api, _ = make_api(ok({"message_id": 1}), min_interval_s=0.0)
    api.send_message(CHAT, "a")

    api.limiter.defer(7.0)
    api.send_message(CHAT, "b")

    assert fake_time.sleeps == [7.0]


def test_network_error_becomes_telegram_error_without_retry(make_api):
    leak = httpx.ConnectError(f"cannot reach https://api.telegram.org/bot{TOKEN}/getUpdates")
    api, server = make_api(leak)

    with pytest.raises(TelegramError) as caught:
        api.get_updates(10)

    error = caught.value
    assert error.code is None
    assert "ConnectError" in error.description
    assert len(server.requests) == 1
    # The httpx exception holds the request (token URL): it must not be chained at all.
    assert error.__cause__ is None and error.__context__ is None
    rendered = "".join(traceback.format_exception(error))
    assert TOKEN not in rendered and "api.telegram.org/bot" not in rendered


def test_a_transport_error_replaces_the_client(make_api, fake_time):
    api, server = make_api(httpx.ConnectError("handshake failed"), ok({"id": 1}), httpx.ReadTimeout(""),
                           httpx.ConnectError("again"))
    first = api._client
    with pytest.raises(TelegramError):
        api.get_me()
    second = api._client
    assert second is not first and api.renewals == 1 and not first.is_closed  # others may still use it
    assert api.get_me() == {"id": 1} and api._client is second  # an answer keeps the client
    with pytest.raises(TelegramError):
        api.get_me()
    fake_time.now += telegram_api.RETIRED_CLIENT_CLOSE_S
    with pytest.raises(TelegramError):
        api.get_me()
    assert api.renewals == 3 and first.is_closed and second.is_closed  # retired long enough ago
    newest = api._client
    api.close()
    assert newest.is_closed and len(server.requests) == 4


def test_failed_handshakes_through_the_proxy_never_use_up_the_pool():
    """httpcore keeps such a connection in its pool for good; without a new client the second
    attempt would end in PoolTimeout (the pool has one slot here) instead of reaching the proxy."""

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    reached = []

    def hang_up_after_connect() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                reached.append(conn.recv(4096).split(b" ", 2)[1])  # CONNECT api.telegram.org:443
                conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

    threading.Thread(target=hang_up_after_connect, daemon=True).start()
    api = BotAPI(TOKEN, proxy=f"http://127.0.0.1:{server.getsockname()[1]}", timeout_s=3.0,
                 limits=httpx.Limits(max_connections=1, max_keepalive_connections=1))
    try:
        for _ in range(3):
            with pytest.raises(TelegramError) as caught:
                api.get_me()
            assert "PoolTimeout" not in caught.value.description
    finally:
        api.close()
        server.close()
    assert reached == [b"api.telegram.org:443"] * 3 and api.renewals == 3


def test_timeouts_are_network_errors(make_api):
    api, _ = make_api(httpx.ReadTimeout(""))

    with pytest.raises(TelegramError) as caught:
        api.get_me()

    assert caught.value.code is None
    assert caught.value.description == "网络错误（ReadTimeout）"


def test_token_never_appears_in_error_text(make_api):
    secret_tail = TOKEN.split(":", 1)[1]
    api, _ = make_api(
        fail(400, f"Bad Request: token {TOKEN} in https://api.telegram.org/bot{TOKEN}/sendMessage"),
        httpx.ConnectError(f"proxy said no for https://api.telegram.org/bot{TOKEN}/getMe"),
        httpx.ConnectError(f"encoded /bot{TOKEN.replace(':', '%3A')}/getMe"),
    )

    errors = []
    for _ in range(3):
        with pytest.raises(TelegramError) as caught:
            api.call("getMe")
        errors.append(caught.value)

    for error in errors:
        for text in (str(error), repr(error), error.description):
            assert TOKEN not in text
            assert secret_tail not in text
            assert "api.telegram.org/bot" not in text
        assert telegram_api.SCRUB_MARK in error.description
    assert secret_tail not in repr(api)


def test_token_variants_are_scrubbed(make_api):
    bot_id, _, secret_tail = TOKEN.partition(":")
    api, _ = make_api(
        httpx.ConnectError(f"lower-case escape /bot{bot_id}%3a{secret_tail}/getMe"),
        httpx.ConnectError(f"only the secret half {secret_tail} leaked"),
        fail(401, f"other bot 987654321:{'Z' * 35} rejected"),
    )

    for _ in range(3):
        with pytest.raises(TelegramError) as caught:
            api.call("getMe")
        text = str(caught.value)
        assert secret_tail not in text and "Z" * 35 not in text
        assert telegram_api.SCRUB_MARK in text


def test_httpx_request_log_never_shows_token(make_api, caplog):
    caplog.set_level(logging.DEBUG)  # root at DEBUG, as a careless log setup would do
    api, _ = make_api(ok({"id": 1}))

    api.get_me()

    assert TOKEN not in caplog.text
    assert not [record for record in caplog.records if record.name.startswith(("httpx", "httpcore"))]


def test_closed_client_raises_telegram_error(make_api):
    api, server = make_api(ok(True))
    api.close()

    with pytest.raises(TelegramError) as caught:
        api.get_me()
    assert caught.value.code is None
    assert api.delete_message(CHAT, 1) is None  # still never raises
    assert server.requests == []


def test_non_bot_api_answer_without_error_status_has_no_code(make_api):
    api, _ = make_api(
        httpx.Response(200, text="<html>captive portal</html>"),
        httpx.Response(200, json={"hello": "proxy"}),
        httpx.Response(302, headers={"location": "http://portal.example/"}),
    )

    for _ in range(3):
        with pytest.raises(TelegramError) as caught:
            api.get_me()
        assert type(caught.value) is TelegramError
        assert caught.value.code is None  # the bridge retries these like network errors


def test_429_on_a_chat_method_defers_the_shared_bucket(make_api, fake_time, monkeypatch):
    api, server = make_api(fail(429, "Too Many Requests: retry after 9", retry_after=9), ok({"message_id": 1}))
    deferred: list[float] = []
    monkeypatch.setattr(api.limiter, "defer", deferred.append)

    api.send_message(CHAT, "x")

    assert deferred == [9.0]
    assert fake_time.sleeps == [9.0]
    assert server.methods == ["sendMessage", "sendMessage"]


def test_429_on_get_updates_leaves_the_bucket_alone(make_api, fake_time, monkeypatch):
    api, _ = make_api(fail(429, "Too Many Requests: retry after 2", retry_after=2), ok([]))
    deferred: list[float] = []
    monkeypatch.setattr(api.limiter, "defer", deferred.append)

    assert api.get_updates(1, timeout_s=0) == []

    assert deferred == []
    assert fake_time.sleeps == [2.0]


def test_rate_limiter_defer_blocks_the_next_sender():
    fake = FakeTime(0.0)
    limiter = RateLimiter(10, 0.0, sleep=fake.sleep, clock=fake.clock)

    limiter.acquire()
    limiter.defer(4.0)
    limiter.defer(1.0)  # a shorter deferral never shortens the block
    limiter.acquire()

    assert fake.sleeps == [4.0]


def test_send_document_retry_after_429_resends_the_file(make_api, fake_time):
    api, server = make_api(fail(429, "Too Many Requests: retry after 1", retry_after=1), ok({"message_id": 3}))
    data = bytearray(b"payload \x00\x01")

    assert api.send_document(CHAT, "r.md", data)["message_id"] == 3

    assert server.methods == ["sendDocument", "sendDocument"]
    for request in server.requests:
        _, files = multipart(request)
        assert files["document"] == ("r.md", bytes(data), "text/markdown")


def test_bad_token_rejected_without_echoing_it():
    for bad in ("", "no-colon", "123:abc/def", "123:abc def"):
        with pytest.raises(ValueError) as caught:
            BotAPI(bad, proxy=None, transport=httpx.MockTransport(lambda request: ok(True)))
        if bad:
            assert bad not in str(caught.value)


def test_token_surrounding_whitespace_is_ignored():
    server = Server(ok({"id": 1}))
    padded = BotAPI(f"  {TOKEN}\r\n", proxy=None, transport=httpx.MockTransport(server))
    try:
        padded.get_me()
    finally:
        padded.close()
    assert str(server.requests[0].url) == f"https://api.telegram.org/bot{TOKEN}/getMe"


def test_httpx_loggers_are_quiet():
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_client_uses_explicit_proxy_and_ignores_environment(monkeypatch):
    seen: list[dict[str, Any]] = []

    class RecordingClient:
        def __init__(self, **kwargs: Any) -> None:
            seen.append(kwargs)

        def close(self) -> None:
            pass

    monkeypatch.setattr(telegram_api.httpx, "Client", RecordingClient)

    BotAPI(TOKEN, proxy="http://127.0.0.1:7897")
    BotAPI(TOKEN, proxy="")
    BotAPI(TOKEN, proxy=None)

    assert [options["proxy"] for options in seen] == ["http://127.0.0.1:7897", None, None]
    assert all(options["trust_env"] is False and options["verify"] is True for options in seen)


def test_real_client_with_proxy_builds_without_network():
    api = BotAPI(TOKEN, proxy="http://127.0.0.1:7897", timeout_s=5)
    try:
        assert api._client.trust_env is False
    finally:
        api.close()


def test_context_manager_closes_client(make_api):
    api, _ = make_api()
    with api as entered:
        assert entered is api
    assert api._client.is_closed


# -- helpers ------------------------------------------------------------------------------


def test_get_updates_params_and_long_poll_timeout(make_api):
    updates = [{"update_id": 5, "message": {"text": "hi"}}, "junk", {"update_id": 6}]
    api, server = make_api(ok(updates), ok(None))

    assert api.get_updates(5, timeout_s=25, allowed_updates=["message", "callback_query"]) == [
        {"update_id": 5, "message": {"text": "hi"}},
        {"update_id": 6},
    ]
    assert server.body(0) == {"offset": 5, "timeout": 25, "allowed_updates": ["message", "callback_query"]}
    assert server.requests[0].extensions["timeout"]["read"] == 35

    assert api.get_updates(None, timeout_s=0) == []
    assert server.body(1) == {"timeout": 0}
    assert server.requests[1].extensions["timeout"]["read"] == 10


def test_send_message_options(make_api):
    api, server = make_api(ok({"message_id": 11}))

    result = api.send_message(CHAT, "<b>hi</b>", thread_id=77, reply_to=9, silent=True)

    assert result == {"message_id": 11}
    assert server.methods == ["sendMessage"]
    assert server.body() == {
        "chat_id": CHAT,
        "text": "<b>hi</b>",
        "parse_mode": "HTML",
        "message_thread_id": 77,
        "reply_parameters": {"message_id": 9, "allow_sending_without_reply": True},
        "disable_notification": True,
        "link_preview_options": {"is_disabled": True},
    }


def test_send_message_minimal_has_no_markup_and_optional_parse_mode(make_api):
    api, server = make_api(ok({"message_id": 1}))

    api.send_message(CHAT, "plain", parse_mode=None)
    api.send_message(CHAT, "plain", buttons=[])

    api.send_message(CHAT, "plain", buttons=[[], []])

    for index in (0, 1, 2):
        body = server.body(index)
        assert "reply_markup" not in body
        assert "message_thread_id" not in body
        assert "reply_parameters" not in body
        assert "disable_notification" not in body
        assert body["link_preview_options"] == {"is_disabled": True}
    assert "parse_mode" not in server.body(0)


def test_send_message_buttons(make_api):
    api, server = make_api(ok({"message_id": 1}))

    api.send_message(
        CHAT,
        "pick",
        buttons=[[("发送", "send:abc"), ("取消", "cancel:abc")], [("打开", "url:https://example.com/x")]],
    )

    assert server.body()["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "发送", "callback_data": "send:abc"}, {"text": "取消", "callback_data": "cancel:abc"}],
            [{"text": "打开", "url": "https://example.com/x"}],
        ]
    }


def test_callback_data_is_limited_to_64_bytes(make_api):
    api, server = make_api(ok({"message_id": 1}))

    assert inline_keyboard([[("ok", "x" * 64)]])["inline_keyboard"][0][0]["callback_data"] == "x" * 64
    with pytest.raises(ValueError):
        inline_keyboard([[("too long", "x" * 65)]])
    with pytest.raises(ValueError):
        inline_keyboard([[("multibyte", "确" * 22)]])  # 66 bytes in UTF-8
    with pytest.raises(ValueError):
        inline_keyboard([[("empty", "")]])
    with pytest.raises(ValueError):
        inline_keyboard([[("", "data")]])
    with pytest.raises(ValueError):
        inline_keyboard([[("link", "url:")]])
    with pytest.raises(ValueError):
        api.send_message(CHAT, "x", buttons=[[("bad", "y" * 65)]])
    assert server.requests == []
    assert inline_keyboard([[], [("a", "b")]]) == {"inline_keyboard": [[{"text": "a", "callback_data": "b"}]]}


def test_edit_message_payload_and_not_modified(make_api):
    api, server = make_api(
        ok({"message_id": 5, "text": "new"}),
        fail(400, "Bad Request: message is not modified: specified new message content and reply markup are the same"),
        ok(True),
    )

    assert api.edit_message(CHAT, 5, "new", buttons=[[("停止", "stop:1")]]) == {"message_id": 5, "text": "new"}
    assert server.body(0) == {
        "chat_id": CHAT,
        "message_id": 5,
        "text": "new",
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
        "reply_markup": {"inline_keyboard": [[{"text": "停止", "callback_data": "stop:1"}]]},
    }
    assert api.edit_message(CHAT, 5, "new") is None
    assert "reply_markup" not in server.body(1)
    assert api.edit_message(CHAT, 5, "new", buttons=[], parse_mode=None) is None  # result True (inline message)
    assert server.body(2)["reply_markup"] == {"inline_keyboard": []}
    assert "parse_mode" not in server.body(2)


def test_edit_message_other_errors_propagate(make_api):
    api, _ = make_api(fail(400, "Bad Request: message to edit not found"))

    with pytest.raises(TelegramError):
        api.edit_message(CHAT, 5, "x")


def test_edit_buttons_none_removes_keyboard(make_api):
    api, server = make_api(ok(True), ok(True), fail(400, "Bad Request: message is not modified"))

    assert api.edit_buttons(CHAT, 3) is None
    assert server.body(0) == {"chat_id": CHAT, "message_id": 3, "reply_markup": {"inline_keyboard": []}}
    api.edit_buttons(CHAT, 3, [[("重试", "retry:3")]])
    assert server.body(1)["reply_markup"] == {"inline_keyboard": [[{"text": "重试", "callback_data": "retry:3"}]]}
    api.edit_buttons(CHAT, 3, [])  # not modified is swallowed
    assert server.methods == ["editMessageReplyMarkup"] * 3


def test_send_document_multipart(make_api):
    api, server = make_api(ok({"message_id": 20, "document": {"file_name": "回复.md"}}))
    data = "# 完整回复\n\n第二行\n".encode() + b"\x00\xff"

    result = api.send_document(CHAT, "回复.md", data, caption="完整回复", thread_id=77, reply_to=19, silent=True)

    assert result["message_id"] == 20
    request = server.requests[0]
    assert server.methods == ["sendDocument"]
    assert request.headers["content-type"].startswith("multipart/form-data")
    fields, files = multipart(request)
    assert fields == {
        "chat_id": str(CHAT),
        "message_thread_id": "77",
        "caption": "完整回复",
        "reply_parameters": json.dumps({"message_id": 19, "allow_sending_without_reply": True}, ensure_ascii=False),
        "disable_notification": "true",
    }
    assert list(files) == ["document"]
    filename, payload, _content_type = files["document"]
    assert filename == "回复.md"
    assert payload == data


PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


def test_send_photo_multipart(make_api):
    api, server = make_api(ok({"message_id": 21, "photo": [{"file_id": "p"}]}))

    result = api.send_photo(CHAT, "首页.png", PNG, caption="设计有更新\n首页.png", thread_id=77, reply_to=19,
                            silent=True)

    assert result["message_id"] == 21
    request = server.requests[0]
    assert server.methods == ["sendPhoto"]
    assert request.headers["content-type"].startswith("multipart/form-data")
    fields, files = multipart(request)
    assert fields == {
        "chat_id": str(CHAT),
        "message_thread_id": "77",
        "caption": "设计有更新\n首页.png",
        "reply_parameters": json.dumps({"message_id": 19, "allow_sending_without_reply": True}, ensure_ascii=False),
        "disable_notification": "true",
    }
    assert files == {"photo": ("首页.png", PNG, "image/png")}


def test_send_photo_caption_is_plain_and_clipped(make_api):
    api, server = make_api(ok({"message_id": 1}))

    api.send_photo(CHAT, "a.jpg", PNG, caption="c" * 2000)
    fields, files = multipart(server.requests[0])
    assert len(fields["caption"]) == telegram_api.CAPTION_MAX and fields["caption"].endswith("…")
    assert "parse_mode" not in fields
    assert files["photo"][2] == "image/jpeg"

    api.send_photo(CHAT, "a.webp", PNG)
    fields, files = multipart(server.requests[1])
    assert fields == {"chat_id": str(CHAT)}
    assert files["photo"][0] == "a.webp"


def test_send_photo_shares_the_chat_bucket_and_resends_after_429(make_api, fake_time, monkeypatch):
    api, server = make_api(fail(429, "Too Many Requests: retry after 2", retry_after=2), ok({"message_id": 3}))
    deferred: list[float] = []
    monkeypatch.setattr(api.limiter, "defer", deferred.append)
    data = bytearray(PNG)

    assert api.send_photo(CHAT, "a.png", data)["message_id"] == 3

    assert "sendPhoto" in telegram_api.RATE_LIMITED_METHODS
    assert deferred == [2.0] and fake_time.sleeps == [2.0]
    assert server.methods == ["sendPhoto", "sendPhoto"]
    for request in server.requests:
        assert multipart(request)[1]["photo"] == ("a.png", PNG, "image/png")


def test_send_photo_refused_as_a_photo_raises_400_without_retry(make_api, fake_time):
    api, server = make_api(fail(400, "Bad Request: PHOTO_INVALID_DIMENSIONS"))

    with pytest.raises(TelegramError) as caught:
        api.send_photo(CHAT, "long.png", PNG)

    assert caught.value.code == 400 and caught.value.method == "sendPhoto"
    assert "PHOTO_INVALID_DIMENSIONS" in caught.value.description
    assert server.methods == ["sendPhoto"] and fake_time.sleeps == []


def test_send_document_caption_is_plain_and_clipped(make_api):
    api, server = make_api(ok({"message_id": 1}))

    api.send_document(CHAT, "log.txt", b"x", caption="c" * 2000)
    fields, files = multipart(server.requests[0])
    assert len(fields["caption"]) == telegram_api.CAPTION_MAX
    assert fields["caption"].endswith("…")
    assert "parse_mode" not in fields
    assert files["document"][2] == "text/plain"

    api.send_document(CHAT, "blob.bin", b"x")
    fields, files = multipart(server.requests[1])
    assert fields == {"chat_id": str(CHAT)}
    assert files["document"][2] == "application/octet-stream"

    api.send_document(CHAT, "a.txt", b"x", caption="<b>c</b>", parse_mode="HTML")
    fields, _ = multipart(server.requests[2])
    assert fields["caption"] == "<b>c</b>" and fields["parse_mode"] == "HTML"


def test_answer_callback(make_api):
    api, server = make_api(
        ok(True),
        ok(True),
        fail(400, "Bad Request: query is too old and response timeout expired or query ID is invalid"),
    )

    api.answer_callback("cb1")
    assert server.body(0) == {"callback_query_id": "cb1"}
    api.answer_callback("cb2", "已发送" * 100, alert=True)
    body = server.body(1)
    assert body["show_alert"] is True
    assert len(body["text"]) == telegram_api.CALLBACK_TEXT_MAX
    assert api.answer_callback("cb3", "late") is None


def test_answer_callback_other_errors_propagate(make_api):
    api, _ = make_api(fail(401, "Unauthorized"))

    with pytest.raises(TelegramError):
        api.answer_callback("cb")


def test_topics(make_api):
    api, server = make_api(
        ok({"message_thread_id": 77, "name": "pc1·Codex·x"}),
        ok(True),
        fail(400, "Bad Request: TOPIC_NOT_MODIFIED"),
        ok(True),
        fail(400, "Bad Request: TOPIC_NOT_MODIFIED"),
        ok(True),
        fail(400, "Bad Request: TOPIC_NOT_MODIFIED"),
    )

    assert api.create_topic(CHAT, "  pc1·Codex·\n" + "长" * 200) == 77
    name = server.body(0)["name"]
    assert len(name) == telegram_api.TOPIC_NAME_MAX and name.startswith("pc1·Codex· 长") and name.endswith("…")

    assert api.edit_topic(CHAT, 77, "新名字") is None
    assert server.body(1) == {"chat_id": CHAT, "message_thread_id": 77, "name": "新名字"}
    assert api.edit_topic(CHAT, 77, "新名字") is None
    assert api.close_topic(CHAT, 77) is None
    assert api.close_topic(CHAT, 77) is None
    assert server.body(3) == {"chat_id": CHAT, "message_thread_id": 77}
    assert api.reopen_topic(CHAT, 77) is None
    assert api.reopen_topic(CHAT, 77) is None
    assert server.methods == [
        "createForumTopic",
        "editForumTopic",
        "editForumTopic",
        "closeForumTopic",
        "closeForumTopic",
        "reopenForumTopic",
        "reopenForumTopic",
    ]


def test_lengths_are_counted_in_utf16_units(make_api):
    api, server = make_api(ok({"message_thread_id": 5, "message_id": 1}))

    api.create_topic(CHAT, "😀" * 100)  # 100 code points, 200 UTF-16 units
    name = server.body(0)["name"]
    assert name == "😀" * 63 + "…"  # 126 + 1 units: a surrogate pair is never split

    api.answer_callback("cb", "🎉" * 150)
    assert server.body(1)["text"] == "🎉" * 99 + "…"

    api.send_document(CHAT, "a.txt", b"x", caption="👍" * 600)
    fields, _ = multipart(server.requests[2])
    assert fields["caption"] == "👍" * 511 + "…"

    api.create_topic(CHAT, "短名字 😀")
    assert server.body(3)["name"] == "短名字 😀"


def test_topic_errors(make_api):
    api, server = make_api(ok({"name": "no id"}), fail(400, "Bad Request: not enough rights to manage topics"))

    with pytest.raises(ValueError):
        api.create_topic(CHAT, "   ")
    assert server.requests == []
    with pytest.raises(TelegramError):
        api.create_topic(CHAT, "x")
    with pytest.raises(TelegramError) as caught:
        api.close_topic(CHAT, 77)
    assert caught.value.code == 400


def test_close_topic_network_error_is_not_swallowed(make_api):
    api, _ = make_api(httpx.ConnectError("down"))

    with pytest.raises(TelegramError):
        api.close_topic(CHAT, 1)


def test_delete_message_never_raises(make_api):
    api, server = make_api(ok(True), fail(400, "Bad Request: message can't be deleted"), httpx.ConnectError("down"))

    for _ in range(3):
        assert api.delete_message(CHAT, 5) is None
    assert server.body(0) == {"chat_id": CHAT, "message_id": 5}
    assert server.methods == ["deleteMessage"] * 3


def test_chat_helpers(make_api):
    api, server = make_api(
        ok({"id": CHAT, "type": "supergroup", "is_forum": True}),
        ok(3),
        ok(True),
        ok(True),
        ok({"url": "", "pending_update_count": 0}),
        ok(True),
    )

    assert api.get_chat(CHAT)["is_forum"] is True
    assert api.member_count(CHAT) == 3
    assert api.set_commands([("list", "列出会话"), ("new", "新建会话")]) is None
    assert server.body(2) == {
        "commands": [{"command": "list", "description": "列出会话"}, {"command": "new", "description": "新建会话"}]
    }
    api.set_commands([("list", "列出会话")], scope={"type": "chat", "chat_id": CHAT})
    assert server.body(3)["scope"] == {"type": "chat", "chat_id": CHAT}
    assert api.webhook_info() == {"url": "", "pending_update_count": 0}
    assert api.leave_chat(CHAT) is None
    assert server.methods == [
        "getChat",
        "getChatMemberCount",
        "setMyCommands",
        "setMyCommands",
        "getWebhookInfo",
        "leaveChat",
    ]


# -- rate limiting ------------------------------------------------------------------------


def test_chat_methods_are_spaced_by_min_interval(make_api, fake_time):
    api, _ = make_api(ok({"message_id": 1}))

    for _ in range(3):
        api.send_message(CHAT, "x")

    assert fake_time.sleeps == pytest.approx([1.05, 1.05])


def test_non_chat_methods_are_not_rate_limited(make_api, fake_time):
    api, _ = make_api(ok([]), per_minute=1)

    for _ in range(5):
        api.get_updates(0, timeout_s=0)
        api.answer_callback("cb")
        api.get_me()

    assert fake_time.sleeps == []


def test_all_chat_methods_share_one_bucket(make_api, fake_time):
    api, server = make_api(ok({"message_id": 1, "message_thread_id": 2}), per_minute=3, min_interval_s=0.0)

    api.send_message(CHAT, "a")
    api.edit_buttons(CHAT, 1)
    api.create_topic(CHAT, "t")
    assert fake_time.sleeps == []
    api.delete_message(CHAT, 1)  # fourth within 60 s: waits for the first to leave the window

    assert fake_time.sleeps == pytest.approx([60.0])
    assert len(server.requests) == 4


def test_rate_limiter_rolling_window():
    fake = FakeTime(0.0)
    limiter = RateLimiter(3, 1.0, sleep=fake.sleep, clock=fake.clock)

    stamps = []
    for _ in range(7):
        limiter.acquire()
        stamps.append(fake.now)

    assert stamps == pytest.approx([0.0, 1.0, 2.0, 60.0, 61.0, 62.0, 120.0])
    for index in range(len(stamps) - 3):
        assert stamps[index + 3] - stamps[index] >= 60.0


def test_rate_limiter_idle_gap_needs_no_wait():
    fake = FakeTime(0.0)
    limiter = RateLimiter(2, 1.05, sleep=fake.sleep, clock=fake.clock)

    limiter.acquire()
    fake.now += 5.0
    limiter.acquire()

    assert fake.sleeps == []


def test_rate_limiter_is_thread_safe():
    granted: list[float] = []

    class Recording(deque):
        def append(self, value: float) -> None:  # runs under the limiter's state lock
            granted.append(value)
            super().append(value)

    limiter = RateLimiter(4, 0.02, window_s=0.3)
    limiter._stamps = Recording()

    def worker() -> None:
        for _ in range(2):
            limiter.acquire()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)

    assert not any(thread.is_alive() for thread in threads)
    assert len(granted) == 8
    assert granted == sorted(granted)
    slack = 1e-9  # float rounding of "stamp + interval - now <= 0"
    for earlier, later in zip(granted, granted[1:], strict=False):
        assert later - earlier >= 0.02 - slack
    for index in range(len(granted) - 4):
        assert granted[index + 4] - granted[index] >= 0.3 - slack


def test_rate_limiter_rejects_zero_budget():
    with pytest.raises(ValueError):
        RateLimiter(0, 1.0)

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from agent_telegram.tgbridge import cli, redact, telegram_api
from agent_telegram.tgbridge import config as config_module
from agent_telegram.tgbridge.bridge import OWNER_LOCK
from agent_telegram.tgbridge.config import Config
from agent_telegram.tgbridge.state import BridgeState
from agent_telegram.tgbridge.telegram_api import TelegramError

TOKEN = "123456789:" + "AbC-d_" * 6
MARKER = "（群已安全锁定，只发标题）"


class FakeAPI:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.media: list[dict[str, Any]] = []   # uploads that went through
        self.calls: list[tuple[str, str]] = []  # (method, text or file name) in order, failed ones too
        self.errors: dict[tuple[str, str], Exception] = {}   # (method, file name) -> raised error
        self.members: int | Exception = 2      # the owner and one bot: fine (expected 1 + 2)

    def member_count(self, chat_id: int) -> int:
        if isinstance(self.members, Exception):
            raise self.members
        return self.members

    def send_message(self, chat_id: int, text: str, **kwargs: Any) -> dict:
        self.sent.append({"chat_id": chat_id, "text": text, **kwargs})
        self.calls.append(("sendMessage", text))
        return {"message_id": 1}

    def _upload(self, method: str, chat_id: int, filename: str, data: bytes, **kwargs: Any) -> dict:
        self.calls.append((method, filename))
        if (method, filename) in self.errors:
            raise self.errors[method, filename]
        self.media.append({"method": method, "chat_id": chat_id, "filename": filename, "data": data, **kwargs})
        return {"message_id": 2}

    def send_photo(self, chat_id: int, filename: str, data: bytes, **kwargs: Any) -> dict:
        return self._upload("sendPhoto", chat_id, filename, data, **kwargs)

    def send_document(self, chat_id: int, filename: str, data: bytes, **kwargs: Any) -> dict:
        return self._upload("sendDocument", chat_id, filename, data, **kwargs)


@pytest.fixture
def bound(monkeypatch: pytest.MonkeyPatch) -> FakeAPI:
    cfg = Config(chat_id=-1001, allowed_user_ids=[42])
    api = FakeAPI()
    monkeypatch.setattr(Config, "load", classmethod(lambda cls, *a, **k: cfg))
    monkeypatch.setattr(config_module, "load_bot_token", lambda: TOKEN)
    monkeypatch.setattr(redact, "load_local_secrets", lambda *a, **k: [])  # never read ~/.codex
    monkeypatch.setattr(cli, "_api", lambda cfg, token: api)
    return api


def write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "msg.txt"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_notify_sends_the_file_as_plain_text(bound: FakeAPI, tmp_path: Path) -> None:
    assert cli.main(["notify", "--file", write(tmp_path, "🎨 设计有更新\n改动 <3>\n"), "--silent"]) == 0
    assert bound.sent == [{"chat_id": -1001, "text": "🎨 设计有更新\n改动 <3>", "silent": True,
                           "parse_mode": None}]


def test_notify_reads_stdin(bound: FakeAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("hello\n"))
    assert cli.main(["notify"]) == 0
    assert bound.sent[0]["text"] == "hello" and bound.sent[0]["silent"] is False


def test_notify_redacts_the_bot_token(bound: FakeAPI, tmp_path: Path) -> None:
    assert cli.main(["notify", "--file", write(tmp_path, f"title\nlog says {TOKEN}")]) == 0
    assert TOKEN not in bound.sent[0]["text"] and bound.sent[0]["text"].startswith("title\nlog says ")


def test_notify_refuses_empty_text_and_unbound_bridge(bound: FakeAPI, tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.main(["notify", "--file", write(tmp_path, "  \n")]) == 2
    assert cli.main(["notify", "--file", str(tmp_path / "missing.txt")]) == 2
    monkeypatch.setattr(config_module, "load_bot_token", lambda: None)
    assert cli.main(["notify", "--file", write(tmp_path, "x")]) == 2
    assert bound.sent == []


def test_notify_sends_a_short_title_under_a_security_lock(bound: FakeAPI, tmp_path: Path) -> None:
    first = "⚠️ export failed: " + "https://example.com/design/K " * 20
    BridgeState().set_locked(True, "群成员数 3 超过预期 2")
    assert cli.main(["notify", "--file", write(tmp_path, first + "\ndetails that may be private")]) == 5
    title, marker = bound.sent[-1]["text"].split("\n")
    assert marker == MARKER and len(title) <= 128 and title.startswith("⚠️ export failed: https://")
    assert "private" not in bound.sent[-1]["text"]
    BridgeState().set_locked(True, OWNER_LOCK)   # the owner's own /lock blocks writes only
    assert cli.main(["notify", "--file", write(tmp_path, "title\ndetails")]) == 0
    assert bound.sent[-1]["text"] == "title\ndetails"


@pytest.mark.parametrize("first", ["⚠️ export failed: " + TOKEN,
                                   "⚠️ export failed: " + "x" * 90 + " " + TOKEN])
def test_the_locked_title_is_redacted_before_it_is_cut(bound: FakeAPI, tmp_path: Path, first: str) -> None:
    BridgeState().set_locked(True, "群成员数 3 超过预期 2")
    assert cli.main(["notify", "--file", write(tmp_path, first + "\ndetails")]) == 5
    text = bound.sent[-1]["text"]
    assert not any(TOKEN[:n] in text for n in range(10, len(TOKEN) + 1))   # no prefix left at the cut


def test_notify_fails_closed_on_a_damaged_state_without_touching_it(bound: FakeAPI, tmp_path: Path) -> None:
    state = config_module.tg_dir() / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_bytes(b"\x00" * 64)
    assert cli.main(["notify", "--file", write(tmp_path, "title\ndetails")]) == 5
    assert bound.sent[-1]["text"] == "title\n" + MARKER
    assert state.read_bytes() == b"\x00" * 64
    assert [p.name for p in state.parent.iterdir()] == ["state.json"]


@pytest.mark.parametrize("members", [4, TelegramError("network")])
def test_extra_members_or_an_unknown_count_mean_titles_only(bound: FakeAPI, tmp_path: Path,
                                                             members: int | Exception) -> None:
    bound.members = members      # this machine's bridge has not locked (yet): check anyway
    assert cli.main(["notify", "--file", write(tmp_path, "title\ndetails")]) == 5
    assert bound.sent[-1]["text"] == "title\n" + MARKER


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def image(tmp_path: Path, name: str, size: int = 0) -> str:
    path = tmp_path / name
    path.write_bytes(PNG + bytes([len(name)]) * size)
    return str(path)


def test_notify_sends_photos_in_order_after_the_text(bound: FakeAPI, tmp_path: Path,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    first, second = image(tmp_path, "home.png"), image(tmp_path, "设置页.jpg", 3)
    args = ["notify", "--file", write(tmp_path, "🎨 设计有更新\n改动 3 处"), "--photo", first, "--photo", second]

    assert cli.main(args) == 0

    assert bound.calls == [("sendMessage", "🎨 设计有更新\n改动 3 处"), ("sendPhoto", "home.png"),
                           ("sendPhoto", "设置页.jpg")]
    assert [item["data"] for item in bound.media] == [Path(first).read_bytes(), Path(second).read_bytes()]
    assert [item["caption"] for item in bound.media] == ["🎨 设计有更新\nhome.png", "🎨 设计有更新\n设置页.jpg"]
    assert all(item["chat_id"] == -1001 and item["parse_mode"] is None and item["silent"] is True
               for item in bound.media)
    out = capsys.readouterr().out
    # A caller may time out during the uploads: this line says the text went out.
    assert out.index(cli.NOTIFY_TEXT_SENT) < out.index("图片 2/2 张")


@pytest.mark.parametrize("lock", ["state", "members"])
def test_notify_sends_no_photos_when_only_a_title_may_go_out(bound: FakeAPI, tmp_path: Path, lock: str,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    if lock == "state":
        BridgeState().set_locked(True, "群成员数 3 超过预期 2")
    else:
        bound.members = 4
    photo = image(tmp_path, "home.png")

    assert cli.main(["notify", "--file", write(tmp_path, "title\ndetails"), "--photo", photo]) == 5

    assert bound.calls == [("sendMessage", "title\n" + MARKER)] and bound.media == []
    assert "1 张图片没有发送" in capsys.readouterr().out


def test_notify_sends_no_photos_when_the_text_fails(bound: FakeAPI, tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(chat_id: int, text: str, **kwargs: Any) -> dict:
        raise TelegramError("network")

    monkeypatch.setattr(bound, "send_message", refuse)
    assert cli.main(["notify", "--file", write(tmp_path, "title"), "--photo", image(tmp_path, "a.png")]) == 1
    assert bound.calls == []


def test_large_images_go_as_documents_and_huge_ones_are_skipped(bound: FakeAPI, tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch,
                                                                capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(telegram_api, "PHOTO_MAX_BYTES", len(PNG) + 10)
    monkeypatch.setattr(telegram_api, "DOCUMENT_MAX_BYTES", len(PNG) + 100)
    small, large, huge = image(tmp_path, "s.png", 10), image(tmp_path, "l.png", 11), image(tmp_path, "h.png", 101)

    assert cli.main(["notify", "--file", write(tmp_path, "title"), "--photo", small, "--photo", large,
                     "--photo", huge]) == 0

    assert bound.calls[1:] == [("sendPhoto", "s.png"), ("sendDocument", "l.png")]
    assert bound.media[1]["data"] == Path(large).read_bytes() and bound.media[1]["caption"] == "title\nl.png"
    out = capsys.readouterr().out
    assert f"跳过图片 {huge}" in out and "50 MB" in out
    assert "图片 2/3 张（1 张按文件发送），1 张没有发出去" in out


def test_images_telegram_refuses_as_photos_go_as_documents(bound: FakeAPI, tmp_path: Path,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    bound.errors[("sendPhoto", "long.png")] = TelegramError("Bad Request: PHOTO_INVALID_DIMENSIONS", code=400)
    bound.errors[("sendPhoto", "down.png")] = TelegramError("网络错误（ConnectError）")
    paths = [image(tmp_path, name) for name in ("long.png", "down.png", "ok.png")]

    assert cli.main(["notify", "--file", write(tmp_path, "title"), *sum((["--photo", p] for p in paths), [])]) == 0

    assert bound.calls[1:] == [("sendPhoto", "long.png"), ("sendDocument", "long.png"), ("sendPhoto", "down.png"),
                               ("sendPhoto", "ok.png")]      # a network error is not retried as a document
    assert [(item["method"], item["filename"]) for item in bound.media] == [("sendDocument", "long.png"),
                                                                          ("sendPhoto", "ok.png")]
    out = capsys.readouterr().out
    assert "PHOTO_INVALID_DIMENSIONS" in out and f"图片 {paths[1]} 没有发出去" in out
    assert "图片 2/3 张（1 张按文件发送），1 张没有发出去" in out


def test_missing_unreadable_or_empty_images_are_skipped(bound: FakeAPI, tmp_path: Path,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    missing, empty = str(tmp_path / "missing.png"), tmp_path / "empty.png"
    empty.write_bytes(b"")
    folder = tmp_path / "folder.png"
    folder.mkdir()
    photo = image(tmp_path, "ok.png")

    args = ["notify", "--file", write(tmp_path, "title")]
    for path in (missing, str(empty), str(folder), photo):
        args += ["--photo", path]
    assert cli.main(args) == 0

    assert bound.calls == [("sendMessage", "title"), ("sendPhoto", "ok.png")]
    out = capsys.readouterr().out
    assert f"跳过图片 {missing}" in out and f"跳过图片 {empty}：文件是空的" in out and f"跳过图片 {folder}" in out
    assert "图片 1/4 张，3 张没有发出去" in out


def test_photo_captions_are_redacted_and_short(bound: FakeAPI, tmp_path: Path) -> None:
    key = "sk-" + "Q7" * 12
    first = "⚠️ export failed: " + "x" * 300 + " " + TOKEN
    photo = image(tmp_path, f"shot-{key}.png")

    assert cli.main(["notify", "--file", write(tmp_path, f"{TOKEN} {first}\nmore"), "--photo", photo]) == 0

    caption, name = bound.media[0]["caption"], bound.media[0]["filename"]
    assert not any(TOKEN[:n] in caption for n in range(10, len(TOKEN) + 1))
    assert key not in caption and key not in name and redact.MARK in name
    head, tail = caption.split("\n")
    assert head.startswith(redact.MARK + " ⚠️ export failed: xxx") and head.endswith("…")
    assert len(head) <= cli.NOTIFY_CAPTION_MAX and tail == name


def test_notify_refuses_more_than_ten_photos(bound: FakeAPI, tmp_path: Path) -> None:
    photos = [image(tmp_path, f"p{index}.png") for index in range(11)]
    args = ["notify", "--file", write(tmp_path, "title"), *sum((["--photo", p] for p in photos), [])]
    assert cli.main(args) == 2
    assert bound.calls == []
    assert cli.main(args[:-2]) == 0
    assert len(bound.media) == 10


def test_status_shows_only_the_log_of_the_current_run() -> None:
    from datetime import datetime

    started = datetime(2026, 10, 2, 12, 29, 7).timestamp()
    lines = [
        "2026-10-02 09:13:17 WARNING getUpdates 失败：旧的网络错误",
        "2026-10-02 12:18:57 INFO 桥已启动：上一次",
        "2026-10-02 12:29:07 INFO tgbridge 启动（pid 27328）",
        "2026-10-02 12:29:09 ERROR 出错了：",
        "Traceback (most recent call last):",
        "2026-10-02 12:30:00 INFO 后来的一行",
    ]
    assert cli.lines_since(lines, started) == lines[2:]
    assert cli.lines_since(lines, started + 3600) == []


@pytest.mark.skip(reason='Old agent_telegram cutover guard is excluded from agent_telegram; its migration gate is a later step.')
def test_run_and_hooks_install_refuse_after_cutover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("agent_telegram.cutover")  # the standalone bridge build has no cutover guard
    state = tmp_path / "migration-state.json"
    state.write_text('{"phase": "ACTIVE"}', encoding="utf-8")
    monkeypatch.setenv("AGENT_TG_SUPERSEDED_STATE", str(state))
    monkeypatch.setattr(cli, "cmd_run", lambda _args: pytest.fail("bridge ran after cutover"))
    calls: list[str] = []
    monkeypatch.setattr(cli, "cmd_hooks", lambda args: calls.append(args.hooks_action) or 0)
    for argv in (["run"], ["hooks", "install"], ["hooks", "install", "--force"]):
        with pytest.raises(SystemExit) as exit_info:
            cli.main(argv)
        assert exit_info.value.code == 3
        assert "phase=ACTIVE" in capsys.readouterr().err
    assert calls == []
    assert cli.main(["hooks", "remove"]) == 0
    assert cli.main(["hooks", "status"]) == 0
    assert calls == ["remove", "status"]

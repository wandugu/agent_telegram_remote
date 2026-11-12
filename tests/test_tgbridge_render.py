from __future__ import annotations

import html
import json
import re
import threading
import time
from html.parser import HTMLParser
from pathlib import Path

import pytest

from agent_telegram.tgbridge import render
from agent_telegram.tgbridge.model import CLAUDE, CODEX, Question, SessionView
from agent_telegram.tgbridge.redact import MARK, Redactor, load_local_secrets, redact_patterns
from agent_telegram.tgbridge.render import (
    MAX_TEXT,
    agent_label,
    ago,
    clip,
    confirm_card,
    duration,
    esc,
    permission_card,
    session_line,
    split_text,
    status_icon,
    topic_name,
    turn_card,
    utf16_len,
    visible,
)

TG_TOKEN = "1234567890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawx"
ALLOWED_TAGS = {"b", "i", "code", "pre", "blockquote"}


class _TagChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []
        self.text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"unexpected tag {tag}")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack.pop() != tag:
            self.errors.append(f"unbalanced </{tag}>")

    def handle_data(self, data: str) -> None:
        self.text.append(data)


def check_html(card: str) -> str:
    """Assert the card is balanced Telegram HTML; return its visible text."""

    checker = _TagChecker()
    checker.feed(card)
    checker.close()
    assert not checker.errors, checker.errors
    assert not checker.stack, checker.stack
    # Raw "<", ">" and "&" must never survive outside of tags and entities.
    stripped = re.sub(r"</?[a-z]+>", "", card)
    assert "<" not in stripped and ">" not in stripped
    assert not re.search(r"&(?!(?:lt|gt|amp);)", stripped)
    return "".join(checker.text)


# --- redaction -----------------------------------------------------------------------


def test_exact_secrets_longest_first_and_short_ignored() -> None:
    values = ["abcdefgh", "abcdefgh12345678", "short", "  padded-secret-value \n", 42]
    redactor = Redactor(values)  # type: ignore[arg-type]
    out = redactor("x abcdefgh12345678 y abcdefgh z short padded-secret-value")
    assert out == f"x {MARK} y {MARK} z short {MARK}"
    assert "12345678" not in out


def test_add_secret_later_and_empty_text() -> None:
    redactor = Redactor()
    assert redactor("my value zz-plain-value-99 here") == "my value zz-plain-value-99 here"
    redactor.add_secret("zz-plain-value-99")
    redactor.add_secret("tiny")
    redactor.add_secret(None)  # type: ignore[arg-type]
    assert redactor("my value zz-plain-value-99 here") == f"my value {MARK} here"
    assert redactor("tiny") == "tiny"
    assert redactor("") == ""


def test_empty_redactor_is_still_truthy_callable() -> None:
    redactor = Redactor()
    assert redactor
    assert redactor(f"bot{TG_TOKEN}") == f"bot{MARK}"


def test_exact_secret_with_regex_metacharacters() -> None:
    secret = "a+b*c(d)[e]{f}.g?h|i^j$k\\l"
    redactor = Redactor([secret])
    assert redactor(f"<{secret}>") == f"<{MARK}>"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("密码：hunter2", f"密码：{MARK}"),
        ("口令=abc123", f"口令={MARK}"),
        ("密钥: xyz-987", f"密钥: {MARK}"),
        ("验证码：123456", f"验证码：{MARK}"),
        ("令牌 = qwe", f"令牌 = {MARK}"),
        ("数据库密码：p@ss 然后", f"数据库密码：{MARK} 然后"),
    ],
)
def test_chinese_labels_keep_label(text: str, expected: str) -> None:
    assert Redactor()(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        f"token {TG_TOKEN}.",
        f"https://api.telegram.org/bot{TG_TOKEN}/getMe",
        f"curl https://api.telegram.org/bot{TG_TOKEN}/sendMessage -d x",
    ],
)
def test_telegram_bot_token(text: str) -> None:
    out = Redactor()(text)
    assert TG_TOKEN not in out
    assert "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsawx" not in out
    assert MARK in out


def test_telegram_token_in_url_keeps_url_shape() -> None:
    out = Redactor()(f"https://api.telegram.org/bot{TG_TOKEN}/getMe")
    assert out == f"https://api.telegram.org/bot{MARK}/getMe"


@pytest.mark.parametrize(
    "secret",
    [
        "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123",
        "sk-proj-ABCDEFGHIJKLMNOP1234",
        "ghp_" + "a1" * 18,
        "gho_" + "Z9" * 12,
        "ghs_" + "x" * 36,
        "github_pat_11ABCDEFG0123456789_abcdefghij",
        "glpat-abcdefghij1234567890",
        "hf_" + "AbCdEfGhIj" * 3,
        "xoxb-1234567890-abcdefghij",
        "xoxp-12345-67890-abcdef",
        "AIzaSyA-1234567890abcdefghijklmnopqrstu",
        "ya29.a0AfB_byC" + "x1Y2z3" * 6,
        "GOCSPX-" + "AbCdEf123_" * 3,
        "npm_" + "a1B2" * 9,
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
    ],
)
def test_token_prefixes(secret: str) -> None:
    out = Redactor()(f"before {secret} after")
    assert out == f"before {MARK} after"


@pytest.mark.parametrize(
    "text",
    [
        "task-management-system-v2 is fine",
        "The basic idea and a bearer of news, basically.",
        "Basic understanding of Bearer tokens",
        "meeting at 12:30:45 on 2026-10-01",
        "https://example.com/path/to/page?id=42&lang=zh",
        "hf_hub is a library; npm_config is fine",
        "修复了登录页面的样式问题，测试全部通过。",
        "desk-top and risk-assessment-framework-v2",
        "user@example.com wrote C:\\proj\\scripts",
    ],
)
def test_ordinary_text_untouched(text: str) -> None:
    assert Redactor()(text) == text


def test_private_key_blocks() -> None:
    complete = (
        "before\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEAabc\nqwertyuiopasdf==\n"
        "-----END RSA PRIVATE KEY-----\nafter"
    )
    assert Redactor()(complete) == f"before\n{MARK}\nafter"
    cut_off = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0B\nAQEFAASCBKcwggSjAgEAAoIB\n...(truncated)"
    out = Redactor()(cut_off)
    assert out == f"{MARK}\n...(truncated)"
    json_escaped = '{"key": "-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADAN\\n-----END PRIVATE KEY-----\\n"}'
    assert "MIIEvQIBADAN" not in Redactor()(json_escaped)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("postgres://user:pa55word@db.local/app", f"postgres://user:{MARK}@db.local/app"),
        ("Authorization: Bearer abc.def-123", f"Authorization: {MARK}"),
        ("authorization: basic dXNlcjpwYXNz", f"authorization: {MARK}"),
        ("Authorization: token ghp_" + "a" * 36, f"Authorization: {MARK}"),
        ("use Bearer dXNlcjpwYXNz here", f"use {MARK} here"),
        ("use bearer abc123def456 here", f"use {MARK} here"),
        ("Cookie: sid=abc; theme=dark\nnext line", f"Cookie: {MARK}\nnext line"),
        ("Set-Cookie: sid=abc; HttpOnly", f"Set-Cookie: {MARK}"),
        ("export GITHUB_TOKEN=abc123", f"export GITHUB_TOKEN={MARK}"),
        ("password = 'hunter 2' done", f"password = '{MARK}' done"),
        ('{"api_key": "abcdef", "name": "x"}', f'{{"api_key": "{MARK}", "name": "x"}}'),
        ('{"password": "p\\"w", "ok": 1}', f'{{"password": "{MARK}", "ok": 1}}'),
        ("GET /x?access_token=abc&x=1", f"GET /x?access_token={MARK}&x=1"),
        ("https://h/cb?code=1&client_secret=zzz#frag", f"https://h/cb?code=1&client_secret={MARK}#frag"),
    ],
)
def test_original_patterns(text: str, expected: str) -> None:
    assert Redactor()(text) == expected


def test_redaction_is_idempotent_and_removes_every_secret() -> None:
    secrets = ["local-exact-secret-1", "sk-abcdefghijklmnopqrstuvwx", TG_TOKEN, "hunter2"]
    text = (
        "exact local-exact-secret-1; key sk-abcdefghijklmnopqrstuvwx\n"
        f"bot {TG_TOKEN}\n密码：hunter2\nAuthorization: Bearer abcdefgh12345\n"
        "url https://u:p4ss@host/x?token=t0k3n&y=2"
    )
    redactor = Redactor(["local-exact-secret-1"])
    once = redactor(text)
    assert redactor(once) == once
    for secret in [*secrets, "abcdefgh12345", "p4ss", "t0k3n"]:
        assert secret not in once
    assert "&y=2" in once


@pytest.mark.parametrize(
    ("unit", "count"),
    [
        ("a-", 20_000),
        ("a.", 20_000),
        ("x_Y-", 10_000),
        ("-----BEGIN PRIVATE KEY----- x ", 1_000),
        ("Bearer basic ", 5_000),
    ],
)
def test_redaction_stays_linear_on_long_runs(unit: str, count: int) -> None:
    # The unbounded affixes of the original patterns took seconds here (minutes at 100k).
    text = unit * count
    started = time.perf_counter()
    Redactor(["some-exact-secret"])(text)
    assert time.perf_counter() - started < 2.0


def test_redact_patterns_function_has_no_exact_secrets() -> None:
    assert redact_patterns("x local-exact-secret-1") == "x local-exact-secret-1"
    assert redact_patterns(f"t {TG_TOKEN}") == f"t {MARK}"


def test_redactor_concurrent_add_and_call() -> None:
    redactor = Redactor()
    values = [f"concurrent-secret-{i:04d}" for i in range(200)]
    errors: list[BaseException] = []

    def add() -> None:
        try:
            for value in values:
                redactor.add_secret(value)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def call() -> None:
        try:
            for _ in range(200):
                redactor("text " + values[-1])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=add), threading.Thread(target=call), threading.Thread(target=call)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert redactor(" ".join(values)) == " ".join([MARK] * len(values))


# --- load_local_secrets ---------------------------------------------------------------------


def _write_json(path: Path, data: object, *, bom: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + raw)


def test_load_local_secrets_collects_secret_keys(tmp_path: Path) -> None:
    claude_home = tmp_path / "claude"
    codex_home = tmp_path / "codex"
    _write_json(
        claude_home / "settings.json",
        {
            "env": {
                "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_settings_pat_value_123",
                "ANTHROPIC_AUTH_TOKEN": "  anthropic-auth-value-456  ",
                "MAX_THINKING_TOKENS": "31999",
                "HTTP_PROXY": "http://127.0.0.1:7897/proxy-url",
            },
            "hooks": {
                "PermissionRequest": [
                    {"hooks": [{"type": "http", "url": "http://127.0.0.1:8767/claude/permission",
                                "headers": {"X-TgBridge-Token": "hook-token-value-789"}}]}
                ]
            },
            "apiKeys": ["list-item-secret-0001", "short", 7],
            "model": "claude-opus-long-model-name",
            "duplicate_secret": "ghp_settings_pat_value_123",
        },
        bom=True,
    )
    _write_json(
        codex_home / "auth.json",
        {"OPENAI_API_KEY": None, "tokens": {"id_token": "codex-id-token-value", "access_token": "codex-access-value",
                                            "account_id": "acct-1"}, "last_refresh": "2026-10-01T00:00:00Z"},
    )
    found = load_local_secrets(claude_home, codex_home)
    assert found == [
        "ghp_settings_pat_value_123",
        "anthropic-auth-value-456",
        "hook-token-value-789",
        "list-item-secret-0001",
        "codex-id-token-value",
        "codex-access-value",
    ]


def test_load_local_secrets_never_raises(tmp_path: Path) -> None:
    assert load_local_secrets(tmp_path / "missing", tmp_path / "missing2") == []
    claude_home = tmp_path / "claude"
    claude_home.mkdir()
    (claude_home / "settings.json").write_bytes(b"{not json")
    codex_home = tmp_path / "codex"
    _write_json(codex_home / "auth.json", ["top-level-list-not-secret-value"])
    assert load_local_secrets(claude_home, codex_home) == []
    # settings.json is a directory, auth.json deeply nested
    weird = tmp_path / "weird"
    (weird / "settings.json").mkdir(parents=True)
    nested: dict = {"token": "deep-secret-value-000"}
    for _ in range(100):
        nested = {"a": nested}
    _write_json(weird / "auth.json", nested)
    assert load_local_secrets(weird, weird) == []
    assert load_local_secrets(None, None) == []  # type: ignore[arg-type]


def test_loaded_secrets_feed_redactor(tmp_path: Path) -> None:
    _write_json(tmp_path / "c" / "settings.json", {"env": {"SOME_SECRET": "plain-looking-value-42"}})
    redactor = Redactor(load_local_secrets(tmp_path / "c", tmp_path / "x"))
    assert redactor("value plain-looking-value-42 end") == f"value {MARK} end"


# --- basic render helpers ---------------------------------------------------------------------


def test_esc_keeps_quotes() -> None:
    assert esc('<a href="x">&\'') == '&lt;a href="x"&gt;&amp;\''


def test_visible() -> None:
    assert visible("a\tb\nc") == "a\tb\nc"
    assert visible("line\r\nnext") == "line\nnext"
    assert visible("x\ry") == "x⟨U+000D⟩y"
    assert visible("\x00\x1b[31m\x7f\x85") == "⟨U+0000⟩⟨U+001B⟩[31m⟨U+007F⟩⟨U+0085⟩"
    bidi = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f\u061c"
    assert visible(bidi) == "".join(f"⟨U+{ord(ch):04X}⟩" for ch in bidi)
    zero_width = "\u200b\u200c\u200d\u2060\ufeff"
    assert visible(zero_width) == "⟨U+200B⟩⟨U+200C⟩⟨U+200D⟩⟨U+2060⟩⟨U+FEFF⟩"
    assert visible("中文 émoji 😀 …") == "中文 émoji 😀 …"
    assert visible("broken \ud83d end") == "broken ⟨U+D83D⟩ end"


def test_clip_short_and_exact() -> None:
    assert clip("hello", 5) == ("hello", False)
    assert clip("", 3) == ("", False)
    assert clip("hello!", 5) == ("hell…", True)
    assert clip("abc", 1) == ("…", True)
    assert clip("abc", 0) == ("…", True)


def test_clip_prefers_blank_line_then_line_end() -> None:
    text = "a" * 85 + "\n\n" + "b" * 5 + "\n" + "c" * 50
    out, truncated = clip(text, 100)
    assert truncated
    assert out == "a" * 85 + "…"
    text = "a" * 90 + "\n" + "b" * 50
    assert clip(text, 100) == ("a" * 90 + "…", True)
    # Line ends before the last 20% are ignored: hard cut.
    text = "a" * 10 + "\n" + "b" * 200
    out, truncated = clip(text, 100)
    assert truncated and len(out) == 100 and out.endswith("b…")


def test_clip_counts_utf16_units() -> None:
    text = "😀" * 10  # 20 UTF-16 units
    assert clip(text, 20) == (text, False)
    out, truncated = clip(text, 15)
    assert truncated and out == "😀" * 7 + "…"
    assert utf16_len(out) <= 15


@pytest.mark.parametrize("limit", [1, 2, 7, 50, 100, 3900])
def test_clip_never_exceeds_limit(limit: int) -> None:
    text = ("第一行 line\n\n" * 50 + "x😀" * 300) * 3
    out, truncated = clip(text, limit)
    assert truncated
    assert utf16_len(out) <= limit
    assert out.endswith("…")


def test_split_text_reconstructs_and_respects_limit() -> None:
    text = "".join(f"line {i} " + "x" * (i % 37) + "\n" for i in range(300))
    chunks = split_text(text, 200)
    assert "".join(chunks) == text
    assert all(utf16_len(chunk) <= 200 for chunk in chunks)
    # no line is split when every line fits
    assert all(chunk.endswith("\n") for chunk in chunks)


def test_split_text_hard_splits_long_lines() -> None:
    assert split_text("a\n" * 5 + "b" * 25, 10) == ["a\na\na\na\na\n", "b" * 10, "b" * 10, "b" * 5]
    words = " ".join(["word"] * 30)  # 149 chars
    chunks = split_text(words, 50)
    assert "".join(chunks) == words
    assert all(len(chunk) <= 50 for chunk in chunks)
    assert chunks[0].endswith(" ")


def test_split_text_edge_cases() -> None:
    assert split_text("") == []
    assert split_text("\n\n\n", 2) == []
    assert split_text("short") == ["short"]
    emoji = "😀" * 30
    chunks = split_text(emoji, 11)
    assert "".join(chunks) == emoji
    assert all(utf16_len(chunk) <= 11 for chunk in chunks)
    crlf = "a\r\nb\r\n" * 3
    assert "".join(split_text(crlf, 4)) == crlf


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, ""),
        (0, "0秒"),
        (-5, "0秒"),
        (40, "40秒"),
        (39.6, "40秒"),
        (59.4, "59秒"),
        (60, "1分"),
        (12 * 60 + 29, "12分"),
        (3599, "59分"),
        (3600, "1小时"),
        (3900, "1小时5分"),
        (26 * 3600 + 60, "26小时1分"),
        (float("nan"), ""),
    ],
)
def test_duration(seconds: float | None, expected: str) -> None:
    assert duration(seconds) == expected


@pytest.mark.parametrize(
    ("epoch", "now", "expected"),
    [
        (0, 1000, ""),
        (-1, 1000, ""),
        (1000, 1030, "刚刚"),
        (2000, 1000, "刚刚"),
        (1000, 1000 + 180, "3分钟前"),
        (1000, 1000 + 3599, "59分钟前"),
        (1000, 1000 + 7200, "2小时前"),
        (1000, 1000 + 3 * 86400 + 5, "3天前"),
    ],
)
def test_ago(epoch: float, now: float, expected: str) -> None:
    assert ago(epoch, now) == expected


def test_status_icon_and_agent_label() -> None:
    assert [status_icon(s) for s in ("completed", "failed", "aborted", "running", "waiting", "idle", "unknown")] == [
        "✅", "❌", "⏹", "⏳", "⚠️", "💤", "❔",
    ]
    assert status_icon("weird") == "❔"
    assert agent_label(CODEX) == "Codex"
    assert agent_label(CLAUDE) == "Claude"
    assert agent_label("other") == "other"


def test_topic_name() -> None:
    assert topic_name("pc1", CODEX, "  修复\n  登录\t问题 ") == "pc1·Codex·修复 登录 问题"
    assert topic_name("pc2", CLAUDE, "") == "pc2·Claude·（无标题）"
    assert topic_name("pc1", CLAUDE, "a\u202eb\u200bc") == "pc1·Claude·abc"
    long = topic_name("pc1", CODEX, "标题" * 100)
    assert len(long) == 128 and long.endswith("…")
    emoji = topic_name("pc1", CODEX, "😀" * 100)
    assert utf16_len(emoji) <= 128 and emoji.endswith("…")


# --- cards ---------------------------------------------------------------------------------


def test_turn_card_layout_and_escaping() -> None:
    card, truncated = turn_card(
        machine="pc1",
        agent=CODEX,
        title="修复 <bug> & 测试",
        status="completed",
        duration_s=725,
        text="结果 <b>ok</b> & done",
        labels=["自动继续", "来自 <Telegram>"],
        summary="改了 3 个文件 <x>",
    )
    assert not truncated
    assert card == (
        "<b>✅ pc1·Codex·修复 &lt;bug&gt; &amp; 测试</b> · 12分\n"
        "<i>自动继续 · 来自 &lt;Telegram&gt;</i>\n"
        "改了 3 个文件 &lt;x&gt;\n\n"
        "结果 &lt;b&gt;ok&lt;/b&gt; &amp; done"
    )
    check_html(card)


def test_turn_card_fallbacks() -> None:
    card, _ = turn_card(machine="m", agent=CLAUDE, title="t", status="failed", duration_s=None,
                        text=None, error="API Error: 529 <overloaded>")
    assert card == "<b>❌ m·Claude·t</b>\n\nAPI Error: 529 &lt;overloaded&gt;"
    card, _ = turn_card(machine="m", agent=CLAUDE, title="t", status="aborted", duration_s=3,
                        text="   ", error="")
    assert card == "<b>⏹ m·Claude·t</b> · 3秒\n\n（没有最终回复）"
    check_html(card)


def test_turn_card_truncates_to_limit() -> None:
    text = "段落一 <tag>\n\n" + "长文本 & " * 400
    card, truncated = turn_card(machine="pc1", agent=CODEX, title="t", status="completed", duration_s=1,
                                text=text, limit=200)
    assert truncated
    body = card.split("\n\n", 1)[1]
    assert utf16_len(html.unescape(body)) <= 200
    assert body.endswith("…")
    check_html(card)


def test_turn_card_total_never_exceeds_max_text() -> None:
    card, truncated = turn_card(
        machine="pc1", agent=CODEX, title="😀" * 200, status="completed", duration_s=99999,
        text="<>&" * 5000, labels=["标签" * 300], summary="摘要" * 1000, footer="📊 额度 <" * 100, limit=10_000,
    )
    assert truncated
    visible_text = check_html(card)
    assert utf16_len(visible_text) <= MAX_TEXT and card.endswith("…</i>")


def test_turn_card_footer_follows_the_reply() -> None:
    card, _ = turn_card(machine="pc1", agent=CODEX, title="t", status="completed", duration_s=None, text="好 <x>",
                        footer="📊 Codex 额度：本周剩 95%（10-09 09:23 重置）")
    assert card == "<b>✅ pc1·Codex·t</b>\n\n好 &lt;x&gt;\n\n<i>📊 Codex 额度：本周剩 95%（10-09 09:23 重置）</i>"
    check_html(card)


def _view(**overrides: object) -> SessionView:
    values: dict = {
        "agent": CLAUDE, "id": "s1", "title": "  a\n <title> ", "cwd": "C:\\x", "state": "running",
        "last_activity": 1000.0, "detail": "",
    }
    values.update(overrides)
    return SessionView(**values)


def test_session_line() -> None:
    assert session_line(_view(), 1200) == "⏳ Claude · a &lt;title&gt; · 3分钟前"
    line = session_line(_view(agent=CODEX, state="waiting", last_activity=0, detail="input needed " * 10), 1200)
    assert line.startswith("⚠️ Codex · a &lt;title&gt; · input needed")
    detail = line.split(" · ")[-1]
    assert len(detail) <= 60 and detail.endswith("…")
    assert session_line(_view(state="idle", title="", detail="  "), 1000) == "💤 Claude · （无标题） · 刚刚"
    long = session_line(_view(title="x" * 300), 1000)
    assert len(long) < 120


def test_permission_card_layout() -> None:
    tool_input = {"command": "rm -rf build\u202e && echo <done>", "description": "清理"}
    card, truncated, full = permission_card(
        machine="pc1", title="本地工具", cwd="C:\\projects\\<repo>", tool_name="Bash", tool_input=tool_input,
        permission_mode="default", agent_id=None,
    )
    assert not truncated
    dump = json.dumps(tool_input, ensure_ascii=False, indent=2)
    size = f"{len(dump.encode('utf-8'))} 字节 / {dump.count(chr(10)) + 1} 行"
    assert card.startswith("<b>🔐 pc1·Claude·本地工具 请求权限</b>\n")
    assert "工具：<code>Bash</code>" in card
    assert "目录：<code>C:\\projects\\&lt;repo&gt;</code>" in card
    assert "模式：<code>default</code>" in card
    assert f"大小：{size}" in card
    assert "子代理" not in card
    assert "⟨U+202E⟩ &amp;&amp; echo &lt;done&gt;" in card
    assert "\u202e" not in card
    assert card.endswith("</pre>")
    check_html(card)
    assert full.startswith("# 🔐 pc1·Claude·本地工具 请求权限\n")
    assert "```json\n" in full and "echo <done>" in full and "\u202e" not in full


def test_permission_card_subagent_and_truncation() -> None:
    tool_input = {"file_path": "C:\\x.py", "content": "print('x')\n" * 2000}
    card, truncated, full = permission_card(
        machine="pc1", title="t", cwd="C:\\x", tool_name="Write", tool_input=tool_input,
        permission_mode="acceptEdits", agent_id="agent-123", limit=300,
    )
    assert truncated
    assert "子代理：<code>agent-123</code>" in card
    body = card.split("<pre>", 1)[1].removesuffix("</pre>")
    assert utf16_len(html.unescape(body)) <= 300
    assert body.endswith("…")
    check_html(card)
    # the full text keeps everything
    assert full.count("print('x')") == 2000


def test_permission_card_redacts_everything() -> None:
    redactor = Redactor(["exact-local-secret-value"])
    tool_input = {"command": f"curl https://api.telegram.org/bot{TG_TOKEN}/getMe -H 'X: exact-local-secret-value'",
                  "env": {"API_KEY": "abc123"}}
    card, _, full = permission_card(
        machine="pc1", title="exact-local-secret-value", cwd="C:\\exact-local-secret-value",
        tool_name="Bash", tool_input=tool_input, permission_mode="bypassPermissions", agent_id=None,
        redact=redactor,
    )
    for secret in ("exact-local-secret-value", TG_TOKEN, "abc123"):
        assert secret not in card
        assert secret not in full
    assert MARK in card and MARK in full


def test_permission_card_fence_and_odd_input() -> None:
    card, _, full = permission_card(
        machine="m", title="t", cwd="", tool_name="", tool_input={"md": "```python\nx\n````"},
        permission_mode="", agent_id=None,
    )
    assert "`````json\n" in full and full.rstrip().endswith("`````")
    assert "工具：<code>?</code>" in card and "模式：<code>default</code>" in card
    card, _, _ = permission_card(
        machine="m", title="t", cwd="c", tool_name="X", tool_input={"when": object.__new__(object)},
        permission_mode="default", agent_id=None,
    )
    check_html(card)
    card, _, full = permission_card(
        machine="m", title="t", cwd="c", tool_name="X", tool_input={"s": "half \ud83d emoji"},
        permission_mode="default", agent_id=None,
    )
    assert "⟨U+D83D⟩" in card and "\ud83d" not in card and "\ud83d" not in full
    card.encode("utf-8")


def test_permission_card_huge_limit_stays_under_max_text() -> None:
    card, truncated, _ = permission_card(
        machine="m", title="t" * 500, cwd="c" * 2000, tool_name="Bash", tool_input={"c": "<x>" * 5000},
        permission_mode="default", agent_id="a", limit=100_000,
    )
    assert truncated
    assert utf16_len(check_html(card)) <= MAX_TEXT


@pytest.mark.parametrize(
    ("action", "header"),
    [
        ("send", "<b>📨 确认发送到 pc1·Claude·标题 &lt;1&gt;？</b>"),
        ("stop", "<b>⏹ 确认中断 pc1·Claude·标题 &lt;1&gt; 当前这一轮？</b>"),
        ("new", "<b>🆕 确认在 pc1·Claude·标题 &lt;1&gt; 新建会话？</b>"),
    ],
)
def test_confirm_card_actions(action: str, header: str) -> None:
    card = confirm_card(machine="pc1", agent=CLAUDE, title="标题 <1>", action=action, text="请继续 <x> & y")
    assert card == header + "\n<blockquote>请继续 &lt;x&gt; &amp; y</blockquote>"
    check_html(card)


def test_confirm_card_without_text_and_unknown_action() -> None:
    card = confirm_card(machine="m", agent=CODEX, title="t", action="stop", text="  ")
    assert card == "<b>⏹ 确认中断 m·Codex·t 当前这一轮？</b>"
    with pytest.raises(ValueError):
        confirm_card(machine="m", agent=CODEX, title="t", action="delete", text="x")


def test_confirm_card_truncates() -> None:
    text = "hello <world> " * 300
    card = confirm_card(machine="m", agent=CODEX, title="t", action="send", text=text, limit=100)
    quoted = card.split("<blockquote>", 1)[1].split("</blockquote>", 1)[0]
    assert utf16_len(html.unescape(quoted)) <= 100 and quoted.endswith("…")
    assert f"共 {len(text.strip())} 字" in card
    check_html(card)
    big = confirm_card(machine="m", agent=CODEX, title="t", action="new", text="<" * 10000, limit=50_000)
    assert utf16_len(check_html(big)) <= MAX_TEXT


def test_render_module_has_no_invisible_source_characters() -> None:
    source = Path(render.__file__).read_text(encoding="utf-8")
    assert visible(source) == source.replace("\r\n", "\n")


# --- review additions: leaks, encodability and robustness ------------------------------------

# Built with chr() so that this source file stays plain, encodable text.
HIGH = chr(0xD83D)  # lone high surrogate
LOW = chr(0xDE00)
REPLACEMENT = chr(0xFFFD)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # empty user part, the usual Redis form
        ("redis://:supersecretpass@localhost:6379/0", f"redis://:{MARK}@localhost:6379/0"),
        # a password containing "@" is not half shown
        ("postgres://user:p@ss@db.local/app", f"postgres://user:{MARK}@db.local/app"),
        ("mongodb+srv://admin:pa#ss@cluster0/db", f"mongodb+srv://admin:{MARK}@cluster0/db"),
    ],
)
def test_uri_credentials_review_cases(text: str, expected: str) -> None:
    assert Redactor()(text) == expected


def test_json_escaped_shell_quotes_and_keys_are_fully_redacted() -> None:
    redactor = Redactor()
    dumped = json.dumps({"command": 'export API_TOKEN="abc 123" && run'})
    out = redactor(dumped)
    assert "abc" not in out and "123" not in out
    assert json.loads(out) == {"command": f'export API_TOKEN="{MARK}" && run'}
    nested = json.dumps({"body": json.dumps({"api_key": "abc def", "name": "keep"})})
    out = redactor(nested)
    assert "abc" not in out and "def" not in out
    assert json.loads(json.loads(out)["body"]) == {"api_key": MARK, "name": "keep"}


def test_secret_names_of_any_length() -> None:
    name = "THIS_IS_A_VERY_LONG_ENVIRONMENT_VARIABLE_NAME_FOR_THE_" * 2 + "TOKEN_" + "X" * 60
    assert Redactor()(f"{name}=abcdef") == f"{name}={MARK}"
    # a name after "&" is a query parameter: the rest of the URL stays
    assert Redactor()("GET /cb?a=1&x_token=abc&y=2") == f"GET /cb?a=1&x_token={MARK}&y=2"
    # the name run must start at a boundary, and a non-ASCII letter before it is one
    assert Redactor()("中文token=abc") == f"中文token={MARK}"


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN PGP PRIVATE KEY BLOCK-----\n\nlQOYBFabc\n=Xyz1\n-----END PGP PRIVATE KEY BLOCK-----",
        "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----",
        # cut off, inside a serialised JSON string (line breaks as two characters)
        json.dumps({"k": "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0B\nAQEFAASCBKcwggSjAgEAAoIB"}),
        # cut off, on a single line
        "-----BEGIN EC PRIVATE KEY----- MHcCAQEEIIvQIBADANBgkqhkiG9w0B AQEFAASCBKcwggSjAgEAAoIB",
    ],
)
def test_private_key_variants(text: str) -> None:
    out = Redactor()(text)
    assert MARK in out
    for fragment in ("lQOYBF", "b3BlbnNzaC1r", "MIIEvQIBADAN", "AQEFAASCBKcw", "MHcCAQEEIIvQ"):
        assert fragment not in out


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN PRIVATE KEY----- is the header line you paste first",
        "http://host:8080/path and https://example.com/a@b",
        "a bearer of bad news",
        "tokentokentokentoken",
    ],
)
def test_review_ordinary_text_untouched(text: str) -> None:
    assert Redactor()(text) == text


def test_long_lowercase_bearer_token_and_fullwidth_label() -> None:
    assert Redactor()("use Bearer abcdefghijklmnopqrstuvwxyz now") == f"use {MARK} now"
    assert Redactor()("密码＝hunter2 结束") == f"密码＝{MARK} 结束"


def test_redactor_coerces_non_text() -> None:
    redactor = Redactor(["exact-secret-value"])
    assert redactor(None) == ""  # type: ignore[arg-type]
    assert redactor(ValueError("bad exact-secret-value")) == f"bad {MARK}"  # type: ignore[arg-type]
    assert redactor(12345) == "12345"  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("unit", "count"),
    [
        ("tokentoken", 20_000),
        ("&token", 20_000),
        ("a://b:", 30_000),
        ('x_token=\\"', 20_000),
        ('token="', 20_000),
        ("-----BEGIN PRIVATE KEY-----\n", 2_000),
        ("-----BEGIN PRIVATE KEY----- AAAAAAAAAAAA ", 2_000),
    ],
)
def test_review_patterns_stay_linear(unit: str, count: int) -> None:
    text = unit * count
    started = time.perf_counter()
    Redactor(["some-exact-secret"])(text)
    assert time.perf_counter() - started < 2.0


def test_load_local_secrets_inherits_secret_keys_and_splits_schemes(tmp_path: Path) -> None:
    _write_json(
        tmp_path / "claude" / "settings.json",
        {
            "credentials": {"github": "nested-under-credentials-1", "list": [{"value": "deeper-list-value-2"}]},
            "mcpServers": {"x": {"headers": {"Authorization": "Bearer header-bearer-token-3"}}},
            "env": {"PROJECT_HOME": "plain-value-not-sensitive"},
        },
    )
    found = load_local_secrets(tmp_path / "claude", tmp_path / "codex")
    assert found == [
        "nested-under-credentials-1",
        "deeper-list-value-2",
        "Bearer header-bearer-token-3",
        "header-bearer-token-3",
    ]
    assert Redactor(found)("curl -H 'X: header-bearer-token-3'") == f"curl -H 'X: {MARK}'"


def test_esc_repairs_surrogates() -> None:
    assert esc(f"a {HIGH} <b>") == f"a {REPLACEMENT} &lt;b&gt;"
    assert esc(HIGH + LOW) == chr(0x1F600)  # a split pair is joined
    esc(f"x {LOW} y").encode("utf-8")


def test_cards_with_lone_surrogates_can_be_sent_as_utf8() -> None:
    card, _ = turn_card(machine="m", agent=CODEX, title="t" + HIGH, status="completed", duration_s=1,
                        text=f"x {HIGH} y", labels=["l" + LOW], summary="s" + HIGH)
    card.encode("utf-8")
    confirm_card(machine="m", agent=CODEX, title="t", action="send", text=f"x {HIGH} y").encode("utf-8")
    session_line(_view(title="t" + HIGH, detail="d" + HIGH), 1000).encode("utf-8")
    _, _, full = permission_card(machine="m", title="t" + HIGH, cwd="c" + HIGH, tool_name="X" + HIGH,
                                 tool_input={"s": HIGH}, permission_mode="default", agent_id=None)
    full.encode("utf-8")


def test_visible_marks_more_invisible_characters() -> None:
    codes = (0x00AD, 0x180E, 0x2028, 0x2029, 0x2061, 0x2064, 0x206A, 0xFFF9, 0xE0001, 0xE0041, 0xE007F)
    hidden = "".join(map(chr, codes))
    assert visible(hidden) == "".join(f"⟨U+{code:04X}⟩" for code in codes)
    # emoji with a variation selector and ordinary text stay as they are
    assert visible("⚠️ ok ✅") == "⚠️ ok ✅"
    assert topic_name("m", CODEX, "a" + chr(0xE0041) + chr(0xE0042) + "b") == "m·Codex·ab"


def test_session_line_redacts_and_thirty_lines_fit_one_message() -> None:
    redactor = Redactor(["exact-local-secret-value"])
    line = session_line(_view(title="fix exact-local-secret-value", detail=f"bot{TG_TOKEN}"), 1200, redact=redactor)
    assert "exact-local-secret-value" not in line and TG_TOKEN not in line
    assert MARK in line
    worst = _view(agent=CLAUDE, state="waiting", title=chr(0x1F600) * 200, detail="细节" * 200, last_activity=1.0)
    lines = [f"{index}. {session_line(worst, 10**9)}" for index in range(1, 31)]
    head = "[pc2] 最近 30 个会话"
    text = html.unescape("\n".join([head, *lines]))
    assert utf16_len(text) <= MAX_TEXT


def test_permission_card_redacts_strings_before_serialising() -> None:
    redactor = Redactor()
    tool_input = {
        "command": 'export API_TOKEN="abc 123" && curl -H "Authorization: Bearer zz9-token" x',
        "body": json.dumps({"password": "p w"}),
        "nested": [{"env": {"DB_PASSWORD": "has space"}}],
    }
    card, _, full = permission_card(machine="m", title="t", cwd="c", tool_name="Bash", tool_input=tool_input,
                                    permission_mode="default", agent_id=None, redact=redactor)
    for fragment in ("abc", "123", "zz9-token", "p w", "has space"):
        assert fragment not in card
        assert fragment not in full
    check_html(card)


def test_permission_card_survives_cycles_deep_nesting_and_odd_types() -> None:
    cyclic: dict = {"a": "x"}
    cyclic["self"] = cyclic
    deep: list = []
    node = deep
    for _ in range(5000):
        child: list = []
        node.append(child)
        node = child
    for tool_input in (cyclic, {"deep": deep}, {1: "int key", (2, 3): "tuple key"}):
        card, _, full = permission_card(
            machine="m", title=None, cwd=None, tool_name=None, tool_input=tool_input,  # type: ignore[arg-type]
            permission_mode=None, agent_id=None, redact=Redactor(),  # type: ignore[arg-type]
        )
        check_html(card)
        assert full.startswith("# 🔐 m·Claude·")


@pytest.mark.parametrize(("kind", "tool"), [("command", "Codex 命令"), ("file", "Codex 文件修改")])
def test_codex_approval_card_uses_codex_title_and_permission_display(kind: str, tool: str) -> None:
    tool_input = {"command": "echo <done>", "cwd": "C:\\project", "reason": "访问网络",
                  "network": {"host": "example.test"}, "extra_permissions": {"write": True},
                  "files": ["src/<x>.py"]}
    card, cut, full = render.codex_approval_card(
        machine="m", title="任务", cwd="C:\\project", kind=kind, tool_input=tool_input)
    assert not cut
    assert card.startswith("<b>🔐 m·Codex·任务 请求权限</b>")
    assert f"工具：<code>{tool}</code>" in card
    assert "echo &lt;done&gt;" in card and "src/&lt;x&gt;.py" in card
    assert "访问网络" in card and "example.test" in card and '"write": true' in card
    assert full.startswith("# 🔐 m·Codex·任务 请求权限\n")
    assert "echo <done>" in full
    check_html(card)


def test_codex_approval_input_omits_empty_fields_and_ipc_identifiers() -> None:
    from types import SimpleNamespace

    event = SimpleNamespace(command="echo x", command_cwd="C:\\x", reason="", network=None,
                            extra_permissions={"network": True}, files=["a.py"], request_id=42,
                            digest="private-identity")
    assert render.codex_approval_input(event) == {
        "command": "echo x", "cwd": "C:\\x", "extra_permissions": {"network": True}, "files": ["a.py"]}


def test_codex_approval_light_redaction_keeps_command_context() -> None:
    redactor = Redactor(["exact-local-secret-value"])
    card, _, full = render.codex_approval_card(
        machine="m", title="t", cwd="C:\\x", kind="command", redact=redactor.light,
        tool_input={"command": "API_TOKEN=exact-local-secret-value && echo <after>",
                    "network": {"header": f"bot{TG_TOKEN}"}})
    for text in (card, full):
        assert "exact-local-secret-value" not in text and TG_TOKEN not in text
        assert "API_TOKEN=" in text and "after" in text and MARK in text
    check_html(card)


@pytest.mark.parametrize("field", ["title", "cwd", "tool_input"])
def test_codex_approval_card_reports_all_truncated_fields(field: str) -> None:
    args = {"machine": "m", "title": "t", "cwd": "C:\\x", "kind": "command",
            "tool_input": {"command": "echo x"}}
    if field == "tool_input":
        args[field] = {"command": "echo x\n" * 2000}
    else:
        args[field] = "长标题目录" * 300
    card, cut, full = render.codex_approval_card(**args)
    assert cut
    assert utf16_len(check_html(card)) <= MAX_TEXT
    if field == "tool_input":
        assert full.count("echo x") == 2000
    else:
        assert args[field] in full


def test_codex_approval_card_rejects_other_approval_kinds() -> None:
    with pytest.raises(ValueError, match="command and file"):
        render.codex_approval_card(machine="m", title="t", cwd="c", kind="permissions", tool_input={})


def test_split_text_long_single_line_is_fast() -> None:
    text = "A" * 3_000_000
    started = time.perf_counter()
    chunks = split_text(text, 3900)
    assert time.perf_counter() - started < 1.0
    assert "".join(chunks) == text
    assert all(utf16_len(chunk) <= 3900 for chunk in chunks)
    mixed = (chr(0x1F600) + "x ") * 100_000
    chunks = split_text(mixed, 3900)
    assert "".join(chunks) == mixed
    assert all(utf16_len(chunk) <= 3900 for chunk in chunks)


QUESTIONS = [Question("scope", "先做哪一部分？<b>", "范围", [("P1 优先（推荐）", "先打基础"), ("并行", "")]),
             Question("data", "数据从哪来？", "", [])]


def test_question_card_lists_questions_options_and_what_was_picked() -> None:
    card, truncated = render.question_card(machine="pc1", agent=CODEX, title="论文", questions=QUESTIONS,
                                           answers=[["并行"], []], typed=[False, False], index=1, review=False,
                                           footer="点选项选择")
    assert not truncated and card.startswith("<b>❓ pc1·Codex·论文 在问你</b>")
    assert "<b>1/2 · 范围</b>\n先做哪一部分？&lt;b&gt;\nA. P1 优先（推荐） — 先打基础\nB. 并行" in card
    assert "<i>✅ 已选：并行</i>" in card and "<b>👉 2/2 · 问题 2</b>" in card and card.endswith("<i>点选项选择</i>")
    typed, _ = render.question_card(machine="pc1", agent=CLAUDE, title="t", questions=QUESTIONS,
                                    answers=[["自己写的"], []], typed=[True, False], index=1, review=True)
    assert "✍️ 已填：自己写的" in typed and "（未回答）" in typed and "👉" not in typed
    check_html(card)
    check_html(typed)


def test_question_card_and_plan_card_fit_one_message() -> None:
    long = [Question(f"q{index}", "很长的问题" * 300, "标题" * 50, [(f"选项{option}" * 40, "说明" * 200)
                                                               for option in range(8)]) for index in range(4)]
    card, truncated = render.question_card(machine="pc1", agent=CLAUDE, title="t", questions=long,
                                           answers=[[] for _ in long], typed=[False] * 4, index=1, review=False,
                                           footer="点选项选择")
    assert truncated and utf16_len(card) <= 3800 and card.endswith("<i>点选项选择</i>")
    assert card.count("H. 选项7") == 1 and "👉 2/4" in card  # the question being answered keeps its options
    check_html(card)
    tiny, truncated = render.question_card(machine="pc1", agent=CLAUDE, title="t", questions=long,
                                           answers=[[] for _ in long], typed=[False] * 4, index=0, review=False,
                                           limit=2600)
    assert truncated and utf16_len(tiny) <= 2600 and "A. 选项0" in tiny and "说明" not in tiny
    check_html(tiny)
    plan, cut = render.plan_card(machine="pc1", title="t", plan="步骤\n" * 3000, feedback="先别删", footer="点按钮")
    assert cut and utf16_len(plan) <= 3800 and "✍️ 修改意见：先别删" in plan and plan.endswith("<i>点按钮</i>")
    assert plan.startswith("<b>📋 pc1·Claude·t 的计划等你批准</b>\n\n<blockquote>步骤")
    check_html(plan)
    short, cut = render.plan_card(machine="pc1", title="t", plan="1. 改代码 <x>")
    assert not cut and "1. 改代码 &lt;x&gt;" in short


def test_a_question_card_that_shortens_anything_says_so() -> None:
    long_option = [Question("q", "选哪个？", "", [("很长的选项" * 30, "")])]
    card, truncated = render.question_card(machine="pc1", agent=CLAUDE, title="t", questions=long_option,
                                           answers=[[]], typed=[False], index=0, review=False)
    assert truncated and utf16_len(card) < 1000  # it fits, but the option shows cut: [查看全文]
    _, truncated = render.question_card(machine="pc1", agent=CLAUDE, title="t", questions=QUESTIONS,
                                        answers=[[], []], typed=[False, False], index=0, review=False)
    assert not truncated


def test_a_closing_line_always_shows() -> None:
    long = [Question(f"q{index}", "很长的问题" * 300, "标题", [(f"选项{option}" * 40, "说明" * 200)
                                                         for option in range(8)]) for index in range(4)]
    outcome = "💻 已在电脑上回答：\n" + "答案" * 2000
    card, truncated = render.question_card(machine="pc1", agent=CODEX, title="t", questions=long,
                                           answers=[[] for _ in long], typed=[], index=-1, review=False,
                                           footer=outcome)
    assert truncated and utf16_len(card) <= 3800 and "已在电脑上回答" in card and card.endswith("…</i>")
    assert "👉" not in card and "（未回答）" not in card  # closed: no question is being answered
    check_html(card)
    plan, _ = render.plan_card(machine="pc1", title="t", plan="步骤\n" * 3000, footer="⌛ " + "很久" * 2000)
    assert utf16_len(plan) <= 3800 and "⌛ 很久" in plan
    check_html(plan)


def test_question_markdown_and_button_labels() -> None:
    text = render.question_markdown(machine="pc1", agent=CODEX, title="论文", questions=QUESTIONS)
    assert "## 1. 范围" in text and "- **A. P1 优先（推荐）** — 先打基础" in text and "## 2. 问题" in text
    assert render.question_markdown(machine="pc1", agent=CLAUDE, title="t", plan="# 计划").startswith(
        "# pc1·Claude·t\n\n# 计划")
    assert render.button_label("A. " + "很长" * 40 + "​") == "A. " + "很长" * 18 + "…"
    assert render.option_letter(0) == "A" and render.option_letter(26) == "27"

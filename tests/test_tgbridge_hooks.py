from __future__ import annotations

import copy
import io
import json
import os
import subprocess
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from agent_telegram.tgbridge import hook_report, hooks_install
LEGACY_TITLE_HOOK_MODULE = "standalone_unrelated.claude_title_prefix"
from agent_telegram.tgbridge.hooks_install import HOOK_MODULE

TOKEN = "hook-token-AbC_123-xyz"
FAKE_PAT = "github_pat_11FAKEFAKEFAKEFAKEFAKE_do_not_print"
_HOOK_ENV = (
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "AGENT_TELEGRAM_HEADLESS",
)


@pytest.fixture(autouse=True)
def clean_hook_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests may themselves run inside a Claude Code session: never forward its inbox."""

    for name in _HOOK_ENV:
        monkeypatch.delenv(name, raising=False)


# --- hook_report -----------------------------------------------------------------------------


class Recorder:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error = error

    def __call__(self, url: str, body: bytes, headers: dict[str, str], timeout: float) -> None:
        self.calls.append({"url": url, "body": json.loads(body.decode("utf-8")), "headers": headers,
                           "timeout": timeout, "raw": body})
        if self.error is not None:
            raise self.error


@pytest.fixture
def tg(isolated_var: Path) -> Path:
    base = isolated_var / "tgbridge"
    base.mkdir(parents=True)
    (base / "config.json").write_text(json.dumps({"hook_port": 9123, "machine": "pc1"}), encoding="utf-8")
    (base / "hook_token.txt").write_text(TOKEN + "\n", encoding="utf-8")
    return base


def stop_input(**extra: Any) -> dict[str, Any]:
    data = {
        "hook_event_name": "Stop",
        "session_id": "0f5c7d8e-1111-2222-3333-444455556666",
        "transcript_path": r"C:\Users\x\.claude\projects\C--w\0f5c7d8e.jsonl",
        "cwd": r"C:\projects\code",
        "permission_mode": "default",
        "last_assistant_message": "完成了。",
        "stop_hook_active": False,
    }
    data.update(extra)
    return data


def raw(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def test_report_posts_trimmed_payload(tg: Path, monkeypatch: pytest.MonkeyPatch,
                                      capfd: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "claude-desktop")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", r"\\.\pipe\LOCAL\cc-msg-0123456789abcdef0123456789abcdef")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "inbox-secret")
    monkeypatch.setattr(hook_report, "_hook_parent_pid", lambda: 4242)
    post = Recorder()

    assert hook_report.main([], stdin=raw(stop_input(unknown_field="dropped", model="claude-opus")), post=post) == 0

    assert len(post.calls) == 1
    call = post.calls[0]
    assert call["url"] == "http://127.0.0.1:9123/claude/event"
    assert call["headers"]["X-TgBridge-Token"] == TOKEN
    assert call["headers"]["Content-Type"].startswith("application/json")
    assert call["timeout"] == 3.0
    assert call["body"] == {
        "event": "Stop",
        "session_id": "0f5c7d8e-1111-2222-3333-444455556666",
        "transcript_path": r"C:\Users\x\.claude\projects\C--w\0f5c7d8e.jsonl",
        "cwd": r"C:\projects\code",
        "permission_mode": "default",
        "agent_id": None,
        "agent_type": None,
        "source": None,
        "last_assistant_message": "完成了。",
        "error": None,
        "error_details": None,
        "stop_hook_active": False,
        "model": "claude-opus",
        "ppid": 4242,
        "entrypoint": "claude-desktop",
        "headless": False,
        "inbox": {"socket": r"\\.\pipe\LOCAL\cc-msg-0123456789abcdef0123456789abcdef", "token": "inbox-secret"},
    }
    assert "完成了".encode() in call["raw"]  # UTF-8, not \u escapes
    out, err = capfd.readouterr()
    assert out == "" and err == ""


def test_report_caps_message_and_forwards_failure_fields(tg: Path) -> None:
    post = Recorder()
    data = stop_input(hook_event_name="StopFailure", last_assistant_message="x" * 25_000,
                      error="rate_limit", error_details="429 Too Many Requests", agent_id="sub-1",
                      agent_type="Explore")

    assert hook_report.main(stdin=raw(data), post=post) == 0

    body = post.calls[0]["body"]
    assert body["event"] == "StopFailure"
    assert body["last_assistant_message"] == "x" * hook_report.MAX_MESSAGE_CHARS
    assert (body["error"], body["error_details"]) == ("rate_limit", "429 Too Many Requests")
    assert (body["agent_id"], body["agent_type"]) == ("sub-1", "Explore")
    assert "inbox" not in body


@pytest.mark.parametrize("env", [
    {"CLAUDE_CODE_MESSAGING_SOCKET": r"\\.\pipe\LOCAL\cc-msg-00"},
    {"CLAUDE_CODE_MESSAGING_TOKEN": "t"},
    {"CLAUDE_CODE_MESSAGING_SOCKET": "", "CLAUDE_CODE_MESSAGING_TOKEN": "t"},
])
def test_report_inbox_needs_socket_and_token(tg: Path, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    post = Recorder()
    hook_report.main(stdin=raw(stop_input(hook_event_name="SessionStart", source="startup")), post=post)
    body = post.calls[0]["body"]
    assert "inbox" not in body
    assert body["source"] == "startup"


@pytest.mark.parametrize(("value", "expected"), [("1", True), ("0", False), ("true", False)])
def test_report_headless_flag(tg: Path, monkeypatch: pytest.MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("AGENT_TELEGRAM_HEADLESS", value)
    post = Recorder()
    hook_report.main(stdin=raw(stop_input()), post=post)
    assert post.calls[0]["body"]["headless"] is expected
    assert post.calls[0]["body"]["entrypoint"] == ""


@pytest.mark.parametrize("switch", ["disabled", "no_config", "no_token", "empty_token"])
def test_report_kill_switches(tg: Path, switch: str) -> None:
    if switch == "disabled":
        (tg / "hooks.disabled").write_text("", encoding="utf-8")
    elif switch == "no_config":
        (tg / "config.json").unlink()
    elif switch == "no_token":
        (tg / "hook_token.txt").unlink()
    else:
        (tg / "hook_token.txt").write_text("  \n", encoding="utf-8")
    post = Recorder()
    assert hook_report.main(stdin=raw(stop_input()), post=post) == 0
    assert post.calls == []


def test_report_tolerates_bom_in_state_files(tg: Path) -> None:
    (tg / "hook_token.txt").write_bytes(b"\xef\xbb\xbf" + TOKEN.encode() + b"\r\n")
    (tg / "config.json").write_bytes(b"\xef\xbb\xbf" + json.dumps({"hook_port": 9555}).encode())
    post = Recorder()
    hook_report.main(stdin=raw(stop_input()), post=post)
    assert post.calls[0]["headers"]["X-TgBridge-Token"] == TOKEN
    assert post.calls[0]["url"] == "http://127.0.0.1:9555/claude/event"


def test_build_payload_uses_the_given_environment() -> None:
    env = {"CLAUDE_CODE_ENTRYPOINT": "cli", "AGENT_TELEGRAM_HEADLESS": "1",
           "CLAUDE_CODE_MESSAGING_SOCKET": "uds:x", "CLAUDE_CODE_MESSAGING_TOKEN": "y"}
    payload = hook_report.build_payload({"hook_event_name": "SessionStart", "source": "resume"}, env)
    assert (payload["event"], payload["source"], payload["entrypoint"], payload["headless"]) == \
        ("SessionStart", "resume", "cli", True)
    assert payload["inbox"] == {"socket": "uds:x", "token": "y"}
    assert payload["session_id"] is None


def test_report_without_tgbridge_dir_does_nothing(isolated_var: Path) -> None:
    post = Recorder()
    assert hook_report.main(stdin=raw(stop_input()), post=post) == 0
    assert post.calls == []


@pytest.mark.parametrize("stdin", [
    b"",
    b"not json",
    b"[1, 2]",
    b'{"session_id": "no event name"}',
    b"\xff\xfe garbage",
    b"{" + b" " * hook_report.MAX_STDIN + b"}",
], ids=["empty", "text", "list", "no_event", "binary", "oversize"])
def test_report_ignores_unusable_input(tg: Path, stdin: bytes, capfd: pytest.CaptureFixture[str]) -> None:
    post = Recorder()
    assert hook_report.main(stdin=stdin, post=post) == 0
    assert post.calls == []
    assert capfd.readouterr() == ("", "")


def test_report_accepts_utf8_bom(tg: Path) -> None:
    post = Recorder()
    hook_report.main(stdin=b"\xef\xbb\xbf" + raw(stop_input()), post=post)
    assert post.calls[0]["body"]["event"] == "Stop"


def test_report_survives_a_lone_surrogate(tg: Path) -> None:
    """A \\ud800 escape is legal JSON but cannot be encoded as UTF-8; the event still goes out."""

    post = Recorder()
    stdin = b'{"hook_event_name": "Stop", "session_id": "s", "last_assistant_message": "a\\ud800b \xe5\xa5\xbd"}'
    assert hook_report.main(stdin=stdin, post=post) == 0
    assert post.calls[0]["body"]["last_assistant_message"] == "a?b 好"


# --- hook_report: Claude Code's pid ------------------------------------------------------


@pytest.fixture
def in_venv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Pretend to run as the base interpreter started by the venv redirector at `launcher`."""

    launcher = tmp_path / "venv" / "Scripts" / "pythonw.exe"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "base"))
    monkeypatch.setattr(sys, "executable", str(launcher))
    monkeypatch.setattr(hook_report.os, "getppid", lambda: 500)
    return launcher


def test_parent_pid_steps_over_the_venv_redirector(in_venv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[int] = []

    def parent(pid: int) -> tuple[int, str]:
        asked.append(pid)
        return 77, str(in_venv).upper()  # Windows paths compare case-insensitively

    monkeypatch.setattr(hook_report, "_process_parent", parent)
    assert hook_report._hook_parent_pid() == 77
    assert asked == [500]


@pytest.mark.parametrize("answer", [
    (77, r"C:\Program Files\Claude\claude.exe"),  # started directly by Claude Code
    None,  # parent already gone / not visible
    (0, "same"),  # no usable grandparent
    OSError("snapshot failed"),
])
def test_parent_pid_falls_back_to_getppid(in_venv: Path, monkeypatch: pytest.MonkeyPatch, answer: Any) -> None:
    def parent(pid: int) -> tuple[int, str] | None:
        if isinstance(answer, BaseException):
            raise answer
        if answer is not None and answer[1] == "same":
            return answer[0], str(in_venv)
        return answer

    monkeypatch.setattr(hook_report, "_process_parent", parent)
    assert hook_report._hook_parent_pid() == 500


def test_parent_pid_outside_a_venv_is_getppid(in_venv: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)

    def unexpected(pid: int) -> None:
        raise AssertionError("no process lookup outside a venv")

    monkeypatch.setattr(hook_report, "_process_parent", unexpected)
    assert hook_report._hook_parent_pid() == 500


@pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp / QueryFullProcessImageNameW are Windows APIs")
def test_process_parent_reads_the_real_process_table() -> None:
    parent, image = hook_report._process_parent(os.getpid())
    assert parent == os.getppid()
    assert Path(image).is_file() and Path(image).name.lower().startswith("python")
    assert hook_report._process_parent(0) is None  # the idle process cannot be opened


@pytest.mark.parametrize("error", [OSError("connection refused"), TimeoutError(), ValueError("bad"),
                                   KeyboardInterrupt(), SystemExit(3)])
def test_report_swallows_every_error(tg: Path, error: BaseException, capfd: pytest.CaptureFixture[str]) -> None:
    post = Recorder(error)
    assert hook_report.main(stdin=raw(stop_input()), post=post) == 0
    assert len(post.calls) == 1
    assert capfd.readouterr() == ("", "")


@pytest.mark.parametrize("config", ["{broken", '{"hook_port": "9000"}', '{"hook_port": 0}',
                                    '{"hook_port": 70000}', '{"hook_port": true}', "[]"])
def test_report_falls_back_to_default_port(tg: Path, config: str) -> None:
    (tg / "config.json").write_text(config, encoding="utf-8")
    post = Recorder()
    hook_report.main(stdin=raw(stop_input()), post=post)
    assert post.calls[0]["url"] == "http://127.0.0.1:8767/claude/event"


def test_report_reads_sys_stdin(tg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw(stop_input())), encoding="utf-8"))
    post = Recorder()
    assert hook_report.main(post=post) == 0
    assert post.calls[0]["body"]["session_id"] == "0f5c7d8e-1111-2222-3333-444455556666"


def test_read_stdin_falls_back_to_fd0_without_sys_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [b"ab", b"cd", b""]
    reads: list[tuple[int, int]] = []

    def fake_read(fd: int, size: int) -> bytes:
        reads.append((fd, size))
        return chunks.pop(0)

    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(hook_report.os, "read", fake_read)
    assert hook_report._read_stdin() == b"abcd"
    assert {fd for fd, _ in reads} == {0}


def test_read_stdin_without_any_stdin_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(fd: int, size: int) -> bytes:
        raise OSError(9, "Bad file descriptor")

    monkeypatch.setattr(sys, "stdin", None)
    monkeypatch.setattr(hook_report.os, "read", broken)
    assert hook_report._read_stdin() == b""


def test_read_stdin_stops_after_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"z" * 100)))
    assert hook_report._read_stdin(limit=10) == b"z" * 11


class _Capture(BaseHTTPRequestHandler):
    received: list[dict[str, Any]] = []

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.received.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                              "body": json.loads(self.rfile.read(length).decode("utf-8"))})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def _interpreters() -> list[Path]:
    exe = Path(sys.executable)
    found = [exe]
    pythonw = exe.with_name("pythonw.exe")
    if pythonw.is_file():
        found.append(pythonw)
    return found


@pytest.mark.parametrize("interpreter", _interpreters(), ids=lambda path: path.name)
def test_hook_command_end_to_end(tmp_path: Path, isolated_var: Path, interpreter: Path) -> None:
    """The exact installed command line posts to a loopback server, prints nothing and exits 0."""

    _Capture.received = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = isolated_var / "tgbridge"
        base.mkdir(parents=True)
        (base / "config.json").write_text(json.dumps({"hook_port": server.server_address[1]}), encoding="utf-8")
        (base / "hook_token.txt").write_text(TOKEN, encoding="utf-8")
        env = {name: value for name, value in os.environ.items()
               if not name.startswith(("CLAUDE_CODE_", "AGENT_TELEGRAM_"))}
        env.update({
            "AGENT_TG_VAR_DIR": str(isolated_var),
            "CLAUDE_CODE_MESSAGING_SOCKET": r"\\.\pipe\LOCAL\cc-msg-00112233445566778899aabbccddeeff",
            "CLAUDE_CODE_MESSAGING_TOKEN": "inbox-token",
            "HTTP_PROXY": "http://127.0.0.1:9",  # must be ignored
            "http_proxy": "http://127.0.0.1:9",
        })
        command = [str(interpreter), "-X", "utf8", "-P", "-m", HOOK_MODULE]
        completed = subprocess.run(command, input=raw(stop_input(last_assistant_message="好的")), capture_output=True,
                                   env=env, cwd=tmp_path, timeout=60,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    finally:
        server.shutdown()
        server.server_close()
    assert completed.returncode == 0
    assert completed.stdout == b"" and completed.stderr == b""
    assert len(_Capture.received) == 1
    request = _Capture.received[0]
    assert request["path"] == "/claude/event"
    assert request["headers"]["x-tgbridge-token"] == TOKEN
    assert request["headers"]["host"] == f"127.0.0.1:{server.server_address[1]}"
    assert request["body"]["event"] == "Stop"
    assert request["body"]["last_assistant_message"] == "好的"
    assert request["body"]["inbox"] == {"socket": r"\\.\pipe\LOCAL\cc-msg-00112233445566778899aabbccddeeff",
                                        "token": "inbox-token"}
    # The process that ran the command (here: this test; in real use: Claude Code), not the
    # venv redirector that sits between it and the interpreter.
    assert request["body"]["ppid"] == os.getpid()


def test_hook_command_with_nothing_listening_exits_quietly(tmp_path: Path, isolated_var: Path) -> None:
    import socket

    with socket.socket() as probe:  # a port that was free a moment ago
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = isolated_var / "tgbridge"
    base.mkdir(parents=True)
    (base / "config.json").write_text(json.dumps({"hook_port": port}), encoding="utf-8")
    (base / "hook_token.txt").write_text(TOKEN, encoding="utf-8")
    env = {name: value for name, value in os.environ.items() if not name.startswith("CLAUDE_CODE_")}
    env["AGENT_TG_VAR_DIR"] = str(isolated_var)
    completed = subprocess.run([sys.executable, "-X", "utf8", "-P", "-m", HOOK_MODULE], input=raw(stop_input()),
                               capture_output=True, env=env, cwd=tmp_path, timeout=60,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert (completed.returncode, completed.stdout, completed.stderr) == (0, b"", b"")


def test_hook_report_imports_stay_light(tmp_path: Path) -> None:
    code = (
        "import sys, agent_telegram.tgbridge.hook_report\n"
        "print(','.join(sorted(m for m in sys.modules if m.split('.')[0] in ('agent_telegram', 'httpx', 'mcp', 'win32api'))))"
    )
    completed = subprocess.run([sys.executable, "-P", "-c", code], capture_output=True, text=True, cwd=tmp_path,
                               timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().split(",") == [
        "agent_telegram", "agent_telegram.paths", "agent_telegram.tgbridge",
        "agent_telegram.tgbridge.hook_report",
    ]


# --- hooks_install: pure functions -------------------------------------------------------


PYTHONW = Path(r"C:\venv\Scripts\pythonw.exe")


def ours(port: int = 8767, token: str = TOKEN, pythonw: Path = PYTHONW, include_http: bool = True) -> dict:
    return hooks_install.build_hooks(pythonw=pythonw, port=port, token=token, include_http=include_http)


def sample_settings() -> dict[str, Any]:
    return {
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": FAKE_PAT, "MAX_THINKING_TOKENS": "32000"},
        "permissions": {"allow": ["Bash(git status:*)"], "defaultMode": "default"},
        "hooks": {
            "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "notify.exe --done"}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.cmd"},
                                                         {"type": "http", "url": "http://127.0.0.1:9999/check"}]}],
            "Notification": [{"hooks": [{"type": "command", "command": "toast.exe"}]}],
        },
        "statusLine": {"type": "command", "command": "状态栏.cmd"},
        "model": "opus",
    }


def test_build_hooks_shape() -> None:
    hooks = ours(port=9000)
    assert list(hooks) == ["SessionStart", "Stop", "StopFailure", "PreToolUse", "PermissionRequest"]
    for event in ("SessionStart", "Stop", "StopFailure"):
        assert hooks[event] == [{"hooks": [{
            "type": "command", "command": str(PYTHONW), "args": ["-X", "utf8", "-P", "-m", HOOK_MODULE],
            "async": True, "timeout": 30,
        }]}]
    assert hooks["PreToolUse"] == [{"hooks": [{"type": "http", "url": "http://127.0.0.1:9000/claude/pretooluse",
                                               "timeout": 3, "headers": {"X-TgBridge-Token": TOKEN}}]}]
    timeout = hooks_install.PERMISSION_TIMEOUT_S
    assert hooks["PermissionRequest"] == [{"hooks": [{
        "type": "http", "url": "http://127.0.0.1:9000/claude/permission", "timeout": timeout,
        "headers": {"X-TgBridge-Token": TOKEN, "X-TgBridge-Timeout": str(timeout)}}]}]
    assert timeout > 6 * 3600  # the bridge waits up to 6 h and answers before the hook's own timeout
    assert hooks["Stop"][0]["hooks"][0]["args"] is not hooks["SessionStart"][0]["hooks"][0]["args"]
    assert list(ours(include_http=False)) == ["SessionStart", "Stop", "StopFailure"]


@pytest.mark.parametrize(("hook", "expected"), [
    ({"type": "command", "command": "x.exe", "args": ["-m", HOOK_MODULE]}, True),
    ({"type": "command", "command": f"pythonw.exe -X utf8 -m {HOOK_MODULE}"}, True),
    ({"type": "command", "command": r"C:\proj\packages\agent_telegram\.venv\Scripts\pythonw.exe",
      "args": ["-m", hooks_install.LEGACY_HOOK_MODULE]}, True),
    ({"type": "command", "command": f'"C:\\proj space\\packages\\agent_telegram\\.venv\\Scripts\\pythonw.exe" -m "{hooks_install.LEGACY_HOOK_MODULE}"'}, True),
    ({"type": "command", "command": r"C:\proj\packages\agent_telegram\.venv\Scripts\pythonw.exe",
      "args": ["-m", LEGACY_TITLE_HOOK_MODULE, "stop-hook"]}, False),
    ({"type": "command", "command": "x.exe", "args": ["-m", hooks_install.LEGACY_HOOK_MODULE + "_other"]}, False),
    ({"type": "command", "command": "x.exe", "args": ["-m", HOOK_MODULE + "_other"]}, False),
    ({"type": "command", "command": "notify.exe"}, False),
    ({"type": "http", "url": "http://127.0.0.1:8767/claude/permission", "headers": {"X-TgBridge-Token": "t"}}, True),
    ({"type": "http", "url": "http://127.0.0.1:1/claude/x", "headers": {"x-tgbridge-token": "t"}}, True),
    ({"type": "http", "url": "http://127.0.0.1:8767/claude/permission"}, False),
    ({"type": "http", "url": "http://127.0.0.1:8767/other", "headers": {"X-TgBridge-Token": "t"}}, False),
    ({"type": "http", "url": "http://localhost:8767/claude/event", "headers": {"X-TgBridge-Token": "t"}}, False),
    ({"type": "http", "url": "https://127.0.0.1:8767/claude/event", "headers": {"X-TgBridge-Token": "t"}}, False),
    ({"type": "http", "url": "http://127.0.0.1:8767/claude/x", "headers": ["X-TgBridge-Token"]}, False),
    ({"type": "prompt", "prompt": HOOK_MODULE}, False),
    ("not a dict", False),
    (None, False),
])
def test_is_ours(hook: Any, expected: bool) -> None:
    assert hooks_install.is_ours(hook) is expected


def test_merge_into_empty_settings() -> None:
    assert hooks_install.merge({}, ours()) == {"hooks": ours()}
    assert hooks_install.merge({"hooks": {}}, ours()) == {"hooks": ours()}


def test_merge_keeps_foreign_hooks_and_keys() -> None:
    original = sample_settings()
    snapshot = copy.deepcopy(original)

    merged = hooks_install.merge(original, ours())

    assert original == snapshot  # pure
    assert {key: value for key, value in merged.items() if key != "hooks"} == \
        {key: value for key, value in original.items() if key != "hooks"}
    hooks = merged["hooks"]
    assert list(hooks)[:3] == ["Stop", "PreToolUse", "Notification"]  # existing events keep their place
    assert hooks["Stop"][0] == original["hooks"]["Stop"][0]
    assert hooks["Stop"][1:] == ours()["Stop"]
    assert hooks["PreToolUse"][0] == original["hooks"]["PreToolUse"][0]
    assert hooks["PreToolUse"][1:] == ours()["PreToolUse"]
    assert hooks["Notification"] == original["hooks"]["Notification"]
    assert hooks["SessionStart"] == ours()["SessionStart"]
    assert hooks_install.describe(merged) == {
        "ours": {"Stop": 1, "PreToolUse": 1, "SessionStart": 1, "StopFailure": 1, "PermissionRequest": 1},
        "foreign": {"Stop": 1, "PreToolUse": 2, "Notification": 1},
    }


def test_merge_is_idempotent_and_replaces_stale_entries() -> None:
    once = hooks_install.merge(sample_settings(), ours())
    assert hooks_install.merge(once, ours()) == once

    refreshed = hooks_install.merge(once, ours(port=9100, token="new-token", pythonw=Path(r"D:\py\pythonw.exe")))
    assert refreshed == hooks_install.merge(sample_settings(), ours(port=9100, token="new-token",
                                                                    pythonw=Path(r"D:\py\pythonw.exe")))
    assert TOKEN not in json.dumps(refreshed)
    assert "8767" not in json.dumps(refreshed["hooks"])


def test_merge_without_http_drops_installed_http_hooks() -> None:
    full = hooks_install.merge(sample_settings(), ours())
    commands_only = hooks_install.merge(full, ours(include_http=False))
    assert "PermissionRequest" not in commands_only["hooks"]
    assert commands_only["hooks"]["PreToolUse"] == sample_settings()["hooks"]["PreToolUse"]
    assert hooks_install.describe(commands_only)["ours"] == {"Stop": 1, "SessionStart": 1, "StopFailure": 1}


def test_merge_and_strip_replace_both_hook_generations_without_duplicates() -> None:
    original = sample_settings()
    legacy = {"type": "command", "command": r"C:\proj\packages\agent_telegram\.venv\Scripts\pythonw.exe",
              "args": ["-X", "utf8", "-P", "-m", hooks_install.LEGACY_HOOK_MODULE]}
    mixed = hooks_install.merge(original, ours())
    for event in hooks_install.COMMAND_EVENTS:
        mixed["hooks"][event].append({"hooks": [legacy]})
    assert hooks_install.strip(mixed) == original
    refreshed = hooks_install.merge(mixed, ours(pythonw=Path(r"C:\new\pythonw.exe")))
    assert refreshed == hooks_install.merge(original, ours(pythonw=Path(r"C:\new\pythonw.exe")))
    assert hooks_install.merge(refreshed, ours(pythonw=Path(r"C:\new\pythonw.exe"))) == refreshed


def test_merge_keeps_foreign_hook_sharing_a_group_with_ours() -> None:
    mine = ours()["Stop"][0]["hooks"][0]
    settings = {"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "a.exe"}, mine]}]}}
    merged = hooks_install.merge(settings, ours())
    assert merged["hooks"]["Stop"] == [{"matcher": "", "hooks": [{"type": "command", "command": "a.exe"}]},
                                       *ours()["Stop"]]


def test_merge_leaves_malformed_foreign_parts_alone() -> None:
    settings = {"hooks": {"Stop": ["weird", {"matcher": "x"}, {"hooks": "nope"}], "Custom": "text"}}
    merged = hooks_install.merge(settings, ours())
    assert merged["hooks"]["Stop"][:3] == ["weird", {"matcher": "x"}, {"hooks": "nope"}]
    assert merged["hooks"]["Custom"] == "text"
    assert hooks_install.strip(merged) == settings


@pytest.mark.parametrize("settings", [{"hooks": []}, {"hooks": None}, {"hooks": {"Stop": {"hooks": []}}}])
def test_merge_rejects_unexpected_shapes(settings: dict) -> None:
    with pytest.raises(ValueError):
        hooks_install.merge(settings, ours())


def test_strip_restores_the_original() -> None:
    for original in (sample_settings(), {"model": "opus"}, {}):
        merged = hooks_install.merge(original, ours())
        snapshot = copy.deepcopy(merged)
        stripped = hooks_install.strip(merged)
        assert merged == snapshot  # pure
        assert stripped == original
        assert hooks_install.strip(stripped) == stripped


def test_strip_only_drops_what_it_emptied() -> None:
    settings = {"hooks": {"Stop": [], "PreToolUse": [{"matcher": "Bash", "hooks": []}]}, "x": 1}
    assert hooks_install.strip(settings) == settings
    assert hooks_install.strip({"hooks": {}}) == {"hooks": {}}
    with_ours = hooks_install.merge(settings, ours())
    # an event list that only held our entries goes away, even if it was an empty list before
    assert hooks_install.strip(with_ours) == {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": []}]}, "x": 1}
    assert hooks_install.strip({"hooks": "odd"}) == {"hooks": "odd"}


def test_describe_counts() -> None:
    assert hooks_install.describe({}) == {"ours": {}, "foreign": {}}
    assert hooks_install.describe({"hooks": "odd"}) == {"ours": {}, "foreign": {}}
    assert hooks_install.describe(sample_settings()) == {"ours": {},
                                                         "foreign": {"Stop": 1, "PreToolUse": 2, "Notification": 1}}


def test_default_pythonw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    python = scripts / "python.exe"
    python.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(python))
    assert hooks_install.default_pythonw() == python
    (scripts / "pythonw.exe").write_bytes(b"")
    assert hooks_install.default_pythonw() == scripts / "pythonw.exe"
    monkeypatch.setattr(sys, "executable", str(scripts / "pythonw.exe"))
    assert hooks_install.default_pythonw() == scripts / "pythonw.exe"


# --- hooks_install: apply ----------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "claude-home"
    path.mkdir()
    return path


def write_settings(home: Path, settings: Any, *, bom: bool = False, crlf: bool = False) -> bytes:
    text = json.dumps(settings, ensure_ascii=False, indent=4)
    if crlf:
        text = text.replace("\n", "\r\n")
    data = (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")
    (home / "settings.json").write_bytes(data)
    return data


def backups(home: Path) -> list[Path]:
    folder = home / "backups"
    return sorted(folder.glob("settings.json.bak-tgbridge-*")) if folder.is_dir() else []


def leftovers(home: Path) -> list[Path]:
    return [path for path in home.iterdir() if path.name.endswith(".tmp")]


def merge_ours(settings: dict) -> dict:
    return hooks_install.merge(settings, ours())


def test_apply_writes_utf8_lf_without_bom_and_backs_up(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    original = write_settings(home, sample_settings(), bom=True, crlf=True)
    path = home / "settings.json"

    assert hooks_install.apply(path, merge_ours, backup_dir=home / "backups") is True

    data = path.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in data and data.endswith(b"}\n")
    assert "状态栏.cmd".encode() in data  # ensure_ascii=False
    assert data == (json.dumps(merge_ours(sample_settings()), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    saved = backups(home)
    assert len(saved) == 1 and saved[0].read_bytes() == original
    assert hooks_install._backup_pattern("settings.json").match(saved[0].name)
    assert leftovers(home) == []
    assert capsys.readouterr() == ("", "")


def test_apply_unchanged_returns_false_without_touching(home: Path) -> None:
    path = home / "settings.json"
    write_settings(home, merge_ours(sample_settings()))
    before = path.read_bytes()
    stamp = path.stat().st_mtime_ns

    assert hooks_install.apply(path, merge_ours, backup_dir=home / "backups") is False

    assert path.read_bytes() == before and path.stat().st_mtime_ns == stamp
    assert backups(home) == []


@pytest.mark.parametrize("content", [
    b'{"env": {"TOKEN": "' + FAKE_PAT.encode() + b'"},',
    b'["' + FAKE_PAT.encode() + b'"]',
    b'"' + FAKE_PAT.encode() + b'"',
    b"",
    b'{"a": "\xff' + FAKE_PAT.encode() + b'"}',
    b'{"env": 1, "env": "' + FAKE_PAT.encode() + b'"}',
])
def test_apply_refuses_non_object_files_without_quoting_them(home: Path, content: bytes) -> None:
    path = home / "settings.json"
    path.write_bytes(content)

    with pytest.raises(ValueError) as caught:
        hooks_install.apply(path, merge_ours, backup_dir=home / "backups")

    assert FAKE_PAT not in "".join(traceback.format_exception(caught.value))
    assert path.read_bytes() == content
    assert backups(home) == [] and leftovers(home) == []


def test_apply_refuses_content_it_cannot_write_back(home: Path) -> None:
    path = home / "settings.json"
    content = b'{"env": {"TOKEN": "' + FAKE_PAT.encode() + b'\\ud800"}}'  # parses, but not encodable as UTF-8
    path.write_bytes(content)

    with pytest.raises(ValueError) as caught:
        hooks_install.apply(path, merge_ours, backup_dir=home / "backups")

    assert FAKE_PAT not in "".join(traceback.format_exception(caught.value))
    assert path.read_bytes() == content
    assert backups(home) == [] and leftovers(home) == []


def test_apply_refuses_a_transform_that_touches_other_keys(home: Path) -> None:
    path = home / "settings.json"
    original = write_settings(home, sample_settings())

    def evil(settings: dict) -> dict:
        settings["model"] = "haiku"
        return merge_ours(settings)

    with pytest.raises(ValueError):
        hooks_install.apply(path, evil, backup_dir=home / "backups")
    assert path.read_bytes() == original
    assert backups(home) == []


def test_apply_creates_a_missing_file(home: Path) -> None:
    path = home / "settings.json"
    assert hooks_install.apply(path, merge_ours, backup_dir=home / "backups") is True
    assert json.loads(path.read_text(encoding="utf-8")) == {"hooks": ours()}
    assert not (home / "backups").exists()
    assert hooks_install.apply(home / "other.json", hooks_install.strip, backup_dir=home / "backups") is False
    assert not (home / "other.json").exists()


def test_apply_rotates_only_its_own_backups(home: Path) -> None:
    folder = home / "backups"
    folder.mkdir()
    old = [folder / f"settings.json.bak-tgbridge-2020010{day}-120000" for day in range(1, 5)]
    for item in old:
        item.write_bytes(b"{}")
    foreign = [folder / ".claude.json.backup.1790000000", folder / "settings.json.bak", folder / "notes.txt"]
    for item in foreign:
        item.write_bytes(b"keep")
    write_settings(home, sample_settings())

    assert hooks_install.apply(home / "settings.json", merge_ours, backup_dir=folder) is True

    remaining = backups(home)
    assert len(remaining) == 3
    assert old[3] in remaining and old[2] in remaining
    assert old[0] not in remaining and old[1] not in remaining
    assert all(item.read_bytes() == b"keep" for item in foreign)


def test_rotation_orders_same_second_backups(home: Path) -> None:
    folder = home / "backups"
    folder.mkdir()
    names = ["settings.json.bak-tgbridge-20260101-120000", "settings.json.bak-tgbridge-20260101-120000-1",
             "settings.json.bak-tgbridge-20260101-120000-2", "settings.json.bak-tgbridge-20260101-120000-10",
             "settings.json.bak-tgbridge-20251231-235959-99"]
    for name in names:
        (folder / name).write_bytes(b"{}")
    hooks_install._rotate(folder, "settings.json", 2)
    assert sorted(path.name for path in folder.iterdir()) == sorted([names[3], names[2]])


def test_backup_names_do_not_collide(home: Path) -> None:
    folder = home / "backups"
    first = hooks_install._backup(folder, "settings.json", b"1")
    second = hooks_install._backup(folder, "settings.json", b"2")
    assert first != second
    assert (first.read_bytes(), second.read_bytes()) == (b"1", b"2")


def test_apply_starts_over_after_a_concurrent_change(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = home / "settings.json"
    write_settings(home, sample_settings())
    real_read = hooks_install._read_bytes
    calls = {"n": 0}

    def racing_read(target: Path, *args: Any, **kwargs: Any) -> bytes | None:
        calls["n"] += 1
        if calls["n"] == 2:  # the re-check of the first attempt: another program saved meanwhile
            changed = sample_settings()
            changed["theme"] = "dark"
            write_settings(home, changed)
        return real_read(target, *args, **kwargs)

    monkeypatch.setattr(hooks_install, "_read_bytes", racing_read)

    assert hooks_install.apply(path, merge_ours, backup_dir=home / "backups") is True

    result = json.loads(path.read_text(encoding="utf-8"))
    assert result["theme"] == "dark"
    assert hooks_install.describe(result)["ours"]["Stop"] == 1
    saved = backups(home)
    assert len(saved) == 1 and json.loads(saved[0].read_text(encoding="utf-8"))["theme"] == "dark"
    assert leftovers(home) == []


def test_apply_gives_up_when_the_file_keeps_changing(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = home / "settings.json"
    write_settings(home, sample_settings())
    real_read = hooks_install._read_bytes
    calls = {"n": 0}

    def busy_read(target: Path, *args: Any, **kwargs: Any) -> bytes | None:
        calls["n"] += 1
        if calls["n"] % 2 == 0:
            changed = sample_settings()
            changed["counter"] = calls["n"]
            write_settings(home, changed)
        return real_read(target, *args, **kwargs)

    monkeypatch.setattr(hooks_install, "_read_bytes", busy_read)

    with pytest.raises(RuntimeError):
        hooks_install.apply(path, merge_ours, backup_dir=home / "backups")

    assert calls["n"] == 2 * hooks_install.MAX_ATTEMPTS
    assert json.loads(path.read_text(encoding="utf-8"))["counter"] == 6  # the other writer's content survives
    assert backups(home) == [] and leftovers(home) == []


def test_apply_restores_the_original_when_the_check_after_replace_fails(home: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    path = home / "settings.json"
    original = write_settings(home, sample_settings())
    real_read = hooks_install._read_bytes
    calls = {"n": 0}

    def tampered_read(target: Path, *args: Any, **kwargs: Any) -> bytes | None:
        calls["n"] += 1
        data = real_read(target, *args, **kwargs)
        if calls["n"] == 3:  # the parse after os.replace sees another model value
            return data.replace(b'"opus"', b'"haiku"')
        return data

    monkeypatch.setattr(hooks_install, "_read_bytes", tampered_read)

    with pytest.raises(RuntimeError):
        hooks_install.apply(path, merge_ours, backup_dir=home / "backups")

    assert path.read_bytes() == original
    assert len(backups(home)) == 1
    assert leftovers(home) == []


def test_apply_rewrites_the_target_of_a_symlink(home: Path, tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    link = home / "settings.json"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("creating symlinks needs Developer Mode or admin rights here")

    assert hooks_install.apply(link, merge_ours, backup_dir=home / "backups") is True

    assert link.is_symlink()
    assert json.loads(real.read_text(encoding="utf-8"))["model"] == "opus"
    assert "hooks" in json.loads(real.read_text(encoding="utf-8"))


# --- hooks_install: install / remove / status -----------------------------------------------


@pytest.fixture
def pythonw(tmp_path: Path) -> Path:
    path = tmp_path / "venv" / "Scripts" / "pythonw.exe"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    return path


def test_install_status_remove_roundtrip(home: Path, pythonw: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_settings(home, sample_settings())
    before = hooks_install.status(claude_home=home)
    assert before["exists"] and not before["installed"] and not before["http_installed"]
    assert before["foreign"] == {"Stop": 1, "PreToolUse": 2, "Notification": 1}

    assert hooks_install.install(claude_home=home, pythonw=pythonw, port=8767, token=TOKEN) is True
    assert hooks_install.install(claude_home=home, pythonw=pythonw, port=8767, token=TOKEN) is False

    state = hooks_install.status(claude_home=home)
    assert state["installed"] and state["http_installed"]
    assert state["ours"] == {"Stop": 1, "PreToolUse": 1, "SessionStart": 1, "StopFailure": 1, "PermissionRequest": 1}
    assert state["foreign"] == before["foreign"]
    assert state["commands"] == [str(pythonw)] and state["missing_commands"] == []
    assert state["ports"] == [8767] and state["outdated"] == []
    assert state["error"] == "" and state["disable_all_hooks"] is False
    dumped = json.dumps(state)
    assert TOKEN not in dumped and FAKE_PAT not in dumped

    assert hooks_install.install(claude_home=home, pythonw=pythonw, port=8800, token="other-token",
                                 include_http=True) is True
    assert hooks_install.status(claude_home=home)["ports"] == [8800]
    assert hooks_install.install(claude_home=home, pythonw=pythonw, port=8800, token="other-token",
                                 include_http=False) is True
    state = hooks_install.status(claude_home=home)
    assert state["installed"] and not state["http_installed"] and state["ports"] == []

    assert hooks_install.remove(claude_home=home) is True
    assert json.loads((home / "settings.json").read_text(encoding="utf-8")) == sample_settings()
    assert hooks_install.remove(claude_home=home) is False
    assert len(backups(home)) == 3
    assert capsys.readouterr() == ("", "")


def test_status_flags_hooks_an_older_version_installed(home: Path, pythonw: Path) -> None:
    old = ours(pythonw=pythonw)
    old["PermissionRequest"] = [{"hooks": [{"type": "http", "url": "http://127.0.0.1:8767/claude/permission",
                                            "timeout": 580, "headers": {"X-TgBridge-Token": TOKEN}}]}]
    write_settings(home, hooks_install.merge({}, old))
    assert hooks_install.status(claude_home=home)["outdated"] == ["PermissionRequest"]
    assert hooks_install.install(claude_home=home, pythonw=pythonw, port=8767, token=TOKEN) is True
    assert hooks_install.status(claude_home=home)["outdated"] == []


def test_install_into_a_fresh_home(home: Path, pythonw: Path) -> None:
    assert hooks_install.status(claude_home=home)["exists"] is False
    assert hooks_install.remove(claude_home=home) is False
    assert not (home / "settings.json").exists()
    assert hooks_install.install(claude_home=home, pythonw=pythonw, port=8767, token=TOKEN) is True
    assert hooks_install.status(claude_home=home)["installed"] is True
    assert hooks_install.remove(claude_home=home) is True
    assert json.loads((home / "settings.json").read_text(encoding="utf-8")) == {}


def test_status_reports_problems(home: Path, pythonw: Path) -> None:
    (home / "settings.json").write_text('{"env": {"T": "' + FAKE_PAT + '"', encoding="utf-8")
    state = hooks_install.status(claude_home=home)
    assert state["exists"] and state["error"] and FAKE_PAT not in state["error"]
    assert not state["installed"]

    gone = home / "gone" / "pythonw.exe"
    write_settings(home, hooks_install.merge({"disableAllHooks": True}, ours(pythonw=gone)))
    state = hooks_install.status(claude_home=home)
    assert state["disable_all_hooks"] is True and state["error"] == ""
    assert state["installed"] is True
    assert state["commands"] == [str(gone)] and state["missing_commands"] == [str(gone)]


def test_status_does_not_treat_a_shell_command_line_as_a_path(home: Path) -> None:
    shell_form = {"type": "command", "command": f"pythonw.exe -X utf8 -m {HOOK_MODULE}", "async": True}
    write_settings(home, {"hooks": {"Stop": [{"hooks": [shell_form]}]}})
    state = hooks_install.status(claude_home=home)
    assert state["ours"] == {"Stop": 1}
    assert state["commands"] == [] and state["missing_commands"] == []
    assert hooks_install.remove(claude_home=home) is True
    assert json.loads((home / "settings.json").read_text(encoding="utf-8")) == {}


@pytest.mark.parametrize(("port", "token"), [(0, TOKEN), (70000, TOKEN), (True, TOKEN), ("8767", TOKEN),
                                             (8767, ""), (8767, "has space"), (8767, "line\nbreak"),
                                             (8767, "令牌"), (8767, "abc$HOME"), (8767, "abc${HOME}")])
def test_install_validates_arguments(home: Path, pythonw: Path, port: Any, token: str) -> None:
    with pytest.raises(ValueError) as caught:
        hooks_install.install(claude_home=home, pythonw=pythonw, port=port, token=token)
    if token:
        assert token not in str(caught.value)
    assert not (home / "settings.json").exists()


def test_install_needs_an_existing_interpreter(home: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        hooks_install.install(claude_home=home, pythonw=tmp_path / "missing" / "pythonw.exe", port=8767, token=TOKEN)
    with pytest.raises(ValueError):
        hooks_install.install(claude_home=home, pythonw=Path("pythonw.exe"), port=8767, token=TOKEN)
    assert not (home / "settings.json").exists()

from __future__ import annotations

import gc
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_telegram import jobs
from agent_telegram.tgbridge import headless, inbox, winsys

windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows API")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BASE_PYTHON = getattr(sys, "_base_executable", sys.executable)
SESSION = "0f8fad5b-d9cb-469f-a165-70867728950e"
SOCKET = r"\\.\pipe\LOCAL\cc-msg-" + "ab" * 16


# --- helpers: our own child processes (never anything else) -------------------------------

FAMILY = r"""
import subprocess, sys, time
sys.stdin.readline()
grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], creationflags=0x08000000)
print(grandchild.pid, flush=True)
time.sleep(60)
"""


class Family:
    """A child python that starts a grandchild on request; cleaned up by identity (pid + creation time)."""

    def __init__(self) -> None:
        self.child = subprocess.Popen(
            [BASE_PYTHON, "-c", FAMILY], stdin=subprocess.PIPE, stdout=subprocess.PIPE, creationflags=NO_WINDOW
        )
        self.child_created = jobs.process_creation_time(self.child.pid)
        self.grandchild: tuple[int, int | None] | None = None

    def release(self) -> tuple[int, int | None]:
        assert self.child.stdin is not None and self.child.stdout is not None
        self.child.stdin.write(b"go\n")
        self.child.stdin.flush()
        pid = int(self.child.stdout.readline())
        self.grandchild = (pid, jobs.process_creation_time(pid))
        assert self.grandchild[1] is not None
        return self.grandchild

    def cleanup(self) -> None:
        if self.grandchild:
            jobs.terminate_process(*self.grandchild)
        if self.child.poll() is None:
            self.child.kill()
        self.child.wait(10)
        for stream in (self.child.stdin, self.child.stdout):
            if stream:
                stream.close()


@pytest.fixture
def family() -> Any:
    made: list[Family] = []

    def factory() -> Family:
        made.append(Family())
        return made[-1]

    yield factory
    for item in made:
        item.cleanup()


def wait_dead(pid: int, created: int | None, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not jobs.process_alive(pid, created):
            return True
        time.sleep(0.05)
    return False


# --- winsys: presence ---------------------------------------------------------------------


def test_idle_ms_handles_tick_wrap() -> None:
    assert winsys._idle_ms(6000, 1000) == 5000
    assert winsys._idle_ms(100, 0xFFFFFF00) == 356
    assert winsys._idle_ms(5, 5) == 0


@windows_only
def test_idle_seconds_reads_last_input(monkeypatch: pytest.MonkeyPatch) -> None:
    def last_input(ref: Any) -> int:
        assert ref._obj.cbSize == 8
        ref._obj.dwTime = 1000
        return 1

    monkeypatch.setattr(winsys, "_user32", SimpleNamespace(GetLastInputInfo=last_input))
    monkeypatch.setattr(winsys, "_kernel32", SimpleNamespace(GetTickCount=lambda: 6500))
    assert winsys.idle_seconds() == 5.5


@windows_only
def test_idle_seconds_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winsys, "_user32", SimpleNamespace(GetLastInputInfo=lambda ref: 0))
    assert winsys.idle_seconds() is None

    def boom(ref: Any) -> int:
        raise OSError("no session")

    monkeypatch.setattr(winsys, "_user32", SimpleNamespace(GetLastInputInfo=boom))
    assert winsys.idle_seconds() is None


@windows_only
def test_workstation_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[int] = []
    fake = SimpleNamespace(OpenInputDesktop=lambda flags, inherit, access: 0, CloseDesktop=closed.append)
    monkeypatch.setattr(winsys, "_user32", fake)
    assert winsys.workstation_locked() is True
    assert closed == []

    seen: list[tuple[int, bool, int]] = []

    def open_desktop(flags: int, inherit: bool, access: int) -> int:
        seen.append((flags, inherit, access))
        return 1234

    fake.OpenInputDesktop = open_desktop
    assert winsys.workstation_locked() is False
    assert seen == [(0, False, 0x0100)] and closed == [1234]

    def broken(*args: Any) -> int:
        raise OSError("gone")

    fake.OpenInputDesktop = broken
    assert winsys.workstation_locked() is None


def test_presence_rules() -> None:
    def presence(idle: float | None, locked: bool | None) -> bool:
        return winsys.Presence(180, idle=lambda: idle, locked=lambda: locked).at_pc()

    assert presence(10.0, False) is True
    assert presence(179.9, False) is True
    assert presence(180.0, False) is False
    assert presence(10.0, True) is False
    assert presence(10.0, None) is False
    assert presence(None, False) is False


def test_presence_skips_idle_when_locked_and_survives_errors() -> None:
    calls: list[str] = []

    def idle() -> float:
        calls.append("idle")
        return 1.0

    assert winsys.Presence(60, idle=idle, locked=lambda: True).at_pc() is False
    assert calls == []

    def boom() -> float:
        raise RuntimeError("api failure")

    assert winsys.Presence(60, idle=boom, locked=lambda: False).at_pc() is False
    assert winsys.Presence(60, idle=idle, locked=boom).at_pc() is False  # type: ignore[arg-type]


@windows_only
def test_real_presence_probes_return_expected_types() -> None:
    # Read-only probes of this session; values depend on the machine, only the types are checked.
    idle = winsys.idle_seconds()
    assert idle is None or (isinstance(idle, float) and idle >= 0)
    assert winsys.workstation_locked() in (True, False, None)
    assert isinstance(winsys.Presence(60).at_pc(), bool)


# --- winsys: job objects and kill_tree ----------------------------------------------------


@windows_only
def test_job_object_terminates_the_whole_tree(family: Any) -> None:
    item = family()
    job = winsys.JobObject()
    try:
        assert job.assign(item.child.pid) is True
        grandchild = item.release()  # started after the assignment, so it is in the job too
        assert jobs.process_alive(*grandchild)
        assert job.terminate(7) is True
        assert item.child.wait(10) == 7
        assert wait_dead(*grandchild)
    finally:
        job.close()


@windows_only
def test_job_object_close_is_idempotent_and_does_not_kill(family: Any) -> None:
    item = family()
    job = winsys.JobObject()
    assert job.assign(item.child.pid) is True
    job.close()
    job.close()
    assert job.closed
    assert item.child.poll() is None  # no kill-on-close
    assert job.assign(item.child.pid) is False
    assert job.terminate() is False


@windows_only
def test_job_object_kill_on_close_terminates_the_whole_tree(family: Any) -> None:
    item = family()
    job = winsys.JobObject(kill_on_close=True)
    try:
        assert job.assign(item.child.pid) is True
        grandchild = item.release()
        assert jobs.process_alive(*grandchild)
        job.close()
        assert job.closed
        item.child.wait(10)
        assert wait_dead(*grandchild)
    finally:
        job.close()


@windows_only
def test_job_object_handle_is_released_when_dropped(family: Any) -> None:
    item = family()
    job = winsys.JobObject()
    assert job.assign(item.child.pid) is True
    job.__del__()  # what garbage collection does once the bridge drops a finished run
    assert job.closed
    assert item.child.poll() is None  # releasing the handle never kills
    del job
    gc.collect()
    broken = winsys.JobObject.__new__(winsys.JobObject)  # __init__ never ran
    broken.__del__()


@windows_only
def test_job_object_assign_rejects_missing_process() -> None:
    job = winsys.JobObject()
    try:
        assert job.assign(0) is False
        assert job.assign(-5) is False
    finally:
        job.close()


@windows_only
def test_kill_tree_checks_identity_then_kills_descendants(family: Any) -> None:
    item = family()
    grandchild = item.release()
    assert item.child_created is not None
    assert winsys.kill_tree(item.child.pid, item.child_created + 1) is False
    assert winsys.kill_tree(item.child.pid, None) is False
    assert item.child.poll() is None
    assert winsys.kill_tree(item.child.pid, item.child_created) is True
    item.child.wait(10)
    assert wait_dead(*grandchild)


def test_kill_tree_command_is_windowless(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "process_creation_time", lambda pid: 42)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    assert winsys.kill_tree(4321, 42, run=run) is True
    argv, kwargs = calls[0]
    assert Path(argv[0]).name.lower().startswith("taskkill")
    assert argv[1:] == ["/PID", "4321", "/T", "/F"]
    assert kwargs["creationflags"] & NO_WINDOW == NO_WINDOW
    assert kwargs["timeout"] == 30

    assert winsys.kill_tree(4321, 41, run=run) is False  # pid reused by another process
    assert winsys.kill_tree(os.getpid(), 42, run=run) is False
    assert winsys.kill_tree(0, 42, run=run) is False
    assert len(calls) == 1

    assert winsys.kill_tree(4321, 42, run=lambda argv, **kw: SimpleNamespace(returncode=128)) is False

    def missing(argv: list[str], **kwargs: Any) -> Any:
        raise FileNotFoundError("taskkill")

    assert winsys.kill_tree(4321, 42, run=missing) is False


# --- inbox ------------------------------------------------------------------------------


class FakePipe:
    def __init__(self, chunk: int | None = None, fail: bool = False) -> None:
        self.data = b""
        self.closed = False
        self.chunk = chunk
        self.fail = fail

    def write(self, data: Any) -> int:
        if self.fail:
            raise BrokenPipeError(32, "broken pipe")
        data = bytes(data)[: self.chunk] if self.chunk else bytes(data)
        self.data += data
        return len(data)

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, *errors: BaseException, pipe: FakePipe | None = None) -> None:
        self.errors = list(errors)
        self.pipe = pipe or FakePipe()
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def __call__(self, path: str, mode: str, **kwargs: Any) -> FakePipe:
        self.calls.append((path, mode, kwargs))
        if self.errors:
            raise self.errors.pop(0)
        return self.pipe


def busy() -> OSError:
    return OSError(0, "All pipe instances are busy", None, 231)


def expected_lines(token: str, text: str) -> bytes:
    auth = json.dumps({"type": "auth", "token": token}, ensure_ascii=False)
    user = json.dumps({"type": "user", "message": {"role": "user", "content": text}}, ensure_ascii=False)
    return f"{auth}\n{user}\n".encode()


def test_normalize_accepts_pipe_paths() -> None:
    assert inbox.normalize(SOCKET) == SOCKET
    assert inbox.normalize("uds:" + SOCKET) == SOCKET
    assert inbox.normalize("UDS:" + SOCKET + "  ") == SOCKET
    assert inbox.normalize(SOCKET + "\n") == SOCKET  # env values may carry a line end
    upper = r"\\.\PIPE\local\CC-MSG-" + "AB" * 8
    assert inbox.normalize(upper) == upper


@pytest.mark.parametrize(
    "value",
    [
        "",
        r"\\.\pipe\LOCAL\cc-msg-abc",  # too short
        r"\\.\pipe\LOCAL\cc-msg-" + "g" * 32,  # not hex
        r"\\.\pipe\LOCAL\cc-msg-" + "a" * 65,  # too long
        r"\\.\pipe\other-" + "a" * 32,
        r"\\server\pipe\LOCAL\cc-msg-" + "a" * 32,
        SOCKET + "\nx",
        SOCKET + r"\..\x",
        "/tmp/cc-msg-" + "a" * 32,
    ],
)
def test_normalize_rejects_other_paths(value: str) -> None:
    with pytest.raises(inbox.InboxError):
        inbox.normalize(value)


def test_post_writes_exactly_two_lines() -> None:
    opener = FakeOpener()
    text = '继续：修复 "bug" <b>\n第二行 [tg:ab12]'
    inbox.post("uds:" + SOCKET, "tok-123", text, opener=opener)
    assert opener.calls == [(SOCKET, "r+b", {"buffering": 0})]
    assert opener.pipe.data == expected_lines("tok-123", text)
    assert "继续".encode() in opener.pipe.data  # UTF-8, not \u escapes
    assert opener.pipe.data.count(b"\n") == 2  # the text's newline is escaped inside JSON
    assert opener.pipe.closed


def test_post_completes_short_writes() -> None:
    opener = FakeOpener(pipe=FakePipe(chunk=5))
    inbox.post(SOCKET, "tok", "hello world", opener=opener)
    assert opener.pipe.data == expected_lines("tok", "hello world")


def test_post_retries_busy_pipe() -> None:
    opener = FakeOpener(busy(), busy())
    inbox.post(SOCKET, "tok", "hi", opener=opener)
    assert len(opener.calls) == 3
    assert opener.pipe.data == expected_lines("tok", "hi")


def test_post_gives_up_when_pipe_stays_busy() -> None:
    opener = FakeOpener(*[busy() for _ in range(100)])
    started = time.monotonic()
    with pytest.raises(inbox.InboxError, match="一直忙"):
        inbox.post(SOCKET, "tok", "hi", timeout_s=0.3, opener=opener)
    assert 0.25 <= time.monotonic() - started < 3
    assert 2 <= len(opener.calls) < 20


def test_post_missing_pipe_means_closed_session() -> None:
    opener = FakeOpener(FileNotFoundError(2, "not found"))
    with pytest.raises(inbox.InboxError) as caught:
        inbox.post(SOCKET, "tok", "hi", opener=opener)
    assert str(caught.value) == "会话的收件箱不存在（会话可能已经关闭）"
    assert len(opener.calls) == 1


def test_post_other_open_errors() -> None:
    with pytest.raises(inbox.InboxError, match="没有权限"):
        inbox.post(SOCKET, "tok", "hi", opener=FakeOpener(PermissionError(13, "denied")))
    with pytest.raises(inbox.InboxError, match="无法打开"):
        inbox.post(SOCKET, "tok", "hi", opener=FakeOpener(OSError(22, "bad")))


def test_post_write_failure_closes_pipe_and_hides_token() -> None:
    opener = FakeOpener(pipe=FakePipe(fail=True))
    with pytest.raises(inbox.InboxError, match="写入") as caught:
        inbox.post(SOCKET, "secret-token-value", "hi", opener=opener)
    assert opener.pipe.closed
    assert "secret-token-value" not in str(caught.value)


@pytest.mark.parametrize(
    ("token", "text", "match"),
    [
        ("tok", "", "为空"),
        ("tok", "  \n ", "为空"),
        ("tok", "x" * (inbox.MAX_TEXT + 1), "太长"),
        ("", "hi", "令牌"),
        ("  ", "hi", "令牌"),
    ],
    ids=["empty", "blank", "too-long", "no-token", "blank-token"],
)
def test_post_rejects_bad_input_before_opening(token: str, text: str, match: str) -> None:
    opener = FakeOpener()
    with pytest.raises(inbox.InboxError, match=match):
        inbox.post(SOCKET, token, text, opener=opener)
    assert opener.calls == []


def test_post_accepts_max_length_and_bad_socket_is_rejected_first() -> None:
    opener = FakeOpener()
    inbox.post(SOCKET, "tok", "x" * inbox.MAX_TEXT, opener=opener)
    assert len(opener.calls) == 1
    with pytest.raises(inbox.InboxError, match="地址无效"):
        inbox.post(r"C:\temp\file.txt", "tok", "hi", opener=opener)
    assert len(opener.calls) == 1


def test_post_replaces_lone_surrogates() -> None:
    opener = FakeOpener()
    inbox.post(SOCKET, "tok", "a\ud83db", opener=opener)
    assert json.loads(opener.pipe.data.splitlines()[1])["message"]["content"] == "a?b"


class PipeServer:
    """A one-instance named-pipe server on a random cc-msg name (never a real session's pipe).

    Serves `sessions` clients one after another, recording what each wrote and the
    impersonation level the client granted.
    """

    def __init__(self, sessions: int = 1) -> None:
        self.win32pipe = pytest.importorskip("win32pipe")
        self.win32file = pytest.importorskip("win32file")
        self.win32api = pytest.importorskip("win32api")
        self.win32security = pytest.importorskip("win32security")
        self.pywintypes = pytest.importorskip("pywintypes")
        self.name = r"\\.\pipe\LOCAL\cc-msg-" + secrets.token_hex(16)
        self.handle = self.win32pipe.CreateNamedPipe(
            self.name,
            self.win32pipe.PIPE_ACCESS_DUPLEX,
            self.win32pipe.PIPE_TYPE_BYTE | self.win32pipe.PIPE_READMODE_BYTE | self.win32pipe.PIPE_WAIT,
            1,
            65536,
            65536,
            0,
            None,
        )
        self.received: list[bytes] = []
        self.levels: list[int | None] = []
        self.thread = threading.Thread(target=self._serve, args=(sessions,), daemon=True)
        self.thread.start()

    def _serve(self, sessions: int) -> None:
        try:
            for _ in range(sessions):
                self._serve_one()
        except self.pywintypes.error:
            pass  # the test closed the pipe early (it failed); its assertions report that

    def _serve_one(self) -> None:
        try:
            self.win32pipe.ConnectNamedPipe(self.handle, None)
        except self.pywintypes.error as exc:
            if exc.winerror != 535:  # ERROR_PIPE_CONNECTED: the client was faster
                raise
        chunks: list[bytes] = []
        level: int | None = None
        while True:
            try:
                _, data = self.win32file.ReadFile(self.handle, 65536)
            except self.pywintypes.error:
                break  # the client closed the pipe
            chunks.append(data)
            if level is None:
                level = self._client_level()
        self.received.append(b"".join(chunks))
        self.levels.append(level)
        self.win32pipe.DisconnectNamedPipe(self.handle)

    def _client_level(self) -> int | None:
        try:
            self.win32security.ImpersonateNamedPipeClient(self.handle)
        except self.pywintypes.error:
            return None
        try:
            token = self.win32security.OpenThreadToken(
                self.win32api.GetCurrentThread(), self.win32security.TOKEN_QUERY, True
            )
            return self.win32security.GetTokenInformation(token, self.win32security.TokenImpersonationLevel)
        except self.pywintypes.error:
            return None
        finally:
            self.win32security.RevertToSelf()

    def close(self) -> None:
        self.win32file.CloseHandle(self.handle)


@windows_only
def test_post_through_a_real_named_pipe() -> None:
    server = PipeServer()
    try:
        inbox.post(server.name, "tok", "你好 [tg:1a2b]")
        server.thread.join(10)
        assert server.received == [expected_lines("tok", "你好 [tg:1a2b]")]
        # The session process may identify the bridge's user but never act as it.
        assert server.levels == [server.win32security.SecurityIdentification]
    finally:
        server.close()
    with pytest.raises(inbox.InboxError, match="不存在"):
        inbox.post(server.name, "tok", "again", timeout_s=0.2)


def test_payload_carries_the_priority_only_when_it_is_not_the_default() -> None:
    import json

    assert inbox._payload("tok", "hi") == expected_lines("tok", "hi")
    for priority in ("later", "now"):
        auth, frame, _ = inbox._payload("tok", "hi", priority).split(b"\n")
        assert json.loads(auth) == {"type": "auth", "token": "tok"}
        assert json.loads(frame) == {"type": "user", "message": {"role": "user", "content": "hi"}, "priority": priority}
    with pytest.raises(inbox.InboxError, match="投递方式"):
        inbox.post(r"\\.\pipe\LOCAL\cc-msg-" + "0" * 32, "tok", "hi", priority="soon")


@windows_only
def test_post_retries_a_really_busy_pipe() -> None:
    # The builtin open() reports ERROR_PIPE_BUSY as a bare EINVAL; the default opener must keep winerror 231.
    server = PipeServer(sessions=2)
    blocker = inbox._open_pipe(server.name)  # takes the only instance
    try:
        with pytest.raises(inbox.InboxError, match="一直忙"):
            inbox.post(server.name, "tok", "busy", timeout_s=0.3)
        timer = threading.Timer(0.4, blocker.close)
        timer.start()
        started = time.monotonic()
        inbox.post(server.name, "tok", "排队后送达", timeout_s=5)
        assert time.monotonic() - started >= 0.3
        timer.join(5)
        server.thread.join(10)
        assert server.received == [b"", expected_lines("tok", "排队后送达")]
    finally:
        blocker.close()
        server.close()


@windows_only
def test_default_opener_keeps_windows_error_codes() -> None:
    missing = r"\\.\pipe\LOCAL\cc-msg-" + secrets.token_hex(16)
    with pytest.raises(FileNotFoundError) as caught:
        inbox._open_pipe(missing)
    assert caught.value.winerror == 2


# --- headless: finding claude.exe ---------------------------------------------------------


def touch(path: Path, data: bytes = b"MZ") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def desktop_exe(localappdata: Path, version: str) -> Path:
    root = localappdata / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude" / "claude-code"
    return touch(root / version / "claude.exe")


def npm_install(prefix: Path, version: str, shim: str = "claude.cmd") -> Path:
    touch(prefix / shim, b"@ECHO off\n")
    package = prefix / "node_modules" / "@anthropic-ai" / "claude-code"
    touch(package / "package.json", json.dumps({"name": "@anthropic-ai/claude-code", "version": version}).encode())
    return touch(package / "bin" / "claude.exe")


@pytest.fixture
def fake_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    tree = SimpleNamespace(
        localappdata=tmp_path / "Local", appdata=tmp_path / "Roaming", npm=tmp_path / "nodejs", other=tmp_path / "bin"
    )
    for folder in (tree.localappdata, tree.appdata, tree.npm, tree.other):
        folder.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(tree.localappdata))
    monkeypatch.setenv("APPDATA", str(tree.appdata))
    monkeypatch.setenv("PATH", os.pathsep.join([str(tree.other), f'"{tree.npm}"', ""]))
    return tree


def test_find_claude_exe_nothing_installed(fake_tree: SimpleNamespace) -> None:
    assert headless.find_claude_exe() is None
    touch(fake_tree.npm / "claude.cmd")  # a shim without the package is not enough
    assert headless.find_claude_exe() is None


def test_find_claude_exe_prefers_highest_version(fake_tree: SimpleNamespace) -> None:
    desktop_exe(fake_tree.localappdata, "2.1.281")
    newest_desktop = desktop_exe(fake_tree.localappdata, "2.1.284")
    touch(fake_tree.localappdata / "Packages" / "Claude_x" / "LocalCache" / "Roaming" / "Claude" / "claude-code"
          / "2.1.999" / "readme.txt")  # fmt: skip
    assert headless.find_claude_exe() == newest_desktop

    roaming = touch(fake_tree.appdata / "Claude" / "claude-code" / "2.1.290" / "claude.exe")
    assert headless.find_claude_exe() == roaming

    npm = npm_install(fake_tree.npm, "2.1.300")
    assert headless.find_claude_exe() == npm
    assert headless.find_claude_exe().suffix == ".exe"


def test_find_claude_exe_tie_prefers_desktop_msix(fake_tree: SimpleNamespace) -> None:
    msix = desktop_exe(fake_tree.localappdata, "2.1.284")
    touch(fake_tree.appdata / "Claude" / "claude-code" / "2.1.284" / "claude.exe")
    npm_install(fake_tree.npm, "2.1.284")
    assert headless.find_claude_exe() == msix


def test_find_claude_exe_version_parsing(fake_tree: SimpleNamespace) -> None:
    desktop_exe(fake_tree.localappdata, "2.1.9")
    ten = desktop_exe(fake_tree.localappdata, "2.1.10")  # numeric, not lexical order
    desktop_exe(fake_tree.localappdata, "latest")
    desktop_exe(fake_tree.localappdata, "2.1.500.tmp")
    assert headless.find_claude_exe() == ten


def test_find_claude_exe_npm_with_sh_shim_and_bad_package_json(fake_tree: SimpleNamespace) -> None:
    npm = npm_install(fake_tree.npm, "2.1.300", shim="claude")
    assert headless.find_claude_exe() == npm
    (npm.parents[1] / "package.json").write_text("{not json", encoding="utf-8")
    assert headless.find_claude_exe() == npm  # unknown version still usable
    desktop = desktop_exe(fake_tree.localappdata, "1.0.0")
    assert headless.find_claude_exe() == desktop  # a known version beats an unknown one


def test_find_claude_exe_bare_exe_on_path_is_last_resort(fake_tree: SimpleNamespace) -> None:
    bare = touch(fake_tree.other / "claude.exe")
    assert headless.find_claude_exe() == bare
    desktop = desktop_exe(fake_tree.localappdata, "2.0.0")
    assert headless.find_claude_exe() == desktop


def test_find_claude_exe_ignores_aliases_and_relative_path_entries(
    fake_tree: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(fake_tree.other / "claude.exe", b"")  # a 0-byte App Execution Alias may open a GUI app
    assert headless.find_claude_exe() is None
    npm_install(tmp_path / "rel", "9.9.9")
    touch(tmp_path / "rel" / "claude.exe")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["rel", r".\rel", str(fake_tree.other)]))
    assert headless.find_claude_exe() is None  # relative entries would depend on the bridge's cwd


def test_find_claude_exe_configured_wins(fake_tree: SimpleNamespace, tmp_path: Path) -> None:
    desktop = desktop_exe(fake_tree.localappdata, "9.9.9")
    pinned = touch(tmp_path / "pinned" / "claude.exe")
    assert headless.find_claude_exe(str(pinned)) == pinned
    assert headless.find_claude_exe(f'  "{pinned}" ') == pinned
    shim = touch(tmp_path / "pinned" / "claude.cmd")
    assert headless.find_claude_exe(str(shim)) == desktop  # never a .cmd
    assert headless.find_claude_exe(str(tmp_path / "missing" / "claude.exe")) == desktop
    assert headless.find_claude_exe(str(tmp_path / "pinned")) == desktop  # a directory


# --- headless: environment and auth -------------------------------------------------------


def test_scrubbed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    scrubbed = {
        "CLAUDECODE": "1",
        "CLAUDE_PID": "123",
        "CLAUDE_EFFORT": "high",
        "CLAUDE_CODE_ENTRYPOINT": "claude-desktop",
        "CLAUDE_CODE_MESSAGING_SOCKET": SOCKET,
        "CLAUDE_CODE_MESSAGING_TOKEN": "secret",
        "CLAUDE_AGENT_SDK_VERSION": "1",
        "AGENT_TELEGRAM_NO_WAIT": "1",
        "TGBRIDGE_LOG_DIR": "x",
    }
    kept = {"CLAUDE_CONFIG_DIR": r"C:\cfg", "ANTHROPIC_BASE_URL": "http://x", "AGENT_TG_VAR_DIR": "v"}
    for key, value in {**scrubbed, **kept}.items():
        monkeypatch.setenv(key, value)
    env = headless.scrubbed_env({"EXTRA": 5, "AGENT_TELEGRAM_HEADLESS": "1"})
    for key in scrubbed:
        assert key not in env
    for key, value in kept.items():
        assert env[key] == value
    assert env["AGENT_TELEGRAM_HEADLESS"] == "1"
    assert env["EXTRA"] == "5"
    assert env["PATH"] == os.environ["PATH"]
    assert headless.scrubbed_env({"FOO": "bar"})["FOO"] == "bar"
    assert "EXTRA" not in headless.scrubbed_env()


def test_auth_status_parses_json(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    output = {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(output).encode(), stderr=b"")

    exe = tmp_path / "claude.exe"
    assert headless.auth_status(exe, run=run) == output
    argv, kwargs = calls[0]
    assert argv == [str(exe), "auth", "status"]
    assert kwargs["creationflags"] & NO_WINDOW == NO_WINDOW
    assert kwargs["timeout"] == 30
    assert kwargs["env"]["AGENT_TELEGRAM_HEADLESS"] == "1"


def test_auth_status_failures(tmp_path: Path) -> None:
    exe = tmp_path / "claude.exe"

    def result(code: int, stdout: bytes, stderr: bytes = b"") -> Any:
        return lambda argv, **kw: SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)

    logged_out = headless.auth_status(exe, run=result(1, b'{"loggedIn": false}\n'))
    assert logged_out == {"loggedIn": False}
    noisy = headless.auth_status(exe, run=result(0, b'warning: x\n{"loggedIn": true}\n'))
    assert noisy["loggedIn"] is True
    garbage = headless.auth_status(exe, run=result(2, b"Usage: claude", b"\nerror: unknown command\n"))
    assert garbage["loggedIn"] is False and "退出码 2" in garbage["error"] and "unknown command" in garbage["error"]

    def timeout(argv: list[str], **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(argv, 30)

    assert headless.auth_status(exe, run=timeout) == {"loggedIn": False, "error": "claude auth status 超时"}

    def missing(argv: list[str], **kwargs: Any) -> Any:
        raise FileNotFoundError(2, "missing")

    failed = headless.auth_status(exe, run=missing)
    assert failed["loggedIn"] is False and "FileNotFoundError" in failed["error"]


# --- headless: runs -----------------------------------------------------------------------


class FakeStdin:
    def __init__(self, fail: bool = False, block: threading.Event | None = None) -> None:
        self.data = b""
        self.closed = False
        self.fail = fail
        self.block = block  # a child that never reads: write() waits until the event is set

    def write(self, data: bytes) -> int:
        if self.block is not None:
            self.block.wait(30)
            raise BrokenPipeError(32, "broken pipe")
        if self.fail:
            raise BrokenPipeError(32, "broken pipe")
        self.data += data
        return len(data)

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, pid: int, stdin_fails: bool = False, block: threading.Event | None = None) -> None:
        self.pid = pid
        self.stdin = FakeStdin(stdin_fails, block)
        self.returncode: int | None = None
        self.killed = False

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True


class FakePopen:
    def __init__(
        self, *, fail_first: bool = False, stdin_fails: bool = False, block: threading.Event | None = None
    ) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.fail_first = fail_first
        self.stdin_fails = stdin_fails
        self.block = block
        self.process: FakeProcess | None = None

    def __call__(self, argv: list[str], **kwargs: Any) -> FakeProcess:
        self.calls.append((argv, kwargs))
        if self.fail_first and len(self.calls) == 1:
            raise OSError(5, "breakaway not allowed")
        kwargs["stdout"].write(b"child output\n")
        self.process = FakeProcess(4242, self.stdin_fails, self.block)
        return self.process


class FakeJob:
    made: list[FakeJob] = []

    def __init__(self, assign_ok: bool = True, terminate_ok: bool = True) -> None:
        self.assigned: list[int] = []
        self.terminated: list[int] = []
        self.closed = False
        self.assign_ok = assign_ok
        self.terminate_ok = terminate_ok
        FakeJob.made.append(self)

    def assign(self, pid: int) -> bool:
        self.assigned.append(pid)
        return self.assign_ok

    def terminate(self, code: int = 1) -> bool:
        self.terminated.append(code)
        return self.terminate_ok

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def run_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    FakeJob.made = []
    kills: list[tuple[int, int | None]] = []

    def fake_kill_tree(pid: int, created: int | None) -> bool:
        kills.append((pid, created))  # the fake pid 4242 must never reach the real taskkill
        return True

    monkeypatch.setattr(headless, "JobObject", FakeJob)
    monkeypatch.setattr(headless, "kill_tree", fake_kill_tree)
    monkeypatch.setattr(jobs, "process_creation_time", lambda pid: 777)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "parent-token")
    cwd = tmp_path / "proj"
    cwd.mkdir()
    return SimpleNamespace(
        exe=touch(tmp_path / "cc" / "claude.exe"), cwd=cwd, log=tmp_path / "logs" / "run.log", kills=kills
    )


def start(setup: SimpleNamespace, popen: Any, **overrides: Any) -> headless.HeadlessRun:
    kwargs: dict[str, Any] = {
        "exe": setup.exe,
        "cwd": setup.cwd,
        "prompt": "修一下测试 [tg:ab12]",
        "session_id": SESSION,
        "resume": False,
        "permission_mode": "acceptEdits",
        "log_path": setup.log,
        "popen": popen,
    }
    kwargs.update(overrides)
    return headless.start_run(**kwargs)


def test_start_run_new_session(run_setup: SimpleNamespace) -> None:
    popen = FakePopen()
    before = time.time()
    run = start(run_setup, popen)
    argv, kwargs = popen.calls[0]
    assert argv == [
        str(run_setup.exe), "-p", "--output-format", "stream-json", "--verbose",
        "--permission-mode", "acceptEdits", "--session-id", SESSION,
    ]  # fmt: skip
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    assert kwargs["creationflags"] == NO_WINDOW | group | breakaway
    assert kwargs["cwd"] == str(run_setup.cwd)
    assert kwargs["stdin"] == subprocess.PIPE and kwargs["stderr"] == subprocess.STDOUT
    env = kwargs["env"]
    assert env["AGENT_TELEGRAM_HEADLESS"] == "1"
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_MESSAGING_TOKEN" not in env
    assert "修一下测试" not in " ".join(argv)  # the prompt never goes into argv
    process = popen.process
    assert process is not None
    assert process.stdin.data == "修一下测试 [tg:ab12]".encode() and process.stdin.closed
    assert run_setup.log.read_bytes() == b"child output\n"
    fields = (run.session_id, run.pid, run.created, run.cwd, run.resume)
    assert fields == (SESSION, 4242, 777, str(run_setup.cwd), False)
    assert run.log_path == run_setup.log and run.log_offset == 0
    assert before <= run.started_at <= time.time()
    assert run.process is process
    assert run.job is FakeJob.made[0] and FakeJob.made[0].assigned == [4242]


def test_start_run_resume_appends_to_log(run_setup: SimpleNamespace) -> None:
    touch(run_setup.log, b"earlier run\n")
    popen = FakePopen()
    run = start(run_setup, popen, resume=True, permission_mode="default")
    argv = popen.calls[0][0]
    assert argv[-2:] == ["--resume", SESSION] and "--session-id" not in argv
    assert argv[argv.index("--permission-mode") + 1] == "default"
    assert run.resume is True and run.log_offset == len(b"earlier run\n")
    assert run_setup.log.read_bytes() == b"earlier run\nchild output\n"


def test_start_run_retries_without_breakaway(run_setup: SimpleNamespace) -> None:
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    if not breakaway:
        pytest.skip("no breakaway flag on this platform")
    popen = FakePopen(fail_first=True)
    run = start(run_setup, popen)
    assert len(popen.calls) == 2
    assert popen.calls[0][1]["creationflags"] & breakaway
    assert not popen.calls[1][1]["creationflags"] & breakaway
    assert popen.calls[1][1]["creationflags"] & NO_WINDOW
    assert run.pid == 4242


def test_start_run_without_job(run_setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    run = start(run_setup, FakePopen(), permission_mode="plan")
    assert run.job is not None
    monkeypatch.setattr(headless, "JobObject", lambda: FakeJob(assign_ok=False))
    run = start(run_setup, FakePopen())
    assert run.job is None and FakeJob.made[-1].closed

    def no_job() -> FakeJob:
        raise OSError("no job objects")

    monkeypatch.setattr(headless, "JobObject", no_job)
    assert start(run_setup, FakePopen()).job is None


def test_start_run_prompt_write_failure(run_setup: SimpleNamespace) -> None:
    popen = FakePopen(stdin_fails=True)
    with pytest.raises(OSError, match="读取提示词"):
        start(run_setup, popen)
    assert popen.process is not None and popen.process.killed
    job = FakeJob.made[0]
    assert job.terminated == [1] and job.closed
    assert run_setup.kills == []  # the job already ended the tree


def test_start_run_prompt_write_failure_without_job_kills_tree(
    run_setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(headless, "JobObject", lambda: FakeJob(assign_ok=False))
    popen = FakePopen(stdin_fails=True)
    with pytest.raises(OSError, match="读取提示词"):
        start(run_setup, popen)
    assert popen.process is not None and popen.process.killed
    assert run_setup.kills == [(4242, 777)]  # whatever the child already spawned goes too

    monkeypatch.setattr(headless, "JobObject", lambda: FakeJob(terminate_ok=False))
    with pytest.raises(OSError, match="读取提示词"):
        start(run_setup, FakePopen(stdin_fails=True))
    assert run_setup.kills == [(4242, 777), (4242, 777)]  # the job could not end it
    assert FakeJob.made[-1].terminated == [1] and FakeJob.made[-1].closed


def test_start_run_child_that_never_reads_cannot_hang_the_bridge(
    run_setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(headless, "_STDIN_TIMEOUT_S", 0.3)
    release = threading.Event()
    popen = FakePopen(block=release)
    started = time.monotonic()
    try:
        with pytest.raises(OSError, match="没有读取提示词"):
            start(run_setup, popen)
        assert 0.25 <= time.monotonic() - started < 5
        assert popen.process is not None and popen.process.killed
        job = FakeJob.made[0]
        assert job.terminated == [1] and job.closed
    finally:
        release.set()  # lets the abandoned writer thread finish


@pytest.mark.parametrize(
    ("override", "match"),
    [
        ({"session_id": "not-a-uuid"}, "UUID"),
        ({"session_id": SESSION + "\n"}, "UUID"),
        ({"session_id": "--help"}, "UUID"),
        ({"permission_mode": "yolo"}, "权限模式"),
        ({"prompt": "   "}, "提示词"),
        ({"cwd": Path("Z:/definitely/missing/dir")}, "工作目录"),
    ],
)
def test_start_run_validation(run_setup: SimpleNamespace, override: dict[str, Any], match: str) -> None:
    popen = FakePopen()
    with pytest.raises(ValueError, match=match):
        start(run_setup, popen, **override)
    assert popen.calls == []


def test_start_run_rejects_missing_exe_and_shims(run_setup: SimpleNamespace, tmp_path: Path) -> None:
    popen = FakePopen()
    with pytest.raises(ValueError, match="claude.exe"):
        start(run_setup, popen, exe=tmp_path / "nope" / "claude.exe")
    with pytest.raises(ValueError, match="claude.exe"):
        start(run_setup, popen, exe=touch(tmp_path / "npm" / "claude.cmd"))
    with pytest.raises(ValueError, match="claude.exe"):
        start(run_setup, popen, cwd=run_setup.cwd, exe=run_setup.cwd)
    assert popen.calls == []
    assert headless.PERMISSION_MODES == ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")
    for mode in headless.PERMISSION_MODES:
        start(run_setup, popen, permission_mode=mode)


def test_run_roundtrip_dict(tmp_path: Path) -> None:
    run = headless.HeadlessRun(SESSION, 11, 22, tmp_path / "a.log", 1.5, r"C:\projects", True, job=FakeJob(), log_offset=9)
    data = run.to_dict()
    assert json.loads(json.dumps(data)) == data
    assert data == {
        "session_id": SESSION, "pid": 11, "created": 22, "log_path": str(tmp_path / "a.log"),
        "started_at": 1.5, "cwd": r"C:\projects", "resume": True, "log_offset": 9,
    }  # fmt: skip
    restored = headless.HeadlessRun.from_dict(data)
    assert restored == run
    assert restored.job is None and restored.process is None
    legacy = {key: value for key, value in data.items() if key != "log_offset"} | {"created": None}
    assert headless.HeadlessRun.from_dict(legacy).log_offset == 0
    assert headless.HeadlessRun.from_dict(legacy).created is None


def test_run_alive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[tuple[int, int | None]] = []

    def alive(pid: int, created: int | None) -> bool:
        seen.append((pid, created))
        return True

    monkeypatch.setattr(jobs, "process_alive", alive)
    run = headless.HeadlessRun(SESSION, 11, 22, tmp_path / "a.log", 0.0, "", False)
    assert headless.run_alive(run) is True and seen == [(11, 22)]
    process = FakeProcess(11)
    run.process = process  # type: ignore[assignment]
    assert headless.run_alive(run) is True
    process.returncode = 0
    assert headless.run_alive(run) is False
    assert len(seen) == 2


def test_stop_run_prefers_job(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    kills: list[tuple[int, int | None]] = []

    def fake_kill(pid: int, created: int | None) -> bool:
        kills.append((pid, created))
        return True

    monkeypatch.setattr(headless, "kill_tree", fake_kill)
    run = headless.HeadlessRun(SESSION, 11, 22, tmp_path / "a.log", 0.0, "", False, job=FakeJob())  # type: ignore[arg-type]
    assert headless.stop_run(run) is True
    assert run.job.terminated == [1] and kills == []  # type: ignore[union-attr]

    run.job = FakeJob(terminate_ok=False)  # type: ignore[assignment]
    assert headless.stop_run(run) is True and kills == [(11, 22)]

    run.job = None
    assert headless.stop_run(run) is True and kills == [(11, 22), (11, 22)]
    monkeypatch.setattr(headless, "kill_tree", lambda pid, created: False)
    assert headless.stop_run(run) is False


# --- headless: stream-json results --------------------------------------------------------


def record(kind: str, **fields: Any) -> bytes:
    return json.dumps({"type": kind, **fields}, ensure_ascii=False).encode() + b"\n"


def result_record(text: str, *, is_error: bool = False, subtype: str = "success") -> bytes:
    return record(
        "result", subtype=subtype, is_error=is_error, result=text, session_id=SESSION, total_cost_usd=0.12,
        duration_ms=1000, num_turns=2,
    )  # fmt: skip


def test_parse_result_last_result_line(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    assert headless.parse_result(log) is None
    log.write_bytes(
        b"stderr: warming up\n"
        + record("system", subtype="init", session_id=SESSION)
        + record("assistant", message={"content": [{"type": "text", "text": '"result"'}]})
        + result_record("第一轮")
        + b"\r\n\n"
        + record("assistant", message={"content": []})
        + result_record("最终回复", is_error=True, subtype="error_during_execution")
        + b'not json {"result"\n'
        + b'{"type": "result", "truncat'
    )
    assert headless.parse_result(log) == {
        "is_error": True,
        "result": "最终回复",
        "session_id": SESSION,
        "subtype": "error_during_execution",
        "total_cost_usd": 0.12,
    }


def test_parse_result_without_result(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    log.write_bytes(
        record("system", subtype="init")
        + b"\xff\xfe broken bytes\n"
        + b'{"type": "result", "result": "\xff"}\n'  # invalid UTF-8
        + b'{"type": "results", "result": 1}\n'
        + b"[1, 2]\n"
    )
    assert headless.parse_result(log) is None
    log.write_bytes(b"")
    assert headless.parse_result(log) is None


def test_parse_result_respects_offset(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    earlier = result_record("上一次")
    log.write_bytes(earlier + record("system", subtype="init"))
    assert headless.parse_result(log)["result"] == "上一次"  # type: ignore[index]
    assert headless.parse_result(log, offset=len(earlier)) is None
    with log.open("ab") as handle:
        handle.write(result_record("这一次"))
    assert headless.parse_result(log, offset=len(earlier))["result"] == "这一次"  # type: ignore[index]
    assert headless.parse_result(log, offset=10**9)["result"] == "这一次"  # type: ignore[index]  # log replaced
    assert headless.parse_result(log, offset=-5)["result"] == "这一次"  # type: ignore[index]  # clamped, no seek error


def test_parse_result_tail_limit(tmp_path: Path) -> None:
    log = tmp_path / "run.log"
    first = result_record("开头")
    second = result_record("结尾")
    log.write_bytes(first + second)
    # The tail starts inside `first`: that partial line is dropped, `second` is found.
    assert headless.parse_result(log, max_bytes=len(second) + 5)["result"] == "结尾"  # type: ignore[index]
    # The tail starts exactly at a line start: the whole line is kept.
    assert headless.parse_result(log, max_bytes=len(second))["result"] == "结尾"  # type: ignore[index]
    log.write_bytes(first + record("system", subtype="x" * 50))
    assert headless.parse_result(log, max_bytes=20) is None


# --- headless: a real detached run (python standing in for claude.exe) ---------------------

FAKE_CLAUDE = r"""
import json, os, sys, time
prompt = sys.stdin.buffer.read().decode("utf-8")
print(json.dumps({"type": "system", "subtype": "init", "argv": sys.argv[1:]}), flush=True)
print("some stderr noise", file=sys.stderr, flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": prompt,
                  "session_id": sys.argv[-1], "total_cost_usd": 0.0,
                  "headless": os.environ.get("AGENT_TELEGRAM_HEADLESS"),
                  "parent": os.environ.get("CLAUDECODE")}), flush=True)
time.sleep(60)
"""


@windows_only
def test_real_run_uses_job_and_stops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    exe = touch(tmp_path / "cc" / "claude.exe")
    cwd = tmp_path / "proj"
    cwd.mkdir()
    log = tmp_path / "logs" / "run.log"
    launched: list[list[str]] = []

    def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen:
        launched.append(argv)
        return subprocess.Popen([BASE_PYTHON, "-c", FAKE_CLAUDE, *argv[1:]], **kwargs)

    run = headless.start_run(
        exe=exe, cwd=cwd, prompt="你好，Claude", session_id=SESSION, resume=False,
        permission_mode="acceptEdits", log_path=log, popen=popen,
    )  # fmt: skip
    try:
        assert run.job is not None and run.created is not None
        assert headless.run_alive(run)
        deadline = time.monotonic() + 20
        result = None
        while result is None and time.monotonic() < deadline:
            result = headless.parse_result(log, offset=run.log_offset)
            time.sleep(0.1)
        assert result is not None
        assert result == {
            "is_error": False, "result": "你好，Claude", "session_id": SESSION, "subtype": "success",
            "total_cost_usd": 0.0,
        }  # fmt: skip
        lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.startswith("{")]
        assert lines[-1]["headless"] == "1" and lines[-1]["parent"] is None
        assert "some stderr noise" in log.read_text(encoding="utf-8")
        assert headless.stop_run(run) is True
        assert run.process is not None and run.process.wait(10) == 1
        assert not headless.run_alive(run)
    finally:
        if run.process is not None and run.process.poll() is None:
            run.process.kill()
            run.process.wait(10)
        if run.job is not None:
            run.job.close()
    assert launched[0][0] == str(exe)

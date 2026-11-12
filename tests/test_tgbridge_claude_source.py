from __future__ import annotations

import json
import os
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from conftest import thinking_signature

from agent_telegram.tgbridge import claude_source as cs
from agent_telegram.tgbridge.model import CLAUDE, AttentionEvent, ProgressEvent, TurnEvent

SID = "11111111-2222-3333-4444-555555555555"
SID2 = "66666666-7777-8888-9999-000000000000"
SID3 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CWD = r"C:\proj\demo-app"
SLUG = "C--proj-demo-app"
PIPE = r"\\.\pipe\LOCAL\cc-msg-0123456789abcdef0123456789abcdef"
NOW = 1_790_000_000.0


# --- fakes ------------------------------------------------------------------------------


@dataclass
class FakeConfig:
    claude_enabled: bool = True
    claude_include_scheduled: bool = False
    waiting_alert_s: float = 600.0
    active_window_h: float = 24.0
    content_exclude: list[str] = field(default_factory=list)

    def excluded(self, cwd: str) -> bool:
        norm = os.path.normcase(os.path.normpath(cwd))
        for prefix in self.content_exclude:
            base = os.path.normcase(os.path.normpath(prefix))
            if norm == base or norm.startswith(base.rstrip("\\/") + os.sep):
                return True
        return False


class FakeSecrets:
    def __init__(self) -> None:
        self.data: dict[str, dict] = {}
        self.puts = 0

    def get(self, key: str) -> dict | None:
        value = self.data.get(key)
        return dict(value) if value is not None else None

    def put(self, key: str, value: dict) -> None:
        self.puts += 1
        self.data[key] = dict(value)

    def drop(self, key: str) -> None:
        self.data.pop(key, None)

    def keys(self) -> list[str]:
        return list(self.data)


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class InboxError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def inbox_stub(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Never touch the real inbox module (or a real pipe) from these tests."""

    module = types.ModuleType("agent_telegram.tgbridge.inbox")
    module.InboxError = InboxError  # type: ignore[attr-defined]

    def post(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the real inbox.post must not run in tests")

    module.post = post  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent_telegram.tgbridge.inbox", module)
    return module


@pytest.fixture(autouse=True)
def no_real_appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "no-localappdata"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "no-appdata"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-claude-home"))


# --- builders ---------------------------------------------------------------------------


def iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


_counter = [0]


def _uuid() -> str:
    _counter[0] += 1
    return f"u-{_counter[0]:06d}"


def prompt(text: str, at: float, *, origin: str = "human", meta: bool = False, blocks: bool = False, **extra: Any) -> dict:
    content: Any = [{"type": "text", "text": text}] if blocks else text
    record = {"type": "user", "uuid": _uuid(), "timestamp": iso(at), "sessionId": SID, "cwd": CWD,
              "isSidechain": False, "message": {"role": "user", "content": content}, "origin": {"kind": origin}}
    if meta:
        record["isMeta"] = True
    record.update(extra)
    return record


def tool_result(at: float, *, side: bool = False) -> dict:
    return {"type": "user", "uuid": _uuid(), "timestamp": iso(at), "cwd": CWD, "isSidechain": side,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}}


def assistant(message_id: str, blocks: list[dict], stop: str | None, at: float, *, side: bool = False,
              uuid: str | None = None, **extra: Any) -> dict:
    record = {"type": "assistant", "uuid": uuid or _uuid(), "timestamp": iso(at), "cwd": CWD, "isSidechain": side,
              "message": {"id": message_id, "role": "assistant", "model": "claude", "content": blocks,
                          "stop_reason": stop}}
    record.update(extra)
    return record


def text(value: str) -> dict:
    return {"type": "text", "text": value}


THINK = {"type": "thinking", "thinking": "..."}
TOOL = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}


def api_error(at: float, message: str = "Connection error.", formatted: str = "Connection dropped (ECONNRESET)") -> dict:
    return {"type": "system", "subtype": "api_error", "uuid": _uuid(), "timestamp": iso(at), "cwd": CWD,
            "level": "error", "error": {"message": message, "formatted": formatted}, "retryAttempt": 1, "maxRetries": 10}


def peer_queued(value: str, at: float) -> dict:
    return {"type": "attachment", "uuid": _uuid(), "timestamp": iso(at), "cwd": CWD, "isSidechain": False,
            "attachment": {"type": "queued_command", "prompt": value, "commandMode": "prompt",
                           "origin": {"kind": "peer", "selfSent": True}}}


def enqueue(value: str, at: float) -> dict:
    return {"type": "queue-operation", "operation": "enqueue", "timestamp": iso(at), "sessionId": SID, "content": value}


def title_record(value: str) -> dict:
    return {"type": "custom-title", "customTitle": value, "sessionId": SID}


def lines(records: list[dict]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)


def write_transcript(home: Path, records: list[dict], *, session_id: str = SID, slug: str = SLUG,
                     tail: str = "") -> Path:
    path = home / "projects" / slug / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(lines(records) + tail, encoding="utf-8")
    return path


def append(path: Path, records: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        stream.write(lines(records))


def write_registry(home: Path, pid: int, session_id: str = SID, *, status: str = "idle", waiting_for: str | None = None,
                   kind: str = "interactive", host: str = "", name: str = "", updated_ms: int | None = None,
                   status_ms: int | None = None, socket: str = PIPE, proc_start: Any = "134000000000000000",
                   cwd: str = CWD, entrypoint: str = "claude-desktop") -> Path:
    folder = home / "sessions"
    folder.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "pid": pid, "sessionId": session_id, "cwd": cwd, "startedAt": int((NOW - 3600) * 1000),
        "procStart": proc_start, "version": "2.1.284", "kind": kind, "entrypoint": entrypoint,
        "hostSessionId": host, "name": name, "status": status,
        "statusUpdatedAt": status_ms if status_ms is not None else int((NOW - 30) * 1000),
        "updatedAt": updated_ms if updated_ms is not None else int((NOW - 10) * 1000),
        "messagingSocketPath": socket,
    }
    if waiting_for is not None:
        data["waitingFor"] = waiting_for
    path = folder / f"{pid}.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def write_meta(root: Path, local_id: str, cli_session_id: str = SID, *, title: str = "Desktop 标题", cwd: str = CWD,
               last_ms: int | None = None, archived: bool = False, mode: str = "default", detail: str = "",
               summarizes: str = "", last_assistant: str = "", scheduled: bool = False, needs_action: Any = "",
               completed: Any = 3) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "sessionId": local_id, "cliSessionId": cli_session_id, "cwd": cwd, "title": title,
        "lastActivityAt": last_ms if last_ms is not None else int((NOW - 60) * 1000), "isArchived": archived,
        "permissionMode": mode, "completedTurns": completed, "lastAssistantUuid": last_assistant,
    }
    if detail or summarizes:
        data["postTurnSummary"] = {"status_category": "review_ready", "status_detail": detail,
                                   "needs_action": needs_action, "summarizes_uuid": summarizes}
    if scheduled:
        data["scheduledTaskId"] = "task-1"
    path = root / f"{local_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "claude-home"
    path.mkdir()
    return path


@pytest.fixture
def desk(tmp_path: Path) -> Path:
    path = tmp_path / "desktop" / "acct" / "org"
    path.mkdir(parents=True)
    return path


class Harness:
    def __init__(self, home: Path, desk: Path, *, alive: set[int] | None = None, cfg: FakeConfig | None = None) -> None:
        self.home = home
        self.desk = desk
        self.cfg = cfg or FakeConfig()
        self.events: list[Any] = []
        self.alive_pids = alive if alive is not None else set()
        self.alive_calls: list[tuple[int, int | None]] = []
        self.clock = Clock()
        self.secrets = FakeSecrets()
        self.source = cs.ClaudeSource(self.cfg, self.events.append, home=home, desktop_root=desk,
                                      secrets=self.secrets, clock=self.clock, alive=self.alive)

    def alive(self, pid: int, created: int | None) -> bool:
        self.alive_calls.append((pid, created))
        return pid in self.alive_pids

    def turn_events(self) -> list[TurnEvent]:
        return [event for event in self.events if isinstance(event, TurnEvent)]

    def attention(self) -> list[AttentionEvent]:
        return [event for event in self.events if isinstance(event, AttentionEvent)]


@pytest.fixture
def harness(home: Path, desk: Path) -> Harness:
    return Harness(home, desk, alive={100})


# --- registry ---------------------------------------------------------------------------


def test_read_registry_filters_dead_and_parses_fields(home: Path) -> None:
    write_registry(home, 100, SID, status="busy", host="local_abc", name="名字", proc_start="134000000000000001",
                   updated_ms=1_790_000_000_500, status_ms=1_789_999_990_000)
    write_registry(home, 200, SID2, proc_start=134000000000000002)  # dead process
    (home / "sessions" / "100.deadbeef.key").write_bytes(b"\x00secret")
    (home / "sessions" / "300.json").write_text("{not json", encoding="utf-8")
    calls: list[tuple[int, int | None]] = []

    def alive(pid: int, created: int | None) -> bool:
        calls.append((pid, created))
        return pid == 100

    entries = cs.read_registry(home, alive=alive)
    assert [entry.session_id for entry in entries] == [SID]
    entry = entries[0]
    assert entry.pid == 100 and entry.status == "busy" and entry.kind == "interactive"
    assert entry.proc_start == 134000000000000001
    assert entry.host_session_id == "local_abc" and entry.name == "名字" and entry.socket == PIPE
    assert entry.updated_at == pytest.approx(1_790_000_000.5)
    assert entry.status_updated_at == pytest.approx(1_789_999_990.0)
    assert entry.entrypoint == "claude-desktop" and entry.version == "2.1.284" and entry.cwd == CWD
    assert (100, 134000000000000001) in calls and (200, 134000000000000002) in calls
    assert all(pid != 300 for pid, _ in calls)


def test_read_registry_dedupes_by_session_and_handles_missing_dir(home: Path) -> None:
    assert cs.read_registry(home / "nowhere", alive=lambda pid, created: True) == []
    write_registry(home, 100, SID, updated_ms=int((NOW - 100) * 1000))
    write_registry(home, 101, SID, updated_ms=int((NOW - 5) * 1000), status="busy")
    write_registry(home, 102, SID2, proc_start=None)
    entries = cs.read_registry(home, alive=lambda pid, created: True)
    by_id = {entry.session_id: entry for entry in entries}
    assert by_id[SID].pid == 101 and by_id[SID].status == "busy"
    assert by_id[SID2].proc_start is None
    assert entries[0].session_id == SID  # newest first


def test_read_registry_alive_errors_mean_dead(home: Path) -> None:
    write_registry(home, 100)

    def boom(pid: int, created: int | None) -> bool:
        raise OSError("access denied")

    assert cs.read_registry(home, alive=boom) == []


# --- Desktop metadata -------------------------------------------------------------------


def test_desktop_sessions_dir_picks_org_with_newest_file(tmp_path: Path) -> None:
    local = tmp_path / "local"
    roaming = tmp_path / "roaming"
    pkg = local / "Packages" / "Claude_pzs8sxrjxfjjc" / "LocalCache" / "Roaming" / "Claude" / "claude-code-sessions"
    old = write_meta(pkg / "acct1" / "org1", "local_a")
    new = write_meta(pkg / "acct2" / "org2", "local_b")
    os.utime(old, (NOW - 1000, NOW - 1000))
    os.utime(new, (NOW, NOW))
    assert cs.desktop_sessions_dir(localappdata=str(local), appdata=str(roaming)) == pkg / "acct2" / "org2"
    fallback = write_meta(roaming / "Claude" / "claude-code-sessions" / "acct3" / "org3", "local_c")
    os.utime(fallback, (NOW + 50, NOW + 50))
    assert cs.desktop_sessions_dir(localappdata=str(local), appdata=str(roaming)) == fallback.parent
    assert cs.desktop_sessions_dir(localappdata=str(tmp_path / "x"), appdata=str(tmp_path / "y")) is None
    assert cs.desktop_sessions_dir(localappdata="", appdata="") is None


def test_desktop_sessions_dir_uses_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roaming = tmp_path / "roaming-env"
    meta = write_meta(roaming / "Claude" / "claude-code-sessions" / "a" / "o", "local_a")
    monkeypatch.setenv("APPDATA", str(roaming))
    assert cs.desktop_sessions_dir() == meta.parent


def test_desktop_index_parses_and_looks_up(desk: Path) -> None:
    write_meta(desk, "local_1", SID, title="第一个", mode="bypassPermissions", detail="已完成审查",
               summarizes="a-9", last_assistant="a-9", needs_action="", completed=None)
    write_meta(desk, "local_2", SID2, archived=True, scheduled=True, detail="等待确认", needs_action="请确认",
               last_ms=int(NOW * 1000))
    (desk / "other.json").write_text("{}", encoding="utf-8")
    index = cs.DesktopIndex(desk)
    index.refresh()
    meta = index.by_local_id("local_1")
    assert meta is not None and meta is index.by_cli_session(SID)
    assert meta.title == "第一个" and meta.permission_mode == "bypassPermissions" and meta.cwd == CWD
    assert meta.status_detail == "已完成审查" and meta.status_category == "review_ready"
    assert meta.summarizes_uuid == "a-9" and meta.last_assistant_uuid == "a-9"
    assert meta.needs_action is False and meta.completed_turns == 0 and not meta.scheduled and not meta.archived
    assert meta.last_activity == pytest.approx(NOW - 60)
    other = index.by_cli_session(SID2)
    assert other is not None and other.archived and other.scheduled and other.needs_action is True
    assert [item.local_id for item in index.all()] == ["local_2", "local_1"]
    assert index.by_local_id("") is None and index.by_cli_session("missing") is None


def test_desktop_index_caches_by_stamp_and_keeps_last_good(desk: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_meta(desk, "local_1", SID, title="旧标题")
    gone = write_meta(desk, "local_2", SID2)
    reads: list[str] = []
    real = cs._read_json_object

    def counting(target: Path) -> dict | None:
        reads.append(target.name)
        return real(target)

    monkeypatch.setattr(cs, "_read_json_object", counting)
    monkeypatch.setattr(cs.time, "sleep", lambda seconds: None)
    index = cs.DesktopIndex(desk)
    index.refresh()
    index.refresh()
    assert sorted(reads) == ["local_1.json", "local_2.json"]
    gone.unlink()
    path.write_text("{truncated", encoding="utf-8")
    os.utime(path, (NOW + 5, NOW + 5))
    index.refresh()
    assert index.by_cli_session(SID2) is None
    meta = index.by_local_id("local_1")
    assert meta is not None and meta.title == "旧标题"  # last good parse survives a half-written file
    write_meta(desk, "local_1", SID, title="新标题")
    os.utime(path, (NOW + 10, NOW + 10))
    index.refresh()
    assert index.by_local_id("local_1").title == "新标题"  # type: ignore[union-attr]


def test_desktop_index_without_root() -> None:
    index = cs.DesktopIndex(None)
    index.refresh()
    assert index.all() == [] and index.by_local_id("local_x") is None


def test_desktop_index_prefers_most_recent_meta_for_shared_cli_id(desk: Path) -> None:
    write_meta(desk, "local_old", SID, title="旧", last_ms=int((NOW - 500) * 1000))
    write_meta(desk, "local_new", SID, title="新", last_ms=int((NOW - 5) * 1000))
    index = cs.DesktopIndex(desk)
    index.refresh()
    assert index.by_cli_session(SID).title == "新"  # type: ignore[union-attr]


# --- transcripts: lookup ----------------------------------------------------------------


def test_project_slug() -> None:
    assert cs.project_slug(CWD) == SLUG
    assert cs.project_slug(r"C:\docs\a b.c") == "C--docs-a-b-c"


def test_find_transcript_by_slug_then_glob(home: Path) -> None:
    exact = write_transcript(home, [prompt("hi", NOW)])
    assert cs.find_transcript(home, SID, CWD) == exact
    other = write_transcript(home, [prompt("hi", NOW)], session_id=SID2, slug="C--very-long-path-hash123")
    assert cs.find_transcript(home, SID2, r"C:\very\long\path") == other
    assert cs.find_transcript(home, SID2) == other
    sub = home / "projects" / "p" / SID3 / "subagents" / "agent.jsonl"  # short slug: stays under MAX_PATH
    sub.parent.mkdir(parents=True)
    sub.write_text(lines([prompt("x", NOW)]), encoding="utf-8")
    assert cs.find_transcript(home, SID3) is None
    assert cs.find_transcript(home, "../evil") is None
    assert cs.find_transcript(home, "*") is None
    assert cs.find_transcript(home, "") is None


def test_find_transcript_picks_newest_duplicate(home: Path) -> None:
    first = write_transcript(home, [prompt("a", NOW)], slug="C--a")
    second = write_transcript(home, [prompt("b", NOW)], slug="C--b")
    os.utime(first, (NOW - 100, NOW - 100))
    os.utime(second, (NOW, NOW))
    assert cs.find_transcript(home, SID) == second


def test_recent_transcripts_filters_and_sorts(home: Path) -> None:
    a = write_transcript(home, [prompt("a", NOW)], session_id=SID)
    b = write_transcript(home, [prompt("b", NOW)], session_id=SID2, slug="C--other")
    c = write_transcript(home, [prompt("c", NOW)], session_id=SID3)
    os.utime(a, (NOW - 10, NOW - 10))
    os.utime(b, (NOW - 5, NOW - 5))
    os.utime(c, (NOW - 10_000, NOW - 10_000))
    sub = home / "projects" / SLUG / SID / "subagents" / "agent-1.jsonl"
    sub.parent.mkdir(parents=True)
    sub.write_text("{}\n", encoding="utf-8")
    found = cs.recent_transcripts(home, NOW - 100)
    assert [path for path, _ in found] == [b, a]
    assert found[0][1] == pytest.approx(NOW - 5)
    assert cs.recent_transcripts(home / "missing", 0) == []


def test_transcript_title_last_custom_title(home: Path) -> None:
    path = write_transcript(home, [title_record("第一个标题"), prompt("hi", NOW), title_record("  最终\n标题 "),
                                   prompt("again", NOW + 1)], tail='{"type":"custom-title","customTitle":"半行')
    assert cs.transcript_title(path) == "最终 标题"
    plain = write_transcript(home, [prompt("hi", NOW)], session_id=SID2)
    assert cs.transcript_title(plain) == ""
    assert cs.transcript_title(home / "missing.jsonl") == ""


def test_transcript_title_reads_across_blocks(home: Path) -> None:
    filler = [prompt("x" * 5000, NOW + index) for index in range(80)]
    path = write_transcript(home, [title_record("早期标题")] + filler)
    assert cs.transcript_title(path) == "早期标题"
    assert cs.transcript_title(path, max_bytes=10_000) == ""


def test_reverse_lines_skips_partial_lines(tmp_path: Path) -> None:
    path = tmp_path / "t.jsonl"
    path.write_bytes(b"first\nsecond\nthird\npartial")
    assert list(cs._reverse_lines(path, 1 << 20, block=4)) == [b"third", b"second", b"first"]
    assert list(cs._reverse_lines(path, 13, block=5)) == [b"third"]  # the window starts at "third"
    assert list(cs._reverse_lines(path, 15, block=5)) == [b"third"]  # the cut line "second" is dropped
    assert list(cs._reverse_lines(path, 8, block=5)) == []
    path.write_bytes(b"only-partial")
    assert list(cs._reverse_lines(path, 1 << 20)) == []
    path.write_bytes(b"")
    assert list(cs._reverse_lines(path, 1 << 20)) == []


# --- transcripts: turns -----------------------------------------------------------------


def test_read_turns_completed_with_streamed_message(home: Path) -> None:
    path = write_transcript(home, [
        title_record("t"),
        prompt("请修复", NOW),
        assistant("m1", [THINK], "tool_use", NOW + 1),
        assistant("m1", [text("先看看代码")], "tool_use", NOW + 2),
        assistant("m1", [TOOL], "tool_use", NOW + 3),
        tool_result(NOW + 4),
        {"type": "attachment", "uuid": _uuid(), "timestamp": iso(NOW + 5), "attachment": {"type": "total_tokens_reminder"}},
        assistant("m2", [THINK], "end_turn", NOW + 6),
        assistant("m2", [text("第一段")], "end_turn", NOW + 7),
        assistant("m2", [text("第二段")], "end_turn", NOW + 8),
        {"type": "system", "subtype": "stop_hook_summary", "timestamp": iso(NOW + 9)},
    ])
    turns = cs.read_turns(path)
    assert len(turns) == 1
    turn = turns[0]
    assert turn.user_text == "请修复" and turn.status == "completed"
    assert turn.final_text == "第一段\n\n第二段"
    assert turn.started_at == pytest.approx(NOW) and turn.completed_at == pytest.approx(NOW + 8)
    assert turn.error == ""


def test_read_turns_running_states(home: Path) -> None:
    base = [prompt("一", NOW), assistant("m1", [text("完成")], "end_turn", NOW + 1)]
    path = write_transcript(home, base + [prompt("二", NOW + 10)])
    turns = cs.read_turns(path)
    assert [turn.status for turn in turns] == ["completed", "running"]
    assert turns[1].final_text is None and turns[1].completed_at is None
    path = write_transcript(home, base + [prompt("二", NOW + 10), assistant("m2", [text("进行中"), TOOL], "tool_use", NOW + 11)])
    last = cs.read_turns(path)[-1]
    assert last.status == "running" and last.final_text == "进行中"
    path = write_transcript(home, base + [prompt("二", NOW + 10), assistant("m2", [TOOL], "tool_use", NOW + 11),
                                          tool_result(NOW + 12)])
    assert cs.read_turns(path)[-1].status == "running"


def test_read_turns_aborted_by_interrupt(home: Path) -> None:
    path = write_transcript(home, [
        prompt("做点什么", NOW),
        assistant("m1", [text("开始"), TOOL], "tool_use", NOW + 1),
        tool_result(NOW + 2),
        prompt("[Request interrupted by user for tool use]", NOW + 3, blocks=True, origin=""),
    ])
    turn = cs.read_turns(path)[-1]
    assert turn.status == "aborted" and turn.user_text == "做点什么" and turn.final_text == "开始"
    assert turn.completed_at == pytest.approx(NOW + 3)


def test_read_turns_failed_by_api_error_and_synthetic_message(home: Path) -> None:
    path = write_transcript(home, [prompt("问", NOW), api_error(NOW + 1)])
    turn = cs.read_turns(path)[-1]
    assert turn.status == "failed" and turn.error == "Connection dropped (ECONNRESET)"
    synthetic = assistant("m9", [text("API Error: 500 overloaded")], "stop_sequence", NOW + 2, isApiErrorMessage=True)
    synthetic["message"]["model"] = "<synthetic>"
    path = write_transcript(home, [prompt("问", NOW), api_error(NOW + 1), synthetic])
    turn = cs.read_turns(path)[-1]
    assert turn.status == "failed" and turn.error == "API Error: 500 overloaded" and turn.final_text is None


def test_read_turns_api_error_then_success_completes(home: Path) -> None:
    path = write_transcript(home, [prompt("问", NOW), api_error(NOW + 1),
                                   assistant("m1", [text("好了")], "end_turn", NOW + 5)])
    turn = cs.read_turns(path)[-1]
    assert turn.status == "completed" and turn.error == "" and turn.final_text == "好了"


def test_read_turns_synthetic_no_response_completes(home: Path) -> None:
    synthetic = assistant("m9", [text("No response requested.")], "stop_sequence", NOW + 1)
    synthetic["message"]["model"] = "<synthetic>"
    path = write_transcript(home, [prompt("hi", NOW), synthetic])
    turn = cs.read_turns(path)[-1]
    assert turn.status == "completed" and turn.final_text is None


def test_read_turns_ignores_sidechain_meta_and_compact_summary(home: Path) -> None:
    path = write_transcript(home, [
        prompt("主任务", NOW),
        prompt("子代理提示", NOW + 1, isSidechain=True),
        assistant("s1", [text("子代理回复")], "end_turn", NOW + 2, side=True),
        prompt("<command-message>meta</command-message>", NOW + 3, meta=True),
        prompt("压缩摘要", NOW + 4, isCompactSummary=True, isVisibleInTranscriptOnly=True),
        assistant("m1", [text("主回复")], "end_turn", NOW + 5),
    ])
    turns = cs.read_turns(path)
    assert len(turns) == 1 and turns[0].user_text == "主任务" and turns[0].final_text == "主回复"


def test_read_turns_local_command_output_completes(home: Path) -> None:
    path = write_transcript(home, [
        prompt("<command-name>/model</command-name>", NOW, origin=""),
        prompt("<local-command-stdout>Set model</local-command-stdout>", NOW + 1, origin=""),
    ])
    turn = cs.read_turns(path)[-1]
    assert turn.status == "completed" and turn.user_text == "<command-name>/model</command-name>"


def test_read_turns_peer_message_mid_turn_and_merge(home: Path) -> None:
    path = write_transcript(home, [
        prompt("长任务", NOW),
        assistant("m1", [TOOL], "tool_use", NOW + 1),
        enqueue("[tg:abc123] 顺便看看", NOW + 2),
        tool_result(NOW + 3),
        peer_queued("[tg:abc123] 顺便看看", NOW + 4),
        prompt("[tg:abc123] 顺便看看", NOW + 4, origin="peer"),  # same message recorded twice
        assistant("m2", [text("看过了")], "end_turn", NOW + 5),
    ])
    turns = cs.read_turns(path)
    assert [turn.user_text for turn in turns] == ["长任务", "[tg:abc123] 顺便看看"]
    assert [turn.status for turn in turns] == ["completed", "completed"]
    assert turns[1].final_text == "看过了" and turns[1].started_at == pytest.approx(NOW + 4)


def test_read_turns_human_queued_command_is_not_a_boundary(home: Path) -> None:
    human = peer_queued("插一句", NOW + 2)
    human["attachment"]["origin"] = {"kind": "human"}
    path = write_transcript(home, [prompt("任务", NOW), assistant("m1", [TOOL], "tool_use", NOW + 1), tool_result(NOW + 2),
                                   human, assistant("m2", [text("结束")], "end_turn", NOW + 3)])
    turns = cs.read_turns(path)
    assert len(turns) == 1 and turns[0].final_text == "结束"


def test_read_turns_cut_turn_is_aborted_and_limit_applies(home: Path) -> None:
    records = []
    for index in range(5):
        records += [prompt(f"问{index}", NOW + index * 10),
                    assistant(f"m{index}", [text(f"答{index}")], "end_turn", NOW + index * 10 + 1)]
    records += [prompt("崩溃前", NOW + 100), assistant("mx", [TOOL], "tool_use", NOW + 101), prompt("重新开始", NOW + 110)]
    path = write_transcript(home, records)
    turns = cs.read_turns(path, turns=3)
    assert [turn.user_text for turn in turns] == ["问4", "崩溃前", "重新开始"]
    assert [turn.status for turn in turns] == ["completed", "aborted", "running"]
    assert cs.read_turns(path, turns=0) == []


def test_read_turns_tail_window_and_partial_lines(home: Path) -> None:
    big = [prompt("老问题" + "x" * 3000, NOW), assistant("m0", [text("y" * 3000)], "end_turn", NOW + 1)]
    recent = [prompt("新问题", NOW + 10), assistant("m1", [text("新回答")], "end_turn", NOW + 11)]
    path = write_transcript(home, big + recent, tail='{"type":"assistant","message":{"id":"m2","content":[{"type":"text","text":"半')
    size = len(lines(recent).encode("utf-8")) + 200
    turns = cs.read_turns(path, max_bytes=size)
    assert [turn.user_text for turn in turns] == ["新问题"] and turns[0].final_text == "新回答"
    # A tail inside one long turn still yields that turn (without its prompt).
    turns = cs.read_turns(path, max_bytes=len(lines(recent[1:]).encode("utf-8")) + 100)
    assert len(turns) == 1 and turns[0].user_text == "" and turns[0].final_text == "新回答"
    assert turns[0].started_at is None and turns[0].status == "completed"


def test_read_turns_dedupes_records(home: Path) -> None:
    record = assistant("m1", [text("一次")], "end_turn", NOW + 1, uuid="same")
    cumulative_a = assistant("m2", [text("甲")], "end_turn", NOW + 3)
    cumulative_b = assistant("m2", [text("甲"), text("乙")], "end_turn", NOW + 4)
    path = write_transcript(home, [prompt("a", NOW), record, dict(record)])
    assert cs.read_turns(path)[-1].final_text == "一次"
    path = write_transcript(home, [prompt("b", NOW), cumulative_a, cumulative_b])
    assert cs.read_turns(path)[-1].final_text == "甲\n\n乙"


def test_read_turns_bad_lines_are_skipped(home: Path) -> None:
    path = home / "projects" / SLUG / f"{SID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text("not json\n[1,2]\n" + lines([prompt("好", NOW), assistant("m", [text("行")], "end_turn", NOW + 1)]),
                    encoding="utf-8")
    assert cs.read_turns(path)[-1].final_text == "行"


# --- delivered --------------------------------------------------------------------------


def test_delivered_only_counts_inbox_records_after_offset(home: Path) -> None:
    marker = "[tg:abc123]"
    path = write_transcript(home, [prompt("hi", NOW), enqueue(f"{marker} 旧的", NOW)])
    offset = path.stat().st_size
    assert not cs.delivered(path, marker, offset)
    append(path, [prompt(f"{marker} 人工输入", NOW + 1), assistant("m", [text(f"引用 {marker}")], "end_turn", NOW + 2)])
    assert not cs.delivered(path, marker, offset)
    append(path, [enqueue(f"{marker} 新的", NOW + 3)])
    assert cs.delivered(path, marker, offset)
    assert not cs.delivered(path, "", offset)
    assert not cs.delivered(home / "missing.jsonl", marker, 0)


@pytest.mark.parametrize("record", [
    lambda marker: peer_queued(f"{marker} hi", NOW),
    lambda marker: prompt(f"{marker} hi", NOW, origin="peer"),
    lambda marker: prompt(f"{marker} hi", NOW, origin="peer", blocks=True),
])
def test_delivered_record_kinds(home: Path, record: Any) -> None:
    marker = "[tg:ff00aa]"
    path = write_transcript(home, [prompt("hi", NOW)])
    offset = path.stat().st_size
    append(path, [record(marker)])
    assert cs.delivered(path, marker, offset)


def test_delivered_ignores_removed_and_partial_offsets(home: Path) -> None:
    marker = "[tg:xyz789]"
    path = write_transcript(home, [prompt("hi", NOW)])
    append(path, [{"type": "queue-operation", "operation": "remove", "content": f"{marker} x", "timestamp": iso(NOW)}])
    assert not cs.delivered(path, marker, 0)
    size = path.stat().st_size
    append(path, [enqueue(f"{marker} y", NOW + 1)])
    assert cs.delivered(path, marker, size + 5)is False  # mid-line offset cannot see the record
    assert cs.delivered(path, marker, size * 10)  # offset beyond the file (rewritten): scan from the start


def test_delivered_marker_needing_json_escape(home: Path) -> None:
    marker = '[tg:"q"]'
    path = write_transcript(home, [enqueue(f"{marker} hi", NOW)])
    assert cs.delivered(path, marker, 0)


# --- ClaudeSource: hooks ----------------------------------------------------------------


def stop_payload(event: str = "Stop", **extra: Any) -> dict:
    payload = {"event": event, "session_id": SID, "transcript_path": "", "cwd": CWD, "permission_mode": "acceptEdits",
               "agent_id": None, "last_assistant_message": "最终回复", "inbox": {"socket": PIPE, "token": "tok-123"}}
    payload.update(extra)
    return payload


def test_on_hook_stop_emits_turn_with_desktop_title_and_summary(harness: Harness) -> None:
    last = "assistant-last"
    path = write_transcript(harness.home, [prompt("hi", NOW), assistant("m", [text("x")], "end_turn", NOW + 1, uuid=last)])
    write_registry(harness.home, 100, host="local_1", name="注册表名字")
    write_meta(harness.desk, "local_1", title="桌面标题", detail="改好了", summarizes=last)
    harness.source.on_hook(stop_payload(transcript_path=str(path)))
    [event] = harness.turn_events()
    assert event.agent == CLAUDE and event.session_id == SID and event.status == "completed"
    assert event.title == "桌面标题" and event.cwd == CWD and event.final_text == "最终回复"
    assert event.summary == "改好了" and event.completed_at == NOW and event.labels == []
    assert harness.secrets.get(f"claude:{SID}") == {"socket": PIPE, "token": "tok-123", "at": NOW}


def test_on_hook_summary_for_an_older_turn_is_not_used(harness: Harness) -> None:
    write_transcript(harness.home, [prompt("hi", NOW), assistant("m", [text("x")], "end_turn", NOW + 1, uuid="new")])
    write_meta(harness.desk, "local_1", detail="上一轮的摘要", summarizes="old")
    harness.source.on_hook(stop_payload())
    assert harness.turn_events()[0].summary == ""
    assert harness.source.latest_summary(SID) == ""
    write_meta(harness.desk, "local_1", detail="这一轮的摘要", summarizes="new")
    os.utime(harness.desk / "local_1.json", (NOW + 30, NOW + 30))
    harness.clock.now += 5
    assert harness.source.latest_summary(SID) == "这一轮的摘要"


def test_on_hook_title_fallbacks(home: Path, desk: Path) -> None:
    h = Harness(home, desk, alive={100})
    write_registry(home, 100, name="注册表名字")
    h.source.on_hook(stop_payload())
    assert h.turn_events()[-1].title == "注册表名字"
    h = Harness(home, desk, alive=set())
    write_transcript(home, [title_record("记录里的标题"), prompt("hi", NOW)])
    h.source.on_hook(stop_payload())
    assert h.turn_events()[-1].title == "记录里的标题"
    h = Harness(home, desk, alive=set())
    h.source.on_hook(stop_payload(session_id=SID2))
    assert h.turn_events()[-1].title == "demo-app"


def test_a_turn_carries_claude_desktops_usage_record(harness: Harness, tmp_path: Path) -> None:
    harness.source.on_hook(stop_payload())
    assert harness.turn_events()[-1].quota is None  # Claude Desktop recorded nothing
    record = (tmp_path / "no-localappdata" / "Packages" / "Claude_test" / "LocalCache" / "Roaming" / "Claude"
              / "plan-usage-history.json")
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"version": 2, "samples": [{"t": int(NOW * 1000) - 60_000, "org": "o",
                                                             "u": {"fh": 8, "sd": 39}}]}), encoding="utf-8")
    harness.source.on_hook(stop_payload(session_id=SID2))
    found = harness.turn_events()[-1].quota
    assert found.source == "desktop" and found.observed_at == NOW - 60
    assert [(window.label, window.used_percent) for window in found.windows] == [("5h", 8.0), ("本周", 39.0)]


def test_on_hook_stop_failure(harness: Harness) -> None:
    harness.source.on_hook(stop_payload("StopFailure", error="rate_limit", error_details="429 Too Many Requests",
                                        last_assistant_message=""))
    [event] = harness.turn_events()
    assert event.status == "failed" and event.error == "rate_limit：429 Too Many Requests" and event.final_text is None
    harness.source.on_hook(stop_payload("StopFailure", error=None, error_details={"code": 500}))
    assert harness.turn_events()[-1].error == '{"code": 500}'
    harness.source.on_hook(stop_payload("StopFailure", error=None, error_details=None))
    assert harness.turn_events()[-1].error == "未知错误"


def test_on_hook_subagent_and_session_start_only_store_inbox(harness: Harness) -> None:
    harness.source.on_hook(stop_payload(agent_id="agent-7", permission_mode="plan"))
    harness.source.on_hook(stop_payload("SessionStart", inbox={"socket": "uds:" + PIPE, "token": "tok-2"}))
    assert harness.events == []
    assert harness.secrets.get(f"claude:{SID}")["token"] == "tok-2"
    assert harness.source.permission_mode(SID) == "acceptEdits"  # the subagent's mode is ignored


def test_on_hook_stores_inbox_only_when_changed(harness: Harness) -> None:
    harness.source.on_hook(stop_payload("SessionStart"))
    harness.source.on_hook(stop_payload("SessionStart"))
    assert harness.secrets.puts == 1
    harness.source.on_hook(stop_payload("SessionStart", inbox={"socket": PIPE, "token": "tok-new"}))
    assert harness.secrets.puts == 2
    harness.source.on_hook(stop_payload("SessionStart", inbox={"socket": PIPE}))
    harness.source.on_hook(stop_payload("SessionStart", inbox="bad"))
    assert harness.secrets.puts == 2


def test_on_hook_rejects_bad_payloads(harness: Harness) -> None:
    harness.source.on_hook("nope")  # type: ignore[arg-type]
    harness.source.on_hook(stop_payload(session_id="../../x"))
    harness.source.on_hook(stop_payload(event="PreToolUse"))
    assert harness.events == [] and harness.secrets.puts == 1  # only the PreToolUse payload had a valid id


def test_on_hook_excluded_cwd_hides_content(home: Path, desk: Path) -> None:
    cfg = FakeConfig(content_exclude=[r"C:\proj"])
    h = Harness(home, desk, alive={100}, cfg=cfg)
    last = "a-1"
    write_transcript(home, [prompt("hi", NOW), assistant("m", [text("x")], "end_turn", NOW + 1, uuid=last)])
    write_meta(desk, "local_1", detail="机密摘要", summarizes=last)
    h.source.on_hook(stop_payload())
    [event] = h.turn_events()
    assert event.final_text is None and event.summary == "" and event.labels == ["内容不推送"]


def test_on_hook_scheduled_sessions_skipped_unless_included(home: Path, desk: Path) -> None:
    write_meta(desk, "local_1", scheduled=True)
    h = Harness(home, desk)
    h.source.on_hook(stop_payload())
    assert h.events == [] and h.secrets.puts == 1
    h = Harness(home, desk, cfg=FakeConfig(claude_include_scheduled=True))
    h.source.on_hook(stop_payload())
    assert len(h.turn_events()) == 1


def test_on_hook_disabled(home: Path, desk: Path) -> None:
    h = Harness(home, desk, cfg=FakeConfig(claude_enabled=False))
    h.source.on_hook(stop_payload())
    h.source.poll()
    assert h.events == [] and h.secrets.puts == 0 and h.source.sessions(10) == []


def test_on_hook_started_at_from_observed_busy(harness: Harness) -> None:
    write_registry(harness.home, 100, status="busy", status_ms=int((NOW - 42) * 1000))
    harness.source.poll()
    harness.clock.now += 5
    harness.source.on_hook(stop_payload())
    assert harness.turn_events()[0].started_at == pytest.approx(NOW - 42)
    harness.source.on_hook(stop_payload())
    assert harness.turn_events()[1].started_at is None  # consumed: a second Stop does not reuse it


def test_emit_errors_do_not_escape(home: Path, desk: Path) -> None:
    def bad(event: Any) -> None:
        raise RuntimeError("consumer down")

    source = cs.ClaudeSource(FakeConfig(), bad, home=home, desktop_root=desk, secrets=FakeSecrets(), clock=Clock(),
                             alive=lambda pid, created: True)
    source.on_hook(stop_payload())


# --- ClaudeSource: poll -----------------------------------------------------------------


def test_poll_waiting_alerts_then_loud_once(harness: Harness) -> None:
    write_registry(harness.home, 100, status="busy")
    harness.source.poll()
    assert harness.events == []
    write_registry(harness.home, 100, status="waiting", waiting_for="dialog open", status_ms=int(NOW * 1000))
    harness.source.poll()
    [first] = harness.attention()
    assert first.reason == "dialog open" and not first.loud and first.session_id == SID and first.cwd == CWD
    harness.clock.now += 300
    harness.source.poll()
    assert len(harness.attention()) == 1
    harness.clock.now += 301
    harness.source.poll()
    harness.clock.now += 1000
    harness.source.poll()
    assert [event.loud for event in harness.attention()] == [False, True]
    write_registry(harness.home, 100, status="busy")
    harness.source.poll()
    write_registry(harness.home, 100, status="waiting", status_ms=int(harness.clock.now * 1000))
    harness.source.poll()
    assert harness.attention()[-1].reason == "等待处理" and not harness.attention()[-1].loud


def test_poll_first_sight_of_long_wait_is_loud(harness: Harness) -> None:
    write_registry(harness.home, 100, status="waiting", waiting_for="input needed", status_ms=int((NOW - 3600) * 1000))
    harness.source.poll()
    harness.clock.now += 5
    harness.source.poll()
    assert [event.loud for event in harness.attention()] == [True]


def test_poll_fallback_after_busy_to_idle_without_stop(harness: Harness) -> None:
    path = write_transcript(harness.home, [prompt("任务", NOW - 50), assistant("m", [text("完成了")], "end_turn", NOW - 1)])
    write_registry(harness.home, 100, status="busy", status_ms=int((NOW - 50) * 1000))
    harness.source.poll()
    write_registry(harness.home, 100, status="idle")
    harness.source.poll()
    assert harness.events == []  # grace period for a late Stop hook
    harness.clock.now += cs.FALLBACK_GRACE_S
    harness.source.poll()
    [event] = harness.turn_events()
    assert event.status == "completed" and event.final_text == "完成了" and event.started_at == pytest.approx(NOW - 50)
    assert event.completed_at == pytest.approx(NOW - 1) and event.title == "demo-app"
    harness.clock.now += 100
    harness.source.poll()
    assert len(harness.turn_events()) == 1
    assert path.exists()


def test_poll_fallback_skipped_when_stop_hook_reported(harness: Harness) -> None:
    write_transcript(harness.home, [prompt("任务", NOW), assistant("m", [text("完成了")], "end_turn", NOW + 1)])
    write_registry(harness.home, 100, status="busy")
    harness.source.poll()
    write_registry(harness.home, 100, status="idle")
    harness.source.poll()
    harness.clock.now += 2
    harness.source.on_hook(stop_payload())  # the async hook lands after the registry flipped
    harness.clock.now += cs.FALLBACK_GRACE_S
    harness.source.poll()
    assert len(harness.turn_events()) == 1 and harness.turn_events()[0].final_text == "最终回复"


def test_poll_fallback_waits_for_transcript_then_gives_up(harness: Harness) -> None:
    path = write_transcript(harness.home, [prompt("任务", NOW), assistant("m", [TOOL], "tool_use", NOW + 1)])
    write_registry(harness.home, 100, status="busy")
    harness.source.poll()
    write_registry(harness.home, 100, status="idle")
    harness.source.poll()
    for _ in range(cs.FALLBACK_RETRIES):
        harness.clock.now += cs.FALLBACK_GRACE_S
        harness.source.poll()
        assert harness.events == []
    harness.clock.now += cs.FALLBACK_GRACE_S
    harness.source.poll()
    [event] = harness.turn_events()
    assert event.status == "completed" and event.final_text is None
    assert path.exists()


def test_poll_fallback_picks_finished_turn_when_busy_again(harness: Harness) -> None:
    path = write_transcript(harness.home, [prompt("一", NOW), assistant("m", [text("答一")], "end_turn", NOW + 1)])
    write_registry(harness.home, 100, status="busy")
    harness.source.poll()
    write_registry(harness.home, 100, status="idle")
    harness.source.poll()
    append(path, [prompt("二", NOW + 5)])
    write_registry(harness.home, 100, status="busy", status_ms=int((NOW + 5) * 1000))
    harness.clock.now += cs.FALLBACK_GRACE_S
    harness.source.poll()
    [event] = harness.turn_events()
    assert event.final_text == "答一"


def test_poll_fallback_after_process_exit_and_kind_filter(home: Path, desk: Path) -> None:
    write_transcript(home, [prompt("一", NOW), assistant("m", [text("答")], "end_turn", NOW + 1)])
    h = Harness(home, desk, alive={100})
    write_registry(home, 100, status="busy")
    h.source.poll()
    write_registry(home, 100, status="idle")
    h.source.poll()
    h.alive_pids.clear()  # the process exits during the grace period
    h.clock.now += cs.FALLBACK_GRACE_S
    h.source.poll()
    assert len(h.turn_events()) == 1
    h = Harness(home, desk, alive={100})
    write_registry(home, 100, status="busy", kind="sdk")
    h.source.poll()
    write_registry(home, 100, status="idle", kind="sdk")
    h.source.poll()
    h.clock.now += cs.FALLBACK_GRACE_S
    h.source.poll()
    assert h.events == []


def test_poll_skips_scheduled_sessions(home: Path, desk: Path) -> None:
    write_meta(desk, "local_1", scheduled=True)
    h = Harness(home, desk, alive={100})
    write_registry(home, 100, status="waiting", host="local_1")
    h.source.poll()
    assert h.events == []
    h = Harness(home, desk, alive={100}, cfg=FakeConfig(claude_include_scheduled=True))
    h.source.poll()
    assert len(h.attention()) == 1


def test_poll_fallback_excluded_cwd(home: Path, desk: Path) -> None:
    h = Harness(home, desk, alive={100}, cfg=FakeConfig(content_exclude=[CWD]))
    write_transcript(home, [prompt("一", NOW), assistant("m", [text("秘密")], "end_turn", NOW + 1)])
    write_registry(home, 100, status="busy")
    h.source.poll()
    write_registry(home, 100, status="idle")
    h.source.poll()
    h.clock.now += cs.FALLBACK_GRACE_S
    h.source.poll()
    [event] = h.turn_events()
    assert event.final_text is None and event.labels == ["内容不推送"]


def test_poll_prunes_inbox_tokens_of_dead_sessions(harness: Harness) -> None:
    harness.secrets.put(f"claude:{SID2}", {"socket": PIPE, "token": "old", "at": NOW - 10_000})
    harness.secrets.put(f"claude:{SID3}", {"socket": PIPE, "token": "fresh", "at": NOW - 5})
    harness.secrets.put(f"claude:{SID}", {"socket": PIPE, "token": "live", "at": NOW - 10_000})
    harness.secrets.put("codex:thread", {"x": 1})
    write_registry(harness.home, 100, SID)
    harness.source.poll()
    assert len(harness.secrets.keys()) == 4  # the first check only marks candidates
    harness.clock.now += 60
    harness.source.poll()
    assert len(harness.secrets.keys()) == 4  # checks are INBOX_PRUNE_S apart
    harness.clock.now += cs.INBOX_PRUNE_S
    harness.source.poll()
    assert sorted(harness.secrets.keys()) == sorted([f"claude:{SID3}", f"claude:{SID}", "codex:thread"])
    harness.clock.now += cs.INBOX_PRUNE_S  # the recently stored token is old only from the last check on
    harness.source.poll()
    assert sorted(harness.secrets.keys()) == sorted([f"claude:{SID}", "codex:thread"])


def test_poll_prune_spares_a_session_that_came_back(harness: Harness) -> None:
    harness.secrets.put(f"claude:{SID}", {"socket": PIPE, "token": "tok", "at": NOW - 10_000})
    harness.source.poll()  # registry unreadable / session not live at this moment
    write_registry(harness.home, 100, SID)
    harness.clock.now += cs.INBOX_PRUNE_S
    harness.source.poll()
    (harness.home / "sessions" / "100.json").unlink()
    harness.clock.now += cs.INBOX_PRUNE_S
    harness.source.poll()
    assert harness.secrets.keys() == [f"claude:{SID}"]  # dead once again, but not twice in a row


# --- ClaudeSource: views ----------------------------------------------------------------


def test_sessions_merge_dedupe_and_order(home: Path, desk: Path) -> None:
    h = Harness(home, desk, alive={100, 101})
    write_registry(home, 100, SID, status="idle", host="local_1", updated_ms=int((NOW - 100) * 1000))
    write_registry(home, 101, SID2, status="waiting", waiting_for="input needed", entrypoint="cli", name="",
                   updated_ms=int((NOW - 3000) * 1000))
    write_meta(desk, "local_1", SID, title="桌面会话", detail="摘要一句话", last_ms=int((NOW - 20) * 1000))
    write_meta(desk, "local_arch", "arch-0000-0000", archived=True)
    write_meta(desk, "local_sched", "sched-0000-0000", scheduled=True)
    write_meta(desk, "local_old", "old-0000-0000", last_ms=int((NOW - 3 * 86400) * 1000))
    write_meta(desk, "local_4", SID3, title="只有桌面", last_ms=int((NOW - 500) * 1000))
    cli = write_transcript(home, [title_record("命令行会话"), prompt("x", NOW)], session_id="cli-1111-2222", slug="C--elsewhere")
    os.utime(cli, (NOW - 50, NOW - 50))
    for hidden in ("arch-0000-0000", "sched-0000-0000"):
        os.utime(write_transcript(home, [prompt("x", NOW)], session_id=hidden), (NOW - 5, NOW - 5))
    views = h.source.sessions(10)
    ids = [view.id for view in views]
    assert ids == [SID2, SID, "cli-1111-2222", SID3]
    waiting = views[0]
    assert waiting.state == "waiting" and waiting.detail == "input needed" and waiting.live and waiting.kind == "cli"
    assert waiting.title == "demo-app"
    desktop = views[1]
    assert desktop.title == "桌面会话" and desktop.state == "idle" and desktop.kind == "desktop" and desktop.live
    assert desktop.detail == "摘要一句话" and desktop.last_activity == pytest.approx(NOW - 10)
    transcript_only = views[2]
    assert transcript_only.title == "命令行会话" and not transcript_only.live and transcript_only.kind == "cli"
    assert transcript_only.cwd == CWD and transcript_only.state == "idle"
    assert views[3].title == "只有桌面" and views[3].kind == "desktop" and not views[3].live
    assert [view.id for view in h.source.sessions(2)] == [SID2, SID]
    assert h.source.sessions(0) == []


def test_sessions_hide_the_old_id_of_a_resumed_desktop_session(home: Path, desk: Path) -> None:
    """The live process of local_1 runs under SID2; the metadata still names SID (seen on a real machine)."""

    h = Harness(home, desk, alive={100})
    write_registry(home, 100, SID2, status="busy", host="local_1", updated_ms=int((NOW - 10) * 1000))
    write_meta(desk, "local_1", SID, title="桌面会话", last_ms=int((NOW - 20) * 1000))
    os.utime(write_transcript(home, [prompt("x", NOW)], session_id=SID), (NOW - 30, NOW - 30))
    views = h.source.sessions(10)
    assert [view.id for view in views] == [SID2]
    assert views[0].title == "桌面会话" and views[0].state == "running"


def test_sessions_list_one_process_per_desktop_session(home: Path, desk: Path) -> None:
    """Claude Desktop runs an extra idle claude.exe for the same local_<id> (seen live)."""

    h = Harness(home, desk, alive={100, 101})
    write_registry(home, 100, SID, status="busy", host="local_1", updated_ms=int((NOW - 10) * 1000))
    write_registry(home, 101, SID2, status="idle", host="local_1", updated_ms=int((NOW - 5) * 1000))
    write_meta(desk, "local_1", SID, title="桌面会话", last_ms=int((NOW - 20) * 1000))
    views = h.source.sessions(10)
    assert [view.id for view in views] == [SID]


def test_sessions_scheduled_included_and_headless_kind(home: Path, desk: Path) -> None:
    h = Harness(home, desk, alive={100}, cfg=FakeConfig(claude_include_scheduled=True))
    write_meta(desk, "local_sched", SID2, scheduled=True)
    write_registry(home, 100, SID3, status="busy", entrypoint="sdk-cli", kind="sdk")
    h.source.on_hook(stop_payload("SessionStart", session_id=SID3, headless=True))
    views = {view.id: view for view in h.source.sessions(10)}
    assert SID2 in views
    assert views[SID3].kind == "headless" and views[SID3].state == "running"


def test_sessions_excluded_cwd_hides_detail(home: Path, desk: Path) -> None:
    h = Harness(home, desk, cfg=FakeConfig(content_exclude=[CWD]))
    write_meta(desk, "local_1", SID, detail="机密")
    [view] = h.source.sessions(5)
    assert view.detail == "" and view.title == "Desktop 标题"


def test_view_live_and_lookups(harness: Harness) -> None:
    assert harness.source.view(SID) is None
    assert harness.source.view("../x") is None
    path = write_transcript(harness.home, [prompt("hi", NOW), assistant("m", [text("回复")], "end_turn", NOW + 1)])
    os.utime(path, (NOW - 7, NOW - 7))
    view = harness.source.view(SID)
    assert view is not None and not view.live and view.state == "idle" and view.last_activity == pytest.approx(NOW - 7)
    write_registry(harness.home, 100, status="busy", name="活着")
    view = harness.source.view(SID)
    assert view is not None and view.live and view.state == "running" and view.title == "活着"
    entry = harness.source.live(SID)
    assert entry is not None and entry.pid == 100
    assert harness.source.live(SID2) is None


def test_view_unknown_status(harness: Harness) -> None:
    write_registry(harness.home, 100, status="")
    assert harness.source.view(SID).state == "unknown"  # type: ignore[union-attr]


def test_read_turns_full_text_and_permission_mode(harness: Harness) -> None:
    assert harness.source.read_turns(SID, 3) == [] and harness.source.full_text(SID) is None
    path = write_transcript(harness.home, [prompt("一", NOW), assistant("m1", [text("答一")], "end_turn", NOW + 1)])
    assert harness.source.full_text(SID) == "答一"
    append(path, [prompt("二", NOW + 5), assistant("m2", [text("进行中"), TOOL], "tool_use", NOW + 6)])
    write_registry(harness.home, 100, status="busy")
    turns = harness.source.read_turns(SID, 5)
    assert [turn.status for turn in turns] == ["completed", "running"]
    assert harness.source.full_text(SID) == "答一"  # the running turn's commentary is not the final reply
    (harness.home / "sessions" / "100.json").unlink()
    assert harness.source.permission_mode(SID) == "default"
    harness.source.on_hook(stop_payload("SessionStart"))
    assert harness.source.permission_mode(SID) == "acceptEdits"
    write_meta(harness.desk, "local_1", SID, mode="bypassPermissions")
    harness.clock.now += cs.DESKTOP_REFRESH_S
    assert harness.source.view(SID) is not None  # refreshes the Desktop index
    assert harness.source.permission_mode(SID) == "bypassPermissions"


def test_read_turns_blanked_for_excluded_cwd(home: Path, desk: Path) -> None:
    h = Harness(home, desk, cfg=FakeConfig(content_exclude=[CWD]))
    write_transcript(home, [prompt("秘密问题", NOW), assistant("m1", [text("秘密回答")], "end_turn", NOW + 1)])
    [turn] = h.source.read_turns(SID, 3)
    assert turn.user_text == "" and turn.final_text is None and turn.status == "completed"
    assert h.source.full_text(SID) is None


def test_desktop_root_auto_detected_from_environment(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roaming = tmp_path / "roaming-auto"
    write_meta(roaming / "Claude" / "claude-code-sessions" / "a" / "o", "local_1", SID, title="自动找到")
    monkeypatch.setenv("APPDATA", str(roaming))
    source = cs.ClaudeSource(FakeConfig(), lambda event: None, home=home, secrets=FakeSecrets(), clock=Clock(),
                             alive=lambda pid, created: False)
    assert source.view(SID).title == "自动找到"  # type: ignore[union-attr]


def test_defaults_come_from_config_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = types.ModuleType("agent_telegram.tgbridge.config")
    fake.claude_home = lambda: tmp_path / "cfg-home"  # type: ignore[attr-defined]
    store = FakeSecrets()
    fake.SecretStore = lambda: store  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent_telegram.tgbridge.config", fake)
    source = cs.ClaudeSource(FakeConfig(), lambda event: None, desktop_root=tmp_path, clock=Clock(),
                             alive=lambda pid, created: False)
    assert source.home == tmp_path / "cfg-home" and source.secrets is store


# --- ClaudeSource: delivery -------------------------------------------------------------


def test_inbox_for_requires_live_matching_socket(harness: Harness) -> None:
    harness.source.on_hook(stop_payload("SessionStart", inbox={"socket": "uds:" + PIPE.upper(), "token": "tok"}))
    assert harness.source.inbox_for(SID) is None  # no live process
    write_registry(harness.home, 100)
    assert harness.source.inbox_for(SID) == ("uds:" + PIPE.upper(), "tok")
    write_registry(harness.home, 100, socket=PIPE.replace("0123", "9999"))
    assert harness.source.inbox_for(SID) is None
    write_registry(harness.home, 100, socket="")
    assert harness.source.inbox_for(SID) is None


def test_deliver_posts_and_check_delivered(harness: Harness) -> None:
    marker = "[tg:c0ffee]"
    path = write_transcript(harness.home, [prompt("hi", NOW), assistant("m", [text("ok")], "end_turn", NOW + 1)])
    write_registry(harness.home, 100)
    harness.source.on_hook(stop_payload("SessionStart"))
    posted: list[tuple[str, str, str]] = []

    def post(socket: str, token: str, message: str) -> None:
        posted.append((socket, token, message))
        append(path, [enqueue(message, NOW + 2)])

    size = path.stat().st_size
    info = harness.source.deliver(SID, f"{marker} 你好", post=post)
    assert posted == [(PIPE, "tok-123", f"{marker} 你好")]
    assert info == {"transcript": str(path), "offset": size, "at": NOW}
    assert harness.source.check_delivered(SID, marker, info)
    assert not harness.source.check_delivered(SID, "[tg:other1]", info)


def test_deliver_uses_real_module_post_by_default(harness: Harness, inbox_stub: types.ModuleType) -> None:
    write_registry(harness.home, 100)
    harness.source.on_hook(stop_payload("SessionStart"))
    calls: list[tuple] = []
    inbox_stub.post = lambda *args, **kwargs: calls.append(args)  # type: ignore[attr-defined]
    info = harness.source.deliver(SID, "[tg:aaaaaa] hi")
    assert calls == [(PIPE, "tok-123", "[tg:aaaaaa] hi")] and info["transcript"] == "" and info["offset"] == 0


def test_deliver_errors_are_inbox_errors(harness: Harness) -> None:
    def post(*args: Any) -> None:
        raise AssertionError("must not post")

    with pytest.raises(InboxError, match="没有运行中的进程"):
        harness.source.deliver(SID, "x", post=post)
    write_registry(harness.home, 100)
    with pytest.raises(InboxError, match="收件箱信息"):
        harness.source.deliver(SID, "x", post=post)
    harness.secrets.put(f"claude:{SID}", {"socket": r"\\.\pipe\LOCAL\cc-msg-ffffffffffffffff", "token": "t"})
    with pytest.raises(InboxError, match="过期"):
        harness.source.deliver(SID, "x", post=post)
    harness.source.on_hook(stop_payload("SessionStart"))

    def broken(*args: Any) -> None:
        raise OSError(22, "Invalid argument")

    with pytest.raises(InboxError, match="投递到会话收件箱失败") as caught:
        harness.source.deliver(SID, "x", post=broken)
    assert "tok-123" not in str(caught.value)

    def refused(*args: Any) -> None:
        raise InboxError("会话的收件箱不存在（会话可能已经关闭）")

    with pytest.raises(InboxError, match="收件箱不存在"):
        harness.source.deliver(SID, "x", post=refused)


def test_check_delivered_when_transcript_appears_later(harness: Harness) -> None:
    marker = "[tg:123abc]"
    info = {"transcript": "", "offset": 0, "at": NOW}
    assert not harness.source.check_delivered(SID, marker, info)
    write_transcript(harness.home, [peer_queued(f"{marker} 新会话", NOW)])
    assert harness.source.check_delivered(SID, marker, info)
    moved = {"transcript": str(harness.home / "gone.jsonl"), "offset": 10_000}
    assert harness.source.check_delivered(SID, marker, moved)  # different file: scanned from the start


# --- review additions -------------------------------------------------------------------


def test_iso_epoch_never_raises() -> None:
    assert cs._iso_epoch("garbage") is None and cs._iso_epoch(None) is None and cs._iso_epoch("") is None
    value = cs._iso_epoch("0001-01-01T00:00:00")  # naive pre-1970: OSError on Windows
    assert value is None or isinstance(value, float)
    assert cs._iso_epoch("2026-10-01T00:00:00Z") == pytest.approx(1790812800.0)


def test_read_turns_survives_odd_timestamps(home: Path) -> None:
    odd = prompt("问", NOW)
    odd["timestamp"] = "0001-01-01T00:00:00"
    path = write_transcript(home, [odd, assistant("m", [text("答")], "end_turn", NOW + 1)])
    [turn] = cs.read_turns(path)
    assert turn.final_text == "答" and turn.status == "completed"


def test_read_turns_bash_mode_output_completes(home: Path) -> None:
    path = write_transcript(home, [
        prompt("一", NOW), assistant("m1", [text("答一")], "end_turn", NOW + 1),
        prompt("<bash-input>dir</bash-input>", NOW + 5),
        prompt("<bash-stdout>a.txt</bash-stdout><bash-stderr></bash-stderr>", NOW + 6),
    ])
    turns = cs.read_turns(path)
    assert [turn.status for turn in turns] == ["completed", "completed"]
    assert turns[-1].user_text == "<bash-input>dir</bash-input>" and turns[-1].completed_at == pytest.approx(NOW + 6)


def test_read_turns_task_notification_starts_its_own_turn(home: Path) -> None:
    path = write_transcript(home, [
        prompt("后台跑测试", NOW), assistant("m1", [text("已在后台运行")], "end_turn", NOW + 1),
        prompt("<task-notification>done</task-notification>", NOW + 60, origin="task-notification"),
        assistant("m2", [text("测试通过")], "end_turn", NOW + 61),
    ])
    turns = cs.read_turns(path)
    assert [turn.final_text for turn in turns] == ["已在后台运行", "测试通过"]
    assert all(turn.status == "completed" for turn in turns)


def test_recent_transcripts_skips_agent_files(home: Path) -> None:
    main = write_transcript(home, [prompt("a", NOW)])
    agent = home / "projects" / SLUG / "agent-1234abcd.jsonl"
    agent.write_text(lines([prompt("side", NOW)]), encoding="utf-8")
    assert [path for path, _ in cs.recent_transcripts(home, 0)] == [main]


def test_source_read_turns_stale_running_turn_is_aborted(harness: Harness) -> None:
    write_transcript(harness.home, [prompt("一", NOW), assistant("m1", [TOOL], "tool_use", NOW + 1)])
    assert harness.source.read_turns(SID, 3)[-1].status == "aborted"  # no live process
    write_registry(harness.home, 100, status="busy")
    assert harness.source.read_turns(SID, 3)[-1].status == "running"
    write_registry(harness.home, 100, status="idle", status_ms=int((NOW - 5) * 1000))
    assert harness.source.read_turns(SID, 3)[-1].status == "running"  # just went idle: transcript may lag
    write_registry(harness.home, 100, status="idle", status_ms=int((NOW - cs.STALE_RUNNING_S - 1) * 1000))
    [turn] = harness.source.read_turns(SID, 3)
    assert turn.status == "aborted" and turn.completed_at is None and turn.user_text == "一"


def test_local_transcript_path() -> None:
    name = f"{SID}.jsonl"
    local = rf"C:\Users\u\.claude\projects\x\{name}"
    assert cs._local_transcript_path(local, SID) == Path(local)
    assert cs._local_transcript_path(rf"\\evil\share\{name}", SID) is None
    assert cs._local_transcript_path(f"//evil/share/{name}", SID) is None
    assert cs._local_transcript_path(rf"projects\x\{name}", SID) is None
    assert cs._local_transcript_path(r"C:\x\other.jsonl", SID) is None
    assert cs._local_transcript_path("", SID) is None


def test_on_hook_ignores_unc_transcript_path(harness: Harness) -> None:
    local = write_transcript(harness.home, [prompt("hi", NOW), assistant("m", [text("本地")], "end_turn", NOW + 1)])
    harness.source.on_hook(stop_payload("SessionStart", transcript_path=rf"\\evil\share\{SID}.jsonl"))
    assert SID not in harness.source._paths  # checked first: probing the UNC path would reach the network
    assert harness.source._transcript(SID, CWD) == local


def test_on_hook_excluded_if_session_started_in_excluded_dir(home: Path, desk: Path) -> None:
    h = Harness(home, desk, alive={100}, cfg=FakeConfig(content_exclude=[CWD]))
    write_registry(home, 100)  # the session was started in the excluded dir ...
    h.source.on_hook(stop_payload(cwd=r"C:\somewhere\else"))  # ... and has left it
    [event] = h.turn_events()
    assert event.final_text is None and event.labels == ["内容不推送"]
    h = Harness(home, desk, alive=set(), cfg=FakeConfig(content_exclude=[CWD]))
    write_meta(desk, "local_1", SID, cwd=CWD)
    h.source.on_hook(stop_payload(cwd=r"C:\somewhere\else"))
    assert h.turn_events()[0].final_text is None


def test_source_read_turns_excluded_by_any_known_cwd(home: Path, desk: Path) -> None:
    h = Harness(home, desk, alive={100}, cfg=FakeConfig(content_exclude=[CWD]))
    write_registry(home, 100, cwd=r"C:\elsewhere")
    write_transcript(home, [prompt("秘密", NOW), assistant("m", [text("秘密回答")], "end_turn", NOW + 1)])
    [turn] = h.source.read_turns(SID, 1)  # the transcript's records carry the excluded cwd
    assert turn.user_text == "" and turn.final_text is None


def test_on_hook_full_reply_from_transcript_when_hook_text_was_cut(harness: Harness) -> None:
    full = "开头" + "长" * (cs.HOOK_TEXT_CAP + 500)
    write_transcript(harness.home, [prompt("写报告", NOW - 30), assistant("m", [text(full)], "end_turn", NOW - 1)])
    harness.source.on_hook(stop_payload(last_assistant_message=full[: cs.HOOK_TEXT_CAP]))
    [event] = harness.turn_events()
    assert event.final_text == full
    assert event.started_at == pytest.approx(NOW - 30)  # poll never saw it busy: taken from the transcript


def test_on_hook_missing_text_taken_from_transcript(harness: Harness) -> None:
    write_transcript(harness.home, [prompt("问", NOW - 3), assistant("m", [text("完整回答")], "end_turn", NOW - 1)])
    harness.source.on_hook(stop_payload(last_assistant_message=None))
    assert harness.turn_events()[-1].final_text == "完整回答"


def test_on_hook_keeps_hook_text_when_transcript_differs_or_is_old(harness: Harness) -> None:
    cut = "甲" * cs.HOOK_TEXT_CAP
    path = write_transcript(harness.home, [prompt("问", NOW - 3), assistant("m", [text("乙" * 30000)], "end_turn", NOW - 1)])
    harness.source.on_hook(stop_payload(last_assistant_message=cut))
    assert harness.turn_events()[-1].final_text == cut
    path.write_text(lines([prompt("老问题", NOW - 5000), assistant("m", [text("老回答")], "end_turn", NOW - 4990)]),
                    encoding="utf-8")
    harness.source.on_hook(stop_payload(last_assistant_message=None))
    event = harness.turn_events()[-1]
    assert event.final_text is None and event.started_at is None  # the transcript shows an older turn


def test_on_hook_short_hook_text_does_not_read_transcript(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    write_registry(harness.home, 100, status="busy", status_ms=int((NOW - 20) * 1000))
    harness.source.poll()
    write_transcript(harness.home, [prompt("问", NOW - 20), assistant("m", [text("答")], "end_turn", NOW - 1)])

    def no_read(*args: Any, **kwargs: Any) -> list:
        raise AssertionError("transcript read not needed")

    monkeypatch.setattr(cs, "read_turns", no_read)
    harness.source.on_hook(stop_payload())
    event = harness.turn_events()[-1]
    assert event.final_text == "最终回复" and event.started_at == pytest.approx(NOW - 20)


def test_poll_registry_dropout_does_not_repeat_alert_or_lose_idle(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs.time, "sleep", lambda seconds: None)
    registry = write_registry(harness.home, 100, status="waiting", waiting_for="dialog open", status_ms=int(NOW * 1000))
    harness.source.poll()
    registry.write_text("{half", encoding="utf-8")  # caught mid-write (both attempts)
    harness.clock.now += 2
    harness.source.poll()
    write_registry(harness.home, 100, status="waiting", waiting_for="dialog open", status_ms=int(NOW * 1000))
    harness.clock.now += 2
    harness.source.poll()
    assert len(harness.attention()) == 1
    write_transcript(harness.home, [prompt("任务", NOW), assistant("m", [text("完成")], "end_turn", NOW + 3)])
    registry.unlink()
    harness.clock.now += 2
    harness.source.poll()
    write_registry(harness.home, 100, status="idle")
    harness.source.poll()
    harness.clock.now += cs.FALLBACK_GRACE_S
    harness.source.poll()
    assert [event.final_text for event in harness.turn_events()] == ["完成"]


def test_poll_forgets_exited_sessions_after_missing_polls(harness: Harness) -> None:
    write_registry(harness.home, 100, status="waiting", status_ms=int(NOW * 1000))
    harness.source.poll()
    (harness.home / "sessions" / "100.json").unlink()
    for _ in range(cs._MISSING_POLLS):
        harness.source.poll()
    assert SID not in harness.source._tracked
    write_registry(harness.home, 100, status="waiting", status_ms=int(NOW * 1000))
    harness.source.poll()
    assert len(harness.attention()) == 2  # a new process waiting again is a new alert


def test_poll_hooked_session_gives_stop_hook_longer(harness: Harness) -> None:
    write_transcript(harness.home, [prompt("任务", NOW), assistant("m", [text("完成了")], "end_turn", NOW + 1)])
    harness.source.on_hook(stop_payload("SessionStart"))  # this session runs the hooks
    write_registry(harness.home, 100, status="busy")
    harness.source.poll()
    write_registry(harness.home, 100, status="idle")
    harness.source.poll()
    harness.clock.now += cs.FALLBACK_GRACE_S
    harness.source.poll()
    assert harness.events == []
    harness.clock.now += 20
    harness.source.on_hook(stop_payload())  # a slow Stop hook still wins
    harness.clock.now += cs.HOOKED_GRACE_S
    harness.source.poll()
    assert [event.final_text for event in harness.turn_events()] == ["最终回复"]


def test_poll_hooked_session_fallback_when_stop_never_comes(harness: Harness) -> None:
    write_transcript(harness.home, [prompt("任务", NOW), assistant("m", [text("完成了")], "end_turn", NOW + 1)])
    harness.source.on_hook(stop_payload("SessionStart"))
    write_registry(harness.home, 100, status="busy")
    harness.source.poll()
    write_registry(harness.home, 100, status="idle")
    harness.source.poll()
    harness.clock.now += cs.HOOKED_GRACE_S
    harness.source.poll()
    assert [event.final_text for event in harness.turn_events()] == ["完成了"]


def test_poll_fallback_busy_again_does_not_push_an_older_turn(harness: Harness) -> None:
    write_transcript(harness.home, [
        prompt("旧任务", NOW - 1000), assistant("m0", [text("旧回答")], "end_turn", NOW - 999),
        prompt("新任务", NOW - 50), assistant("m1", [TOOL], "tool_use", NOW - 40),
    ])
    write_registry(harness.home, 100, status="busy", status_ms=int((NOW - 50) * 1000))
    harness.source.poll()
    write_registry(harness.home, 100, status="idle")  # a blip mid-turn
    harness.source.poll()
    write_registry(harness.home, 100, status="busy", status_ms=int(NOW * 1000))
    harness.clock.now += cs.FALLBACK_GRACE_S
    harness.source.poll()
    assert harness.events == []


def test_sessions_archive_flag_comes_from_newest_meta(home: Path, desk: Path) -> None:
    h = Harness(home, desk)
    write_meta(desk, "local_old", SID, archived=True, last_ms=int((NOW - 500) * 1000))
    write_meta(desk, "local_new", SID, title="继续的会话", last_ms=int((NOW - 5) * 1000))
    write_meta(desk, "local_a", SID2, archived=False, last_ms=int((NOW - 500) * 1000))
    write_meta(desk, "local_b", SID2, archived=True, last_ms=int((NOW - 5) * 1000))
    views = h.source.sessions(10)
    assert [view.id for view in views] == [SID] and views[0].title == "继续的会话"


def test_sessions_reads_titles_only_for_returned_views(home: Path, desk: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    h = Harness(home, desk)
    ids = [f"cli-{index:04d}-0000" for index in range(6)]
    for index, session_id in enumerate(ids):
        path = write_transcript(home, [title_record(f"标题{index}"), prompt("x", NOW)], session_id=session_id)
        os.utime(path, (NOW - 100 + index, NOW - 100 + index))
    scanned: list[str] = []
    real = cs.transcript_title

    def counting(path: Path, **kwargs: Any) -> str:
        scanned.append(path.stem)
        return real(path, **kwargs)

    monkeypatch.setattr(cs, "transcript_title", counting)
    views = h.source.sessions(2)
    assert [view.title for view in views] == ["标题5", "标题4"]
    assert sorted(scanned) == sorted(ids[-2:])


def test_desktop_root_switch_keeps_lookups_working(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roaming = tmp_path / "roaming-switch"
    first = write_meta(roaming / "Claude" / "claude-code-sessions" / "a" / "o", "local_1", SID, title="旧目录")
    os.utime(first, (NOW - 100, NOW - 100))
    monkeypatch.setenv("APPDATA", str(roaming))
    clock = Clock()
    source = cs.ClaudeSource(FakeConfig(), lambda event: None, home=home, secrets=FakeSecrets(), clock=clock,
                             alive=lambda pid, created: False)
    assert source.view(SID).title == "旧目录"  # type: ignore[union-attr]
    local = tmp_path / "local-switch"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    pkg = local / "Packages" / "Claude_x" / "LocalCache" / "Roaming" / "Claude" / "claude-code-sessions" / "a" / "o"
    second = write_meta(pkg, "local_1", SID, title="新目录")
    os.utime(second, (NOW, NOW))
    clock.now += cs.DESKTOP_ROOT_RECHECK_S
    assert source.view(SID).title == "新目录"  # type: ignore[union-attr]


def test_concurrent_reads_and_polls(home: Path, desk: Path) -> None:
    import threading

    h = Harness(home, desk, alive={100, 101})
    write_registry(home, 100, SID, status="busy")
    write_registry(home, 101, SID2, status="waiting")
    write_meta(desk, "local_1", SID)
    write_transcript(home, [prompt("hi", NOW), assistant("m", [text("x")], "end_turn", NOW + 1)])
    errors: list[BaseException] = []

    def worker(step: int) -> None:
        try:
            for index in range(30):
                h.clock.now += 0.5
                if step == 0:
                    h.source.poll()
                elif step == 1:
                    h.source.sessions(10)
                elif step == 2:
                    h.source.view(SID)
                    h.source.permission_mode(SID2)
                else:
                    h.source.on_hook(stop_payload("SessionStart", inbox={"socket": PIPE, "token": f"t{index}"}))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(step,)) for step in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == [] and not any(thread.is_alive() for thread in threads)


# --- mid-turn messages ------------------------------------------------------------------------


def narration(value: str) -> dict[str, Any]:
    return {"type": "thinking", "thinking": value, "signature": thinking_signature("narration")}


def thinking(value: str) -> dict[str, Any]:
    return {"type": "thinking", "thinking": value, "signature": thinking_signature("thinking")}


def _progress(events: list[Any]) -> list[ProgressEvent]:
    return [event for event in events if isinstance(event, ProgressEvent)]


def test_claude_source_reports_new_mid_turn_messages_only(home: Path, desk: Path) -> None:
    harness = Harness(home, desk, alive={100})
    path = write_transcript(home, [prompt("做事", NOW - 100), assistant("m0", [text("旧的进展")], "tool_use", NOW - 90)])
    write_registry(home, 100, status="busy")
    harness.source.poll()
    assert _progress(harness.events) == []  # history is not news
    append(path, [assistant("m1", [text("我先读代码")], "tool_use", NOW - 5), tool_result(NOW - 4),
                  assistant("m2", [narration("请用 demo 账号登录 Figma")], "tool_use", NOW - 3),
                  assistant("m3", [thinking("private")], "tool_use", NOW - 2),
                  assistant("m4", [text("最终答复")], "end_turn", NOW - 1)])
    harness.source.poll()
    (event,) = _progress(harness.events)
    assert (event.agent, event.session_id, event.cwd) == (CLAUDE, SID, CWD)
    assert event.texts == ["我先读代码", "请用 demo 账号登录 Figma"]
    assert event.kind == "narration" and event.asks_user is True
    harness.source.poll()
    assert len(_progress(harness.events)) == 1
    # A record still being written waits for its end of line.
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"type": "assistant", "isSidechain": false, "message": {"stop_reason": "tool_use", "content": '
                     '[{"type": "text", "text": "写到一半')
    harness.source.poll()
    assert len(_progress(harness.events)) == 1
    with path.open("a", encoding="utf-8") as stream:
        stream.write('"}]}}\n')
    harness.source.poll()
    assert _progress(harness.events)[-1].texts == ["写到一半"]


def test_claude_source_sends_nothing_for_excluded_folders(home: Path, desk: Path) -> None:
    harness = Harness(home, desk, alive={100}, cfg=FakeConfig(content_exclude=[CWD]))
    path = write_transcript(home, [prompt("做事", NOW - 100)])
    write_registry(home, 100, status="busy")
    harness.source.poll()
    append(path, [assistant("m1", [text("机密进展")], "tool_use", NOW - 5)])
    harness.source.poll()
    assert _progress(harness.events) == []


def test_claude_progress_checks_the_directory_of_each_record(home: Path, desk: Path) -> None:
    secret = CWD + r"\secret"
    harness = Harness(home, desk, alive={100}, cfg=FakeConfig(content_exclude=[secret]))
    path = write_transcript(home, [prompt("做事", NOW - 100)])
    write_registry(home, 100, status="busy")  # started in CWD, which is allowed
    harness.source.poll()
    moved = assistant("m1", [text("机密文件里写着 X")], "tool_use", NOW - 5)
    moved["cwd"] = secret
    append(path, [moved, assistant("m2", [text("回到项目根目录")], "tool_use", NOW - 4)])
    harness.source.poll()
    (event,) = _progress(harness.events)
    assert event.texts == ["回到项目根目录"] and event.cwd == CWD


def test_claude_progress_skips_what_a_finished_turn_wrote(home: Path, desk: Path) -> None:
    harness = Harness(home, desk, alive={100})
    path = write_transcript(home, [prompt("做事", NOW - 100)])
    write_registry(home, 100, status="busy")
    harness.source.poll()
    append(path, [assistant("m1", [text("再跑一次测试")], "tool_use", NOW - 3),
                  assistant("m2", [text("完成")], "end_turn", NOW - 1)])
    harness.source.on_hook(stop_payload(transcript_path=str(path)))  # the bridge drains hooks before polling
    harness.source.poll()
    assert _progress(harness.events) == []  # the turn's result carries it; no message after the result


def test_claude_progress_drops_long_lines_unread(home: Path, desk: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cs, "PROGRESS_READ_BYTES", 4096)
    harness = Harness(home, desk, alive={100})
    path = write_transcript(home, [prompt("做事", NOW - 100)])
    write_registry(home, 100, status="busy")
    harness.source.poll()
    big = tool_result(NOW - 6)
    big["message"]["content"][0]["content"] = "x" * 20_000
    append(path, [assistant("m1", [text("先看日志")], "tool_use", NOW - 7), big,
                  assistant("m2", [text("日志里有错误")], "tool_use", NOW - 5)])
    harness.source.poll()
    assert _progress(harness.events)[-1].texts == ["先看日志", "日志里有错误"]
    # A long line still being written is dropped as it arrives; the next record still counts.
    with path.open("a", encoding="utf-8") as stream:
        stream.write('{"type": "user", "content": "' + "y" * 10_000)
    harness.source.poll()
    with path.open("a", encoding="utf-8") as stream:
        stream.write('"}\n')
    append(path, [assistant("m3", [text("继续")], "tool_use", NOW - 2)])
    harness.source.poll()
    assert _progress(harness.events)[-1].texts == ["继续"]


def test_claude_progress_keeps_its_place_when_the_registry_is_caught_mid_write(home: Path, desk: Path) -> None:
    harness = Harness(home, desk, alive={100})
    path = write_transcript(home, [prompt("做事", NOW - 100)])
    registry = write_registry(home, 100, status="busy")
    harness.source.poll()
    content = registry.read_text(encoding="utf-8")
    registry.write_text("", encoding="utf-8")  # half written
    append(path, [assistant("m1", [text("这段不能丢")], "tool_use", NOW - 5)])
    harness.source.poll()
    registry.write_text(content, encoding="utf-8")
    harness.source.poll()
    assert _progress(harness.events)[-1].texts == ["这段不能丢"]

from __future__ import annotations

import json
import os
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import CodexHome, completed, delegated, event, started, user_message

from agent_telegram import codex_app_client, codex_threads, codex_watch
from agent_telegram import jobs
from agent_telegram.tgbridge import codex_source as cs
from agent_telegram.tgbridge.model import CODEX, ApprovalEvent, AttentionEvent, ProgressEvent, QuestionEvent, TurnEvent

T1 = "01a07078-0000-7000-8000-0000000000c1"
T2 = "01a07078-0000-7000-8000-0000000000d1"
T3 = "01a0edda-0000-7000-8000-000000000003"
T4 = "01a0edda-0000-7000-8000-000000000004"
STARTED_AT = 1_790_529_000.0  # started_at written by conftest.started()
DONE_AT = 1_790_529_100.0  # completed_at written by conftest.completed()
NOW = DONE_AT - 50  # first poll: between a turn's start and its end
OVERLOADED = {"message": "Selected model is at capacity. Please try a different model.",
              "codex_error_info": "server_overloaded"}
USAGE_LIMIT = {"message": "You've hit your usage limit.", "codex_error_info": "usage_limit_exceeded"}


@dataclass
class FakeConfig:
    """Stands in for tgbridge.config.Config with the attributes CodexSource uses."""

    active_window_h: float = 24.0
    codex_include_automation: bool = False
    codex_anchor_thread: str = ""
    content_exclude: list[str] = field(default_factory=list)

    def excluded(self, cwd: str) -> bool:
        key = os.path.normcase(os.path.normpath(cwd))
        for prefix in self.content_exclude:
            base = os.path.normcase(os.path.normpath(prefix))
            if key == base or key.startswith(base.rstrip("\\/") + os.sep):
                return True
        return False


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Rig:
    """A CodexSource with a fake clock, Codex process, app busy check and codex-watch log directory."""

    def __init__(self, tmp_path: Path, now: float = NOW, cfg: FakeConfig | None = None, **options: Any) -> None:
        self.clock = Clock(now)
        self.events: list[TurnEvent] = []
        self.pid: int | None = 4242
        self.app_busy: bool | None = None
        self.busy_calls: list[str] = []
        self.log_dir = tmp_path / "codex-watch"
        self.cfg = cfg or FakeConfig()
        self.source = cs.CodexSource(self.cfg, self.events.append, main_pid=lambda: self.pid,
                                     busy_check=self._busy, clock=self.clock, watch_log_dir=self.log_dir, **options)

    def _busy(self, thread_id: str) -> bool | None:
        self.busy_calls.append(thread_id)
        return self.app_busy

    def poll(self, at: float | None = None) -> None:
        if at is not None:
            self.clock.now = at
        self.source.poll()

    def plan(self, thread_id: str, turn_id: str, due: float) -> None:
        """What a running codex-watch logs when it plans a continuation."""

        codex_watch.EventLog(self.log_dir).incident("failure_detected", "[demo] 发现网络错误", at=due - 15,
                                                    thread_id=thread_id, turn_id=turn_id, due_at=codex_watch._iso(due))


@pytest.fixture(autouse=True)
def no_live_codex(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Nothing here may reach the real Codex Desktop, its pipes, its logs or spawn a worker."""

    monkeypatch.setattr(codex_app_client, "app_reports_busy", lambda _thread_id, **_options: None)
    monkeypatch.setattr(codex_watch, "codex_main_pid", lambda: None)
    # The fake Codex pid (4242) may belong to a real process; never ask Windows about it.
    monkeypatch.setattr(jobs, "process_creation_time", lambda _pid: None)
    monkeypatch.setenv("CODEX_WATCH_LOG_DIR", str(tmp_path / "default-codex-watch"))
    # Never the real ~/.codex, even in a test that forgets the codex_home fixture (which overrides this).
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex-home"))

    def no_spawn(job_id: str) -> int:
        raise AssertionError(f"unexpected worker spawn for {job_id}")

    def no_session(**_options: Any) -> Any:
        raise AssertionError("unexpected app-tools session")

    def no_pipe(*_args: Any, **_options: Any) -> Any:
        raise AssertionError("unexpected Codex Desktop IPC connection")

    monkeypatch.setattr(jobs, "spawn_worker", no_spawn)
    monkeypatch.setattr(codex_app_client, "open_session", no_session)
    monkeypatch.setattr(cs.codex_ipc, "PipeTransport", no_pipe)
    # Discovery is stepped explicitly in approval tests; old source tests must
    # never accidentally start a background snapshot against the local Desktop.
    monkeypatch.setattr(cs.CodexSource, "_ensure_approval_worker", lambda self: None)
    monkeypatch.setattr(codex_app_client, "availability",
                        lambda: {"node": "node.exe", "app_tools_server": "server.mjs", "pipes": 1})


def finished(turn_id: str, message: str | None, at: float, error: dict[str, Any] | None = None) -> dict[str, Any]:
    return event("task_complete", turn_id, last_agent_message=message, error=error, completed_at=int(at))


def commentary(turn_id: str, text: str) -> dict[str, Any]:
    item = {"type": "AgentMessage", "id": "m", "content": [{"type": "Text", "text": text}], "phase": "commentary"}
    return event("item_completed", turn_id, item=item)


def final_answer(turn_id: str, text: str) -> dict[str, Any]:
    item = {"type": "AgentMessage", "id": "f", "content": [{"type": "Text", "text": text}], "phase": "final_answer"}
    return event("item_completed", turn_id, item=item)


def plan(turn_id: str, text: str) -> dict[str, Any]:
    return event("item_completed", turn_id, item={"type": "Plan", "id": "p", "text": text})


def command(turn_id: str) -> dict[str, Any]:
    item = {"type": "CommandExecution", "id": "c", "command": ["pwsh", "-Command", "ls"], "status": "completed",
            "exit_code": 0}
    return event("item_completed", turn_id, item=item)


def touch(home: CodexHome, thread_id: str, updated: float) -> None:
    with sqlite3.connect(home.db) as connection:
        connection.execute("UPDATE threads SET updated_at = ?, updated_at_ms = ? WHERE id = ?",
                           (int(updated), int(updated * 1000), thread_id))


def set_cwd(home: CodexHome, thread_id: str, cwd: str) -> None:
    with sqlite3.connect(home.db) as connection:
        connection.execute("UPDATE threads SET cwd = ? WHERE id = ?", ("\\\\?\\" + cwd, thread_id))


@pytest.fixture
def running(codex_home: CodexHome) -> Path:
    """T1 is running its first turn when the source starts."""

    return codex_home.add(T1, "demo-p18-v3", [started("t1"), user_message("t1", "修改论文")], updated=int(NOW - 30))


# --- CommentaryCollector ---------------------------------------------------------------


def test_commentary_collector_keeps_commentary_plan_and_commands() -> None:
    collector = cs.CommentaryCollector()
    records = [started("a"), user_message("a", "做事")]
    records += [commentary("a", f"进展 {index}") for index in range(7)]
    records += [final_answer("a", "最终答复不算 commentary"), plan("a", "1. 旧计划"), plan("a", "1. 读代码\n2. 改代码"),
                command("a"), command("a"), completed("a", "完成")]
    collector.feed(records)
    details = collector.details("a")
    assert details == {"commentary": [f"进展 {index}" for index in range(2, 7)], "plan": "1. 读代码\n2. 改代码",
                       "commands": 2}
    assert collector.ordered()[0].final_message == "完成"
    assert collector.details("missing") == {"commentary": [], "plan": "", "commands": 0}

    collector.feed([started("b"), commentary("b", "第二轮"), started("c"), command("c")])
    collector.prune(2)
    assert collector.order == ["b", "c"]
    assert set(collector.commentary) == {"b"} and set(collector.commands) == {"c"} and not collector.plans


# --- turn events -----------------------------------------------------------------------


def test_finished_turn_is_emitted_once_and_the_startup_tail_is_not(codex_home, running, tmp_path) -> None:
    codex_home.add(T2, "旧对话", [started("x"), user_message("x", "旧"), completed("x", "旧回复")], updated=int(NOW - 60))
    rig = Rig(tmp_path)
    rig.poll()
    assert rig.events == []  # T2's finished turn and T1's running turn belong to the startup tail

    codex_home.write(running, [commentary("t1", "正在改"), completed("t1", "论文改好了")])
    rig.poll(NOW + 5)
    rig.poll(NOW + 10)
    (turn,) = rig.events
    assert turn == TurnEvent(agent=CODEX, session_id=T1, title="demo-p18-v3", cwd="C:\\projects\\demo",
                             status="completed", final_text="论文改好了", error="", turn_id="t1",
                             started_at=STARTED_AT, completed_at=DONE_AT, labels=[], quiet=False)
    assert not (rig.log_dir / "watch.log").exists() and not (rig.log_dir / "incidents.jsonl").exists()


@pytest.mark.parametrize(
    ("text", "labels"),
    [
        ("[agent_telegram:atg-260928-120000-codex-abcdef] 以下任务由 Claude 转交", ["agent_telegram 任务"]),
        (codex_threads.AUTO_CONTINUE_MARKER + "上一轮因模型容量不足中断", ["自动继续"]),
        (codex_threads.RESTART_CONTINUE_TEXT + "。", ["自动继续"]),
        ("[tg:7f3a9c] 请继续", ["来自 Telegram"]),
        ("普通消息 [tg:x]", []),
    ],
)
def test_labels_tell_where_the_input_came_from(codex_home, running, tmp_path, text: str,
                                               labels: list[str]) -> None:
    rig = Rig(tmp_path)
    rig.poll()
    codex_home.write(running, [completed("t1", "ok"), started("t2"), delegated("t2", text), completed("t2", "好")])
    rig.poll(NOW + 5)
    assert [(item.turn_id, item.labels) for item in rig.events] == [("t1", []), ("t2", labels)]


def test_failure_codex_watch_will_continue_is_quiet_with_its_time(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.poll()
    due = DONE_AT + 15
    rig.plan(T1, "t1", due)
    codex_home.write(running, [completed("t1", None, OVERLOADED)])
    rig.poll(NOW + 55)
    (turn,) = rig.events
    hhmm = datetime.fromtimestamp(due).strftime("%H:%M")
    assert (turn.status, turn.error, turn.final_text) == ("failed", OVERLOADED["message"], None)
    assert turn.labels == [f"codex-watch 将于 {hhmm} 自动继续"] and turn.quiet is True


def test_resumable_failure_waits_briefly_for_the_plan(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.poll()
    codex_home.write(running, [completed("t1", None, OVERLOADED)])
    rig.poll(DONE_AT + 1)
    assert rig.events == []  # codex-watch has not logged its plan yet
    rig.plan(T1, "t1", DONE_AT + 15)
    rig.poll(DONE_AT + 6)
    (turn,) = rig.events
    assert turn.quiet and turn.labels[0].startswith("codex-watch 将于")
    rig.poll(DONE_AT + 60)
    assert len(rig.events) == 1


def test_resumable_failure_without_a_plan_is_pushed_loudly_after_the_grace(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.poll()
    codex_home.write(running, [completed("t1", None, OVERLOADED)])
    rig.poll(DONE_AT + 1)
    rig.poll(DONE_AT + 1 + cs.FAILURE_GRACE_S - 1)
    assert rig.events == []
    rig.poll(DONE_AT + 1 + cs.FAILURE_GRACE_S)
    (turn,) = rig.events
    assert (turn.labels, turn.quiet) == (["模型容量不足"], False)
    rig.poll(DONE_AT + 100)
    assert len(rig.events) == 1


def test_zero_grace_pushes_at_once(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path, failure_grace_s=0)
    rig.poll()
    codex_home.write(running, [completed("t1", None, OVERLOADED)])
    rig.poll(DONE_AT + 1)
    assert [item.labels for item in rig.events] == [["模型容量不足"]]


def test_other_failures_and_aborted_turns(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.poll()
    codex_home.write(running, [completed("t1", None, USAGE_LIMIT),
                               started("t2"), user_message("t2", "再试"),
                               event("turn_aborted", "t2", reason="interrupted", completed_at=int(DONE_AT + 30))])
    rig.poll(NOW + 5)
    failure, aborted = rig.events
    assert (failure.status, failure.labels, failure.quiet, failure.error) == (
        "failed", ["额度用完"], False, USAGE_LIMIT["message"])
    assert (aborted.status, aborted.error, aborted.completed_at) == ("aborted", "本轮已被中止（interrupted）", DONE_AT + 30)


def test_content_excluded_folder_pushes_metadata_only(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path, cfg=FakeConfig(content_exclude=["c:\\projects\\demo"]))
    rig.poll()
    codex_home.write(running, [completed("t1", "机密审稿意见")])
    rig.poll(NOW + 5)
    (turn,) = rig.events
    assert turn.final_text is None and turn.labels == ["内容不推送"] and turn.title == "demo-p18-v3"


def test_a_failing_emit_does_not_stop_later_events(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path)
    delivered: list[str] = []

    def emit(item: TurnEvent) -> None:
        if item.turn_id == "t1":
            raise RuntimeError("队列已满")
        delivered.append(item.turn_id)

    rig.source.emit = emit
    rig.poll()
    codex_home.write(running, [completed("t1", "一")])
    rig.poll(NOW + 5)
    codex_home.write(running, [started("t2"), user_message("t2", "二"), completed("t2", "二")])
    rig.poll(NOW + 10)
    assert delivered == ["t2"]


def test_emit_runs_without_the_source_lock(codex_home, running, tmp_path) -> None:
    import threading

    rig = Rig(tmp_path)
    listed: list[list[str]] = []

    def emit(_item: TurnEvent) -> None:
        # A command thread can list sessions while the bridge pushes the event.
        worker = threading.Thread(target=lambda: listed.append([view.id for view in rig.source.sessions(5)]))
        worker.start()
        worker.join(timeout=10)

    rig.source.emit = emit
    rig.poll()
    codex_home.write(running, [completed("t1", "ok")])
    rig.poll(NOW + 5)
    assert listed == [[T1]]


def test_follow_only_watcher_never_continues(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path, failure_grace_s=0)
    rig.poll()
    codex_home.write(running, [completed("t1", None, OVERLOADED)])
    for offset in (5, 60, 600):
        rig.poll(DONE_AT + offset)
    assert rig.source.watcher.config.continue_failures is False
    assert all(watch.pending is None and watch.sent is None for watch in rig.source.watcher.watches.values())
    assert rig.busy_calls == [] and len(rig.events) == 1


def test_conversation_tracked_late_reports_turns_since_the_start(codex_home, tmp_path) -> None:
    old = NOW - 3 * 86400
    quiet = codex_home.add(T2, "很久没动", [started("old"), user_message("old", "旧"), finished("old", "旧", old)],
                           updated=int(old))
    rig = Rig(tmp_path)
    rig.poll()
    assert T2 not in rig.source.watcher.watches
    codex_home.write(quiet, [started("new"), user_message("new", "新问题"), completed("new", "很快答完")])
    touch(codex_home, T2, DONE_AT)
    rig.poll(NOW + 5)
    assert rig.events == []  # the watcher re-reads the conversation list every 30 s
    rig.poll(NOW + 40)
    assert [(item.session_id, item.turn_id, item.final_text) for item in rig.events] == [(T2, "new", "很快答完")]
    rig.poll(NOW + 80)
    assert len(rig.events) == 1


def test_automation_runs_are_skipped_unless_configured(tmp_path, monkeypatch) -> None:
    for include in (False, True):
        home = CodexHome(tmp_path / f"home-{include}")
        monkeypatch.setenv("CODEX_HOME", str(home.root))
        rollout = home.add(T3, "每日汇总", [started("a")], updated=int(NOW - 30), thread_source="automation")
        rig = Rig(tmp_path / f"rig-{include}", cfg=FakeConfig(codex_include_automation=include))
        rig.poll()
        home.write(rollout, [completed("a", "汇总完成")])
        rig.poll(NOW + 5)
        assert [item.session_id for item in rig.events] == ([T3] if include else [])
        assert [view.id for view in rig.source.sessions(10)] == ([T3] if include else [])


# --- listing -----------------------------------------------------------------------------


def test_sessions_list_running_first_then_recent(codex_home, tmp_path) -> None:
    codex_home.add(T1, "刚完成", [started("a"), user_message("a", "x"), completed("a", "ok")], updated=int(NOW - 10))
    busy = codex_home.add(T2, "正在跑", [started("b"), user_message("b", "y")], updated=int(NOW - 600))
    codex_home.add(T3, "exec 探针", [], source="exec", updated=int(NOW - 5))
    codex_home.add(T4, "已归档", [], archived=True, updated=int(NOW))
    codex_home.add("01a0edda-0000-7000-8000-00000000000a", "", [], updated=int(NOW),
                   source='{"subagent":{"thread_spawn":{"depth":1}}}', thread_source="subagent")
    rig = Rig(tmp_path)
    rig.poll()
    codex_home.write(busy, [commentary("b", "正在\n读取  代码")])
    rig.poll(NOW + 5)

    views = rig.source.sessions(10)
    assert [view.id for view in views] == [T2, T3, T1]
    running, probe, done = views
    assert (running.state, running.live, running.kind, running.detail) == ("running", True, "desktop", "正在 读取 代码")
    assert (probe.state, probe.live, probe.kind) == ("idle", False, "exec")
    assert (done.state, done.title, done.cwd, done.agent) == ("idle", "刚完成", "C:\\projects\\demo", CODEX)
    assert done.last_activity == NOW - 10 and done.key == f"codex:{T1}"
    assert [view.id for view in rig.source.sessions(1)] == [T2] and rig.source.sessions(0) == []

    rig.pid = None  # Codex Desktop quit: nothing is live, and the open turn cannot be running
    running = rig.source.view(T2)
    assert (running.state, running.live) == ("unknown", False) and "Codex 未运行" in running.detail


def _filetime(epoch: float) -> int:
    return int((epoch + 11_644_473_600) * 10_000_000)


def test_turn_left_open_by_a_codex_restart_is_not_running(codex_home, running, tmp_path, monkeypatch) -> None:
    asked: list[int] = []

    def created(pid: int) -> int:
        asked.append(pid)
        return _filetime(app_started)

    monkeypatch.setattr(jobs, "process_creation_time", created)
    app_started = STARTED_AT + 600  # Codex Desktop restarted after the turn began
    rig = Rig(tmp_path)
    rig.poll()
    view = rig.source.view(T1)
    assert (view.state, view.detail, view.live) == ("idle", "上一轮被 Codex 重启打断", True)
    assert [item.state for item in rig.source.sessions(5)] == ["idle"]
    assert asked == [4242]  # the start time is cached per pid

    rig.pid = 5151  # a new Codex process that is older than the turn: the turn may run
    app_started = STARTED_AT - 60
    assert rig.source.view(T1).state == "running" and asked == [4242, 5151]

    monkeypatch.setattr(jobs, "process_creation_time", lambda _pid: None)
    rig.pid = 6161  # start time unknown: trust the rollout
    assert rig.source.view(T1).state == "running"


def test_poll_errors_reach_the_caller_after_the_ready_events(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path)
    broken = {"on": True}
    real = rig.source._load_all

    def flaky() -> list[codex_threads.ThreadInfo]:
        if broken["on"]:
            raise RuntimeError("数据库被锁")
        return real()

    rig.source._load_all = flaky
    with pytest.raises(RuntimeError, match="数据库被锁"):  # the bridge's poll loop logs it
        rig.poll()
    assert rig.source.last_error == "RuntimeError: 数据库被锁" and rig.source.watcher.watches == {}
    broken["on"] = False
    rig.poll(NOW + 1)
    assert rig.source.last_error is None and T1 in rig.source.watcher.watches

    codex_home.write(running, [completed("t1", None, OVERLOADED)])
    rig.poll(DONE_AT + 1)  # held back while codex-watch may still log its plan
    assert rig.events == []
    broken["on"] = True
    with pytest.raises(RuntimeError):
        rig.poll(DONE_AT + 40)  # the list refresh fails, the grace is over
    assert [(item.turn_id, item.labels) for item in rig.events] == [("t1", ["模型容量不足"])]


def test_session_detail_for_failures_and_excluded_folders(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path, cfg=FakeConfig(content_exclude=["C:\\projects\\demo"]))
    rig.poll()
    codex_home.write(running, [commentary("t1", "机密进展")])
    rig.poll(NOW + 5)
    assert rig.source.view(T1).detail == ""  # commentary of an excluded folder never shows
    codex_home.write(running, [completed("t1", None, USAGE_LIMIT)])
    rig.poll(NOW + 10)
    assert rig.source.view(T1).detail == "上一轮失败：额度用完"
    assert rig.source.view(T2) is None


# --- reading -------------------------------------------------------------------------------


def _history(codex_home: CodexHome) -> Path:
    return codex_home.add(T1, "demo", [
        started("a"), user_message("a", "第一轮"), commentary("a", "看一下"), completed("a", "第一轮完成"),
        started("b"), user_message("b", "第二轮"), plan("b", "1. 改\n2. 测"), command("b"), command("b"),
        commentary("b", "改好了，跑测试"), completed("b", None, OVERLOADED),
        started("c"), user_message("c", "第三轮"), commentary("c", "进行中"),
    ])


def test_read_turns_returns_the_last_turns_with_details(codex_home, tmp_path) -> None:
    _history(codex_home)
    rig = Rig(tmp_path)
    turns = rig.source.read_turns(T1, 2)
    assert [turn["turn_id"] for turn in turns] == ["b", "c"]
    second, third = turns
    assert second == {
        "turn_id": "b", "status": "failed", "user_text": "第二轮", "final_message": None,
        "error": OVERLOADED["message"], "error_info": "server_overloaded", "abort_reason": "",
        "commentary": ["改好了，跑测试"], "plan": "1. 改\n2. 测", "commands": 2,
        "started_at": STARTED_AT, "completed_at": DONE_AT, "content_excluded": False,
    }
    assert (third["status"], third["commentary"], third["completed_at"]) == ("running", ["进行中"], None)
    assert len(rig.source.read_turns(T1, 50)) == 3 and rig.source.read_turns(T2, 3) == []


def test_read_turns_widens_the_tail_until_it_holds_the_turns(codex_home, tmp_path, monkeypatch) -> None:
    big = {"timestamp": "2026-09-28T00:05:00Z", "type": "response_item",
           "payload": {"type": "custom_tool_call_output", "output": "x" * 5000}}
    records = []
    for index in range(4):
        records += [started(f"t{index}"), user_message(f"t{index}", f"第 {index} 轮"), big, completed(f"t{index}", f"{index}")]
    codex_home.add(T1, "demo", records)
    monkeypatch.setattr(cs, "READ_START_BYTES", 2000)
    rig = Rig(tmp_path)
    assert [turn["user_text"] for turn in rig.source.read_turns(T1, 3)] == ["第 1 轮", "第 2 轮", "第 3 轮"]
    monkeypatch.setattr(cs, "READ_MAX_BYTES", 6000)
    assert [turn["turn_id"] for turn in rig.source.read_turns(T1, 3)] == ["t2", "t3"]


def test_full_text_and_content_exclusion(codex_home, tmp_path) -> None:
    rollout = _history(codex_home)
    rig = Rig(tmp_path)
    assert rig.source.full_text(T1) is None  # the newest finished turn failed without an answer
    codex_home.write(rollout, [completed("c", "第三轮的完整回复" * 500)])
    assert rig.source.full_text(T1) == "第三轮的完整回复" * 500
    codex_home.write(rollout, [started("d"), user_message("d", "第四轮")])
    assert rig.source.full_text(T1) == "第三轮的完整回复" * 500  # a running turn is skipped
    assert rig.source.full_text(T2) is None

    set_cwd(codex_home, T1, "C:\\secret\\review")
    rig.cfg.content_exclude = ["C:\\secret"]
    assert rig.source.full_text(T1) is None
    (turn,) = rig.source.read_turns(T1, 1)
    assert turn["content_excluded"] and turn["user_text"] == "" and turn["final_message"] is None
    assert turn["commentary"] == [] and turn["status"] == "running"


def test_busy_asks_the_app_only_when_the_rollout_is_not_idle(codex_home, tmp_path) -> None:
    rollout = codex_home.add(T1, "demo", [started("a"), user_message("a", "x"), completed("a", "ok")])
    rig = Rig(tmp_path)
    assert rig.source.busy(T1) is False and rig.busy_calls == []
    codex_home.write(rollout, [started("b"), user_message("b", "y")])
    assert rig.source.busy(T1) is True and rig.busy_calls == [T1]  # the app does not know
    rig.app_busy = False
    assert rig.source.busy(T1) is False  # e.g. a turn cut short by an app restart
    rig.app_busy = True
    assert rig.source.busy(T1) is True
    assert rig.source.busy(T2) is None


def test_busy_is_unknown_when_neither_the_rollout_nor_the_app_can_tell(codex_home, tmp_path, monkeypatch) -> None:
    codex_home.add(T1, "demo", [])
    # No turn event within the rollout's read limit (multi-GB tool outputs).
    monkeypatch.setattr(codex_threads, "thread_activity", lambda _info: {"busy": None, "rollout_found": True})
    rig = Rig(tmp_path)
    assert rig.source.busy(T1) is None and rig.busy_calls == [T1]
    rig.app_busy = True
    assert rig.source.busy(T1) is True


# --- sending -----------------------------------------------------------------------------


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def spawn(job_id: str) -> int:
        calls.append(job_id)
        return 12345

    monkeypatch.setattr(jobs, "spawn_worker", spawn)
    return calls


def test_submit_send_starts_a_raw_codex_job(codex_home, tmp_path, spawned) -> None:
    codex_home.add(T1, "demo-p18-v3", [])
    rig = Rig(tmp_path)
    job_id = rig.source.submit_send(T1, "请继续上一步", nonce="[tg:7f3a9c]")
    assert spawned == [job_id]
    request = jobs.load_request(job_id)
    assert request["kind"] == "codex"
    assert request["params"] == {"thread_id": T1, "thread_name": "demo-p18-v3", "message": "请继续上一步",
                                 "raw": True, "nonce": "[tg:7f3a9c]", "if_busy": "wait", "timeout_s": 4 * 3600}
    view = rig.source.job(job_id)
    assert view["state"]["status"] == "queued" and view["result"] is None

    with pytest.raises(ValueError, match=f"作业 {job_id} 正在使用"):
        rig.source.submit_send(T1, "再来一条", nonce="[tg:000001]")
    jobs.write_result(job_id, {"ok": True, "status": "succeeded", "final_message": "好了"})
    jobs.update_state(job_id, status="succeeded", phase="done")
    assert rig.source.job(job_id)["result"]["final_message"] == "好了"
    second = rig.source.submit_send(T1, "再来一条", nonce="[tg:000001]", if_busy="send")
    assert jobs.load_request(second)["params"]["if_busy"] == "send"


def test_submit_send_refuses_what_it_cannot_deliver(codex_home, tmp_path, spawned, monkeypatch) -> None:
    codex_home.add(T1, "demo", [])
    codex_home.add(T2, "exec 探针", [], source="exec")
    codex_home.add(T3, "已归档", [], archived=True)
    rig = Rig(tmp_path)
    cases = [
        ((T1, "  "), {"nonce": "[tg:1]"}, "不能为空"),
        ((T1, "x" * (cs.MAX_SEND_CHARS + 1)), {"nonce": "[tg:1]"}, "太长"),
        ((T1, "hi"), {"nonce": ""}, "nonce"),
        ((T1, "hi"), {"nonce": "[tg: 1]"}, "nonce"),
        ((T1, "hi"), {"nonce": "[tg:1]", "if_busy": "later"}, "if_busy"),
        ((T2, "hi"), {"nonce": "[tg:1]"}, "不是 Codex Desktop 对话"),
        ((T3, "hi"), {"nonce": "[tg:1]"}, "已归档"),
        ((T4, "hi"), {"nonce": "[tg:1]"}, "找不到"),
    ]
    for args, options, message in cases:
        with pytest.raises(ValueError, match=message):
            rig.source.submit_send(*args, **options)
    monkeypatch.setattr(codex_app_client, "availability",
                        lambda: {"node": "node.exe", "app_tools_server": "server.mjs", "pipes": 0})
    with pytest.raises(ValueError, match="管道"):
        rig.source.submit_send(T1, "hi", nonce="[tg:1]")
    monkeypatch.setattr(codex_app_client, "availability", lambda: {"node": None, "app_tools_server": None, "pipes": 3})
    with pytest.raises(ValueError, match="app-tools 组件"):
        rig.source.submit_send(T1, "hi", nonce="[tg:1]")
    assert spawned == [] and jobs.list_jobs(10) == []


def test_concurrent_sends_to_one_conversation_start_one_job(codex_home, tmp_path, spawned, monkeypatch) -> None:
    import threading
    import time

    codex_home.add(T1, "demo", [])
    rig = Rig(tmp_path)
    real_list = jobs.list_jobs

    def slow_list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        found = real_list(*args, **kwargs)
        time.sleep(0.3)  # both confirmations arrive while the first one is still checking
        return found

    monkeypatch.setattr(jobs, "list_jobs", slow_list)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def send(nonce: str) -> None:
        barrier.wait()
        try:
            outcomes.append(rig.source.submit_send(T1, "hi", nonce=nonce))
        except ValueError as exc:
            outcomes.append(f"refused: {exc}")

    workers = [threading.Thread(target=send, args=(f"[tg:{index}]",)) for index in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)
    assert len(spawned) == 1
    assert sorted(item.startswith("refused") for item in outcomes) == [False, True]


def test_submit_send_marks_the_job_failed_when_the_worker_cannot_start(codex_home, tmp_path, monkeypatch) -> None:
    codex_home.add(T1, "demo", [])

    def broken(_job_id: str) -> int:
        raise OSError("CreateProcess failed")

    monkeypatch.setattr(jobs, "spawn_worker", broken)
    with pytest.raises(OSError, match="CreateProcess"):
        Rig(tmp_path).source.submit_send(T1, "hi", nonce="[tg:1]")
    (item,) = jobs.list_jobs(10)
    assert (item["status"], item["phase"]) == ("failed", "spawn_failed")


# --- new tasks -----------------------------------------------------------------------------


class FakeSession:
    """Records app-tools calls; answers each tool with the text given for it."""

    def __init__(self, answers: dict[str, str], calls: list[dict[str, Any]], thread_id: str | None) -> None:
        self.answers = answers
        self.calls = calls
        self.opened_for = thread_id

    def __enter__(self) -> FakeSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.calls.append({"closed": True})

    def call_tool(self, name: str, arguments: dict[str, Any], *, thread_id: str | None = None,
                  timeout_s: float | None = None) -> dict[str, Any]:
        self.calls.append({"name": name, "arguments": arguments, "thread_id": thread_id, "opened_for": self.opened_for})
        return {"content": [{"type": "text", "text": self.answers[name]}]}


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, str], list[dict[str, Any]]]:
    answers: dict[str, str] = {}
    calls: list[dict[str, Any]] = []

    def open_session(*, thread_id: str | None = None, timeout_s: float = 30.0) -> FakeSession:
        return FakeSession(answers, calls, thread_id)

    monkeypatch.setattr(codex_app_client, "open_session", open_session)
    return answers, calls


def test_anchor_thread_is_configured_or_the_newest_desktop_conversation(codex_home, tmp_path) -> None:
    codex_home.add(T1, "Telegram 控制台", [], updated=int(NOW - 600))
    codex_home.add(T2, "最近的对话", [], updated=int(NOW - 60))
    codex_home.add(T3, "exec 探针", [], source="exec", updated=int(NOW))
    codex_home.add(T4, "定时任务", [], thread_source="automation", updated=int(NOW))
    assert Rig(tmp_path).source.anchor_thread() == T2
    assert Rig(tmp_path, cfg=FakeConfig(codex_anchor_thread="Telegram 控制台")).source.anchor_thread() == T1
    assert Rig(tmp_path, cfg=FakeConfig(codex_anchor_thread=T3)).source.anchor_thread() == T3
    assert Rig(tmp_path, cfg=FakeConfig(codex_anchor_thread="不存在")).source.anchor_thread() == T2


def test_a_stale_anchor_uuid_falls_back(codex_home, tmp_path) -> None:
    codex_home.add(T1, "已归档的锚点", [], archived=True, updated=int(NOW))
    codex_home.add(T2, "最近的对话", [], updated=int(NOW - 60))
    # An archived or deleted anchor would fail the app's read_thread probe on every pipe.
    assert Rig(tmp_path, cfg=FakeConfig(codex_anchor_thread=T1)).source.anchor_thread() == T2
    assert Rig(tmp_path, cfg=FakeConfig(codex_anchor_thread=T4)).source.anchor_thread() == T2
    assert Rig(tmp_path, cfg=FakeConfig(codex_anchor_thread=T2.upper())).source.anchor_thread() == T2


def test_list_projects_normalizes_the_app_answer(codex_home, tmp_path, app) -> None:
    answers, calls = app
    codex_home.add(T1, "锚点", [], updated=int(NOW))
    answers["list_projects"] = json.dumps({"projects": [
        {"projectId": "p-local", "name": "demo-app", "path": "C:\\proj\\demo-app",
         "type": "local", "isGitRepository": True},
        {"id": "p-2", "path": "C:\\projects\\paper"},
        {"name": "没有 id"},
    ]})
    projects = Rig(tmp_path).source.list_projects()
    assert [(item["id"], item["name"], item["path"], item["kind"], item["git"]) for item in projects] == [
        ("p-local", "demo-app", "C:\\proj\\demo-app", "local", True),
        ("p-2", "paper", "C:\\projects\\paper", "", None),
    ]
    assert calls[0] == {"name": "list_projects", "arguments": {}, "thread_id": T1, "opened_for": T1}
    assert calls[-1] == {"closed": True}


def test_list_projects_accepts_grouped_or_plain_lists(codex_home, tmp_path, app) -> None:
    answers, _calls = app
    codex_home.add(T1, "锚点", [], updated=int(NOW))
    rig = Rig(tmp_path)
    answers["list_projects"] = json.dumps({"local": [{"projectId": "a"}], "chatgpt": [{"projectId": "b"}], "note": "x"})
    assert [item["id"] for item in rig.source.list_projects()] == ["a", "b"]
    answers["list_projects"] = json.dumps([{"projectId": "c", "name": "C"}])
    assert [item["name"] for item in rig.source.list_projects()] == ["C"]
    answers["list_projects"] = "No projects."
    assert rig.source.list_projects() == []


def test_create_thread_builds_the_target(codex_home, tmp_path, app) -> None:
    answers, calls = app
    codex_home.add(T1, "锚点", [], updated=int(NOW))
    rig = Rig(tmp_path)
    answers["create_thread"] = json.dumps({"threadId": T4, "hostId": "local"})
    result = rig.source.create_thread("修一下测试", project_id="p-local", title=" 修测试 ")
    assert result == {"ok": True, "thread_id": T4, "host_id": "local", "result": {"threadId": T4, "hostId": "local"}}
    assert calls[0]["arguments"] == {"prompt": "修一下测试", "target": {"type": "project", "projectId": "p-local"},
                                     "title": "修测试"}
    assert calls[0]["thread_id"] == T1

    answers["create_thread"] = json.dumps({"thread": {"id": T3}})
    result = rig.source.create_thread("随便聊聊", project_id=None, title="  ")
    assert result["thread_id"] == T3 and result["host_id"] is None
    assert calls[2]["arguments"] == {"prompt": "随便聊聊", "target": {"type": "projectless"}}

    answers["create_thread"] = "Created."
    assert rig.source.create_thread("x", project_id=None, title=None) == {
        "ok": True, "thread_id": None, "host_id": None, "result": "Created."}
    with pytest.raises(ValueError, match="提示词"):
        rig.source.create_thread("  ", project_id=None, title=None)


def test_new_tasks_need_an_anchor(codex_home, tmp_path, app) -> None:
    codex_home.add(T3, "exec 探针", [], source="exec")
    with pytest.raises(ValueError, match="锚点"):
        Rig(tmp_path).source.list_projects()
    assert app[1] == []


def test_app_tool_errors_propagate(codex_home, tmp_path, monkeypatch) -> None:
    codex_home.add(T1, "锚点", [], updated=int(NOW))

    def refuse(**_options: Any) -> Any:
        raise codex_app_client.AppToolsError("没有找到可用的 Codex app-tools 管道")

    monkeypatch.setattr(codex_app_client, "open_session", refuse)
    with pytest.raises(codex_app_client.AppToolsError, match="管道"):
        Rig(tmp_path).source.create_thread("x", project_id=None, title=None)


def test_thread_target() -> None:
    assert cs.thread_target("p") == {"type": "project", "projectId": "p"}
    assert cs.thread_target(None) == cs.thread_target("") == {"type": "projectless"}


# --- mid-turn commentary -------------------------------------------------------------


def _progress(events: list[Any]) -> list[ProgressEvent]:
    return [event for event in events if isinstance(event, ProgressEvent)]


def test_codex_commentary_of_running_turns_is_reported(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.poll()
    assert _progress(rig.events) == []
    codex_home.write(running, [commentary("t1", "正在改第一节")])
    rig.poll(NOW + 5)
    (event,) = _progress(rig.events)
    assert (event.agent, event.session_id, event.texts, event.kind) == (CODEX, T1, ["正在改第一节"], "commentary")
    assert event.asks_user is False
    rig.poll(NOW + 10)
    codex_home.write(running, [commentary("t1", "需要你确认一下目录"), commentary("t1", "继续改")])
    rig.poll(NOW + 15)
    assert _progress(rig.events)[-1].texts == ["需要你确认一下目录", "继续改"] and _progress(rig.events)[-1].asks_user


ASYNC_CALL = "call_async"
ASYNC_TITLE = "要继续吗？"
ASYNC_ASKED_AT = STARTED_AT + 40


def async_question(turn_id: str, text: str = ASYNC_TITLE, *, call_id: str = ASYNC_CALL,
                   questions: list[dict[str, Any]] | None = None, at: float = ASYNC_ASKED_AT) -> dict[str, Any]:
    item = {"type": "AgentMessage", "id": call_id, "content": [{"type": "Text", "text": text}],
            "phase": "final_answer", "delivery": "async",
            "questions": questions if questions is not None else [{"title": text, "options": ["是", "否"]}]}
    record = event("item_completed", turn_id, item=item)
    record["timestamp"] = _stamp(at)
    return record


def async_reply(call_id: str = ASYNC_CALL, answers: dict[int, tuple[str, str]] | None = None) -> str:
    """A synthetic Desktop reply: keep the serialized original question indices in its envelope."""

    if answers is None:
        answers = {0: (ASYNC_TITLE, "是")}
    entries = [{"questionItemId": json.dumps([codex_threads.ASYNC_INPUT_TOOL, call_id, index]),
                "question": title, "answer": answer} for index, (title, answer) in answers.items()]
    return "<send_user_message_question_reply>\n" + json.dumps(entries, ensure_ascii=False) + (
        "\n</send_user_message_question_reply>")


def desktop_async_reply(turn_id: str, *, client_id: str | None = "desktop-client-1",
                        call_id: str = ASYNC_CALL, answers: dict[int, tuple[str, str]] | None = None) -> dict[str, Any]:
    record = user_message(turn_id, async_reply(call_id, answers))
    item = record["payload"]["item"]
    item["content"][0]["text_elements"] = []
    if client_id is not None:
        item["client_id"] = client_id
    return record


@pytest.fixture
def waiting_async(codex_home, running, tmp_path) -> Rig:
    rig = Rig(tmp_path)
    rig.poll()
    codex_home.write(running, [async_question("t1")])
    rig.poll(NOW + 5)
    return rig


def test_codex_async_questions_are_pushed_as_questions(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.poll()
    codex_home.write(running, [commentary("t1", "仓库有未提交改动"), async_question("t1", "要先暂存再拉取吗？\n- 是\n- 否")])
    rig.poll(NOW + 5)
    (progress_event,) = _progress(rig.events)
    assert progress_event.kind == "commentary" and progress_event.texts == ["仓库有未提交改动"]
    (asked,) = _questions(rig.events)
    assert (asked.mode, asked.request_id, asked.turn_id, asked.closed) == ("async", ASYNC_CALL, "t1", False)
    assert [(question.id, question.text, question.options) for question in asked.questions] == [
        ("0", "要先暂存再拉取吗？\n- 是\n- 否", [("是", ""), ("否", "")])]
    assert asked.answered == asked.answer_sources == {}
    codex_home.write(running, [commentary("t1", "继续检查目录")])
    rig.poll(NOW + 7)
    assert _progress(rig.events)[-1].texts == ["继续检查目录"]
    assert len(_questions(rig.events)) == 1
    codex_home.write(running, [completed("t1", "最终答复")])
    rig.poll(NOW + 10)
    rig.poll(NOW + 15)
    (turn,) = [item for item in rig.events if isinstance(item, TurnEvent)]
    assert turn.final_text == "最终答复"  # the async question is not the turn's answer
    assert len(_questions(rig.events)) == 1  # an ended turn's async question still waits


def test_async_questions_do_not_depend_on_progress_enabled(codex_home, running, tmp_path) -> None:
    cfg = FakeConfig()
    cfg.progress_enabled = False
    rig = Rig(tmp_path, cfg=cfg)
    rig.poll()
    codex_home.write(running, [commentary("t1", "普通进展"), async_question("t1")])
    rig.poll(NOW + 5)
    assert _progress(rig.events) == []
    assert [(item.request_id, item.mode) for item in _questions(rig.events)] == [(ASYNC_CALL, "async")]


def test_async_questions_preserve_titles_options_and_original_indices() -> None:
    raw = [{"title": "  保留首尾空格  ", "options": None}, {"title": None}, {"title": ""},
           {"title": "第二题", "options": [None, "", "  ", "  选择文字  ", "好"]},
           {"title": " ", "options": []}, {"title": "没有 options"}]
    assert [(item.id, item.text, item.options) for item in cs.parse_async_questions(raw)] == [
        ("0", "  保留首尾空格  ", []), ("3", "第二题", [("  选择文字  ", ""), ("好", "")]),
        ("4", " ", []), ("5", "没有 options", [])]
    assert cs.parse_async_questions(None) == cs.parse_async_questions(True) == []


def test_async_question_with_no_options_is_reported_verbatim(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.poll()
    title = "  按原样回答这个问题  "
    codex_home.write(running, [async_question("t1", title, questions=[{"title": title, "options": None}])])
    rig.poll(NOW + 5)
    (asked,) = _questions(rig.events)
    assert [(question.id, question.text, question.options) for question in asked.questions] == [("0", title, [])]


def test_async_answer_in_running_turn_closes_with_desktop_source(codex_home, running, waiting_async) -> None:
    rig = waiting_async
    assert rig.source.async_question_state(T1, ASYNC_CALL) == {
        "turn_id": "t1", "turn_running": True, "turn_last": True, "thread_busy": True,
        "answered": {}, "answer_sources": {}, "waiting": True}
    codex_home.write(running, [desktop_async_reply("t1")])
    rig.poll(NOW + 10)
    opened, closed = _questions(rig.events)
    assert opened.answered == opened.answer_sources == {}  # emitted snapshots do not change in place
    assert (closed.mode, closed.closed, closed.by, closed.questions) == ("async", True, "answered", [])
    assert closed.answer == f"{ASYNC_TITLE}: 是"
    assert closed.answered == {0: "是"} and closed.answer_sources == {0: "client:desktop-client-1"}
    state = rig.source.async_question_state(T1, ASYNC_CALL)
    assert state == {"turn_id": "t1", "turn_running": True, "turn_last": True, "thread_busy": True,
                     "answered": {0: "是"}, "answer_sources": {0: "client:desktop-client-1"}, "waiting": False}
    state["answered"][0] = "修改返回值"
    assert rig.source.async_question_state(T1, ASYNC_CALL)["answered"] == {0: "是"}
    rig.poll(NOW + 15)
    assert len(_questions(rig.events)) == 2


@pytest.mark.parametrize("aborted", [False, True])
def test_async_answer_as_first_input_of_new_turn_is_not_superseded(codex_home, running, waiting_async,
                                                                 aborted: bool) -> None:
    rig = waiting_async
    ended = event("turn_aborted", "t1", reason="interrupted") if aborted else completed("t1", "稍后再答")
    codex_home.write(running, [ended])
    rig.poll(NOW + 10)
    assert len(_questions(rig.events)) == 1
    assert rig.source.async_question_state(T1, ASYNC_CALL) == {
        "turn_id": "t1", "turn_running": False, "turn_last": True, "thread_busy": False,
        "answered": {}, "answer_sources": {}, "waiting": True}
    codex_home.write(running, [started("t2"), desktop_async_reply("t2")])
    rig.poll(NOW + 15)
    closed = _questions(rig.events)[-1]
    assert closed.closed and closed.by == "answered" and closed.answered == {0: "是"}
    assert closed.answer_sources == {0: "client:desktop-client-1"}
    state = rig.source.async_question_state(T1, ASYNC_CALL)
    assert (state["turn_running"], state["turn_last"], state["thread_busy"], state["waiting"]) == (
        False, False, True, False)


def test_async_answer_after_turn_via_delegation_keeps_telegram_marker(codex_home, running, waiting_async) -> None:
    rig = waiting_async
    codex_home.write(running, [completed("t1", "稍后再答")])
    rig.poll(NOW + 10)
    answer = "选 <A> & 保留原文"
    envelope = async_reply(answers={0: (ASYNC_TITLE, answer)})
    codex_home.write(running, [started("t2"), delegated("t2", "[tg:AbC_1-x] " + envelope)])
    rig.poll(NOW + 15)
    closed = _questions(rig.events)[-1]
    assert closed.closed and closed.by == "answered" and closed.answer == f"{ASYNC_TITLE}: {answer}"
    assert closed.answered == {0: answer} and closed.answer_sources == {0: "tg:AbC_1-x"}


@pytest.mark.parametrize("via_delegation", [False, True])
def test_async_answer_without_a_source_has_an_empty_source(codex_home, running, waiting_async,
                                                          via_delegation: bool) -> None:
    record = (delegated("t1", async_reply()) if via_delegation else desktop_async_reply("t1", client_id=None))
    codex_home.write(running, [record])
    waiting_async.poll(NOW + 10)
    closed = _questions(waiting_async.events)[-1]
    assert closed.by == "answered" and closed.answer_sources == {0: ""}


def test_async_question_is_superseded_by_a_new_turn_with_ordinary_input(codex_home, running, waiting_async) -> None:
    rig = waiting_async
    codex_home.write(running, [started("t2"), user_message("t2", "换个话题")])
    rig.poll(NOW + 10)
    closed = _questions(rig.events)[-1]
    assert closed.closed and closed.by == "superseded" and closed.answer == ""
    assert rig.source.async_question_state(T1, ASYNC_CALL)["waiting"] is False


def test_async_question_survives_new_turn_without_input_and_auto_continue(codex_home, running, waiting_async) -> None:
    rig = waiting_async
    codex_home.write(running, [completed("t1", "稍后再答"), started("t2")])
    rig.poll(NOW + 10)
    assert len(_questions(rig.events)) == 1
    state = rig.source.async_question_state(T1, ASYNC_CALL)
    assert (state["turn_running"], state["turn_last"], state["thread_busy"], state["waiting"]) == (
        False, False, True, True)
    codex_home.write(running, [user_message("t2", codex_threads.AUTO_CONTINUE_MARKER + "请继续")])
    rig.poll(NOW + 15)
    assert len(_questions(rig.events)) == 1 and rig.source.async_question_state(T1, ASYNC_CALL)["waiting"]
    codex_home.write(running, [completed("t2", "已继续"), started("t3"), user_message("t3", "现在换个话题")])
    rig.poll(NOW + 20)
    assert _questions(rig.events)[-1].by == "superseded"  # examine all later turns, not just the first


def test_partial_async_answer_stays_open_and_restart_reports_only_unanswered(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.poll()
    codex_home.write(running, [async_question("t1", questions=[{"title": "第一题"}, {"title": "第二题"}])])
    rig.poll(NOW + 5)
    codex_home.write(running, [desktop_async_reply("t1", answers={1: ("第二题", "只答第二题")})])
    rig.poll(NOW + 10)
    rig.poll(NOW + 12)
    assert len(_questions(rig.events)) == 1  # waiting calls keep the same card until a new report is needed
    assert rig.source.async_question_state(T1, ASYNC_CALL) == {
        "turn_id": "t1", "turn_running": True, "turn_last": True, "thread_busy": True,
        "answered": {1: "只答第二题"}, "answer_sources": {1: "client:desktop-client-1"}, "waiting": True}
    fresh = Rig(tmp_path / "fresh-source", now=NOW + 15)
    fresh.poll()
    (asked,) = _questions(fresh.events)
    assert (asked.mode, asked.closed) == ("async", False)
    assert [(item.id, item.text) for item in asked.questions] == [("0", "第一题")]
    assert asked.answered == {1: "只答第二题"} and asked.answer_sources == {1: "client:desktop-client-1"}
    codex_home.write(running, [desktop_async_reply("t1", answers={0: ("第一题", "再答第一题")})])
    fresh.poll(NOW + 20)
    closed = _questions(fresh.events)[-1]
    assert closed.by == "answered" and closed.answer == "第一题: 再答第一题\n第二题: 只答第二题"


def test_partial_async_answer_in_a_new_turn_does_not_supersede(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.poll()
    codex_home.write(running, [async_question("t1", questions=[{"title": "第一题"}, {"title": "第二题"}]),
                               completed("t1", "稍后再答"), started("t2"),
                               desktop_async_reply("t2", answers={1: ("第二题", "只答第二题")})])
    rig.poll(NOW + 5)
    (asked,) = _questions(rig.events)
    assert not asked.closed and [(question.id, question.text) for question in asked.questions] == [("0", "第一题")]
    assert rig.source.async_question_state(T1, ASYNC_CALL)["waiting"]


def test_async_function_call_accepted_output_does_not_close_question(codex_home, running, waiting_async) -> None:
    codex_home.write(running, [{"timestamp": _stamp(NOW + 6), "type": "response_item", "payload": {
        "type": "function_call_output", "call_id": ASYNC_CALL, "output": '{"accepted":true}'}}])
    waiting_async.poll(NOW + 10)
    assert len(_questions(waiting_async.events)) == 1
    assert waiting_async.source.async_question_state(T1, ASYNC_CALL)["waiting"]


def test_async_question_closes_when_thread_is_no_longer_watched(codex_home, waiting_async) -> None:
    rig = waiting_async
    with sqlite3.connect(codex_home.db) as connection:
        connection.execute("UPDATE threads SET archived = 1 WHERE id = ?", (T1,))
    rig.poll(NOW + 40)  # refresh the watcher's conversation list
    closed = _questions(rig.events)[-1]
    assert closed.closed and closed.by == "gone" and closed.answer == ""
    assert rig.source.async_question_state(T1, ASYNC_CALL) is None


def test_async_question_expires_after_six_hours(waiting_async) -> None:
    rig = waiting_async
    rig.poll(ASYNC_ASKED_AT + 6 * 3600 - 1)
    assert len(_questions(rig.events)) == 1
    rig.poll(ASYNC_ASKED_AT + 6 * 3600 + 1)
    closed = _questions(rig.events)[-1]
    assert closed.closed and closed.by == "expired" and closed.answer == ""
    assert rig.source.async_question_state(T1, ASYNC_CALL)["waiting"] is False


def test_async_question_closes_after_a_settled_desktop_restart(waiting_async, monkeypatch) -> None:
    rig = waiting_async
    starts = {4242: STARTED_AT - 600, 5151: NOW + 10}
    monkeypatch.setattr(jobs, "process_creation_time", lambda pid: _filetime(starts[pid]))
    rig.pid = 5151
    for step in range(cs.DESKTOP_SETTLE_POLLS - 1):
        rig.poll(NOW + 20 + step)
        assert len(_questions(rig.events)) == 1
    rig.poll(NOW + 20 + cs.DESKTOP_SETTLE_POLLS)
    closed = _questions(rig.events)[-1]
    assert closed.closed and closed.by == "restart" and closed.answer == ""


def test_async_question_closes_after_desktop_is_gone(waiting_async) -> None:
    rig = waiting_async
    rig.pid = None
    for step in range(cs.DESKTOP_SETTLE_POLLS - 1):
        rig.poll(NOW + 10 + step)
        assert len(_questions(rig.events)) == 1
    rig.poll(NOW + 10 + cs.DESKTOP_SETTLE_POLLS)
    closed = _questions(rig.events)[-1]
    assert closed.closed and closed.by == "desktop_gone" and closed.answer == ""


def test_async_question_closes_as_superseded_when_its_turn_is_pruned(waiting_async) -> None:
    rig = waiting_async
    collector = rig.source.watcher.watches[T1].collector
    collector.feed([started("new")])
    collector.prune(1)
    assert ASYNC_CALL not in collector.inputs
    rig.poll(NOW + 10)
    closed = _questions(rig.events)[-1]
    assert closed.closed and closed.by == "superseded"
    assert rig.source.async_question_state(T1, ASYNC_CALL) is None


def test_unknown_async_reply_and_multi_part_user_message_are_ignored(codex_home, running, waiting_async) -> None:
    unknown = desktop_async_reply("t1", call_id="not-a-known-call")
    two_parts = desktop_async_reply("t1")
    two_parts["payload"]["item"]["content"].append({"type": "text", "text": "第二部分", "text_elements": []})
    codex_home.write(running, [unknown, two_parts])
    waiting_async.poll(NOW + 10)
    assert len(_questions(waiting_async.events)) == 1
    assert waiting_async.source.async_question_state(T1, ASYNC_CALL)["answered"] == {}
    assert waiting_async.source.async_question_state(T1, "not-a-known-call") is None


def test_async_reply_does_not_answer_blocking_calls() -> None:
    collector = cs.CommentaryCollector()
    collector.feed([started("t1"), ask_call("t1", "blocking-call", QUESTIONS),
                    desktop_async_reply("t1", call_id="blocking-call"),
                    delegated("t1", "[tg:AbC_1-x] " + async_reply("blocking-call"))])
    assert not collector.inputs["blocking-call"].done and collector.inputs["blocking-call"].answer == ""


def test_async_question_with_no_readable_titles_never_waits(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.poll()
    codex_home.write(running, [async_question("t1", questions=[{"title": ""}, {"title": None}, {}])])
    rig.poll(NOW + 5)
    assert _questions(rig.events) == []
    call = rig.source.watcher.watches[T1].collector.inputs[ASYNC_CALL]
    assert call.kind == "async" and call.done and call.questions == []
    assert rig.source.async_question_state(T1, ASYNC_CALL)["waiting"] is False


def test_async_questions_of_excluded_folders_carry_no_question_or_answer(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path, cfg=FakeConfig(content_exclude=["C:\\projects\\demo"]))
    rig.poll()
    codex_home.write(running, [async_question("t1")])
    rig.poll(NOW + 5)
    (asked,) = _questions(rig.events)
    assert asked.mode == "async" and asked.excluded and asked.questions == []
    assert asked.answer == "" and asked.answered == {}
    codex_home.write(running, [desktop_async_reply("t1")])
    rig.poll(NOW + 10)
    closed = _questions(rig.events)[-1]
    assert closed.closed and closed.excluded and closed.by == "answered"
    assert closed.questions == [] and closed.answer == "" and closed.answered == {}


# --- request_user_input (the questions of plan mode) ------------------------------------


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat()


def ask_call(turn_id: str, call_id: str, questions: list[dict[str, Any]], *, with_turn: bool = True,
             at: float = STARTED_AT + 30) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": "function_call", "id": "fc_1", "name": "request_user_input",
                               "arguments": json.dumps({"questions": questions}, ensure_ascii=False),
                               "call_id": call_id}
    if with_turn:
        payload["internal_chat_message_metadata_passthrough"] = {"turn_id": turn_id}
    return {"timestamp": _stamp(at), "type": "response_item", "payload": payload}


def ask_output(call_id: str, answers: dict[str, list[str]]) -> dict[str, Any]:
    output = json.dumps({"answers": {qid: {"answers": values} for qid, values in answers.items()}}, ensure_ascii=False)
    return {"timestamp": _stamp(STARTED_AT + 60), "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": call_id, "output": output}}


QUESTIONS = [
    {"id": "scope", "header": "范围", "question": "先做哪一部分？",
     "options": [{"label": "P1 优先（推荐）", "description": "先打基础"}, {"label": "并行", "description": "一起做"}]},
    {"id": "data", "header": "数据", "question": "数据从哪来？", "options": [{"label": "新采集", "description": ""}]},
]


def _questions(events: list[Any]) -> list[QuestionEvent]:
    return [event for event in events if isinstance(event, QuestionEvent)]


def test_commentary_collector_tracks_user_input_calls() -> None:
    collector = cs.CommentaryCollector()
    collector.feed([started("t1"), ask_call("t1", "call_a", QUESTIONS, with_turn=False),
                    ask_call("t1", "call_b", [{"id": "x", "question": ""}])])  # nothing readable: Codex waits all the same
    call = collector.inputs["call_a"]
    assert collector.inputs["call_b"].questions == [] and not collector.inputs["call_b"].done
    assert call.turn_id == "t1" and [question.id for question in call.questions] == ["scope", "data"]
    assert call.questions[0].options == [("P1 优先（推荐）", "先打基础"), ("并行", "一起做")] and not call.done
    collector.feed([ask_output("call_a", {"scope": ["并行"], "data": ["自己写的说明"]})])
    assert call.done and call.answer == "范围: 并行\n数据: 自己写的说明"
    collector.feed([started("t2"), ask_call("t2", "call_c", QUESTIONS), completed("t2", "好")])
    assert collector.inputs["call_c"].done and collector.inputs["call_c"].answer == ""  # its turn ended
    collector.prune(1)
    assert list(collector.inputs) == ["call_c"]


def test_malformed_user_input_calls_do_not_break_the_reader() -> None:
    def item(payload: dict[str, Any]) -> dict[str, Any]:
        return {"timestamp": _stamp(STARTED_AT + 40), "type": "response_item", "payload": payload}

    collector = cs.CommentaryCollector()
    odd = [{"id": "a", "question": "选哪个？", "options": True},
           {"id": "b", "question": "再选", "options": [1, None, {"label": ""}, {"label": "好"}]}]
    collector.feed([
        started("t1"), ask_call("t1", "call_a", odd),
        item({"type": "function_call", "name": "request_user_input", "call_id": "call_b", "arguments": "{not json"}),
        item({"type": "function_call", "name": "request_user_input", "call_id": "call_c", "arguments": "[1, 2]"}),
        item({"type": "function_call", "name": "request_user_input", "call_id": 7, "arguments": "{}"}),
        item({"type": "function_call", "name": "request_user_input", "call_id": "call_d",
              "arguments": json.dumps({"questions": "x"})}),
        item({"type": "function_call_output", "call_id": "call_a", "output": [1, 2]}),
        item({"type": "function_call_output", "call_id": "call_b", "output": json.dumps({"answers": {"a": True}})}),
    ])
    call = collector.inputs["call_a"]
    assert [question.options for question in call.questions] == [[], [("好", "")]]
    assert call.done and call.answer == "" and collector.inputs["call_b"].done
    assert [collector.inputs[key].questions for key in ("call_b", "call_c", "call_d")] == [[], [], []]
    assert 7 not in collector.inputs


def test_waiting_questions_are_reported_once_and_closed_when_answered(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.poll()
    codex_home.write(running, [ask_call("t1", "call_a", QUESTIONS)])
    rig.poll(NOW + 5)
    (asked,) = _questions(rig.events)
    assert (asked.session_id, asked.request_id, asked.turn_id, asked.closed) == (T1, "call_a", "t1", False)
    assert [question.text for question in asked.questions] == ["先做哪一部分？", "数据从哪来？"]
    assert (asked.title, asked.cwd, asked.excluded) == ("demo-p18-v3", "C:\\projects\\demo", False)
    rig.poll(NOW + 10)
    assert len(_questions(rig.events)) == 1
    codex_home.write(running, [ask_output("call_a", {"scope": ["并行"]})])
    rig.poll(NOW + 15)
    closed = _questions(rig.events)[-1]
    assert closed.closed and closed.request_id == "call_a" and closed.answer == "范围: 并行" and closed.questions == []


def test_a_question_waiting_at_start_is_reported_and_closed_by_its_turn_end(codex_home, tmp_path) -> None:
    rollout = codex_home.add(T1, "demo", [started("t1"), user_message("t1", "规划一下"),
                                          ask_call("t1", "call_a", QUESTIONS)], updated=int(NOW - 30))
    rig = Rig(tmp_path)
    rig.poll()
    assert [event.request_id for event in _questions(rig.events)] == ["call_a"]  # e.g. the bridge restarted
    codex_home.write(rollout, [event("turn_aborted", "t1", reason="interrupted")])
    rig.poll(NOW + 5)
    closed = _questions(rig.events)[-1]
    assert closed.closed and closed.answer == ""


def test_questions_need_codex_desktop_and_a_turn_newer_than_it(codex_home, running, tmp_path, monkeypatch) -> None:
    rig = Rig(tmp_path)
    rig.poll()
    codex_home.write(running, [ask_call("t1", "call_a", QUESTIONS)])
    rig.poll(NOW + 5)
    rig.pid = None  # one failed process snapshot changes nothing
    rig.poll(NOW + 10)
    rig.pid = 4242
    rig.poll(NOW + 12)
    assert len(_questions(rig.events)) == 1
    rig.pid = None  # Codex Desktop closed: nothing waits any more
    for step in range(cs.DESKTOP_SETTLE_POLLS):
        assert len(_questions(rig.events)) == 1
        rig.poll(NOW + 13 + step)
    assert [(event.request_id, event.closed) for event in _questions(rig.events)] == [("call_a", False),
                                                                                     ("call_a", True)]
    monkeypatch.setattr(jobs, "process_creation_time", lambda _pid: _filetime(STARTED_AT + 300))
    rig.pid = 5151  # started after the question was asked: the restart cut that turn short
    for step in range(cs.DESKTOP_SETTLE_POLLS + 1):
        rig.poll(NOW + 20 + step)
    assert len(_questions(rig.events)) == 2


def test_a_short_lived_second_codex_instance_changes_no_question(codex_home, running, tmp_path, monkeypatch) -> None:
    starts = {4242: STARTED_AT - 600, 1111: NOW + 9}  # 1111: a second ChatGPT.exe, started just now
    monkeypatch.setattr(jobs, "process_creation_time", lambda pid: _filetime(starts[pid]) if pid in starts else None)
    rig = Rig(tmp_path)
    rig.poll()
    codex_home.write(running, [ask_call("t1", "call_a", QUESTIONS)])
    rig.poll(NOW + 5)
    rig.pid = 1111  # one poll lands in its short life; codex_main_pid prefers the lower pid
    rig.poll(NOW + 10)
    rig.pid = 4242
    rig.poll(NOW + 15)
    assert [(event.request_id, event.closed) for event in _questions(rig.events)] == [("call_a", False)]


def test_a_question_of_a_turn_followed_by_a_newer_one_waits_no_more(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.poll()
    codex_home.write(running, [ask_call("t1", "call_a", QUESTIONS)])
    rig.poll(NOW + 5)
    codex_home.write(running, [started("t2"), user_message("t2", "换个话题")])  # t1 never ended: cut short
    rig.poll(NOW + 10)
    assert [(event.request_id, event.closed) for event in _questions(rig.events)] == [("call_a", False),
                                                                                     ("call_a", True)]


def test_a_question_that_does_not_parse_is_still_reported(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.poll()
    codex_home.write(running, [ask_call("t1", "call_a", [{"id": "x", "prompt": "new shape"}])])
    rig.poll(NOW + 5)
    (asked,) = _questions(rig.events)
    assert asked.questions == [] and not asked.excluded and not asked.closed


def test_questions_of_excluded_folders_carry_no_text(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path, cfg=FakeConfig(content_exclude=["C:\\projects\\demo"]))
    rig.poll()
    codex_home.write(running, [ask_call("t1", "call_a", QUESTIONS)])
    rig.poll(NOW + 5)
    (asked,) = _questions(rig.events)
    assert asked.excluded and asked.questions == []
    codex_home.write(running, [ask_output("call_a", {"scope": ["并行"]})])
    rig.poll(NOW + 10)
    assert _questions(rig.events)[-1].closed and _questions(rig.events)[-1].answer == ""


def test_a_turn_that_ends_with_a_plan_is_marked(codex_home, running, tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.poll()
    codex_home.write(running, [plan("t1", "# 计划\n1. 改代码"), completed("t1", "计划见上"),
                               started("t2"), completed("t2", "普通回复")])
    rig.poll(NOW + 5)
    assert [(item.turn_id, item.plan) for item in rig.events if isinstance(item, TurnEvent)] == [("t1", True),
                                                                                                ("t2", False)]


def token_count(used: float, at: float, resets_at: float = NOW + 86400) -> dict[str, Any]:
    """What Codex logs after each model response: the account's limits (a Pro plan: weekly only)."""

    return {"timestamp": _stamp(at), "type": "event_msg", "payload": {
        "type": "token_count", "info": {"total_token_usage": {"input_tokens": 1}},
        "rate_limits": {"limit_id": "codex", "primary": {"used_percent": used, "window_minutes": 10080,
                                                         "resets_at": resets_at}, "secondary": None}}}


def test_a_finished_turn_carries_the_newest_plan_limits(codex_home, running, tmp_path) -> None:
    other = codex_home.add(T2, "other", [started("o1"), token_count(9.0, STARTED_AT + 5)], updated=int(NOW - 20))
    rig = Rig(tmp_path)
    rig.poll()
    assert rig.source.quota().windows[0].used_percent == 9.0  # read from the tail at start
    codex_home.write(running, [token_count(12.0, STARTED_AT + 40), completed("t1", "好")])
    rig.poll(NOW + 5)
    (turn,) = [item for item in rig.events if isinstance(item, TurnEvent)]
    assert turn.quota.agent == CODEX and [(window.label, window.used_percent) for window in turn.quota.windows] == [
        ("本周", 12.0)]
    codex_home.write(other, [{"timestamp": 7, "type": "event_msg", "payload": {"type": "token_count", "rate_limits": {}}},
                             token_count(13.5, STARTED_AT + 50)])
    rig.poll(NOW + 10)
    assert rig.source.quota().windows[0].used_percent == 13.5  # the account's newest, whichever thread logged it


class FakeIpc:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None
        self.plan = "# 计划\n1. 改代码"
        self.steer_result = {"turn_id": "t1", "other_turn": False}

    def answer_user_input(self, conversation_id: str, **options: Any) -> None:
        self.calls.append(("answer", conversation_id, options))
        if self.error is not None:
            raise self.error

    def steer_question_reply(self, conversation_id: str, **options: Any) -> dict[str, Any]:
        self.calls.append(("steer", conversation_id, options))
        if self.error is not None:
            raise self.error
        return self.steer_result

    def prepare_plan_implementation(self, conversation_id: str, *, turn_id: str,
                                    check: Callable[[str], None] | None = None) -> str:
        self.calls.append(("plan", conversation_id, turn_id))
        if self.error is not None:
            raise self.error
        if check is not None:
            check(self.plan)  # before the switch, as codex_ipc does
        self.calls.append(("switched", conversation_id))
        return self.plan


def test_answer_question_goes_through_the_desktop_ipc(codex_home, running, tmp_path) -> None:
    ipc = FakeIpc()
    rig = Rig(tmp_path, ipc=ipc)
    rig.source.answer_question(T1, "call_a", {"scope": ["并行"]})
    assert ipc.calls == [("answer", T1, {"call_id": "call_a", "answers": {"scope": ["并行"]}})]
    ipc.error = cs.codex_ipc.CodexIpcError("Codex Desktop 里没有窗口负责这个对话")
    with pytest.raises(ValueError, match="没有窗口负责"):
        rig.source.answer_question(T1, "call_a", {"scope": ["并行"]})
    rig.pid = None
    with pytest.raises(ValueError, match="没有运行"):
        rig.source.answer_question(T1, "call_a", {"scope": ["并行"]})
    with pytest.raises(ValueError, match="找不到"):
        rig.source.answer_question(T4, "call_a", {"scope": ["并行"]})


def test_implement_plan_switches_mode_then_sends_the_plan(codex_home, running, tmp_path, spawned) -> None:
    ipc = FakeIpc()
    rig = Rig(tmp_path, ipc=ipc)
    rig.app_busy = False  # the plan turn has ended
    job_id = rig.source.implement_plan(T1, "t1", nonce="[tg:abc123]")
    assert ipc.calls == [("plan", T1, "t1"), ("switched", T1)] and spawned == [job_id]
    params = jobs.load_request(job_id)["params"]
    assert params["message"] == "PLEASE IMPLEMENT THIS PLAN:\n# 计划\n1. 改代码"
    assert (params["raw"], params["nonce"], params["if_busy"]) == (True, "[tg:abc123]", "fail")

    jobs.update_state(job_id, status="succeeded", phase="done")  # that implementation ran
    rig.app_busy = True
    with pytest.raises(ValueError, match="正在运行"):
        rig.source.implement_plan(T1, "t1", nonce="[tg:abc124]")
    rig.app_busy = False
    ipc.error = cs.codex_ipc.CodexIpcError("Codex Desktop 没有在等着实施这个计划")
    with pytest.raises(ValueError, match="没有在等着实施"):
        rig.source.implement_plan(T1, "t1", nonce="[tg:abc125]")


def test_implement_plan_refuses_before_it_switches_the_mode(codex_home, running, tmp_path, spawned,
                                                            monkeypatch) -> None:
    ipc = FakeIpc()
    rig = Rig(tmp_path, ipc=ipc)
    rig.app_busy = False
    other = jobs.create_job("codex", {"thread_id": T1})  # another send follows this conversation
    with pytest.raises(ValueError, match=f"作业 {other} 正在使用"):
        rig.source.implement_plan(T1, "t1", nonce="[tg:abc126]")
    jobs.update_state(other, status="succeeded", phase="done")
    monkeypatch.setattr(codex_app_client, "availability",
                        lambda: {"node": "node.exe", "app_tools_server": "server.mjs", "pipes": 0})
    with pytest.raises(ValueError, match="管道"):
        rig.source.implement_plan(T1, "t1", nonce="[tg:abc127]")
    monkeypatch.setattr(codex_app_client, "availability",
                        lambda: {"node": "node.exe", "app_tools_server": "server.mjs", "pipes": 1})
    ipc.plan = "x" * cs.MAX_SEND_CHARS
    with pytest.raises(ValueError, match="计划太长"):
        rig.source.implement_plan(T1, "t1", nonce="[tg:abc128]")
    assert ("switched", T1) not in ipc.calls and spawned == []


def test_submit_send_can_skip_only_the_active_job_clash(codex_home, running, tmp_path, spawned,
                                                       monkeypatch) -> None:
    rig = Rig(tmp_path)
    checked: list[str] = []

    def clash(thread_id: str) -> None:
        checked.append(thread_id)
        raise ValueError("existing job")

    monkeypatch.setattr(rig.source, "_refuse_clash", clash)
    with pytest.raises(ValueError, match="existing job"):
        rig.source.submit_send(T1, "ordinary", nonce="[tg:before]")
    job_id = rig.source.submit_send(T1, "answer", nonce="[tg:after]", clash_check=False)
    assert checked == [T1] and spawned == [job_id]
    assert jobs.load_request(job_id)["params"]["message"] == "answer"
    with pytest.raises(ValueError, match="不能为空"):
        rig.source.submit_send(T1, "", nonce="[tg:after]", clash_check=False)


def test_answer_async_question_steers_running_turn(waiting_async, spawned) -> None:
    rig = waiting_async
    ipc = FakeIpc()
    rig.source.ipc = ipc
    answers = {0: (ASYNC_TITLE, "是")}
    result = rig.source.answer_async_question(T1, ASYNC_CALL, "t1", answers,
                                               client_id="telegram-client", marker="[tg:Ab3x9_]")
    assert result == {"via": "ipc", "turn_id": "t1", "other_turn": False}
    assert ipc.calls == [("steer", T1, {
        "turn_id": "t1", "call_id": ASYNC_CALL, "indices": [0],
        "envelope": codex_threads.build_question_reply(ASYNC_CALL, answers),
        "client_id": "telegram-client", "cwd": "C:\\projects\\demo"})]
    assert spawned == [] and jobs.list_jobs(10) == []
    ipc.steer_result = {"turn_id": "t2", "other_turn": True}
    assert rig.source.answer_async_question(T1, ASYNC_CALL, "t1", answers,
                                             client_id="telegram-client", marker="[tg:Ab3x9_]") == {
        "via": "ipc", "turn_id": "t2", "other_turn": True}


def test_answer_async_question_after_turn_uses_raw_app_tools(codex_home, running, waiting_async, spawned) -> None:
    rig = waiting_async
    ipc = FakeIpc()
    rig.source.ipc = ipc
    codex_home.write(running, [completed("t1", "稍后再答")])
    rig.poll(NOW + 10)
    # An earlier Telegram send can still follow this idle conversation; it is not a duplicate reply.
    jobs.create_job("codex", {"thread_id": T1, "message": "earlier task"})
    answers = {0: (ASYNC_TITLE, "是")}
    result = rig.source.answer_async_question(T1, ASYNC_CALL, "t1", answers,
                                               client_id="telegram-client", marker="[tg:Ab3x9_]")
    assert result == {"via": "app_tools", "job_id": spawned[0]} and len(spawned) == 1
    params = jobs.load_request(result["job_id"])["params"]
    assert params["message"] == codex_threads.build_question_reply(ASYNC_CALL, answers)
    assert (params["thread_id"], params["raw"], params["nonce"], params["if_busy"]) == (
        T1, True, "[tg:Ab3x9_]", "fail")
    assert ipc.calls == []


@pytest.mark.parametrize("error", [
    cs.codex_ipc.TurnEnded(),
    cs.codex_ipc.CodexIpcError("没有窗口", code="no-client-found"),
    cs.codex_ipc.CodexIpcError("Codex Desktop 里没有窗口负责这个对话"),
    cs.codex_ipc.CodexIpcError("等待 Codex Desktop 发来对话状态超时"),
    cs.codex_ipc.CodexIpcError("thread is not being streamed"),
])
def test_answer_async_question_falls_back_only_after_rollout_ends(codex_home, running, waiting_async, spawned,
                                                                monkeypatch, error: Exception) -> None:
    rig = waiting_async
    ipc = FakeIpc()
    rig.source.ipc = ipc

    def ended(_conversation_id: str, **_options: Any) -> dict[str, Any]:
        codex_home.write(running, [completed("t1", "稍后再答")])
        rig.poll(NOW + 10)
        raise error

    monkeypatch.setattr(ipc, "steer_question_reply", ended)
    result = rig.source.answer_async_question(T1, ASYNC_CALL, "t1", {0: (ASYNC_TITLE, "是")},
                                               client_id="telegram-client", marker="[tg:Ab3x9_]")
    assert result == {"via": "app_tools", "job_id": spawned[0]}


@pytest.mark.parametrize("error", [
    cs.codex_ipc.TurnEnded(),
    cs.codex_ipc.CodexIpcError("没有窗口", code="no-client-found"),
    cs.codex_ipc.CodexIpcError("Codex Desktop 里没有窗口负责这个对话"),
    cs.codex_ipc.CodexIpcError("等待 Codex Desktop 发来对话状态超时"),
    cs.codex_ipc.CodexIpcError("thread is not being streamed"),
    cs.codex_ipc.CodexIpcError("unsupported", code="no-handler-for-request"),
])
def test_answer_async_question_refuses_fallback_while_rollout_runs(waiting_async, spawned,
                                                                error: Exception) -> None:
    ipc = FakeIpc()
    ipc.error = error
    waiting_async.source.ipc = ipc
    with pytest.raises(ValueError) as exc:
        waiting_async.source.answer_async_question(T1, ASYNC_CALL, "t1", {0: (ASYNC_TITLE, "是")},
                                                    client_id="telegram-client", marker="[tg:Ab3x9_]")
    assert str(exc.value) == str(error)
    assert spawned == [] and jobs.list_jobs(10) == []


@pytest.mark.parametrize("error", [cs.codex_ipc.OutcomeUnknown(), cs.codex_ipc.AlreadyAnswered(True),
                                  cs.codex_ipc.AlreadyAnswered(False)])
def test_answer_async_question_preserves_uncertain_or_duplicate_outcome(waiting_async, spawned,
                                                                      error: Exception) -> None:
    ipc = FakeIpc()
    ipc.error = error
    waiting_async.source.ipc = ipc
    with pytest.raises(type(error)) as exc:
        waiting_async.source.answer_async_question(T1, ASYNC_CALL, "t1", {0: (ASYNC_TITLE, "是")},
                                                    client_id="telegram-client", marker="[tg:Ab3x9_]")
    assert exc.value is error
    assert spawned == [] and jobs.list_jobs(10) == []


def test_answer_async_question_refuses_active_reply_job(codex_home, running, waiting_async, spawned) -> None:
    rig = waiting_async
    codex_home.write(running, [completed("t1", "稍后再答")])
    rig.poll(NOW + 10)
    envelope = codex_threads.build_question_reply(ASYNC_CALL, {0: (ASYNC_TITLE, "是")})
    active = jobs.create_job("codex", {"thread_id": T1, "message": "[tg:before] " + envelope})
    with pytest.raises(ValueError, match="上次的答案还在发送"):
        rig.source.answer_async_question(T1, ASYNC_CALL, "t1", {0: (ASYNC_TITLE, "是")},
                                          client_id="telegram-client", marker="[tg:Ab3x9_]")
    assert spawned == []
    jobs.update_state(active, status="failed", phase="failed")
    assert rig.source.answer_async_question(T1, ASYNC_CALL, "t1", {0: (ASYNC_TITLE, "是")},
                                             client_id="telegram-client", marker="[tg:Ab3x9_]") == {
        "via": "app_tools", "job_id": spawned[0]}


def test_concurrent_async_answers_start_only_one_reply_job(codex_home, running, waiting_async, spawned,
                                                           monkeypatch) -> None:
    import threading

    rig = waiting_async
    codex_home.write(running, [completed("t1", "稍后再答")])
    rig.poll(NOW + 10)
    real_list = jobs.list_jobs
    listing = threading.Event()
    second_attempted = threading.Event()

    def held_list(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        found = real_list(*args, **kwargs)
        listing.set()
        assert second_attempted.wait(2)
        return found

    monkeypatch.setattr(jobs, "list_jobs", held_list)
    outcomes: list[dict[str, Any] | str] = []

    def answer(client_id: str) -> None:
        if client_id == "second":
            assert listing.wait(2)
            second_attempted.set()
        try:
            outcomes.append(rig.source.answer_async_question(
                T1, ASYNC_CALL, "t1", {0: (ASYNC_TITLE, "是")}, client_id=client_id, marker=f"[tg:{client_id}]"))
        except ValueError as exc:
            outcomes.append(str(exc))

    workers = [threading.Thread(target=answer, args=(client_id,)) for client_id in ("first", "second")]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
    assert all(not worker.is_alive() for worker in workers)
    assert len(spawned) == 1
    assert len([item for item in outcomes if isinstance(item, dict)]) == 1
    assert "上次的答案还在发送，请稍候" in outcomes


def test_answer_async_question_refuses_an_already_answered_index(codex_home, running, tmp_path, spawned) -> None:
    rig = Rig(tmp_path, ipc=FakeIpc())
    rig.poll()
    codex_home.write(running, [async_question("t1", questions=[{"title": "第一题"}, {"title": "第二题"}]),
                               desktop_async_reply("t1", answers={1: ("第二题", "只答第二题")})])
    rig.poll(NOW + 5)
    assert rig.source.async_question_state(T1, ASYNC_CALL)["waiting"]
    with pytest.raises(ValueError, match="这个问题已经回答过了"):
        rig.source.answer_async_question(T1, ASYNC_CALL, "t1", {1: ("第二题", "重复")},
                                          client_id="telegram-client", marker="[tg:Ab3x9_]")
    assert rig.source.ipc.calls == [] and spawned == []


@pytest.mark.parametrize("stale", [False, True])
def test_answer_async_question_refuses_missing_or_closed_request(waiting_async, monkeypatch, spawned,
                                                                stale: bool) -> None:
    rig = waiting_async
    ipc = FakeIpc()
    rig.source.ipc = ipc
    state = None if not stale else {"waiting": False, "answered": {0: "是"}}
    monkeypatch.setattr(rig.source, "async_question_state", lambda *_args: state)
    with pytest.raises(ValueError, match="这个问题已经不需要回答了"):
        rig.source.answer_async_question(T1, ASYNC_CALL, "t1", {0: (ASYNC_TITLE, "是")},
                                          client_id="telegram-client", marker="[tg:Ab3x9_]")
    assert ipc.calls == [] and spawned == []


@pytest.mark.parametrize("turn_last,thread_busy", [(False, False), (True, True), (False, True)])
def test_answer_async_question_refuses_newer_or_busy_turn(waiting_async, monkeypatch, spawned,
                                                         turn_last: bool, thread_busy: bool) -> None:
    rig = waiting_async
    ipc = FakeIpc()
    rig.source.ipc = ipc
    state = {"waiting": True, "answered": {}, "turn_running": False,
             "turn_last": turn_last, "thread_busy": thread_busy}
    monkeypatch.setattr(rig.source, "async_question_state", lambda *_args: state)
    with pytest.raises(ValueError, match="对话已经开始了新的一轮"):
        rig.source.answer_async_question(T1, ASYNC_CALL, "t1", {0: (ASYNC_TITLE, "是")},
                                          client_id="telegram-client", marker="[tg:Ab3x9_]")
    assert ipc.calls == [] and spawned == []


# --- approval snapshot discovery (fake IPC only) ---------------------------------------


def approval_request(request_id: int | str = 37, *, method: str | None = None, **params: Any) -> dict[str, Any]:
    return {"id": request_id, "method": method or cs.codex_ipc.COMMAND_APPROVAL_METHOD,
            "params": {"itemId": "cmd-1", "turnId": "t1", "approvalId": "approval-1",
                       "command": ["python", "build.py"], "cwd": "C:\\projects\\demo", "reason": "run build",
                       "availableDecisions": ["accept", "decline", "acceptForSession"], **params}}


class ApprovalIpc:
    def __init__(self, source: cs.CodexSource | None = None) -> None:
        self.source = source
        self.state: dict[str, Any] = {"requests": [approval_request()]}
        self.snapshots: list[str] = []
        self.decisions: list[tuple[str, dict[str, Any]]] = []
        self.error: Exception | None = None
        self.entered: threading.Event | None = None
        self.release: threading.Event | None = None

    def connect(self) -> ApprovalIpc:
        assert self.source is not None and not self.source._lock._is_owned()
        return self

    def __enter__(self) -> ApprovalIpc:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def snapshot(self, thread_id: str) -> dict[str, Any]:
        assert self.source is not None and not self.source._lock._is_owned()
        self.snapshots.append(thread_id)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            assert self.release.wait(timeout=5.0)
        if self.error is not None:
            raise self.error
        return self.state

    def decide_approval(self, thread_id: str, **options: Any) -> None:
        self.decisions.append((thread_id, options))
        if self.error is not None:
            raise self.error


def approval_rig(tmp_path: Path, **options: Any) -> tuple[Rig, ApprovalIpc]:
    ipc = ApprovalIpc()
    rig = Rig(tmp_path, ipc=ipc, **options)
    ipc.source = rig.source
    rig.poll()
    return rig, ipc


def collect_approval_snapshot(rig: Rig, at: float) -> None:
    rig.poll(at)
    rig.source._poll_approval_once()
    rig.poll(at)  # drain the worker's outbox on the regular poll/send path


def approval_events(rig: Rig) -> list[ApprovalEvent]:
    return [event for event in rig.events if isinstance(event, ApprovalEvent)]


@pytest.mark.parametrize("policy", ["never", "on-request", {"granular": {"command": False}}, None])
def test_approval_rollout_policy_overrides_database(codex_home, running, tmp_path, policy) -> None:
    with sqlite3.connect(codex_home.db) as connection:
        connection.execute("ALTER TABLE threads ADD COLUMN approval_mode TEXT")
        connection.execute("UPDATE threads SET approval_mode = 'never'")
    codex_home.write(running, [{"type": "turn_context", "payload": {"approval_policy": policy}}])
    rig, ipc = approval_rig(tmp_path)
    collect_approval_snapshot(rig, NOW + 15)
    assert ipc.snapshots == ([] if policy == "never" else [T1])
    collector = rig.source.watcher.watches[T1].collector
    assert collector.approval_policy_seen and collector.approval_policy == policy


@pytest.mark.parametrize("nested", [True, False])
def test_approval_latest_thread_settings_policy_wins(codex_home, running, tmp_path, nested) -> None:
    codex_home.write(running, [{"type": "turn_context", "payload": {"approval_policy": "on-request"}}])
    rig, ipc = approval_rig(tmp_path)
    settings = {"thread_settings": {"approval_policy": "never"}} if nested else {"approval_policy": "never"}
    codex_home.write(running, [event("thread_settings_applied", "t1", **settings)])
    rig.poll(NOW + 4)
    collect_approval_snapshot(rig, NOW + 19)
    assert ipc.snapshots == []
    codex_home.write(running, [event("thread_settings_applied", "t1", approval_policy={"granular": {}})])
    rig.poll(NOW + 20)
    collect_approval_snapshot(rig, NOW + 35)
    assert ipc.snapshots == [T1]


def test_approval_database_never_skips_all_snapshots(codex_home, running, tmp_path) -> None:
    with sqlite3.connect(codex_home.db) as connection:
        connection.execute("ALTER TABLE threads ADD COLUMN approval_mode TEXT")
        connection.execute("UPDATE threads SET approval_mode = 'never'")
    rig, ipc = approval_rig(tmp_path)
    collect_approval_snapshot(rig, NOW + 100)
    assert not ipc.snapshots and not rig.source._approval_jobs


def test_approval_unknown_policy_waits_for_real_byte_stall(codex_home, running, tmp_path) -> None:
    rig, ipc = approval_rig(tmp_path)
    collect_approval_snapshot(rig, NOW + 14.9)
    assert ipc.snapshots == []
    # Windows mtime may not move while Codex appends. Even an unfinished record
    # changes the size signal and must restart the silence interval.
    with running.open("ab") as stream:
        stream.write(b'{"timestamp":')
    collect_approval_snapshot(rig, NOW + 15)
    collect_approval_snapshot(rig, NOW + 29.9)
    assert ipc.snapshots == []
    collect_approval_snapshot(rig, NOW + 30)
    assert ipc.snapshots == [T1]


def test_command_approval_is_reported_once_and_rechecked_after_15_seconds(codex_home, running, tmp_path) -> None:
    rig, ipc = approval_rig(tmp_path)
    collect_approval_snapshot(rig, NOW + 15)
    (opened,) = approval_events(rig)
    assert opened.key == f"codex:{T1}"
    assert opened.request_id == 37 and type(opened.request_id) is int
    assert opened.command == "python build.py" and opened.command_cwd == "C:\\projects\\demo"
    assert opened.digest == cs.codex_ipc.approval_digest(ipc.state["requests"][0])
    assert not opened.closed and opened.kind == "command"
    collect_approval_snapshot(rig, NOW + 29)
    assert ipc.snapshots == [T1]
    collect_approval_snapshot(rig, NOW + 30)
    assert ipc.snapshots == [T1, T1] and approval_events(rig) == [opened]
    ipc.state = {"requests": []}
    collect_approval_snapshot(rig, NOW + 45)
    closed = approval_events(rig)[-1]
    assert closed.closed and closed.by == "pc" and closed.request_id == 37
    collect_approval_snapshot(rig, NOW + 60)
    assert ipc.snapshots == [T1, T1, T1]  # no open card: ordinary 30-second polling
    collect_approval_snapshot(rig, NOW + 75)
    assert len(ipc.snapshots) == 4


def test_open_approval_rechecks_despite_rollout_activity_and_policy_never(codex_home, running, tmp_path) -> None:
    rig, ipc = approval_rig(tmp_path)
    collect_approval_snapshot(rig, NOW + 15)
    codex_home.write(running, [event("thread_settings_applied", "t1", approval_policy="never")])
    rig.poll(NOW + 20)
    codex_home.write(running, [commentary("t1", "still running")])
    rig.poll(NOW + 29)
    ipc.state = {"requests": []}
    collect_approval_snapshot(rig, NOW + 30)
    assert ipc.snapshots == [T1, T1]
    closed = approval_events(rig)[-1]
    assert closed.closed and closed.by == "pc"


def test_approval_exact_request_id_types_remain_distinct(codex_home, running, tmp_path) -> None:
    rig, ipc = approval_rig(tmp_path)
    ipc.state["requests"] = [approval_request(37), approval_request("37")]
    collect_approval_snapshot(rig, NOW + 15)
    assert [(type(e.request_id), e.request_id) for e in approval_events(rig)] == [(int, 37), (str, "37")]
    ipc.state["requests"] = [approval_request("37")]
    collect_approval_snapshot(rig, NOW + 30)
    closed = approval_events(rig)[-1]
    assert closed.closed and type(closed.request_id) is int and closed.by == "pc"


def test_file_approval_collects_paths_from_matching_snapshot_item(codex_home, running, tmp_path) -> None:
    rig, ipc = approval_rig(tmp_path)
    ipc.state = {"requests": [approval_request(method=cs.codex_ipc.FILE_APPROVAL_METHOD, command=None)],
                 "turns": [{"turnId": "t1", "items": [{"type": "fileChange", "id": "cmd-1",
                                                       "changes": [{"path": "main.py"}, {"path": "test.py"}]}]}]}
    collect_approval_snapshot(rig, NOW + 15)
    (opened,) = approval_events(rig)
    assert opened.kind == "file" and opened.files == ["main.py", "test.py"]


@pytest.mark.parametrize("method", [cs.codex_ipc.PERMISSIONS_APPROVAL_METHOD, cs.codex_ipc.ELICITATION_METHOD])
def test_unsupported_approvals_get_only_one_attention_notice(codex_home, running, tmp_path, method) -> None:
    rig, ipc = approval_rig(tmp_path)
    ipc.state["requests"] = [approval_request(method=method)]
    collect_approval_snapshot(rig, NOW + 15)
    collect_approval_snapshot(rig, NOW + 45)
    assert not approval_events(rig)
    notices = [event for event in rig.events if isinstance(event, AttentionEvent)]
    assert len(notices) == 1 and notices[0].reason == "等待审批（请在 Codex Desktop 里处理）"


@pytest.mark.parametrize("record", [completed("t1", "done"), event("turn_aborted", "t1", reason="user")])
def test_approval_closes_on_turn_end_without_a_snapshot(codex_home, running, tmp_path, record) -> None:
    rig, ipc = approval_rig(tmp_path)
    collect_approval_snapshot(rig, NOW + 15)
    codex_home.write(running, [record])
    rig.poll(NOW + 16)
    closed = approval_events(rig)[-1]
    assert closed.closed and closed.by == "turn_ended"
    collect_approval_snapshot(rig, NOW + 100)
    assert ipc.snapshots == [T1]


@pytest.mark.parametrize("new_pid", [None, 4343])
def test_approval_closes_only_after_desktop_pid_change_settles(codex_home, running, tmp_path, new_pid) -> None:
    rig, ipc = approval_rig(tmp_path)
    collect_approval_snapshot(rig, NOW + 15)
    rig.pid = new_pid
    for index in range(cs.DESKTOP_SETTLE_POLLS - 1):
        rig.poll(NOW + 16 + index)
        assert len(approval_events(rig)) == 1
    rig.poll(NOW + 16 + cs.DESKTOP_SETTLE_POLLS)
    closed = approval_events(rig)[-1]
    assert closed.closed and closed.by == "desktop_gone" and ipc.snapshots == [T1]


def test_approval_closes_when_thread_leaves_watches(codex_home, running, tmp_path) -> None:
    rig, ipc = approval_rig(tmp_path)
    collect_approval_snapshot(rig, NOW + 15)
    with sqlite3.connect(codex_home.db) as connection:
        connection.execute("UPDATE threads SET archived = 1 WHERE id = ?", (T1,))
    rig.poll(NOW + 40)
    closed = approval_events(rig)[-1]
    assert closed.closed and closed.by == "gone" and ipc.snapshots == [T1]


def test_approval_snapshot_failure_backs_off_to_five_minutes(codex_home, running, tmp_path) -> None:
    rig, ipc = approval_rig(tmp_path)
    ipc.error = cs.codex_ipc.CodexIpcError("no owner")
    at = NOW + 15
    for delay in (30, 60, 120, 240, 300, 300):
        collect_approval_snapshot(rig, at)
        count = len(ipc.snapshots)
        collect_approval_snapshot(rig, at + delay - 0.1)
        assert len(ipc.snapshots) == count
        assert rig.source._approval_states[T1].next_poll == at + delay
        at += delay
    ipc.error = None
    collect_approval_snapshot(rig, at)
    assert len(approval_events(rig)) == 1 and rig.source._approval_states[T1].failures == 0


def test_approval_one_snapshot_in_flight_and_no_source_lock_during_ipc(codex_home, running, tmp_path) -> None:
    rig, ipc = approval_rig(tmp_path)
    ipc.entered, ipc.release = threading.Event(), threading.Event()
    rig.poll(NOW + 15)
    worker = threading.Thread(target=rig.source._poll_approval_once)
    worker.start()
    try:
        assert ipc.entered.wait(timeout=5.0)
        # A real concurrent poll acquires the source lock and must not schedule
        # another snapshot while the first one is blocked in fake IPC.
        rig.poll(NOW + 45)
        assert not rig.source._approval_jobs
        assert not rig.source._poll_approval_once()
        assert ipc.snapshots == [T1]
    finally:
        ipc.release.set()
        worker.join(timeout=5.0)
    assert not worker.is_alive()
    rig.poll(NOW + 45)
    assert len(approval_events(rig)) == 1


def test_stopping_source_discards_late_snapshot(codex_home, running, tmp_path) -> None:
    rig, ipc = approval_rig(tmp_path)
    ipc.entered, ipc.release = threading.Event(), threading.Event()
    rig.poll(NOW + 15)
    worker = threading.Thread(target=rig.source._poll_approval_once)
    worker.start()
    try:
        assert ipc.entered.wait(timeout=5.0)
        rig.source.stop()
    finally:
        ipc.release.set()
        worker.join(timeout=5.0)
    rig.poll(NOW + 20)
    assert not approval_events(rig) and not rig.source._poll_approval_once()


def test_remote_owned_thread_never_gets_approval_ipc(codex_home, running, tmp_path, monkeypatch) -> None:
    rig, ipc = approval_rig(tmp_path)
    monkeypatch.setattr(rig.source, "_remote_owns", lambda _thread_id: True)
    collect_approval_snapshot(rig, NOW + 30)
    assert ipc.snapshots == []


@pytest.mark.parametrize("command_cwd", ["C:\\projects\\demo", "C:\\private"])
def test_excluded_approval_event_keeps_only_identity(codex_home, running, tmp_path, command_cwd) -> None:
    excluded = "C:\\projects\\demo" if command_cwd.endswith("demo") else "C:\\private"
    rig, ipc = approval_rig(tmp_path, cfg=FakeConfig(content_exclude=[excluded]))
    ipc.state["requests"] = [approval_request(cwd=command_cwd, networkApprovalContext={"host": "secret"},
                                             additionalPermissions={"path": "private"})]
    collect_approval_snapshot(rig, NOW + 15)
    (opened,) = approval_events(rig)
    assert opened.excluded and not opened.command and not opened.command_cwd and not opened.reason
    assert not opened.files and opened.network is None and opened.extra_permissions is None


def test_decide_approval_routes_exact_identity_digest_and_preserves_errors(codex_home, running, tmp_path) -> None:
    rig, ipc = approval_rig(tmp_path)
    request = approval_request()
    fields = {"request_id": 37, "method": request["method"], "item_id": "cmd-1", "turn_id": "t1",
              "approval_id": "approval-1", "digest": cs.codex_ipc.approval_digest(request), "decision": "accept"}
    rig.source.decide_approval(T1, **fields)
    assert ipc.decisions == [(T1, {**{key: value for key, value in fields.items() if key != "request_id"},
                                  "connect": ipc.connect})]
    for error in (cs.codex_ipc.AlreadyAnswered(False), cs.codex_ipc.OutcomeUnknown("uncertain"),
                  cs.codex_ipc.CodexIpcError("failed")):
        ipc.error = error
        with pytest.raises(type(error)) as raised:
            rig.source.decide_approval(T1, **fields)
        assert raised.value is error


@pytest.mark.parametrize("why", ["remote", "archived", "gone", "cli", "desktop_gone"])
def test_decide_approval_refuses_ineligible_threads(codex_home, running, tmp_path, monkeypatch, why) -> None:
    rig, ipc = approval_rig(tmp_path)
    if why == "remote":
        monkeypatch.setattr(rig.source, "_remote_owns", lambda _thread_id: True)
    elif why == "desktop_gone":
        rig.pid = None
    else:
        with sqlite3.connect(codex_home.db) as connection:
            if why == "archived":
                connection.execute("UPDATE threads SET archived = 1")
            elif why == "gone":
                connection.execute("DELETE FROM threads")
            else:
                connection.execute("UPDATE threads SET source = 'cli'")
    with pytest.raises(ValueError):
        rig.source.decide_approval(T1, method=cs.codex_ipc.COMMAND_APPROVAL_METHOD, item_id="cmd-1",
                                   turn_id="t1", approval_id="approval-1", digest="digest", decision="decline")
    assert ipc.decisions == []


def test_agent_telegram_deliveries_get_the_dispatch_label() -> None:
    for head in ("[agent_telegram:atg-1] task", "  [agent-telegram:job-1] task"):
        assert cs.origin_labels(head) == [cs.LABEL_AGENT_TG_LEGACY]
    assert cs.origin_labels("[agent-telegram-exec:job-1] worker prompt") == []

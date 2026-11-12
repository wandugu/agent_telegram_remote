from __future__ import annotations

import json
from typing import Any

import pytest

from agent_telegram import codex_app_client
from agent_telegram.tgbridge import codex_remote as cr
from agent_telegram.tgbridge.codex_source import CodexSource
from agent_telegram.tgbridge.config import Config
from agent_telegram.tgbridge.model import CODEX, AttentionEvent, ProgressEvent, TurnEvent

ANCHOR = "01a07078-0000-7000-8000-0000000000a1"
R1 = "01a07078-0000-7000-8000-0000000000e1"
R2 = "01a07078-0000-7000-8000-0000000000e2"
LOCAL = "01a07078-0000-7000-8000-0000000000c1"
LAB1 = "remote-ssh-discovered:lab1"
LAB2 = "remote-ssh-discovered:lab2"
START = 1_790_800_000.0


def _item(thread_id: str, host: str | None, *, updated: float, status: Any = "idle", kind: str = "codex",
          title: str = "demo-task", cwd: str = "/home/demo/proj") -> dict[str, Any]:
    item = {"id": thread_id, "kind": kind, "status": status, "updatedAt": updated, "title": title, "cwd": cwd,
            "summary": "", "isUnread": False}
    if host is not None:
        item["hostId"] = host
    return item


def _turn(turn_id: str, status: str, *, started: float, completed: float | None, final: str | None = None,
          user: str = "do it", commentary: tuple[str, ...] = (), error: Any = None) -> dict[str, Any]:
    items: list[dict[str, Any]] = [{"type": "userMessage", "content": [{"type": "text", "text": user}]}]
    items += [{"type": "agentMessage", "text": text, "phase": "commentary"} for text in commentary]
    if final is not None:
        items.append({"type": "agentMessage", "text": final, "phase": "final_answer"})
    return {"id": turn_id, "status": status, "error": error, "startedAt": int(started),
            "completedAt": int(completed) if completed else None, "items": items}


class FakeApp:
    """The Codex app behind the app-tools pipe: list_threads and read_thread answers."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []
        self.pinned: list[dict[str, Any]] = []
        self.unavailable: list[str] = []
        self.turns: dict[str, list[dict[str, Any]]] = {}  # newest first, as the app answers
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.opened: list[str] = []
        self.closed = 0
        self.fail: dict[str, Exception] = {}

    def open_session(self, *, thread_id: str, timeout_s: float) -> FakeSession:
        self.opened.append(thread_id)
        return FakeSession(self)


class FakeSession:
    def __init__(self, app: FakeApp) -> None:
        self.app = app

    def call_tool(self, name: str, arguments: dict[str, Any], *, thread_id: str | None = None,
                  timeout_s: float | None = None) -> dict[str, Any]:
        self.app.calls.append((name, arguments, thread_id))
        key = f"{name}:{arguments.get('threadId', '')}"
        if key in self.app.fail:
            raise self.app.fail.pop(key)
        if name == "list_threads":
            data: dict[str, Any] = {"pinnedThreads": self.app.pinned, "threads": self.app.items,
                                    "unavailableHosts": self.app.unavailable, "unavailableSources": []}
        elif name == "read_thread":
            turns = self.app.turns.get(arguments["threadId"], [])[: arguments.get("turnLimit", 10)]
            data = {"thread": {"id": arguments["threadId"]}, "turns": turns,
                    "page": {"order": "newest_first", "limit": arguments.get("turnLimit"), "hasMore": False}}
        else:
            raise AssertionError(name)
        return {"content": [{"type": "text", "text": json.dumps(data)}]}

    def close(self) -> None:
        self.app.closed += 1


class Rig:
    def __init__(self, *, cfg: Config | None = None, anchor: str | None = ANCHOR) -> None:
        self.app = FakeApp()
        self.now = START
        self.events: list[Any] = []
        self.running = True
        self.cfg = cfg or Config(machine="pc1")
        self.remote = cr.RemoteCodex(self.cfg, self.events.append, anchor=lambda: anchor,
                                     open_session=self.app.open_session, desktop_running=lambda: self.running,
                                     clock=lambda: self.now)

    def reads(self) -> list[str]:
        return [args["threadId"] for name, args, _ in self.app.calls if name == "read_thread"]


def test_epoch_and_host_alias() -> None:
    assert cr.epoch(1_790_837_570) == 1_790_837_570.0
    assert cr.epoch(1_783_952_405_000) == 1_783_952_405.0
    assert cr.epoch("1790837570") == 1_790_837_570.0
    assert cr.epoch(None) == 0.0 and cr.epoch("x") == 0.0 and cr.epoch(float("nan")) == 0.0
    assert cr.host_alias(LAB1) == "lab1" and cr.host_alias("ssh:other") == "ssh:other"


def test_thread_state_reads_strings_and_active_flags() -> None:
    assert cr.thread_state("idle") == ("idle", "")
    assert cr.thread_state({"type": "notLoaded"}) == ("idle", "")
    assert cr.thread_state("active") == ("running", "")
    assert cr.thread_state({"type": "active", "activeFlags": []}) == ("running", "")
    assert cr.thread_state({"type": "active", "activeFlags": ["waitingOnApproval"]}) == ("waiting", "等待审批")
    assert cr.thread_state({"type": "active", "activeFlags": ["waitingOnUserInput"]}) == ("waiting", "等待输入")
    assert cr.thread_state("systemError") == ("unknown", "")


def test_old_threads_are_listed_but_not_read_or_reported() -> None:
    rig = Rig()
    rig.app.items = [_item(R1, LAB1, updated=START - 600), _item(LOCAL, "local", updated=START - 5),
                     _item("chat-1", None, updated=START - 5, kind="chatgpt")]
    rig.remote.poll()
    assert rig.reads() == [] and rig.events == []
    views = rig.remote.sessions(10)
    assert [view.id for view in views] == [R1]
    view = views[0]
    assert view.agent == CODEX and view.title == "lab1·demo-task" and view.kind == "remote:lab1"
    assert view.live is False and view.state == "idle" and view.last_activity == START - 600
    # Every call carries the local anchor thread as executor metadata.
    assert {thread_id for _, _, thread_id in rig.app.calls} == {ANCHOR} and rig.app.opened == [ANCHOR]


def test_a_turn_finished_after_the_start_is_reported_once() -> None:
    rig = Rig()
    rig.app.items = [_item(R1, LAB1, updated=(START - 600) * 1000)]  # milliseconds
    rig.app.turns[R1] = [_turn("t-old", "completed", started=START - 900, completed=START - 600, final="old")]
    rig.remote.poll()
    rig.now = START + 120
    rig.app.items = [_item(R1, LAB1, updated=START + 100)]
    rig.app.turns[R1].insert(0, _turn("t-new", "completed", started=START + 10, completed=START + 100,
                                      final="new answer", user="build it"))
    rig.remote.poll()
    assert len(rig.events) == 1
    event = rig.events[0]
    assert isinstance(event, TurnEvent)
    assert (event.agent, event.session_id, event.turn_id, event.status) == (CODEX, R1, "t-new", "completed")
    assert event.final_text == "new answer" and event.title == "lab1·demo-task" and event.labels == ["远程 lab1"]
    assert event.started_at == START + 10 and event.completed_at == START + 100 and event.cwd == "/home/demo/proj"
    # Nothing changed: no read, no second event.
    reads = len(rig.reads())
    rig.remote.poll()
    assert len(rig.reads()) == reads and len(rig.events) == 1


def test_a_thread_first_seen_after_the_start_reports_only_new_turns() -> None:
    rig = Rig()
    rig.now = START + 60
    rig.app.items = [_item(R1, LAB1, updated=START + 50)]
    rig.app.turns[R1] = [_turn("t2", "completed", started=START + 5, completed=START + 50, final="fresh"),
                         _turn("t1", "completed", started=START - 300, completed=START - 200, final="stale")]
    rig.remote.poll()
    assert [event.turn_id for event in rig.events] == ["t2"]


def test_running_thread_shows_progress_and_reports_the_end() -> None:
    rig = Rig()
    rig.app.items = [_item(R1, LAB1, updated=START - 10, status={"type": "active", "activeFlags": []})]
    rig.app.turns[R1] = [_turn("t1", "inProgress", started=START - 30, completed=None,
                               commentary=("reading the logs", "running   the\ntests"))]
    rig.remote.poll()
    view = rig.remote.view(R1)
    assert view is not None and view.state == "running" and view.detail == "running the tests"
    assert rig.events == []
    # Still running with the same updatedAt: read again for progress.
    rig.remote.poll()
    assert rig.reads() == [R1, R1]
    rig.now = START + 300
    rig.app.items = [_item(R1, LAB1, updated=START - 10, status="idle")]  # updatedAt may lag behind
    rig.app.turns[R1] = [_turn("t1", "completed", started=START - 30, completed=START + 250, final="done")]
    rig.remote.poll()
    assert [(event.turn_id, event.final_text) for event in rig.events] == [("t1", "done")]
    assert rig.remote.view(R1).state == "idle" and rig.remote.view(R1).detail == ""


def test_failed_and_interrupted_turns() -> None:
    rig = Rig()
    rig.app.items = [_item(R1, LAB1, updated=START - 600)]
    rig.remote.poll()
    rig.now = START + 200
    rig.app.items = [_item(R1, LAB1, updated=START + 150)]
    rig.app.turns[R1] = [
        _turn("t3", "interrupted", started=START + 120, completed=START + 150),
        _turn("t2", "failed", started=START + 20, completed=START + 100, error={"message": "stream disconnected"}),
    ]
    rig.remote.poll()
    assert [(event.turn_id, event.status, event.error) for event in rig.events] == [
        ("t2", "failed", "stream disconnected"), ("t3", "aborted", "本轮已被中止")]


def test_unknown_turn_status_waits_for_an_end_time() -> None:
    rig = Rig()
    rig.app.items = [_item(R1, LAB1, updated=START - 600)]
    rig.remote.poll()
    rig.now = START + 200
    rig.app.items = [_item(R1, LAB1, updated=START + 150)]
    rig.app.turns[R1] = [_turn("t1", "queued", started=START + 120, completed=None)]
    rig.remote.poll()
    assert rig.events == []
    rig.app.items = [_item(R1, LAB1, updated=START + 180)]
    rig.app.turns[R1] = [_turn("t1", "systemError", started=START + 120, completed=START + 170,
                               error={"message": "host lost"})]
    rig.remote.poll()
    assert [(event.status, event.error) for event in rig.events] == [("failed", "host lost")]


def test_waiting_thread_raises_attention_once_per_wait() -> None:
    rig = Rig()
    waiting = {"type": "active", "activeFlags": ["waitingOnApproval"]}
    rig.app.items = [_item(R1, LAB1, updated=START - 5, status=waiting)]
    rig.remote.poll()
    rig.remote.poll()
    attention = [event for event in rig.events if isinstance(event, AttentionEvent)]
    assert len(attention) == 1 and "等待审批" in attention[0].reason and "lab1" in attention[0].reason
    assert rig.remote.view(R1).state == "waiting"
    rig.app.items = [_item(R1, LAB1, updated=START + 5, status="active")]
    rig.remote.poll()
    rig.app.items = [_item(R1, LAB1, updated=START + 9, status=waiting)]
    rig.remote.poll()
    assert len([event for event in rig.events if isinstance(event, AttentionEvent)]) == 2


def test_hosts_filter_by_alias_or_id() -> None:
    rig = Rig(cfg=Config(machine="pc1", codex_remote_hosts=["lab2"]))
    rig.app.items = [_item(R1, LAB1, updated=START - 60), _item(R2, LAB2, updated=START - 60)]
    rig.remote.poll()
    assert [view.id for view in rig.remote.sessions(10)] == [R2]
    rig = Rig(cfg=Config(machine="pc1", codex_remote_hosts=[LAB1]))
    rig.app.items = [_item(R1, LAB1, updated=START - 60), _item(R2, LAB2, updated=START - 60)]
    rig.remote.poll()
    assert [view.id for view in rig.remote.sessions(10)] == [R1]


def test_pinned_threads_count_too() -> None:
    rig = Rig()
    rig.app.pinned = [_item(R2, LAB2, updated=START - 60)]
    rig.app.items = [_item(R1, LAB1, updated=START - 30)]
    rig.remote.poll()
    assert [view.id for view in rig.remote.sessions(10)] == [R1, R2]
    assert rig.remote.sessions(1)[0].id == R1


def test_content_excluded_folder_pushes_metadata_only() -> None:
    rig = Rig(cfg=Config(machine="pc1", content_exclude=["/home/demo/secret"]))
    rig.app.items = [_item(R1, LAB1, updated=START - 600, cwd="/home/demo/secret/x")]
    rig.remote.poll()
    rig.now = START + 60
    rig.app.items = [_item(R1, LAB1, updated=START + 50, cwd="/home/demo/secret/x",
                           status={"type": "active", "activeFlags": []})]
    rig.app.turns[R1] = [_turn("t2", "inProgress", started=START + 40, completed=None, commentary=("secret plan",)),
                         _turn("t1", "completed", started=START + 5, completed=START + 30, final="secret answer")]
    rig.remote.poll()
    assert rig.events[0].final_text is None and "内容不推送" in rig.events[0].labels
    assert rig.remote.view(R1).detail == ""
    turns = rig.remote.read_turns(R1, 5)
    assert all(turn["final_message"] is None and turn["user_text"] == "" and turn["commentary"] == []
               for turn in turns)
    assert all(turn["content_excluded"] for turn in turns)


def test_unavailable_host_marks_its_threads() -> None:
    rig = Rig()
    rig.app.items = [_item(R1, LAB1, updated=START - 60)]
    rig.app.unavailable = [LAB1]
    rig.remote.poll()
    view = rig.remote.view(R1)
    assert view.state == "unknown" and view.detail == "远程主机连不上"
    assert rig.remote.status()["unavailable"] == ["lab1"]


def test_read_turns_and_full_text() -> None:
    rig = Rig()
    rig.app.items = [_item(R1, LAB1, updated=START - 60)]
    rig.app.turns[R1] = [
        _turn("t3", "inProgress", started=START - 5, completed=None, commentary=("working",)),
        _turn("t2", "completed", started=START - 50, completed=START - 40, final="second"),
        _turn("t1", "completed", started=START - 90, completed=START - 80, final="first", user="hello"),
    ]
    rig.remote.poll()
    turns = rig.remote.read_turns(R1, 3)
    assert [turn["turn_id"] for turn in turns] == ["t1", "t2", "t3"]
    assert turns[0]["user_text"] == "hello" and turns[0]["final_message"] == "first"
    assert turns[2]["status"] == "running" and turns[2]["commentary"] == ["working"]
    assert rig.remote.full_text(R1) == "second"
    rig.remote.read_turns(R1, 50)
    assert rig.app.calls[-1][1]["turnLimit"] == 10 and rig.app.calls[-1][1]["hostId"] == LAB1
    assert rig.remote.read_turns(LOCAL, 3) == [] and rig.remote.view(LOCAL) is None


def test_a_failed_call_reopens_the_session() -> None:
    rig = Rig()
    rig.app.items = [_item(R1, LAB1, updated=START - 60)]
    rig.app.fail["list_threads:"] = codex_app_client.AppToolsError("pipe gone")
    with pytest.raises(codex_app_client.AppToolsError):
        rig.remote.poll()
    assert rig.app.closed == 1 and "pipe gone" in rig.remote.status()["last_error"]
    with pytest.raises(cr.ListUnavailable):
        rig.remote.poll()  # the listing waits for its retry
    rig.now += cr.LIST_RETRY_MIN_S
    rig.remote.poll()
    assert len(rig.app.opened) == 2 and rig.remote.status()["last_error"] == ""


def test_one_unreadable_thread_does_not_stop_the_others() -> None:
    rig = Rig()
    rig.app.items = [_item(R1, LAB1, updated=START + 10), _item(R2, LAB2, updated=START + 10)]
    rig.app.turns[R2] = [_turn("t1", "completed", started=START + 1, completed=START + 10, final="ok")]
    rig.now = START + 20
    rig.app.fail[f"read_thread:{R1}"] = TimeoutError("slow host")
    rig.remote.poll()
    assert [event.session_id for event in rig.events] == [R2]
    assert "lab1" in rig.remote.status()["last_error"]
    # R1 was not marked as read: the next poll tries it again.
    rig.app.turns[R1] = [_turn("t9", "completed", started=START + 2, completed=START + 10, final="late")]
    rig.remote.poll()
    assert [event.turn_id for event in rig.events] == ["t1", "t9"]


def test_known_threads_are_still_read_while_the_listing_hangs() -> None:
    rig = Rig()
    running = {"type": "active", "activeFlags": []}
    rig.app.items = [_item(R1, LAB1, updated=START + 10, status=running), _item(R2, LAB2, updated=START - 600)]
    rig.app.turns[R1] = [_turn("t1", "inProgress", started=START + 5, completed=None)]
    rig.now = START + 20
    rig.remote.poll()
    list_calls = [args for name, args, _ in rig.app.calls if name == "list_threads"]
    assert len(list_calls) == 1
    # From now on list_threads times out (Codex's thread index), read_thread still answers.
    rig.app.fail["list_threads:"] = codex_app_client.AppToolsError("list_threads 超时")
    rig.app.turns[R1].insert(0, _turn("t1", "completed", started=START + 5, completed=START + 40, final="done"))
    rig.now = START + 60
    with pytest.raises(cr.ListUnavailable):
        rig.remote.poll()
    assert [event.turn_id for event in rig.events if isinstance(event, TurnEvent)] == ["t1"]
    assert "超时" in rig.remote.status()["list_error"]
    # The listing waits for its retry; the idle threads are read every RECHECK_IDLE_S meanwhile.
    reads = len(rig.reads())
    rig.now = START + 90
    with pytest.raises(cr.ListUnavailable):
        rig.remote.poll()
    assert len(rig.reads()) == reads and sum(name == "list_threads" for name, _, _ in rig.app.calls) == 2
    rig.app.turns[R2] = [_turn("t7", "completed", started=START + 100, completed=START + 200, final="new task")]
    rig.now = START + 60 + cr.RECHECK_IDLE_S
    rig.app.fail["list_threads:"] = codex_app_client.AppToolsError("list_threads 超时")
    with pytest.raises(cr.ListUnavailable):
        rig.remote.poll()
    assert [event.turn_id for event in rig.events if isinstance(event, TurnEvent)] == ["t1", "t7"]
    # The listing is back: nothing is reported twice and the error clears.
    rig.now += cr.LIST_RETRY_MAX_S
    rig.remote.poll()
    assert len([event for event in rig.events if isinstance(event, TurnEvent)]) == 2
    assert rig.remote.status()["list_error"] == "" and rig.remote.status()["last_error"] == ""


def test_the_listing_gets_more_time_than_a_read() -> None:
    timeouts: dict[str, float | None] = {}

    class TimedSession(FakeSession):
        def call_tool(self, name: str, arguments: dict[str, Any], *, thread_id: str | None = None,
                      timeout_s: float | None = None) -> dict[str, Any]:
            timeouts[name] = timeout_s
            return super().call_tool(name, arguments, thread_id=thread_id, timeout_s=timeout_s)

    rig = Rig()
    rig.app.open_session = lambda *, thread_id, timeout_s: TimedSession(rig.app)  # type: ignore[method-assign]
    rig.remote._open_session = rig.app.open_session
    rig.app.items = [_item(R1, LAB1, updated=START + 10)]
    rig.now = START + 20
    rig.remote.poll()
    assert timeouts == {"list_threads": cr.LIST_TIMEOUT_S, "read_thread": cr.CALL_TIMEOUT_S}


def test_no_anchor_or_no_desktop() -> None:
    rig = Rig(anchor=None)
    with pytest.raises(codex_app_client.AppToolsError):
        rig.remote.poll()
    rig = Rig()
    rig.running = False
    rig.remote.poll()
    assert rig.app.calls == [] and rig.app.opened == []


def test_codex_source_merges_and_routes_remote_threads() -> None:
    rig = Rig()
    rig.app.items = [_item(R1, LAB1, updated=START - 60)]
    rig.app.turns[R1] = [_turn("t1", "completed", started=START - 90, completed=START - 80, final="answer")]
    source = CodexSource(rig.cfg, rig.events.append, load_threads=lambda: [], main_pid=lambda: None,
                         busy_check=lambda _thread_id: None, remote=rig.remote)
    source.poll_remote()
    assert [view.id for view in source.sessions(5)] == [R1]
    assert source.view(R1).kind == "remote:lab1"
    assert source.read_turns(R1, 1)[0]["final_message"] == "answer"
    assert source.full_text(R1) == "answer"
    assert source.view(LOCAL) is None


def test_codex_source_builds_the_remote_poller_from_the_config() -> None:
    def make(cfg: Config) -> CodexSource:
        return CodexSource(cfg, lambda _event: None, load_threads=lambda: [], main_pid=lambda: None,
                           busy_check=lambda _thread_id: None)

    assert isinstance(make(Config(machine="pc1")).remote, cr.RemoteCodex)
    assert make(Config(machine="pc1", codex_remote_enabled=False)).remote is None


def test_remote_commentary_of_running_turns_is_reported() -> None:
    rig = Rig()
    active = {"type": "active", "activeFlags": []}
    rig.app.items = [_item(R1, LAB1, updated=START - 10, status=active)]
    rig.app.turns[R1] = [_turn("t1", "inProgress", started=START - 30, completed=None, commentary=("旧的",))]
    rig.remote.poll()
    assert [event for event in rig.events if isinstance(event, ProgressEvent)] == []  # running at start: only newer commentary counts
    rig.app.turns[R1] = [_turn("t1", "inProgress", started=START - 30, completed=None,
                               commentary=("旧的", "请在远程机器上确认端口"))]
    rig.remote.poll()
    (event,) = [event for event in rig.events if isinstance(event, ProgressEvent)]
    assert event.texts == ["请在远程机器上确认端口"] and event.asks_user and event.title == "lab1·demo-task"
    rig.remote.poll()
    assert len([event for event in rig.events if isinstance(event, ProgressEvent)]) == 1


def test_remote_finished_turn_is_pushed_before_the_next_turns_progress() -> None:
    rig = Rig()
    active = {"type": "active", "activeFlags": []}
    rig.app.items = [_item(R1, LAB1, updated=START - 600)]
    rig.remote.poll()
    rig.now = START + 200
    rig.app.items = [_item(R1, LAB1, updated=START + 150, status=active)]
    rig.app.turns[R1] = [
        _turn("t2", "inProgress", started=START + 120, completed=None, commentary=("开始第二轮",)),
        _turn("t1", "completed", started=START + 20, completed=START + 100, final="第一轮结果"),
    ]
    rig.remote.poll()
    assert [type(event).__name__ for event in rig.events] == ["TurnEvent", "ProgressEvent"]

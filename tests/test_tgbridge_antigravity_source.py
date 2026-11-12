from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

import agent_telegram.tgbridge.antigravity_source as ags
from agent_telegram.tgbridge.antigravity_source import AntigravitySource, default_home, find_stores
from agent_telegram.tgbridge.codex_source import LABEL_EXCLUDED
from agent_telegram.tgbridge.model import ANTIGRAVITY, ProgressEvent, TurnEvent


SCHEMA = """CREATE TABLE conversation_summaries (
conversation_id text, title text NOT NULL DEFAULT '', preview text NOT NULL DEFAULT '',
step_count integer NOT NULL DEFAULT 0, last_modified_time datetime NOT NULL,
workspace_uris text NOT NULL, status text NOT NULL DEFAULT '', source text NOT NULL DEFAULT '',
project_id text NOT NULL DEFAULT '', agent_name text NOT NULL DEFAULT '',
parent_conversation_id text NOT NULL DEFAULT '', nesting_depth integer NOT NULL DEFAULT 0,
battle_id text NOT NULL DEFAULT '', winning_conversation_id text NOT NULL DEFAULT '',
not_fully_idle numeric NOT NULL DEFAULT false, killed numeric NOT NULL DEFAULT false,
last_user_input_time datetime NOT NULL, last_user_input_step_index integer NOT NULL DEFAULT -1,
app_data_dir text NOT NULL DEFAULT '', raw_summary blob, group_id text NOT NULL DEFAULT '',
PRIMARY KEY (conversation_id))"""

NOW = 1_791_500_000.0


class Clock:
    def __init__(self, value: float = NOW) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeConfig:
    active_window_h = 24.0
    progress_enabled = True
    antigravity_home = ""

    def __init__(self, excluded: list[str] | None = None) -> None:
        self.content_exclude = excluded or []

    def excluded(self, cwd: str) -> bool:
        return any(cwd.lower().startswith(item.lower()) for item in self.content_exclude)


def _stamp(epoch: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def step(index: int, source: str, typ: str, *, content: str = "", at: float = NOW,
         tools: list[dict[str, object]] | None = None, **extra: object) -> dict[str, object]:
    result: dict[str, object] = {"step_index": index, "source": source, "type": typ,
                                 "status": "DONE", "created_at": _stamp(at)}
    if content:
        result["content"] = content
    if tools is not None:
        result["tool_calls"] = tools
    result.update(extra)
    return result


class Store:
    def __init__(self, root: Path, name: str = "desktop") -> None:
        self.root = root / name
        self.name = name
        (self.root / "brain").mkdir(parents=True)
        with sqlite3.connect(self.root / "conversation_summaries.db") as db:
            db.execute(SCHEMA)

    def transcript(self, cid: str) -> Path:
        path = self.root / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def add(self, cid: str, *, title: str = "Demo", cwd: str = "C:/proj/app",
            status: str = "CASCADE_RUN_STATUS_RUNNING", count: int = 0,
            modified: str = "2026-10-09 08:34:55.3631099+00:00", parent: str = "",
            not_idle: int = 0, killed: int = 0) -> Path:
        path = self.transcript(cid)
        row = (cid, title, "", count, modified, json.dumps(["file:///" + cwd.replace(":", "%3A")]),
               status, "", "", "", parent, 0, "", "", not_idle, killed, modified, -1, "", None, "")
        with sqlite3.connect(self.root / "conversation_summaries.db") as db:
            db.execute("INSERT OR REPLACE INTO conversation_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        return path

    def update(self, cid: str, **values: object) -> None:
        with sqlite3.connect(self.root / "conversation_summaries.db") as db:
            for key, value in values.items():
                db.execute(f"UPDATE conversation_summaries SET {key}=? WHERE conversation_id=?", (value, cid))

    def append(self, cid: str, *items: dict[str, object], newline: bool = True) -> None:
        path = self.transcript(cid)
        with path.open("ab") as stream:
            for item in items:
                data = json.dumps(item, ensure_ascii=False).encode()
                stream.write(data + (b"\n" if newline else b""))


@pytest.fixture
def rig(tmp_path: Path):
    home = tmp_path / ".gemini"
    desktop = Store(home, "antigravity")
    cli = Store(home, "antigravity-cli")
    clock = Clock()
    events: list[object] = []
    source = AntigravitySource(FakeConfig(), events.append, home=home, clock=clock)
    return source, events, desktop, cli, clock


def test_default_home_and_store_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "gem"
    (home / "antigravity-cli").mkdir(parents=True)
    (home / "antigravity").mkdir()
    cfg = FakeConfig()
    cfg.antigravity_home = str(home)
    assert default_home(cfg) == home
    assert [name for name, _ in find_stores(home)] == ["desktop", "cli"]


@pytest.mark.parametrize(
    ("user_text", "final_text", "expected"),
    [
        (" [agent-mailbox tick] claim work", "MAILBOX_IDLE", True),
        ("[agent-mailbox tick]", "`MAILBOX_IDLE`", True),
        ("[agent-mailbox tick]", "**MAILBOX_IDLE！**", True),
        ("[agent-mailbox tick]", "MAILBOX_IDLE.", True),
        ("[agent-mailbox tick]", "MAILBOX_IDLE and no task", False),
        ("ordinary request", "MAILBOX_IDLE", False),
    ],
)
def test_mailbox_tick_helpers(user_text: str, final_text: str, expected: bool) -> None:
    assert ags.is_mailbox_tick(user_text) is (user_text.strip().startswith(ags.MAILBOX_TICK_MARKER))
    assert ags.is_mailbox_idle(user_text, final_text) is expected


def test_mailbox_idle_tick_has_no_progress_or_turn_push(rig) -> None:
    source, events, desktop, _cli, clock = rig
    source.poll()
    desktop.add("idle-tick", count=3, status="CASCADE_RUN_STATUS_RUNNING")
    desktop.append(
        "idle-tick",
        step(0, "USER_EXPLICIT", "USER_INPUT", content="[agent-mailbox tick]"),
        step(1, "MODEL", "PLANNER_RESPONSE", content="checking mailbox", tools=[{"name": "read"}], at=NOW + 1),
    )
    clock.value += 1
    source.poll()
    assert not [event for event in events if isinstance(event, (ProgressEvent, TurnEvent))]

    desktop.append("idle-tick", step(2, "MODEL", "PLANNER_RESPONSE", content="MAILBOX_IDLE", at=NOW + 2))
    desktop.update("idle-tick", status="CASCADE_RUN_STATUS_IDLE", step_count=3)
    clock.value += 1
    source.poll()
    assert not [event for event in events if isinstance(event, (ProgressEvent, TurnEvent))]
    assert ("idle-tick", "0") in source._emitted

    source.poll()
    assert not [event for event in events if isinstance(event, (ProgressEvent, TurnEvent))]


def test_mailbox_tick_that_runs_a_task_pushes_only_final_turn(rig) -> None:
    source, events, desktop, _cli, clock = rig
    source.poll()
    desktop.add("task-tick", count=3, status="CASCADE_RUN_STATUS_RUNNING")
    desktop.append(
        "task-tick",
        step(0, "USER_EXPLICIT", "USER_INPUT", content="[agent-mailbox tick] claim one task"),
        step(1, "MODEL", "PLANNER_RESPONSE", content="claimed task", tools=[{"name": "run"}], at=NOW + 1),
    )
    clock.value += 1
    source.poll()
    assert not [event for event in events if isinstance(event, ProgressEvent)]

    desktop.append("task-tick", step(2, "MODEL", "PLANNER_RESPONSE", content="task result", at=NOW + 2))
    desktop.update("task-tick", status="CASCADE_RUN_STATUS_IDLE", step_count=3)
    clock.value += 1
    source.poll()
    progress = [event for event in events if isinstance(event, ProgressEvent)]
    turns = [event for event in events if isinstance(event, TurnEvent) and event.session_id == "task-tick"]
    assert progress == []
    assert len(turns) == 1 and turns[0].final_text == "task result"


def test_history_is_baselined_and_new_turn_is_reported(rig) -> None:
    source, events, desktop, _cli, clock = rig
    path = desktop.add("conv-1", title="标题", count=3)
    desktop.append("conv-1", step(0, "USER_EXPLICIT", "USER_INPUT", content="old"),
                   step(1, "MODEL", "PLANNER_RESPONSE", content="old progress", tools=[{"name": "run"}]),
                   step(2, "MODEL", "PLANNER_RESPONSE", content="old final"))
    source.poll()
    assert events == []
    desktop.append("conv-1", step(3, "USER_EXPLICIT", "USER_INPUT", content="new", at=NOW + 1),
                   step(4, "MODEL", "PLANNER_RESPONSE", content="working", at=NOW + 2, tools=[{"name": "run"}], thinking="secret"))
    desktop.update("conv-1", status="CASCADE_RUN_STATUS_RUNNING", step_count=5,
                   last_modified_time="2026-10-09 08:34:58.3631099+00:00")
    clock.value += 2
    source.poll()
    progress = [e for e in events if isinstance(e, ProgressEvent)]
    assert len(progress) == 1 and progress[0].texts == ["working"] and "secret" not in repr(progress[0])
    desktop.append("conv-1", step(5, "MODEL", "PLANNER_RESPONSE", content="answer", at=NOW + 3))
    desktop.update("conv-1", status="CASCADE_RUN_STATUS_IDLE", step_count=6,
                   last_modified_time="2026-10-09 08:35:00.3631099+00:00")
    clock.value += 1
    source.poll()
    turns = [e for e in events if isinstance(e, TurnEvent)]
    assert len(turns) == 1
    assert turns[0].status == "completed" and turns[0].final_text == "answer"
    assert turns[0].cwd == "C:\\proj\\app" and turns[0].title == "标题"
    assert turns[0].started_at == NOW + 1 and turns[0].completed_at == NOW + 3
    source.poll()
    assert len([e for e in events if isinstance(e, TurnEvent)]) == 1
    assert path.exists()


def test_finish_rules_errors_abort_and_not_fully_idle(rig) -> None:
    source, events, desktop, _cli, clock = rig
    source.poll()
    desktop.add("conv-2", count=2, not_idle=1)
    desktop.append("conv-2", step(0, "USER_EXPLICIT", "USER_INPUT", content="q", at=NOW + 1),
                   step(1, "MODEL", "PLANNER_RESPONSE", content="answer", at=NOW + 2))
    source.poll()
    assert not [e for e in events if isinstance(e, TurnEvent)]
    desktop.update("conv-2", not_fully_idle=0, status="CASCADE_RUN_STATUS_IDLE")
    source.poll()
    assert [e.status for e in events if isinstance(e, TurnEvent)] == ["completed"]
    desktop.add("conv-3", count=2, killed=1)
    desktop.append("conv-3", step(0, "USER_EXPLICIT", "USER_INPUT", content="q", at=NOW + 1),
                   step(1, "MODEL", "GENERIC", content="stopped", at=NOW + 2))
    desktop.update("conv-3", status="CASCADE_RUN_STATUS_IDLE")
    source.poll()
    assert [e.status for e in events if isinstance(e, TurnEvent) and e.session_id == "conv-3"] == ["aborted"]


def test_error_message_is_failed_with_previous_answer(rig) -> None:
    source, events, desktop, _cli, clock = rig
    source.poll()
    desktop.add("error", count=3)
    desktop.append("error", step(0, "USER_EXPLICIT", "USER_INPUT", content="q", at=NOW + 1),
                   step(1, "MODEL", "PLANNER_RESPONSE", content="partial", at=NOW + 2),
                   step(2, "SYSTEM", "ERROR_MESSAGE", content="failed", at=NOW + 3, error="Access denied"))
    desktop.update("error", status="CASCADE_RUN_STATUS_IDLE", step_count=3,
                   last_modified_time="2026-10-09 06:53:24.3631099+00:00")
    clock.value += 3
    source.poll()
    event = next(e for e in events if isinstance(e, TurnEvent) and e.session_id == "error")
    assert event.status == "failed" and event.error == "Access denied" and event.final_text == "partial"


def test_error_full_content_exclusion_and_reading(rig) -> None:
    source, events, desktop, _cli, clock = rig
    source.cfg.content_exclude = ["C:\\secret"]
    source.poll()
    desktop.add("conv-4", count=2, cwd="C:/secret", title="")
    desktop.append("conv-4", step(0, "USER_EXPLICIT", "USER_INPUT", content="ask", at=NOW + 1),
                   step(1, "MODEL", "PLANNER_RESPONSE", content="short", at=NOW + 2, truncated_fields=["content"]))
    full = desktop.transcript("conv-4").with_name("transcript_full.jsonl")
    full.write_bytes((json.dumps(step(1, "MODEL", "PLANNER_RESPONSE", content="full answer")) + "\n").encode())
    desktop.update("conv-4", status="CASCADE_RUN_STATUS_IDLE", last_modified_time="2026-10-09 06:53:22.3631099+00:00")
    source.poll()
    event = next(e for e in events if isinstance(e, TurnEvent) and e.session_id == "conv-4")
    assert event.final_text is None and LABEL_EXCLUDED in event.labels
    items = source.read_turns("conv-4", 1)
    assert items[0]["content_excluded"] and items[0]["user_text"] == "" and items[0]["final_message"] is None
    assert source.full_text("conv-4") is None


def test_partial_lines_new_conversations_cli_and_subagents(rig) -> None:
    source, events, desktop, cli, clock = rig
    desktop.add("parent", count=2)
    desktop.append("parent", step(0, "USER_EXPLICIT", "USER_INPUT", content="old"),
                   step(1, "MODEL", "PLANNER_RESPONSE", content="done"))
    source.poll()
    cli.add("child", parent="parent", count=2)
    cli.append("child", step(0, "USER_EXPLICIT", "USER_INPUT", content="ignored"), newline=False)
    source.poll()
    assert source.view("child") is None
    cli.add("cli-1", count=1, title="CLI")
    cli.append("cli-1", step(0, "USER_EXPLICIT", "USER_INPUT", content="new", at=NOW + 1), newline=False)
    clock.value += 1
    source.poll()
    assert not [e for e in events if isinstance(e, TurnEvent) and e.session_id == "cli-1"]
    with cli.transcript("cli-1").open("ab") as stream:
        stream.write(b"\n")
    cli.append("cli-1", step(1, "MODEL", "PLANNER_RESPONSE", content="done", at=NOW + 2))
    cli.update("cli-1", status="CASCADE_RUN_STATUS_IDLE", step_count=2)
    clock.value += 1
    source.poll()
    view = source.view("cli-1")
    assert view is not None and view.kind == "cli" and view.live is False


def test_fallback_index_settle_and_status_shapes(tmp_path: Path) -> None:
    home = tmp_path / ".gemini"
    store = Store(home, "antigravity")
    cid = "fallback"
    source_events: list[object] = []
    clock = Clock(NOW)
    source = AntigravitySource(FakeConfig(), source_events.append, home=home, clock=clock)
    source.poll()
    path = store.transcript(cid)
    path.write_bytes((json.dumps(step(0, "USER_EXPLICIT", "USER_INPUT", content="fallback", at=NOW)) + "\n" +
                     json.dumps(step(1, "MODEL", "PLANNER_RESPONSE", content="done", at=NOW + 1)) + "\n").encode())
    os.utime(path, (NOW, NOW))
    import gc
    gc.collect()
    (store.root / "conversation_summaries.db").unlink()
    path.touch()
    source.poll()
    assert source.last_error and source.status()["stores"] == ["desktop"]
    clock.value += 16
    source.poll()
    assert [e.status for e in source_events if isinstance(e, TurnEvent)] == ["completed"]
    assert source.quota() is None


def test_newer_user_input_drops_previous_turn(rig) -> None:
    source, events, desktop, _cli, clock = rig
    desktop.add("drop", count=3)
    desktop.append("drop", step(0, "USER_EXPLICIT", "USER_INPUT", content="first"))
    source.poll()
    desktop.append("drop", step(1, "USER_EXPLICIT", "USER_INPUT", content="second", at=NOW + 1),
                   step(2, "MODEL", "PLANNER_RESPONSE", content="answer", at=NOW + 2))
    desktop.update("drop", status="CASCADE_RUN_STATUS_IDLE", step_count=3)
    clock.value += 2
    source.poll()
    turns = [e for e in events if isinstance(e, TurnEvent)]
    assert len(turns) == 1 and turns[0].turn_id == "1"


def test_new_conversation_reports_all_finished_post_baseline_turns(rig) -> None:
    source, events, desktop, _cli, clock = rig
    source.poll()
    desktop.add("multi", count=4, status="CASCADE_RUN_STATUS_IDLE")
    desktop.append("multi", step(0, "USER_EXPLICIT", "USER_INPUT", content="one", at=NOW + 1),
                   step(1, "MODEL", "PLANNER_RESPONSE", content="first", at=NOW + 2),
                   step(2, "USER_EXPLICIT", "USER_INPUT", content="two", at=NOW + 3),
                   step(3, "MODEL", "PLANNER_RESPONSE", content="second", at=NOW + 4))
    desktop.update("multi", last_modified_time="2026-10-09 06:53:26.3631099+00:00")
    clock.value += 4
    source.poll()
    turns = [e for e in events if isinstance(e, TurnEvent) and e.session_id == "multi"]
    assert [item.turn_id for item in turns] == ["0", "2"]


def test_reading_sessions_views_and_limits(rig) -> None:
    source, _events, desktop, _cli, _clock = rig
    desktop.add("list-1", title="One", count=2, status="CASCADE_RUN_STATUS_IDLE")
    desktop.append("list-1", step(0, "USER_EXPLICIT", "USER_INPUT", content="u"),
                   step(1, "MODEL", "PLANNER_RESPONSE", content="f"))
    source.poll()
    views = source.sessions(10)
    assert views and views[0].agent == ANTIGRAVITY and views[0].live is False
    assert source.sessions(0) == []
    assert source.view("LIST-1") is not None
    items = source.read_turns("list-1", 20)
    assert items[0]["final_message"] == "f" and source.full_text("list-1") == "f"
    status = source.status()
    assert status["conversations"] == 1 and status["stores"] == ["desktop", "cli"]


def test_full_text_skips_a_running_last_turn(rig) -> None:
    source, _events, desktop, _cli, clock = rig
    source.poll()
    desktop.add("history", count=4, status="CASCADE_RUN_STATUS_RUNNING")
    desktop.append("history", step(0, "USER_EXPLICIT", "USER_INPUT", content="old", at=NOW + 1),
                   step(1, "MODEL", "PLANNER_RESPONSE", content="old answer", at=NOW + 2),
                   step(2, "USER_EXPLICIT", "USER_INPUT", content="new", at=NOW + 3),
                   step(3, "MODEL", "PLANNER_RESPONSE", content="working", at=NOW + 4, tools=[{"name": "run"}]))
    desktop.update("history", last_modified_time="2026-10-09 06:53:25.3631099+00:00")
    clock.value += 4
    source.poll()
    assert source.full_text("history") == "old answer"
    assert [item["status"] for item in source.read_turns("history", 2)] == ["completed", "running"]


def test_index_is_read_only(rig) -> None:
    source, _events, desktop, _cli, _clock = rig
    db = desktop.root / "conversation_summaries.db"
    before = (db.stat().st_mtime_ns, db.read_bytes())
    source.poll()
    after = (db.stat().st_mtime_ns, db.read_bytes())
    assert before == after


def test_state_keeps_only_slim_current_turn_after_repeated_polls(rig) -> None:
    source, events, desktop, _cli, clock = rig
    source.poll()
    desktop.add("slim", count=3, status="CASCADE_RUN_STATUS_RUNNING")
    desktop.append("slim", step(0, "USER_EXPLICIT", "USER_INPUT", content="q", at=NOW + 1),
                   step(1, "MODEL", "PLANNER_RESPONSE", content="working", at=NOW + 2,
                        tools=[{"name": "run_command", "args": {"secret": "x"}}], thinking="private"))
    desktop.update("slim", last_modified_time="2026-10-09 06:53:27.3631099+00:00")
    clock.value += 2
    source.poll()
    desktop.append("slim", step(2, "MODEL", "PLANNER_RESPONSE", content="done", at=NOW + 3))
    desktop.update("slim", status="CASCADE_RUN_STATUS_IDLE", step_count=3)
    clock.value += 1
    source.poll()
    for index in range(3, 8):
        desktop.append("slim", step(index, "SYSTEM", "CHECKPOINT", content=f"checkpoint-{index}", at=NOW + index,
                                      thinking="private", media={"raw": "x"}, token_count=100))
        clock.value += 1
        source.poll()
    state = source._states["slim"]
    assert [item["step_index"] for item in state.steps] == [0, 1, 2, 3, 4, 5, 6, 7]
    allowed = {"step_index", "source", "type", "status", "created_at", "content", "error", "truncated_fields", "tool_calls"}
    assert all(set(item) <= allowed and "thinking" not in item and item.get("tool_calls") in (None, True, False)
               for item in state.steps)


def test_baseline_stream_keeps_only_last_historical_turn(rig) -> None:
    source, _events, desktop, _cli, _clock = rig
    desktop.add("many", count=6)
    for turn in range(3):
        base = turn * 2
        desktop.append("many", step(base, "USER_EXPLICIT", "USER_INPUT", content=f"q{turn}"),
                       step(base + 1, "MODEL", "PLANNER_RESPONSE", content=f"a{turn}",
                            tools=[{"name": "run"}], thinking="private"))
    source.poll()
    state = source._states["many"]
    assert [item["step_index"] for item in state.steps] == [4, 5]
    assert all("thinking" not in item and not isinstance(item.get("tool_calls"), list) for item in state.steps)


def test_initial_baseline_waits_for_a_trailing_partial_line(rig) -> None:
    source, _events, desktop, _cli, clock = rig
    desktop.add("partial-initial", count=1)
    path = desktop.transcript("partial-initial")
    path.write_bytes((json.dumps(step(0, "USER_EXPLICIT", "USER_INPUT", content="q")) + "\n" +
                     json.dumps(step(1, "MODEL", "PLANNER_RESPONSE", content="unfinished"))).encode())
    source.poll()
    assert [item["step_index"] for item in source._states["partial-initial"].steps] == [0]
    with path.open("ab") as stream:
        stream.write(b"\n")
    source.poll()
    assert [item["step_index"] for item in source._states["partial-initial"].steps] == [0, 1]


def test_full_transcript_is_targeted_only_for_truncated_result(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    source, events, desktop, _cli, clock = rig
    source.poll()
    desktop.add("target", count=2, status="CASCADE_RUN_STATUS_IDLE")
    desktop.append("target", step(0, "USER_EXPLICIT", "USER_INPUT", content="q", at=NOW + 1),
                   step(1, "MODEL", "PLANNER_RESPONSE", content="answer", at=NOW + 2))
    desktop.update("target", last_modified_time="2026-10-09 06:53:28.3631099+00:00")
    calls: list[int] = []
    original = ags.AntigravitySource._lookup_full_step

    def counted(path: Path | None, index: int) -> str | None:
        calls.append(index)
        return original(path, index)

    monkeypatch.setattr(ags.AntigravitySource, "_lookup_full_step", staticmethod(counted))
    source.poll()
    assert calls == []
    source.poll()
    assert calls == []
    desktop.append("target", step(2, "SYSTEM", "CHECKPOINT", content="tail"))
    source.poll()
    assert calls == []
    desktop.add("truncated", count=2, status="CASCADE_RUN_STATUS_IDLE")
    desktop.append("truncated", step(0, "USER_EXPLICIT", "USER_INPUT", content="q", at=NOW + 3),
                   step(1, "MODEL", "PLANNER_RESPONSE", content="short", at=NOW + 4,
                        truncated_fields=["content"]))
    full = desktop.transcript("truncated").with_name("transcript_full.jsonl")
    full.write_bytes((json.dumps(step(1, "MODEL", "PLANNER_RESPONSE", content="full answer")) + "\n").encode())
    desktop.update("truncated", last_modified_time="2026-10-09 06:53:29.3631099+00:00")
    clock.value += 4
    source.poll()
    assert calls == [1]
    source.poll()
    assert calls == [1]
    assert next(item for item in events if isinstance(item, TurnEvent) and item.session_id == "truncated").final_text == "full answer"


def test_full_lookup_scans_backwards_in_small_chunks(rig, monkeypatch: pytest.MonkeyPatch) -> None:
    source, events, desktop, _cli, clock = rig
    source.poll()
    desktop.add("chunks", count=2, status="CASCADE_RUN_STATUS_IDLE")
    desktop.append("chunks", step(0, "USER_EXPLICIT", "USER_INPUT", content="q", at=NOW + 1),
                   step(1, "MODEL", "PLANNER_RESPONSE", content="short", at=NOW + 2,
                        truncated_fields=["content"]))
    full = desktop.transcript("chunks").with_name("transcript_full.jsonl")
    lines = [json.dumps({"step_index": i, "content": "filler" * 20}) for i in range(20)]
    lines.insert(10, json.dumps({"step_index": 1, "content": "middle full answer"}))
    full.write_bytes(("\n".join(lines) + "\n").encode())
    desktop.update("chunks", last_modified_time="2026-10-09 06:53:30.3631099+00:00")
    monkeypatch.setattr(ags, "FULL_SCAN_CHUNK", 37)
    clock.value += 2
    source.poll()
    event = next(item for item in events if isinstance(item, TurnEvent) and item.session_id == "chunks")
    assert event.final_text == "middle full answer"


def test_emit_error_is_logged_and_does_not_drop_following_events(rig, caplog: pytest.LogCaptureFixture) -> None:
    source, _events, desktop, _cli, clock = rig
    source.poll()
    desktop.add("first", count=2, status="CASCADE_RUN_STATUS_IDLE")
    desktop.add("second", count=2, status="CASCADE_RUN_STATUS_IDLE")
    desktop.append("first", step(0, "USER_EXPLICIT", "USER_INPUT", content="one", at=NOW + 1),
                   step(1, "MODEL", "PLANNER_RESPONSE", content="done", at=NOW + 2))
    desktop.append("second", step(0, "USER_EXPLICIT", "USER_INPUT", content="two", at=NOW + 1),
                   step(1, "MODEL", "PLANNER_RESPONSE", content="done", at=NOW + 2))
    desktop.update("first", last_modified_time="2026-10-09 06:53:31.3631099+00:00")
    desktop.update("second", last_modified_time="2026-10-09 06:53:31.3631099+00:00")
    delivered: list[str] = []

    def emit(event: object) -> None:
        delivered.append(getattr(event, "session_id", ""))
        if len(delivered) == 1:
            raise RuntimeError("sink failed")

    source.emit = emit
    clock.value += 2
    with caplog.at_level("ERROR", logger=ags.__name__):
        source.poll()
    assert delivered == ["first", "second"]
    assert "Antigravity turn event not delivered: RuntimeError: sink failed" in caplog.text


def test_user_request_wrapper_and_metadata_are_stripped(rig) -> None:
    source, events, desktop, _cli, _clock = rig
    source.poll()
    desktop.add("wrapped", title="", status="CASCADE_RUN_STATUS_IDLE", count=2)
    typed = ("<USER_REQUEST>\nFix the parser\nplease\n</USER_REQUEST>\n<ADDITIONAL_METADATA>\n"
             "The current local time is: 2026-10-08T11:23:13+08:00.\n</ADDITIONAL_METADATA>")
    desktop.append("wrapped", step(0, "USER_EXPLICIT", "USER_INPUT", content=typed),
                   step(1, "MODEL", "PLANNER_RESPONSE", content="Fixed."))
    source.poll()
    turn = next(event for event in events if isinstance(event, TurnEvent))
    assert turn.title == "Fix the parser"
    assert source.read_turns("wrapped", 1)[0]["user_text"] == "Fix the parser\nplease"


def test_query_index_skips_child_conversations(tmp_path: Path) -> None:
    home = tmp_path / ".gemini"
    store = Store(home, "antigravity")
    store.add("parent", title="Parent", count=0)
    store.add("child", title="Child", parent="parent", count=0)

    rows = ags.query_index("desktop", store.root)

    assert ags.ConversationMeta is ags._Meta
    assert set(rows) == {"parent"}
    assert rows["parent"].store == "desktop"
    assert rows["parent"].transcript == store.transcript("parent")


def test_fallback_index_reads_transcripts_without_database(tmp_path: Path) -> None:
    home = tmp_path / ".gemini"
    store = Store(home, "antigravity")
    transcript = store.transcript("fallback")
    store.append("fallback", step(0, "USER_EXPLICIT", "USER_INPUT", content="Fallback title"))
    os.utime(transcript, (NOW + 10, NOW + 10))
    import gc
    gc.collect()
    (store.root / "conversation_summaries.db").unlink()

    rows = ags.fallback_index("desktop", store.root)

    assert list(rows) == ["fallback"]
    assert rows["fallback"].title == "Fallback title"
    assert rows["fallback"].index_available is False
    assert rows["fallback"].last_modified == NOW + 10


def test_list_conversations_merges_stores_newest_first_and_falls_back(tmp_path: Path) -> None:
    home = tmp_path / ".gemini"
    desktop = Store(home, "antigravity")
    cli = Store(home, "antigravity-cli")
    desktop.add("older", title="Older", modified="2026-10-09 08:34:55.3631099+00:00")
    cli.transcript("newer")
    cli.append("newer", step(0, "USER_EXPLICIT", "USER_INPUT", content="Newer"))
    cli_db = cli.root / "conversation_summaries.db"
    import gc
    gc.collect()
    cli_db.unlink()
    os.utime(cli.transcript("newer"), (NOW + 100000, NOW + 100000))

    rows = ags.list_conversations(home)

    assert [row.conversation_id for row in rows] == ["newer", "older"]
    assert rows[0].index_available is False
    assert rows[1].index_available is True


def test_read_conversation_restores_full_content_and_blanks_excluded(tmp_path: Path) -> None:
    home = tmp_path / ".gemini"
    store = Store(home, "antigravity")
    store.add("read", title="Read", status="CASCADE_RUN_STATUS_IDLE", count=3)
    store.append(
        "read",
        step(0, "USER_EXPLICIT", "USER_INPUT", content="short question", truncated_fields=["content"]),
        step(1, "MODEL", "PLANNER_RESPONSE", content="short progress", tools=[{"name": "run"}],
             truncated_fields=["content"]),
        step(2, "MODEL", "PLANNER_RESPONSE", content="short final", at=NOW + 1,
             truncated_fields=["content"]),
    )
    full = store.transcript("read").with_name("transcript_full.jsonl")
    full.write_text(
        json.dumps(step(0, "USER_EXPLICIT", "USER_INPUT", content="full question")) + "\n"
        + json.dumps(step(1, "MODEL", "PLANNER_RESPONSE", content="full progress", tools=[{"name": "run"}])) + "\n"
        + json.dumps(step(2, "MODEL", "PLANNER_RESPONSE", content="full answer")) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    meta = ags.query_index("desktop", store.root)["read"]

    items = ags.read_conversation(meta, 20, now=NOW)
    excluded = ags.read_conversation(meta, 20, excluded=True, now=NOW)

    assert items[0]["user_text"] == "full question"
    assert items[0]["final_message"] == "full answer"
    assert items[0]["commentary"] == ["full progress"]
    assert items[0]["content_excluded"] is False
    assert excluded[0]["user_text"] == ""
    assert excluded[0]["final_message"] is None
    assert excluded[0]["commentary"] == []
    assert excluded[0]["content_excluded"] is True


def test_archive_fallback_when_brain_directory_is_pruned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    home = tmp_path / ".gemini"
    arch_dir = tmp_path / "archive"
    monkeypatch.setenv("ANTIGRAVITY_ARCHIVE_DIR", str(arch_dir))

    store = Store(home, "antigravity")
    store.add("261025-conv", title="261025-blender-qa", status="CASCADE_RUN_STATUS_IDLE", count=2)
    store.append(
        "261025-conv",
        step(0, "USER_EXPLICIT", "USER_INPUT", content="<USER_REQUEST>\nBlender MCP setup\n</USER_REQUEST>"),
        step(1, "MODEL", "PLANNER_RESPONSE", content="Configured mcp-for-blender.", at=NOW + 1),
    )

    # Copy transcript and meta.json to archive, then delete brain/261025-conv to simulate PruneAll
    cid_arch = arch_dir / "261025-conv"
    cid_arch.mkdir(parents=True)
    shutil.copy2(store.transcript("261025-conv"), cid_arch / "transcript.jsonl")
    (cid_arch / "meta.json").write_text(
        json.dumps(
            {
                "conversation_id": "261025-conv",
                "title": "261025-blender-qa",
                "cwd": r"C:\projects",
                "status": "CASCADE_RUN_STATUS_IDLE",
                "step_count": 2,
                "last_modified": NOW + 1,
            }
        ),
        encoding="utf-8",
    )
    shutil.rmtree(store.root / "brain" / "261025-conv")
    assert not store.transcript("261025-conv").exists()

    # 1. Row still in conversation_summaries.db -> resolves transcript from archive_dir
    rows = ags.query_index("desktop", store.root)
    assert rows["261025-conv"].transcript == cid_arch / "transcript.jsonl"
    turns = ags.read_conversation(rows["261025-conv"], 5, now=NOW + 10)
    assert len(turns) == 1
    assert turns[0]["user_text"] == "Blender MCP setup"
    assert turns[0]["final_message"] == "Configured mcp-for-blender."

    # 2. Even if conversation_summaries.db is also removed -> _merge_archived_rows restores it
    import gc

    gc.collect()
    (store.root / "conversation_summaries.db").unlink()
    fb_rows = ags.fallback_index("desktop", store.root)
    assert "261025-conv" in fb_rows
    assert fb_rows["261025-conv"].title == "261025-blender-qa"
    fb_turns = ags.read_conversation(fb_rows["261025-conv"], 5, now=NOW + 10)
    assert len(fb_turns) == 1
    assert fb_turns[0]["final_message"] == "Configured mcp-for-blender."


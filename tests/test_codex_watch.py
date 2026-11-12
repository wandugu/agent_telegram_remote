from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import completed, delegated, event, started, user_message

from agent_telegram import codex_app_client
from agent_telegram import codex_watch as cw
from agent_telegram.codex_threads import AUTO_CONTINUE_MARKER, TurnCollector

T1 = "01a07078-0000-7000-8000-0000000000c1"
T2 = "01a07078-0000-7000-8000-0000000000d1"
FAILED_AT = 1_790_529_100.0  # completed_at written by conftest.completed()
OVERLOADED = {"message": "Selected model is at capacity. Please try a different model.",
              "codex_error_info": "server_overloaded"}


def failed(turn_id: str, at: float, error: dict[str, Any] | None = None) -> dict[str, Any]:
    return event("task_complete", turn_id, last_agent_message=None, error=error or OVERLOADED, completed_at=int(at))


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Harness:
    """A Watcher with a fake clock, sender, busy check and Codex process."""

    def __init__(self, tmp_path: Path, now: float, config: cw.WatchConfig | None = None, **options: Any) -> None:
        self.clock = Clock(now)
        self.sent: list[tuple[str, str]] = []
        self.busy: bool | None = False
        self.pid: int | None = 4242
        self.send_error: Exception | None = None
        self.log_dir = tmp_path / "watch-logs"
        self.watcher = cw.Watcher(
            config or cw.WatchConfig(), cw.EventLog(self.log_dir), clock=self.clock, send=self._send,
            busy_check=lambda _thread: self.busy, main_pid=lambda: self.pid, **options,
        )

    def _send(self, thread_id: str, text: str) -> dict[str, Any]:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((thread_id, text))
        return {"pipe": "codex-browser-use-test"}

    def poll(self, at: float | None = None) -> None:
        if at is not None:
            self.clock.now = at
        self.watcher.poll_once()

    def events(self, *kinds: str) -> list[dict[str, Any]]:
        path = self.log_dir / "incidents.jsonl"
        items = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
        return [item for item in items if not kinds or item["event"] in kinds]


@pytest.fixture
def running(codex_home) -> Path:
    """T1 is running its first turn; failures are appended by the tests."""

    return codex_home.add(T1, "demo-p18-v3", [started("t1"), user_message("t1", "修改论文")],
                          updated=int(FAILED_AT) - 100)


def test_capacity_failure_is_logged_and_continued_after_the_backoff(codex_home, running, tmp_path) -> None:
    h = Harness(tmp_path, FAILED_AT - 50)
    h.poll()
    assert h.events("failure_detected") == [] and h.sent == []

    codex_home.write(running, [failed("t1", FAILED_AT)])
    h.poll(FAILED_AT + 5)
    (detected,) = h.events("failure_detected")
    assert (detected["task"], detected["thread_id"], detected["attempt"]) == ("demo-p18-v3", T1, 1)
    assert (detected["reason"], detected["error_kind"]) == ("模型容量不足", "server_overloaded")
    assert detected["error"] == OVERLOADED["message"]
    assert detected["failed_at"] == cw._iso(FAILED_AT) and detected["cwd"] == "C:\\projects\\demo"
    h.poll(FAILED_AT + 14)
    assert h.sent == []

    h.poll(FAILED_AT + 15)
    ((thread_id, text),) = h.sent
    assert thread_id == T1 and text.startswith(AUTO_CONTINUE_MARKER) and "模型容量不足" in text
    codex_home.write(running, [started("t2"), delegated("t2", text), completed("t2", "改好了")])
    h.poll(FAILED_AT + 100)
    assert [item["event"] for item in h.events("continued", "recovered")] == ["continued", "recovered"]
    assert h.events("recovered")[0]["continued"] == 1
    log = (h.log_dir / "watch.log").read_text(encoding="utf-8")
    assert "[demo-p18-v3] 发现网络错误（模型容量不足）" in log and "已自动继续" in log


def test_retry_by_codex_before_the_deadline_cancels_the_continuation(codex_home, running, tmp_path) -> None:
    h = Harness(tmp_path, FAILED_AT - 50)
    h.poll()
    codex_home.write(running, [failed("t1", FAILED_AT), started("t2")])
    h.poll(FAILED_AT + 5)
    h.poll(FAILED_AT + 60)
    assert h.sent == []
    assert [item["event"] for item in h.events("failure_detected", "continue_skipped")] == [
        "failure_detected", "continue_skipped"]


@pytest.mark.parametrize(
    "error",
    [
        {"message": "You've hit your usage limit.", "codex_error_info": "usage_limit_exceeded"},
        {"message": "This content was flagged for possible cybersecurity risk.", "codex_error_info": "cyber_policy"},
        {"message": "unexpected status 404 Not Found: Unknown error", "codex_error_info": "other"},
    ],
)
def test_other_errors_are_logged_but_never_continued(codex_home, running, tmp_path, error) -> None:
    h = Harness(tmp_path, FAILED_AT - 50)
    h.poll()
    codex_home.write(running, [failed("t1", FAILED_AT, error)])
    for offset in (5, 60, 600):
        h.poll(FAILED_AT + offset)
    assert h.sent == []
    (ignored,) = h.events("failure_ignored")
    assert ignored["error"] == error["message"] and "attempt" not in ignored


def test_a_turn_the_user_stopped_is_not_continued(codex_home, running, tmp_path) -> None:
    h = Harness(tmp_path, FAILED_AT - 50)
    h.poll()
    codex_home.write(running, [event("turn_aborted", "t1", reason="interrupted")])
    h.poll(FAILED_AT + 5)
    h.poll(FAILED_AT + 600)
    assert h.sent == [] and h.events("failure_detected", "failure_ignored") == []


def test_repeated_failures_back_off_then_stop_until_the_user_steps_in(codex_home, running, tmp_path) -> None:
    h = Harness(tmp_path, FAILED_AT - 50, cw.WatchConfig(backoff_s=(15.0, 30.0), max_attempts=2))
    h.poll()
    codex_home.write(running, [failed("t1", FAILED_AT)])
    h.poll(FAILED_AT + 14)
    assert h.sent == []
    h.poll(FAILED_AT + 15)
    assert len(h.sent) == 1

    second = FAILED_AT + 100
    codex_home.write(running, [started("t2"), delegated("t2", h.sent[0][1]), failed("t2", second)])
    h.poll(second + 29)
    assert len(h.sent) == 1
    h.poll(second + 30)
    assert len(h.sent) == 2

    third = second + 100
    codex_home.write(running, [started("t3"), delegated("t3", h.sent[1][1]), failed("t3", third)])
    h.poll(third + 600)
    assert len(h.sent) == 2
    (gave_up,) = h.events("gave_up")
    assert gave_up["attempt"] == 3

    codex_home.write(running, [started("t4"), user_message("t4", "换个方法"), completed("t4", "好了")])
    h.poll(third + 700)
    (recovered,) = h.events("recovered")
    assert (recovered["attempt"], recovered["continued"]) == (3, 2)

    fourth = third + 800
    codex_home.write(running, [started("t5"), user_message("t5", "下一步"), failed("t5", fourth)])
    h.poll(fourth + 15)
    assert len(h.sent) == 3 and h.events("failure_detected")[-1]["attempt"] == 1


def test_default_config_continues_past_twenty_consecutive_failures(codex_home, running, tmp_path) -> None:
    h = Harness(tmp_path, FAILED_AT - 50)
    assert h.watcher.config.max_attempts == 0
    assert h.watcher.config.max_backoff_s == 30.0
    assert [h.watcher.config.delay(i) for i in (1, 2, 3, 5)] == [15.0, 30.0, 30.0, 30.0]
    h.poll()
    at = FAILED_AT
    codex_home.write(running, [failed("t1", at)])
    h.poll(at + 15)
    assert len(h.sent) == 1

    for idx in range(2, 26):
        at += 400
        codex_home.write(running, [started(f"t{idx}"), delegated(f"t{idx}", h.sent[-1][1]), failed(f"t{idx}", at)])
        h.poll(at + 30)
        assert len(h.sent) == idx
    assert h.events("gave_up") == []
    assert h.events("failure_detected")[-1]["attempt"] == 25


def test_backoff_and_attempts_env_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEX_WATCH_MAX_BACKOFF_S", "45")
    monkeypatch.setenv("CODEX_WATCH_BACKOFF_S", "10, 25, 60")
    monkeypatch.setenv("CODEX_WATCH_MAX_ATTEMPTS", "8")
    cfg = cw.WatchConfig(
        backoff_s=cw.default_backoff_s(),
        max_backoff_s=cw.default_max_backoff_s(),
        max_attempts=cw.default_max_attempts(),
    )
    assert cfg.backoff_s == (10.0, 25.0, 60.0)
    assert cfg.max_backoff_s == 45.0
    assert cfg.max_attempts == 8
    assert [cfg.delay(i) for i in (1, 2, 3, 4)] == [10.0, 25.0, 45.0, 45.0]


def test_new_user_input_starts_a_new_run_of_failures(codex_home, running, tmp_path) -> None:
    h = Harness(tmp_path, FAILED_AT - 50, cw.WatchConfig(backoff_s=(15.0, 30.0)))
    h.poll()
    codex_home.write(running, [failed("t1", FAILED_AT)])
    h.poll(FAILED_AT + 15)
    second = FAILED_AT + 100
    codex_home.write(running, [started("t2"), user_message("t2", "我换了个问题"), failed("t2", second)])
    h.poll(second + 15)
    assert len(h.sent) == 2
    assert [item["attempt"] for item in h.events("failure_detected")] == [1, 1]


def test_busy_conversation_is_left_alone(codex_home, running, tmp_path) -> None:
    h = Harness(tmp_path, FAILED_AT - 50)
    h.poll()
    codex_home.write(running, [failed("t1", FAILED_AT)])
    h.busy = True
    h.poll(FAILED_AT + 15)
    h.poll(FAILED_AT + 120)
    assert h.sent == []
    assert "正在运行" in h.events("continue_skipped")[0]["text"]


def test_second_failure_waits_past_codex_own_30_s_retry(codex_home, running, tmp_path) -> None:
    h = Harness(tmp_path, FAILED_AT - 50, cw.WatchConfig(max_backoff_s=0))
    h.poll()
    codex_home.write(running, [failed("t1", FAILED_AT)])
    h.poll(FAILED_AT + 15)
    second = FAILED_AT + 100
    codex_home.write(running, [started("t2"), delegated("t2", h.sent[0][1]), failed("t2", second)])
    h.poll(second + 30)
    assert len(h.sent) == 1  # Codex's on-screen countdown retries at 30 s
    codex_home.write(running, [started("t3")])
    h.poll(second + 45)
    assert len(h.sent) == 1 and "自行继续" in h.events("continue_skipped")[-1]["text"]


def test_retry_that_starts_during_the_busy_check_cancels_the_continuation(codex_home, running, tmp_path) -> None:
    h = Harness(tmp_path, FAILED_AT - 50)
    h.poll()
    codex_home.write(running, [failed("t1", FAILED_AT)])
    h.poll(FAILED_AT + 5)

    def busy_check(_thread_id: str) -> bool:
        codex_home.write(running, [started("t2")])  # Codex's own retry starts meanwhile
        return False

    h.watcher.busy_check = busy_check
    h.poll(FAILED_AT + 15)
    assert h.sent == [] and "自行继续" in h.events("continue_skipped")[-1]["text"]


@pytest.mark.parametrize("source", ["exec", "cli"])
def test_exec_and_cli_sessions_are_never_continued(codex_home, tmp_path, source: str) -> None:
    now = FAILED_AT + 60
    codex_home.add(T1, "Reply with exactly: OK", [started("a"), user_message("a", "Reply with exactly: OK"),
                                                   failed("a", FAILED_AT)], source=source, updated=int(FAILED_AT))
    h = Harness(tmp_path, now)
    h.poll()
    h.poll(now + 600)
    assert h.sent == [] and T1 not in h.watcher.watches and h.events("failure_detected") == []
    assert cw.scan(now=now) == []


def test_unreadable_rollout_is_tailed_later_instead_of_read_whole(codex_home, tmp_path, monkeypatch) -> None:
    now = FAILED_AT + 60
    old = [started("old"), user_message("old", "x"), failed("old", FAILED_AT - 7000)]
    filler = [user_message("t1", f"第 {index} 段说明") for index in range(40)]
    codex_home.add(T1, "p20", old + [started("t1")] + filler, updated=int(now))
    real_tail = cw.read_tail
    calls: list[int] = []

    def flaky_tail(path: Path, max_bytes: int):
        calls.append(max_bytes)
        if len(calls) == 1:
            raise PermissionError("locked")
        return real_tail(path, max_bytes)

    monkeypatch.setattr(cw, "read_tail", flaky_tail)
    h = Harness(tmp_path, now, cw.WatchConfig(tail_bytes=2000))
    h.poll()
    assert calls == [2000, 2000] and h.watcher.watches[T1].started  # _read retried the tail, not offset 0
    h.poll(now + 5)
    assert h.events("failure_stale", "failure_detected") == []  # the old failure lies before the tail


def test_continuation_waits_for_codex_to_run(codex_home, running, tmp_path) -> None:
    h = Harness(tmp_path, FAILED_AT - 50)
    h.pid = None
    h.poll()
    codex_home.write(running, [failed("t1", FAILED_AT)])
    h.poll(FAILED_AT + 15)
    h.poll(FAILED_AT + 20)
    assert h.sent == [] and len(h.events("continue_deferred")) == 1
    assert cw.continuation_due(T1, "t1", log_dir=h.log_dir) == FAILED_AT + 3600 + cw._STEP_S  # until max_age
    h.pid = 777
    h.poll(FAILED_AT + 25)
    assert len(h.sent) == 1
    assert [item["event"] for item in h.events("codex_absent", "codex_started")] == ["codex_absent", "codex_started"]
    assert h.events("codex_started")[0]["pid"] == 777


def test_startup_continues_recent_failures_and_reports_old_ones(codex_home, tmp_path) -> None:
    now = FAILED_AT + 7200
    codex_home.add(T1, "old", [started("a"), user_message("a", "x"), failed("a", now - 7000)], updated=int(now - 7000))
    codex_home.add(T2, "new", [started("b"), user_message("b", "y"), failed("b", now - 60)], updated=int(now - 60))
    h = Harness(tmp_path, now)
    h.poll()
    assert [thread_id for thread_id, _ in h.sent] == [T2]
    (stale,) = h.events("failure_stale")
    assert stale["thread_id"] == T1 and stale["task"] == "old"


def test_startup_counts_the_run_of_failures_for_the_backoff(codex_home, tmp_path) -> None:
    now = FAILED_AT + 1000
    records = [started("a"), user_message("a", "x"), failed("a", now - 300),
               started("b"), failed("b", now - 200), started("c"), failed("c", now - 20)]
    codex_home.add(T1, "p19", records, updated=int(now - 20))
    h = Harness(tmp_path, now, cw.WatchConfig(max_backoff_s=0))
    h.poll()
    (detected,) = h.events("failure_detected")
    assert detected["attempt"] == 3 and h.sent == []  # third failure waits 60 s
    h.poll(now + 40)
    assert len(h.sent) == 1


def test_send_failures_are_retried_with_backoff(codex_home, running, tmp_path) -> None:
    h = Harness(tmp_path, FAILED_AT - 50, cw.WatchConfig(max_backoff_s=0))
    h.poll()
    codex_home.write(running, [failed("t1", FAILED_AT)])
    h.send_error = codex_app_client.AppToolsError("没有找到可用的 Codex app-tools 管道")
    h.poll(FAILED_AT + 15)
    assert len(h.events("send_failed")) == 1
    h.send_error = None
    h.poll(FAILED_AT + 15 + 44)
    assert h.sent == []
    h.poll(FAILED_AT + 15 + 45)
    assert len(h.sent) == 1


def test_missing_continuation_turn_is_resent_then_given_up(codex_home, running, tmp_path) -> None:
    h = Harness(tmp_path, FAILED_AT - 50, cw.WatchConfig(delivery_timeout_s=60.0, max_sends_per_turn=2))
    h.poll()
    codex_home.write(running, [failed("t1", FAILED_AT)])
    h.poll(FAILED_AT + 15)
    assert len(h.sent) == 1
    h.poll(FAILED_AT + 75)
    assert len(h.events("no_new_turn")) == 1
    h.poll(FAILED_AT + 120)
    assert len(h.sent) == 2
    h.poll(FAILED_AT + 180)
    assert len(h.sent) == 2 and len(h.events("gave_up")) == 1


def test_continuation_due_follows_the_newest_plan_for_the_turn(codex_home, running, tmp_path) -> None:
    h = Harness(tmp_path, FAILED_AT - 50, cw.WatchConfig(delivery_timeout_s=60.0, max_sends_per_turn=3, max_backoff_s=0))

    def due(thread_id: str = T1, turn_id: str = "t1") -> float | None:
        return cw.continuation_due(thread_id, turn_id, log_dir=h.log_dir)

    assert cw.continuation_due(T1, "t1", log_dir=tmp_path / "missing") is None
    h.poll()
    codex_home.write(running, [failed("t1", FAILED_AT)])
    h.poll(FAILED_AT + 5)
    assert due() == FAILED_AT + 15 + cw._STEP_S and due(T2) is None and due(T1, "t0") is None
    h.send_error = codex_app_client.AppToolsError("管道暂时不可用")
    h.poll(FAILED_AT + 15)
    assert due() == FAILED_AT + 15 + 45 + cw._STEP_S  # send_failed: resend after the second backoff
    h.send_error = None
    h.poll(FAILED_AT + 60)
    assert len(h.sent) == 1 and due() == FAILED_AT + 60 + 60 + cw._STEP_S  # continued: delivery timeout
    h.poll(FAILED_AT + 120)
    assert due() == FAILED_AT + 120 + 60 + cw._STEP_S  # no_new_turn: resend after the third backoff
    h.poll(FAILED_AT + 180)
    h.poll(FAILED_AT + 240)
    assert len(h.sent) == 2 and len(h.events("gave_up")) == 1 and due() is None


def test_dry_run_only_logs(codex_home, running, tmp_path) -> None:
    h = Harness(tmp_path, FAILED_AT - 50, cw.WatchConfig(dry_run=True))
    h.poll()
    codex_home.write(running, [failed("t1", FAILED_AT)])
    h.poll(FAILED_AT + 15)
    h.poll(FAILED_AT + 600)
    assert h.sent == [] and len(h.events("dry_run")) == 1


def test_archived_conversations_are_dropped(codex_home, running, tmp_path) -> None:
    import sqlite3

    h = Harness(tmp_path, FAILED_AT - 50)
    h.poll()
    codex_home.write(running, [failed("t1", FAILED_AT)])
    h.poll(FAILED_AT + 5)
    with sqlite3.connect(codex_home.db) as connection:
        connection.execute("UPDATE threads SET archived = 1 WHERE id = ?", (T1,))
    h.poll(FAILED_AT + 40)
    assert h.sent == [] and T1 not in h.watcher.watches
    assert "已归档" in h.events("continue_skipped")[0]["text"]


@pytest.mark.parametrize(
    ("kind", "message", "expected"),
    [
        ("server_overloaded", "Selected model is at capacity.", (True, "模型容量不足")),
        ("http_connection_failed", "Connection failed: error sending request", (True, "连接失败")),
        ("response_stream_disconnected", "stream disconnected", (True, "响应流中断")),
        ("response_too_many_failed_attempts", "exceeded retry limit, last status: 429", (True, "请求重试次数超限")),
        ("other", "The model is temporarily unavailable, please retry", (True, "模型暂时不可用")),
        ("other", "stream disconnected before completion: We're currently processing too many requests",
         (True, "模型暂时不可用")),
        ("other", "Model is temporarily unavailable: usage limit reached", (False, "其他错误")),
        ("other", "unexpected status 502 Bad Gateway: Unknown error, url: https://proxy.example.com/v1/responses",
         (True, "网关错误 502")),
        ("other", "Error running remote compact task: stream disconnected before completion: error sending request",
         (True, "响应流中断")),
        ("other", "Transport error: network error: connection reset", (True, "网络连接错误")),
        ("other", "unexpected status 404 Not Found", (False, "其他错误")),
        ("other", '{"error": {"message": "Invalid \'input[43].id\'", "type": "invalid_request_error"}}',
         (False, "其他错误")),
        ("other", "stream disconnected: rate limit exceeded", (False, "其他错误")),
        ("usage_limit_exceeded", "You've hit your usage limit.", (False, "额度用完")),
        ("", "boom", (False, "其他错误")),
    ],
)
def test_classify_mirrors_the_codex_resume_rule(kind: str, message: str, expected: tuple[bool, str]) -> None:
    assert cw.classify(kind, message) == expected


def test_continuation_message_always_carries_the_marker() -> None:
    assert cw.continuation_message(cw.DEFAULT_MESSAGE, "连接失败") == \
        f"{AUTO_CONTINUE_MARKER}上一轮因连接失败中断，请从中断处继续之前未完成的工作。"
    assert cw.continuation_message("继续（{reason}）", "x") == f"{AUTO_CONTINUE_MARKER}继续（x）"


def test_codex_main_pid_picks_the_packaged_parent(monkeypatch: pytest.MonkeyPatch) -> None:
    packaged = r"C:\Program Files\WindowsApps\OpenAI.Codex_26.924.2738.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe"
    processes = [(10, 1, "explorer.exe"), (20, 10, "ChatGPT.exe"), (21, 20, "ChatGPT.exe"), (30, 10, "ChatGPT.exe")]
    paths = {20: packaged, 21: packaged, 30: r"C:\Program Files\OpenAI\ChatGPT\ChatGPT.exe"}
    monkeypatch.setattr(cw, "_processes", lambda: processes)
    monkeypatch.setattr(cw, "_image_path", lambda pid: paths.get(pid, ""))
    assert cw.codex_main_pid() == 20
    monkeypatch.setattr(cw, "_processes", lambda: processes[:1])
    assert cw.codex_main_pid() is None


def test_codex_main_pid_queries_this_machine() -> None:
    pid = cw.codex_main_pid()
    assert pid is None or pid > 0


def test_read_tail_starts_at_a_line_boundary(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    lines = [json.dumps(started(f"t{index}")) for index in range(50)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    records, offset = cw.read_tail(path, 300)
    assert offset == path.stat().st_size
    assert records and records[-1]["payload"]["turn_id"] == "t49"
    assert all(record["type"] == "event_msg" for record in records)


def test_scan_and_report(codex_home, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    now = FAILED_AT + 120
    codex_home.add(T1, "p18", [started("a"), user_message("a", "x"), failed("a", now - 60)], updated=int(now - 60))
    codex_home.add(T2, "done", [started("b"), completed("b", "ok")], updated=int(now - 60))
    rows = cw.scan(now=now)
    assert [(row["task"], row["reason"], row["action"]) for row in rows] == [("p18", "模型容量不足", "监控会自动继续")]

    h = Harness(tmp_path, now)
    h.poll()
    assert [item["event"] for item in cw.report(h.log_dir, 5)] == ["codex_started", "failure_detected", "continued"]
    assert cw.main(["--log-dir", str(h.log_dir), "report", "--limit", "1"]) == 0
    assert "已自动继续" in capsys.readouterr().out


def test_follow_only_mode_never_plans_or_sends(codex_home, tmp_path) -> None:
    now = FAILED_AT + 60
    codex_home.add(T2, "startup", [started("s"), user_message("s", "x"), failed("s", now - 30)], updated=int(now - 30))
    running = codex_home.add(T1, "p18", [started("t1"), user_message("t1", "修改论文")], updated=int(now - 30))
    finished: list[tuple[str, str, str]] = []
    h = Harness(tmp_path, now, cw.WatchConfig(continue_failures=False),
                on_turn_finished=lambda watch, turn, _now: finished.append((watch.info.id, turn.turn_id, turn.status)))
    h.poll()
    codex_home.write(running, [failed("t1", now + 1)])
    for offset in (5, 60, 600):
        h.poll(now + offset)
    assert h.sent == [] and finished == [(T1, "t1", "failed")]
    assert h.events("failure_detected", "failure_ignored", "failure_stale", "continued", "gave_up") == []
    assert all(watch.pending is None and watch.sent is None for watch in h.watcher.watches.values())


def test_turn_callbacks_follow_new_turns_and_survive_errors(codex_home, running, tmp_path) -> None:
    seen: list[tuple[str, str, str, float]] = []

    def on_started(watch: cw.ThreadWatch, turn, now: float) -> None:
        seen.append(("started", watch.info.id, turn.turn_id, now))

    def on_finished(watch: cw.ThreadWatch, turn, now: float) -> None:
        seen.append(("finished", watch.info.id, turn.turn_id, now))
        if turn.turn_id == "t2":
            raise RuntimeError("推送失败")

    h = Harness(tmp_path, FAILED_AT - 50, on_turn_started=on_started, on_turn_finished=on_finished)
    h.poll()
    assert seen == []  # t1 was already running when the conversation was first tracked
    codex_home.write(running, [completed("t1", "第一轮完成")])
    h.poll(FAILED_AT)
    codex_home.write(running, [started("t2"), user_message("t2", "下一步"), completed("t2", "第二轮完成")])
    h.poll(FAILED_AT + 10)
    codex_home.write(running, [started("t3"), user_message("t3", "再下一步")])
    h.poll(FAILED_AT + 20)
    assert seen == [
        ("finished", T1, "t1", FAILED_AT),
        ("started", T1, "t2", FAILED_AT + 10), ("finished", T1, "t2", FAILED_AT + 10),
        ("started", T1, "t3", FAILED_AT + 20),
    ]
    log = (h.log_dir / "watch.log").read_text(encoding="utf-8")
    assert "ERROR [demo-p18-v3] 回合回调出错：RuntimeError: 推送失败" in log


def test_collector_factory_builds_the_collectors(codex_home, running, tmp_path) -> None:
    class Counting(TurnCollector):
        fed = 0

        def feed(self, records: list[dict[str, Any]]) -> None:
            Counting.fed += len(records)
            super().feed(records)

    h = Harness(tmp_path, FAILED_AT - 50, collector_factory=Counting)
    h.poll()
    watch = h.watcher.watches[T1]
    assert isinstance(watch.collector, Counting) and Counting.fed >= 2
    assert watch.collector.ordered()[-1].turn_id == "t1"


def test_single_instance_lock(tmp_path: Path) -> None:
    path = tmp_path / "state" / "watch.lock"
    first = cw.single_instance_lock(path)
    assert first is not None
    try:
        assert cw.single_instance_lock(path) is None
    finally:
        first.close()
    assert path.read_bytes() == b"0"
    again = cw.single_instance_lock(path)
    assert again is not None
    again.close()


def test_single_instance_lock_held_by_another_process(tmp_path: Path) -> None:
    import subprocess
    import sys

    path = tmp_path / "watch.lock"
    holder = (
        "import msvcrt, sys\n"
        "lock = open(sys.argv[1], 'a+b'); lock.write(b'0'); lock.flush(); lock.seek(0)\n"
        "msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)\n"
        "print('locked', flush=True); sys.stdin.read()\n"
    )
    child = subprocess.Popen([sys.executable, "-c", holder, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == b"locked"
        assert cw.single_instance_lock(path) is None
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=30)
    lock = cw.single_instance_lock(path)
    assert lock is not None
    lock.close()


def test_event_log_rotates(tmp_path: Path) -> None:
    log = cw.EventLog(tmp_path, max_bytes=200, backups=2)
    for index in range(40):
        log.line(f"第 {index} 行")
    assert (tmp_path / "watch.log.1").is_file() and (tmp_path / "watch.log.2").is_file()
    assert not (tmp_path / "watch.log.3").exists()


@pytest.mark.skip(reason='Old agent_telegram cutover guard is excluded from agent_telegram; its migration gate is a later step.')
def test_watch_refuses_after_cutover_but_report_still_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                              capsys: pytest.CaptureFixture[str]) -> None:
    pytest.importorskip("agent_telegram.cutover")  # the standalone bridge build has no cutover guard
    state = tmp_path / "migration-state.json"
    state.write_text('{"phase": "ACTIVE"}', encoding="utf-8")
    monkeypatch.setenv("AGENT_TG_SUPERSEDED_STATE", str(state))
    monkeypatch.setattr(cw, "run_watch", lambda *_args, **_kwargs: pytest.fail("watcher ran after cutover"))
    log_dir = tmp_path / "logs"
    for argv in ([], ["watch"], ["watch", "--dry-run"]):
        with pytest.raises(SystemExit) as exit_info:
            cw.main(["--log-dir", str(log_dir), *argv])
        assert exit_info.value.code == 3
        assert "codex_watch watch" in capsys.readouterr().err
    assert not log_dir.exists()
    assert cw.main(["--log-dir", str(log_dir), "report", "--limit", "1"]) == 0

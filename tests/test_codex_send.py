from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest
from conftest import completed, delegated, relay_call, started, user_message

from agent_telegram import codex_app_client, codex_watch
from agent_telegram.codex_send import _retry_grace, compose_message, run_codex_send
from agent_telegram.codex_threads import DeliveryTracker
from agent_telegram.worker import JobFailure

THREAD = "01a07078-1111-7222-8333-944455556666"
OVERLOADED = {"message": "Selected model is at capacity. Please try a different model.", "codex_error_info": "server_overloaded"}
USAGE_LIMIT = {"message": "You've hit your usage limit.", "codex_error_info": "usage_limit_exceeded"}


@pytest.fixture(autouse=True)
def no_live_codex(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Default busy check, process lookup and watcher log must never reach the real Codex Desktop."""

    monkeypatch.setattr(codex_app_client, "app_reports_busy", lambda _thread_id, **_options: None)
    monkeypatch.setattr(codex_watch, "codex_main_pid", lambda: None)
    monkeypatch.setenv("CODEX_WATCH_LOG_DIR", str(tmp_path / "codex-watch"))


def _nonce(text: str) -> str:
    match = re.search(r"\[agent-telegram:[^\]]+\]", text)
    assert match
    return match.group(0)


def _params(output_dir: Path | None, **overrides: object) -> dict:
    params = {
        "thread_id": THREAD,
        "thread_name": "demo-v5审稿",
        "message": "请用 GPT 审一下最新版论文并画方法图",
        "output_dir": str(output_dir) if output_dir else None,
        "model": "gpt-6-sol",
        "thinking": "high",
        "if_busy": "fail",
        "timeout_s": 60,
    }
    params.update(overrides)
    return params


def _no_send(*_args: object, **_options: object) -> dict:
    return {}


def test_compose_message_carries_nonce_and_contract(tmp_path: Path) -> None:
    text = compose_message("atg-260928-120000-codex-abcdef", "画图", tmp_path)
    assert text.startswith("[agent-telegram:atg-260928-120000-codex-abcdef]")
    assert str(tmp_path) in text and "BEGIN_AGENT_TG_RESULT" in text
    assert "END_AGENT_TG_RESULT" in text and "agent_telegram 的 web_*" in text
    assert 'wait_s=0' in text and 'web_job(action="wait", job_id=...)' in text
    assert "本轮不再调度其他代理" in text and "不要用 on_degraded=codex" in text
    contract = text.replace(str(tmp_path), "<output_dir>")
    assert "BEGIN_AGENT_TG_RESULT" in contract and "END_AGENT_TG_RESULT" in contract
    assert "由上游代理通过" in text
    assert "不要对交付目录执行 git add" in text


@pytest.mark.parametrize("tag", ["AGENT_TG_LEGACY", "AGENT_TG"])
def test_codex_send_waits_for_turn_and_verifies_manifest(make_ctx, codex_home, tmp_path: Path, tag: str) -> None:
    rollout = codex_home.add(THREAD, "demo-v5审稿", [started("old"), user_message("old", "旧任务"), completed("old", "旧回复")])
    output = tmp_path / "deliver"
    elsewhere = tmp_path / "codex-var" / "figure.png"
    elsewhere.parent.mkdir(parents=True)
    elsewhere.write_bytes(b"png")
    sent: dict = {}

    def fake_send(thread_id: str, text: str, **options: object) -> dict:
        sent.update(thread_id=thread_id, text=text, **options)
        (output / "review.md").write_text("审稿意见", encoding="utf-8")
        manifest = {"status": "ok", "summary": "完成", "files": [
            {"path": str(output / "review.md"), "kind": "review"},
            {"path": str(elsewhere), "kind": "image"},
            {"path": str(output / "missing.tex"), "kind": "tex"},
        ], "chatgpt_conversations": ["https://chatgpt.com/c/abc"]}
        final = f"已完成。\nBEGIN_{tag}_RESULT\n{json.dumps(manifest, ensure_ascii=False)}\nEND_{tag}_RESULT"
        web_call = relay_call("new", "web_review", file="C:\\P1\\paper.pdf", wait_s=0)
        web_call["payload"]["item"]["server"] = "agent_telegram"
        codex_home.write(rollout, [
            started("new"), delegated("new", text),
            relay_call("new", "paper_review_submit", project="C:\\P1"),
            relay_call("new", "paper_job", action="wait", job_id="pj-1"),
            web_call,
            completed("new", final),
        ])
        return {"pipe": "fake"}

    ctx = make_ctx("codex", _params(output, source_agent="antigravity"))
    result = run_codex_send(ctx, send=fake_send, poll_s=0.01, delivery_timeout_s=5)

    assert sent["thread_id"] == THREAD and _nonce(sent["text"]) == f"[agent-telegram:{ctx.job_id}]"
    assert "由 Antigravity 通过 agent_telegram 转交" in sent["text"]
    assert (sent["model"], sent["thinking"]) == ("gpt-6-sol", "high")
    assert (result["model"], result["thinking"]) == ("gpt-6-sol", "high")
    assert (ctx.directory / "message.txt").read_text(encoding="utf-8") == sent["text"]
    assert result["turn_id"] == "new" and result["final_message"].startswith("已完成")
    assert [Path(item["path"]).name for item in result["files"]] == ["review.md", "figure.png"]
    assert (output / "figure.png").read_bytes() == b"png"
    assert len(result["missing_files"]) == 1 and result["warnings"]
    assert result["web_call_count"] == result["gpt_relay_call_count"] == 3
    assert result["web_calls"] == result["gpt_relay_calls"]
    assert [call["tool"] for call in result["web_calls"]] == ["paper_review_submit", "paper_job", "web_review"]
    record = json.loads((output / "agent_telegram-manifest.json").read_text(encoding="utf-8"))
    assert record["thread"]["id"] == THREAD and record["manifest"]["summary"] == "完成"
    assert record["web_calls"] == record["gpt_relay_calls"] == result["web_calls"]
    assert "已完成" in (output / "codex-reply.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(("origin", "caller"), [
    ({"source_agent": "claude"}, " Claude "),
    ({"source_agent": "codex"}, " Codex "),
    ({"_dispatch_source": {"source_agent": "antigravity"}}, " Antigravity "),
    ({}, "上游代理"),
])
def test_blank_model_options_keep_thread_settings(make_ctx, codex_home, origin: dict, caller: str) -> None:
    rollout = codex_home.add(THREAD, "demo", [])
    sent: dict = {}

    def fake_send(thread_id: str, text: str, **options: object) -> dict:
        sent.update(options)
        assert f"以下任务由{caller}通过 agent_telegram 转交" in text
        codex_home.write(rollout, [started("t"), user_message("t", text), completed("t", "ok")])
        return {}

    run_codex_send(make_ctx("codex", _params(None, model="", thinking="", **origin)), send=fake_send, poll_s=0.01)
    assert sent == {"model": None, "thinking": None}


def _raw_params(message: str, **overrides: object) -> dict:
    params = {"thread_id": THREAD, "thread_name": "demo", "message": message, "raw": True, "nonce": "[tg:7f3a9c]",
              "if_busy": "wait", "timeout_s": 60}
    params.update(overrides)
    return params


@pytest.mark.parametrize(
    ("message", "expected"),
    [("请继续上一步", "[tg:7f3a9c] 请继续上一步"), ("[tg:7f3a9c] 已带随机串", "[tg:7f3a9c] 已带随机串")],
)
def test_raw_mode_sends_the_message_verbatim(make_ctx, codex_home, tmp_path: Path, message: str,
                                             expected: str) -> None:
    rollout = codex_home.add(THREAD, "demo", [started("old"), user_message("old", "旧任务"), completed("old", "旧回复")])
    sent: dict = {}

    def fake_send(thread_id: str, text: str, **options: object) -> dict:
        sent.update(thread_id=thread_id, text=text, options=options)
        codex_home.write(rollout, [started("new"), delegated("new", text), completed("new", "按原文完成")])
        return {"pipe": "fake"}

    output = tmp_path / "never-created"
    ctx = make_ctx("codex", _raw_params(message, output_dir=str(output), model="gpt-6-sol", thinking="high"))
    result = run_codex_send(ctx, send=fake_send, poll_s=0.01, delivery_timeout_s=5)

    assert sent["thread_id"] == THREAD and sent["text"] == expected
    assert sent["options"] == {"model": None, "thinking": None, "navigate": False}
    assert result == {"ok": True, "status": "succeeded", "thread": result["thread"], "turn_id": "new",
                      "turn_chain": ["new"], "final_message": "按原文完成", "warnings": []}
    assert result["thread"]["id"] == THREAD
    assert not output.exists()
    assert not (ctx.directory / "codex-reply.md").exists() and not (ctx.directory / "agent_telegram-manifest.json").exists()


def test_raw_mode_follows_a_retry_and_waits_while_busy(make_ctx, codex_home) -> None:
    rollout = codex_home.add(THREAD, "demo", [started("other"), user_message("other", "别的任务")])

    def fake_send(thread_id: str, text: str, **_options: object) -> dict:
        codex_home.write(rollout, [started("f"), delegated("f", text), completed("f", None, OVERLOADED),
                                   started("retry"), completed("retry", "重试后完成")])
        return {}

    finisher = threading.Timer(0.3, codex_home.write, (rollout, [completed("other", "别的任务完成")]))
    finisher.start()
    try:
        result = run_codex_send(make_ctx("codex", _raw_params("继续")), send=fake_send, poll_s=0.01,
                                retry_grace_s=5, busy_check=lambda _id: None, app_recheck_s=60)
    finally:
        finisher.cancel()
    assert result["final_message"] == "重试后完成" and result["turn_chain"] == ["f", "retry"]


@pytest.mark.parametrize("overrides", [{"nonce": ""}, {"message": "  "}])
def test_raw_mode_requires_nonce_and_message(make_ctx, codex_home, overrides: dict) -> None:
    codex_home.add(THREAD, "demo", [])
    params = _raw_params("x")
    params.update(overrides)
    with pytest.raises(JobFailure, match="raw 模式"):
        run_codex_send(make_ctx("codex", params), send=_no_send, poll_s=0.01)


def test_follows_codex_retry_after_transient_failure(make_ctx, codex_home) -> None:
    rollout = codex_home.add(THREAD, "demo", [])

    def fake_send(thread_id: str, text: str, **_options: object) -> dict:
        codex_home.write(rollout, [
            started("first"), delegated("first", text), relay_call("first", "chatgpt_bridge_status"),
            completed("first", None, OVERLOADED),
            started("retry"), relay_call("retry", "ask_chatgpt_bridge", thread="new"),
            completed("retry", "重试后完成"),
        ])
        return {}

    result = run_codex_send(make_ctx("codex", _params(None)), send=fake_send, poll_s=0.01, retry_grace_s=5)
    assert result["final_message"] == "重试后完成"
    assert result["turn_chain"] == ["first", "retry"] and result["gpt_relay_call_count"] == 2


def _failing_send(codex_home, rollout: Path, *later: dict):
    def fake_send(thread_id: str, text: str, **_options: object) -> dict:
        codex_home.write(rollout, [started("f1"), delegated("f1", text), completed("f1", None, OVERLOADED), *later])
        return {}

    return fake_send


def test_transient_failure_without_retry_fails_after_grace(make_ctx, codex_home) -> None:
    rollout = codex_home.add(THREAD, "demo", [])
    with pytest.raises(JobFailure, match="秒内没有自动重试成功.*at capacity"):
        run_codex_send(make_ctx("codex", _params(None)), send=_failing_send(codex_home, rollout), poll_s=0.01,
                       retry_grace_s=0.3)


def test_new_input_after_a_failure_ends_the_task_at_once(make_ctx, codex_home) -> None:
    rollout = codex_home.add(THREAD, "demo", [])
    send = _failing_send(codex_home, rollout, started("other"), user_message("other", "别人的新任务"))
    began = time.monotonic()
    with pytest.raises(JobFailure, match="at capacity.*带新输入的回合") as error:
        run_codex_send(make_ctx("codex", _params(None)), send=send, poll_s=0.01, retry_grace_s=60)
    assert time.monotonic() - began < 30 and error.value.details["superseded"] is True


def _watcher_event(event: str, at: float, **fields: object) -> None:
    log = codex_watch.EventLog(codex_watch.default_log_dir())
    log.incident(event, "[demo] test", at=time.time(), thread_id=THREAD, turn_id="f1",
                 due_at=codex_watch._iso(at), **fields)


def test_watcher_log_extends_the_wait_for_its_continuation(make_ctx, codex_home) -> None:
    rollout = codex_home.add(THREAD, "demo", [])
    _watcher_event("send_failed", time.time())  # the watcher resends later than the fixed grace allows
    continuation = codex_watch.continuation_message(codex_watch.DEFAULT_MESSAGE, "模型容量不足")
    timer = threading.Timer(0.5, codex_home.write, (rollout, [
        started("c1"), delegated("c1", continuation), completed("c1", "继续后完成")]))
    fail = _failing_send(codex_home, rollout)

    def fake_send(thread_id: str, text: str, **options: object) -> dict:
        result = fail(thread_id, text, **options)
        timer.start()
        return result

    try:
        result = run_codex_send(make_ctx("codex", _params(None)), send=fake_send, poll_s=0.01, retry_grace_s=0.05)
    finally:
        timer.cancel()
    assert result["final_message"] == "继续后完成" and result["turn_chain"] == ["f1", "c1"]


@pytest.mark.parametrize(("event", "due"), [("gave_up", 0.0), ("send_failed", -600.0)])
def test_watcher_log_without_a_pending_continuation_ends_the_wait(make_ctx, codex_home, event, due) -> None:
    rollout = codex_home.add(THREAD, "demo", [])
    _watcher_event(event, time.time() + due)
    began = time.monotonic()
    with pytest.raises(JobFailure, match="at capacity"):
        run_codex_send(make_ctx("codex", _params(None)), send=_failing_send(codex_home, rollout), poll_s=0.01,
                       retry_grace_s=0.05)
    assert time.monotonic() - began < 30


def test_codex_send_follows_a_new_rollout_segment(make_ctx, codex_home) -> None:
    codex_home.add(THREAD, "demo", [])
    segment = codex_home.rollout_for(THREAD, "_01a083e8-0000-7000-8000-000000000000")

    def fake_send(thread_id: str, text: str, **_options: object) -> dict:
        codex_home.write(segment, [started("s1"), delegated("s1", text), completed("s1", "分段后完成")])
        codex_home.set_rollout(thread_id, segment)
        return {"pipe": "fake"}

    result = run_codex_send(make_ctx("codex", _params(None)), send=fake_send, poll_s=0.01, delivery_timeout_s=5)
    assert result["final_message"] == "分段后完成" and "没有 BEGIN_AGENT_TG_RESULT" in result["warnings"][0]


def test_busy_thread_fails_by_default_and_send_overrides(make_ctx, codex_home) -> None:
    rollout = codex_home.add(THREAD, "demo", [started("running"), user_message("running", "还在跑")])
    with pytest.raises(JobFailure, match="正在执行"):
        run_codex_send(make_ctx("codex", _params(None)), send=_no_send, poll_s=0.01, busy_check=lambda _id: None)

    def fake_send(thread_id: str, text: str, **_options: object) -> dict:
        codex_home.write(rollout, [delegated("running", text), completed("running", "合并进当前轮完成")])
        return {}

    result = run_codex_send(make_ctx("codex", _params(None, if_busy="send")), send=fake_send, poll_s=0.01)
    assert result["final_message"] == "合并进当前轮完成"


def test_undelivered_and_aborted_turns_raise(make_ctx, codex_home) -> None:
    rollout = codex_home.add(THREAD, "demo", [])
    with pytest.raises(JobFailure, match="没有出现这条任务"):
        run_codex_send(make_ctx("codex", _params(None)), send=_no_send, poll_s=0.01, delivery_timeout_s=0.2)

    def aborting_send(thread_id: str, text: str, **_options: object) -> dict:
        from conftest import event

        codex_home.write(rollout, [started("a1"), delegated("a1", text),
                                   event("turn_aborted", "a1", reason="interrupted", completed_at=1790529100)])
        return {}

    with pytest.raises(JobFailure, match="interrupted"):
        run_codex_send(make_ctx("codex", _params(None)), send=aborting_send, poll_s=0.01)


def test_turn_interrupted_by_app_restart_does_not_block(make_ctx, codex_home) -> None:
    rollout = codex_home.add(THREAD, "demo", [started("cut"), user_message("cut", "被重启打断的一轮")])

    def fake_send(thread_id: str, text: str, **_options: object) -> dict:
        codex_home.write(rollout, [started("next"), delegated("next", text), completed("next", "ok")])
        return {}

    result = run_codex_send(make_ctx("codex", _params(None)), send=fake_send, poll_s=0.01, busy_check=lambda _id: False)
    assert result["final_message"] == "ok"
    codex_home.write(rollout, [started("live"), user_message("live", "真的在跑")])
    with pytest.raises(JobFailure, match="正在执行"):
        run_codex_send(make_ctx("codex", _params(None)), send=fake_send, poll_s=0.01, busy_check=lambda _id: True)



def _running_send(codex_home, rollout: Path, sent: dict | None = None):
    def fake_send(thread_id: str, text: str, **_options: object) -> dict:
        if sent is not None:
            sent["text"] = text
        codex_home.write(rollout, [started("cut"), delegated("cut", text), relay_call("cut", "paper_job")])
        return {}

    return fake_send


def test_turn_that_dies_with_a_codex_restart_fails_after_the_grace(make_ctx, codex_home) -> None:
    rollout = codex_home.add(THREAD, "demo", [])
    pids = iter([100, 100, 200])  # Codex Desktop restarts while the turn runs
    checked: list[str] = []

    def busy_check(thread_id: str) -> bool:
        checked.append(thread_id)
        return False  # the app reports the thread idle with an interrupted turn

    with pytest.raises(JobFailure, match="被中断") as error:
        run_codex_send(make_ctx("codex", _params(None)), send=_running_send(codex_home, rollout), poll_s=0.01,
                       retry_grace_s=0.2, busy_check=busy_check, main_pid=lambda: next(pids, 200))
    assert checked == [THREAD] and error.value.details["turn_status"] == "interrupted"


def test_silent_turn_checked_with_the_app_then_followed_to_resume(make_ctx, codex_home) -> None:
    rollout = codex_home.add(THREAD, "demo", [])
    answers = iter([True, False])  # alive at the first check, gone at the second

    def busy_check(_thread_id: str) -> bool:
        alive = next(answers)
        if not alive:
            # Someone presses Resume: a new turn without new input continues the task.
            codex_home.write(rollout, [started("resume"), completed("resume", "恢复后完成")])
        return alive

    result = run_codex_send(make_ctx("codex", _params(None)), send=_running_send(codex_home, rollout), poll_s=0.01,
                            retry_grace_s=5, busy_check=busy_check, main_pid=lambda: 100, stall_check_s=0.05)
    assert result["final_message"] == "恢复后完成" and result["turn_chain"] == ["cut", "resume"]


def test_codex_gone_and_unreachable_counts_as_interrupted(make_ctx, codex_home) -> None:
    rollout = codex_home.add(THREAD, "demo", [])
    pids = iter([100, 100])
    with pytest.raises(JobFailure, match="被中断"):
        run_codex_send(make_ctx("codex", _params(None)), send=_running_send(codex_home, rollout), poll_s=0.01,
                       retry_grace_s=0.1, busy_check=lambda _id: None, main_pid=lambda: next(pids, None))


def test_live_turn_is_not_interrupted_by_an_unknown_app_state(make_ctx, codex_home) -> None:
    rollout = codex_home.add(THREAD, "demo", [])
    calls: list[str] = []

    def busy_check(thread_id: str) -> None:
        calls.append(thread_id)
        if len(calls) == 3:
            codex_home.write(rollout, [completed("cut", "长任务完成")])

    result = run_codex_send(make_ctx("codex", _params(None)), send=_running_send(codex_home, rollout), poll_s=0.01,
                            busy_check=busy_check, main_pid=lambda: None, stall_check_s=0.02)
    assert result["final_message"] == "长任务完成" and len(calls) == 3


def test_non_resumable_failure_fails_without_waiting(make_ctx, codex_home) -> None:
    rollout = codex_home.add(THREAD, "demo", [])

    def fake_send(thread_id: str, text: str, **_options: object) -> dict:
        codex_home.write(rollout, [started("f"), delegated("f", text), completed("f", None, USAGE_LIMIT)])
        return {}

    began = time.monotonic()
    with pytest.raises(JobFailure, match="额度用完，不会自动重试"):
        run_codex_send(make_ctx("codex", _params(None)), send=fake_send, poll_s=0.01, retry_grace_s=60)
    assert time.monotonic() - began < 30


def test_retry_grace_follows_the_watcher_backoff() -> None:
    tracker = DeliveryTracker("[agent_telegram:atg-g]")
    tracker.feed([started("t0"), user_message("t0", "[agent_telegram:atg-g] x"), completed("t0", None, OVERLOADED)])
    assert _retry_grace(tracker, 360.0) == 360.0
    for index in range(1, 5):
        tracker.feed([started(f"t{index}"), completed(f"t{index}", None, OVERLOADED)])
    assert len(tracker.chain) == 5 and _retry_grace(tracker, 360.0) == 360.0 + 30.0 - 15.0


def test_manifest_status_other_than_ok_is_flagged(make_ctx, codex_home, tmp_path: Path) -> None:
    rollout = codex_home.add(THREAD, "demo", [])

    def sender(manifest: dict):
        def fake_send(thread_id: str, text: str, **_options: object) -> dict:
            body = json.dumps(manifest, ensure_ascii=False)
            final = f"没做完。\nBEGIN_AGENT_TG_LEGACY_RESULT\n{body}\nEND_AGENT_TG_LEGACY_RESULT"
            turn = _nonce(text)
            codex_home.write(rollout, [started(turn), delegated(turn, text), completed(turn, final)])
            return {}

        return fake_send

    failed = {"status": "failed", "summary": "GPT 审稿失败", "files": [], "notes": "gpt_relay 超时"}
    result = run_codex_send(make_ctx("codex", _params(tmp_path / "a")), send=sender(failed), poll_s=0.01)
    assert result["ok"] and result["task_status"] == "failed"
    assert "status=failed" in result["warnings"][0] and "GPT 审稿失败" in result["warnings"][0]
    assert "gpt_relay 超时" in result["warnings"][0]

    unknown = run_codex_send(make_ctx("codex", _params(tmp_path / "b")), send=sender({"summary": "?"}), poll_s=0.01)
    assert unknown["task_status"] is None and "status 应为 ok、partial 或 failed" in unknown["warnings"][0]
    partial = run_codex_send(make_ctx("codex", _params(tmp_path / "c")), send=sender({"status": "partial"}),
                             poll_s=0.01)
    assert partial["warnings"] == ["Codex 报告任务只完成了一部分（status=partial）。"]


def test_running_turn_behind_a_large_tool_output_is_busy(make_ctx, codex_home) -> None:
    big = {"timestamp": "2026-09-28T00:05:00Z", "type": "response_item",
           "payload": {"type": "custom_tool_call_output", "output": "x" * (3 * 1024 * 1024)}}
    codex_home.add(THREAD, "demo", [started("other"), user_message("other", "别的任务"), big])
    with pytest.raises(JobFailure, match="正在执行"):
        run_codex_send(make_ctx("codex", _params(None)), send=_no_send, poll_s=0.01)


def test_wait_asks_the_app_only_now_and_then(make_ctx, codex_home) -> None:
    rollout = codex_home.add(THREAD, "demo", [started("other"), user_message("other", "别的任务")])
    calls: list[str] = []

    def busy_check(thread_id: str) -> bool:
        calls.append(thread_id)
        return True

    def fake_send(thread_id: str, text: str, **_options: object) -> dict:
        codex_home.write(rollout, [started("mine"), delegated("mine", text), completed("mine", "ok")])
        return {}

    finisher = threading.Timer(0.5, codex_home.write, (rollout, [completed("other", "别的任务完成")]))
    finisher.start()
    try:
        result = run_codex_send(make_ctx("codex", _params(None, if_busy="wait")), send=fake_send, poll_s=0.01,
                                busy_check=busy_check, app_recheck_s=60)
    finally:
        finisher.cancel()
    assert result["final_message"] == "ok" and calls == [THREAD]  # only the check before waiting

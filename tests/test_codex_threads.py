from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from conftest import completed, delegated, relay_call, started, user_message

from agent_telegram import codex_threads as threads

T1 = "01a07078-0000-7000-8000-0000000000e1"
T2 = "01a07078-1111-7222-8333-944455556666"
T3 = "01a07078-aaaa-7bbb-8ccc-dddddddddddd"
SUB = "01a07078-0000-7000-8000-000000000000"


def _seed(codex_home) -> None:
    records = [started("t1"), user_message("t1", "hi"), completed("t1", "done")]
    codex_home.add(T1, "demo-mcp完善", records, updated=1_790_000_300)
    codex_home.add(T2, "demo-v5审稿", [], updated=1_790_000_200)
    codex_home.add(T3, "demo-v4审稿", [], archived=True, updated=1_790_000_100)
    codex_home.add(SUB, "", [], source='{"subagent":{"thread_spawn":{"depth":1}}}', updated=1_790_000_400)


def test_search_skips_archived_and_subagents(codex_home) -> None:
    _seed(codex_home)
    assert [info.id for info in threads.search_threads()] == [T1, T2]
    assert [info.id for info in threads.search_threads("审稿")] == [T2]
    assert [info.id for info in threads.search_threads("审稿", include_archived=True)] == [T2, T3]
    summary = threads.search_threads("v5")[0].summary()
    assert summary["cwd"] == "C:\\projects\\demo" and summary["name"] == "demo-v5审稿"
    assert threads.search_threads(cwd="c:/projects/demo/")[0].id == T1


def test_resolve_by_uuid_exact_name_and_substring(codex_home) -> None:
    _seed(codex_home)
    assert threads.resolve_thread(T2.upper()).id == T2
    assert threads.resolve_thread("demo-v5审稿").id == T2
    with pytest.raises(threads.ThreadLookupError) as error:
        threads.resolve_thread("v5审稿")
    assert error.value.candidates[0]["id"] == T2
    assert threads.resolve_thread("v5审稿", allow_substring=True).id == T2
    with pytest.raises(threads.ThreadLookupError, match="已归档"):
        threads.resolve_thread(T3)
    assert threads.resolve_thread(T3, include_archived=True).archived


def test_duplicate_names_are_ambiguous(codex_home) -> None:
    codex_home.add(T1, "同名对话")
    codex_home.add(T2, "同名对话")
    with pytest.raises(threads.ThreadLookupError, match="UUID") as error:
        threads.resolve_thread("同名对话")
    assert {item["id"] for item in error.value.candidates} == {T1, T2}


def test_read_thread_summarizes_last_turn(codex_home) -> None:
    codex_home.add(
        T1,
        "demo",
        [
            started("t1"), user_message("t1", "第一轮"), completed("t1", "第一轮完成"),
            started("t2"), user_message("t2", "请用 GPT 审稿"),
            relay_call("t2", "chatgpt_bridge_status"),
            relay_call("t2", "paper_review_submit", project="C:\\P1", output_dir="C:\\out", purpose="author_self_review"),
            completed("t2", "审稿完成"),
        ],
    )
    report = threads.read_thread(threads.resolve_thread(T1), turns=1)
    assert report["busy"] is False
    (turn,) = report["turns"]
    assert (turn["turn_id"], turn["status"], turn["final_message"]) == ("t2", "completed", "审稿完成")
    assert [call["tool"] for call in turn["gpt_relay_calls"]] == ["chatgpt_bridge_status", "paper_review_submit"]
    assert turn["web_calls"] == turn["gpt_relay_calls"] and turn["web_call_count"] == 2
    assert turn["gpt_relay_calls"][1]["arguments"]["project"] == "C:\\P1"
    assert len(threads.read_thread(threads.resolve_thread(T1), turns=5)["turns"]) == 2
    expected = datetime.fromisoformat("2026-09-28T00:00:00+00:00").astimezone().isoformat(timespec="seconds")
    assert report["last_activity"] == expected  # taken from the record timestamp, not the file mtime


def test_thread_activity_and_failed_turns(codex_home) -> None:
    rollout = codex_home.add(T1, "demo", [started("t1"), user_message("t1", "x")])
    info = threads.resolve_thread(T1)
    assert threads.thread_activity(info)["busy"] is True
    codex_home.write(rollout, [completed("t1", None, {"message": "Selected model is at capacity."})])
    assert threads.thread_activity(info)["busy"] is False
    turn = threads.read_thread(info)["turns"][0]
    assert (turn["status"], turn["error"]) == ("failed", "Selected model is at capacity.")


def _tool_output(size: int, stamp: str = "2026-09-28T00:05:00Z") -> dict:
    """A response_item line like Codex writes for a multi-MB custom tool output (no turn_id)."""

    return {"timestamp": stamp, "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "call_id": "c1", "output": "x" * size}}


def test_thread_activity_sees_a_running_turn_behind_a_large_tool_output(codex_home) -> None:
    rollout = codex_home.add(T1, "demo", [started("t1"), user_message("t1", "画图"), _tool_output(3 * 1024 * 1024)])
    info = threads.resolve_thread(T1)
    activity = threads.thread_activity(info)
    assert (activity["busy"], activity["busy_turn_id"]) == (True, "t1")
    expected = datetime.fromisoformat("2026-09-28T00:05:00+00:00").astimezone().isoformat(timespec="seconds")
    assert activity["last_activity"] == expected  # the tool output's timestamp still counts as activity
    # No turn event within max_bytes: unknown rather than idle.
    assert threads.thread_activity(info, tail_bytes=1024 * 1024, max_bytes=1024 * 1024)["busy"] is None
    codex_home.write(rollout, [completed("t1", "done"), _tool_output(3 * 1024 * 1024)])
    assert threads.thread_activity(info)["busy"] is False


def test_only_event_records_are_decoded() -> None:
    lines = [
        json.dumps(started("t1")).encode(),
        b'{"timestamp":"2026-09-28T00:00:09Z","type":"response_item","payload":{"broken json',
        b"",
        b"   ",
        b'{"timestamp": "2026-09-28T00:00:10Z", "type": "event_msg", "payload": {"type": "token_count"}}',
    ]
    records = threads._parse_lines(b"\n".join(lines) + b"\n")
    assert records[0]["payload"]["turn_id"] == "t1"
    assert records[1] == {"timestamp": "2026-09-28T00:00:09Z"}
    assert records[2]["payload"]["type"] == "token_count" and len(records) == 3


def test_questions_and_short_outputs_are_decoded_but_long_outputs_stay_stubs() -> None:
    ask = {"timestamp": "2026-09-28T00:00:01Z", "type": "response_item",
           "payload": {"type": "function_call", "name": "request_user_input", "arguments": "{}", "call_id": "c1"}}
    answer = {"timestamp": "2026-09-28T00:00:02Z", "type": "response_item",
              "payload": {"type": "function_call_output", "call_id": "c1", "output": '{"answers":{}}'}}
    other = {"timestamp": "2026-09-28T00:00:03Z", "type": "response_item",
             "payload": {"type": "function_call", "name": "exec_command", "arguments": "{}", "call_id": "c2"}}
    big = {"timestamp": "2026-09-28T00:00:04Z", "type": "response_item",
           "payload": {"type": "function_call_output", "call_id": "c2", "output": "x" * (128 * 1024)}}
    records = threads._parse_lines(b"\n".join(json.dumps(item).encode() for item in (ask, answer, other, big)) + b"\n")
    assert records[:2] == [ask, answer]
    assert records[2:] == [{"timestamp": "2026-09-28T00:00:03Z"}, {"timestamp": "2026-09-28T00:00:04Z"}]


@pytest.mark.parametrize("compact", [True, False])
def test_large_turn_context_is_decoded_only_from_the_record_header(compact: bool) -> None:
    context = {"timestamp": "2026-10-15T00:00:00Z", "type": "turn_context",
               "payload": {"approval_policy": {"granular": {"command": True}}, "instructions": "x" * 10_000}}
    options = {"separators": (",", ":")} if compact else {}
    line = json.dumps(context, **options).encode()
    assert threads._parse_lines(line + b"\n") == [context]
    # A late field in an unrelated response item must not decode its large payload.
    unrelated = {"timestamp": context["timestamp"], "payload": {"padding": "x" * 300}, "type": "turn_context"}
    assert threads._parse_lines(json.dumps(unrelated).encode() + b"\n") == [{"timestamp": context["timestamp"]}]


def test_approval_mode_is_an_optional_database_column(codex_home) -> None:
    codex_home.add(T1, "demo")
    assert threads.load_threads()[0].approval_mode == ""
    with sqlite3.connect(codex_home.db) as connection:
        connection.execute("ALTER TABLE threads ADD COLUMN approval_mode TEXT")
        connection.execute("UPDATE threads SET approval_mode = 'on-request' WHERE id = ?", (T1,))
    assert threads.load_threads()[0].approval_mode == "on-request"


def test_read_thread_stops_at_the_byte_limit(codex_home) -> None:
    records = []
    for index in range(4):
        records += [started(f"t{index}"), user_message(f"t{index}", f"第 {index} 轮"), _tool_output(64 * 1024),
                    completed(f"t{index}", f"完成 {index}")]
    codex_home.add(T1, "demo", records)
    info = threads.resolve_thread(T1)
    full = threads.read_thread(info, turns=3)
    assert [turn["turn_id"] for turn in full["turns"]] == ["t1", "t2", "t3"] and "history_truncated" not in full
    capped = threads.read_thread(info, turns=3, max_bytes=100 * 1024)
    assert capped["history_truncated"] is True
    assert [turn["turn_id"] for turn in capped["turns"]] == ["t2", "t3"]  # t2 only from its tool output on


def test_read_records_is_incremental_and_ignores_partial_lines(tmp_path: Path) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_text(json.dumps(started("t1")) + "\n" + '{"type": "event_msg", "pay', encoding="utf-8")
    records, offset = threads.read_records(path)
    assert len(records) == 1
    with path.open("a", encoding="utf-8") as stream:
        stream.write('load": {"type": "task_started", "turn_id": "t2"}}\n')
    more, _ = threads.read_records(path, offset)
    assert more[0]["payload"]["turn_id"] == "t2"


def test_delivery_tracker_waits_for_nonce_turn() -> None:
    tracker = threads.DeliveryTracker("[agent_telegram:atg-1]")
    tracker.feed([started("old"), user_message("old", "别的任务"), completed("old", "旧回复")])
    assert tracker.turn_id is None and not tracker.done
    tracker.feed([started("new"), user_message("new", "[agent_telegram:atg-1] 请画图")])
    assert tracker.turn_id == "new" and not tracker.done
    tracker.feed([completed("new", "画好了")])
    assert tracker.done and tracker.turn.final_message == "画好了"


@pytest.mark.parametrize("expected_prefix", ["agent_telegram", "agent-telegram"])
@pytest.mark.parametrize("recorded_prefix", ["agent_telegram", "agent-telegram"])
def test_delivery_tracker_reads_both_persisted_nonce_families(expected_prefix: str, recorded_prefix: str) -> None:
    tracker = threads.DeliveryTracker(f"[{expected_prefix}:atg-1]")
    tracker.feed([started("wrong"), user_message("wrong", f"[{recorded_prefix}:atg-10] 另一项任务"),
                  completed("wrong", "done")])
    assert tracker.turn_id is None
    tracker.feed([started("right"), delegated("right", f"[{recorded_prefix}:atg-1] 当前任务"),
                  completed("right", "done")])
    assert tracker.done and tracker.turn_id == "right"


def test_delivery_tracker_keeps_raw_nonces_literal() -> None:
    tracker = threads.DeliveryTracker("[tg:abc]")
    tracker.feed([started("wrong"), delegated("wrong", "[agent-telegram:abc] 另一项任务"), completed("wrong", "done")])
    assert tracker.turn_id is None
    tracker.feed([started("right"), delegated("right", "[tg:abc] 原始消息"), completed("right", "done")])
    assert tracker.done and tracker.turn_id == "right"


def test_web_evidence_counts_both_servers_across_retry_turns(codex_home) -> None:
    def call(turn: str, server: str, tool: str) -> dict:
        record = relay_call(turn, tool, action="wait", job_id="job-1")
        record["payload"]["item"]["server"] = server
        return record

    records = [
        started("first"), delegated("first", "[agent-telegram:atg-mixed] 审稿"),
        call("first", "gpt_relay", "paper_review_submit"),
        call("first", "agent_telegram", "web_review"),
        completed("first", None, {"message": "at capacity", "codex_error_info": "server_overloaded"}),
        started("retry"),
        call("retry", "agent_telegram", "web_job"),
        call("retry", "agent_telegram", "agent_status"),
        call("retry", "another_server", "web_review"),
        completed("retry", "done"),
    ]
    codex_home.add(T1, "mixed web evidence", records)
    report = threads.read_thread(threads.resolve_thread(T1), turns=2)
    assert [turn["web_call_count"] for turn in report["turns"]] == [2, 1]
    assert all(turn["web_calls"] == turn["gpt_relay_calls"] for turn in report["turns"])
    assert report["turns"][1]["mcp_call_count"] == 3

    tracker = threads.DeliveryTracker("[agent-telegram:atg-mixed]")
    tracker.feed(records)
    assert tracker.chain == ["first", "retry"]
    assert [call["tool"] for call in tracker.web_calls] == ["paper_review_submit", "web_review", "web_job"]
    assert tracker.relay_calls == tracker.web_calls
    summary = tracker.turns[0].to_dict(max_calls=1)
    assert summary["web_call_count"] == summary["gpt_relay_call_count"] == 2
    assert summary["web_calls"] == summary["gpt_relay_calls"] == tracker.turns[0].web_calls[-1:]


def test_delivered_message_and_retry_chain() -> None:
    tracker = threads.DeliveryTracker("[agent_telegram:atg-2]")
    overloaded = {"message": "Selected model is at capacity.", "codex_error_info": "server_overloaded"}
    tracker.feed([started("a"), delegated("a", "[agent_telegram:atg-2] 审稿"), completed("a", None, overloaded)])
    assert tracker.chain == ["a"] and tracker.turn.status == "failed" and not tracker.done
    tracker.feed([started("b")])
    assert tracker.chain == ["a", "b"] and not tracker.superseded
    tracker.feed([user_message("b", "其实这是别人的新消息")])
    assert tracker.chain == ["a"] and tracker.superseded
    tracker.feed([started("c"), completed("c", "done")])
    assert tracker.chain == ["a"], "a turn after foreign input is not a retry of ours"


def test_codex_watch_continuations_stay_in_the_retry_chain() -> None:
    tracker = threads.DeliveryTracker("[agent_telegram:atg-3]")
    overloaded = {"message": "Selected model is at capacity.", "codex_error_info": "server_overloaded"}
    tracker.feed([started("a"), delegated("a", "[agent_telegram:atg-3] 审稿"), completed("a", None, overloaded)])
    continuation = threads.AUTO_CONTINUE_MARKER + "上一轮因模型容量不足中断，请从中断处继续之前未完成的工作。"
    tracker.feed([started("b"), delegated("b", continuation)])
    assert tracker.chain == ["a", "b"] and not tracker.superseded
    tracker.feed([completed("b", "审稿完成")])
    assert tracker.done and tracker.turn.final_message == "审稿完成"


def test_turn_cut_short_by_a_restart_is_followed_by_its_continuation() -> None:
    tracker = threads.DeliveryTracker("[agent_telegram:atg-4]")
    tracker.feed([started("a"), delegated("a", "[agent_telegram:atg-4] 审稿"), relay_call("a", "paper_job")])
    assert tracker.chain == ["a"] and tracker.turn.status == "running"
    # The app restarted: turn a never completes, and the restart tool continues the thread.
    tracker.feed([started("b"), delegated("b", threads.RESTART_CONTINUE_TEXT + "。")])
    assert tracker.chain == ["a", "b"] and tracker.turns[0].status == "interrupted"
    tracker.feed([completed("b", "审稿完成")])
    assert tracker.done and tracker.turn.final_message == "审稿完成"


def test_turn_superseded_by_foreign_input_ends_the_chain_interrupted() -> None:
    tracker = threads.DeliveryTracker("[agent_telegram:atg-5]")
    tracker.feed([started("a"), delegated("a", "[agent_telegram:atg-5] 审稿")])
    tracker.feed([started("b"), user_message("b", "别人的新任务")])
    assert tracker.chain == ["a"] and tracker.turn.status == "interrupted" and not tracker.done
    assert tracker.superseded


def test_mark_interrupted_follows_a_resume_turn_and_reverts_when_the_turn_goes_on() -> None:
    tracker = threads.DeliveryTracker("[agent_telegram:atg-6]")
    tracker.feed([started("a"), delegated("a", "[agent_telegram:atg-6] 审稿")])
    tracker.mark_interrupted()
    assert tracker.turn.status == "interrupted"
    tracker.feed([relay_call("a", "paper_job")])  # the app was wrong: the turn still writes records
    assert tracker.turn.status == "running" and tracker.chain == ["a"]
    tracker.mark_interrupted()
    tracker.feed([started("resume")])  # Resume starts a turn without new input
    assert tracker.chain == ["a", "resume"] and tracker.turn.status == "running"


def test_headless_sessions_and_continuation_texts(codex_home) -> None:
    codex_home.add(T1, "desktop", [])
    codex_home.add(T2, "probe", [], source="exec")
    codex_home.add(T3, "terminal", [], source="cli")
    assert [threads.resolve_thread(ref).is_headless for ref in (T1, T2, T3)] == [False, True, True]
    restart = threads.TurnSummary("r", user_text="Codex 已重启，请继续你之前未完成的工作。")
    assert not threads.carries_user_input(restart)
    assert threads.carries_user_input(threads.TurnSummary("u", user_text="请继续"))


def test_error_info_is_one_word_and_prune_keeps_the_newest_turns() -> None:
    collector = threads.TurnCollector()
    connection = {"message": "Connection failed", "codex_error_info": {"http_connection_failed": {"http_status_code": None}}}
    collector.feed([started("a"), completed("a", None, connection), started("b"),
                    completed("b", None, {"message": "busy", "codex_error_info": "server_overloaded"}), started("c")])
    assert [turn.error_info for turn in collector.ordered()] == ["http_connection_failed", "server_overloaded", ""]
    assert collector.ordered()[0].to_dict()["error_info"] == "http_connection_failed"
    collector.prune(2)
    assert collector.order == ["b", "c"] and set(collector.turns) == {"b", "c"}
    collector.feed([completed("c", "ok")])
    assert collector.ordered()[-1].status == "completed"


def test_parse_manifest_variants() -> None:
    body = {"status": "ok", "summary": "s", "files": [{"path": "C:\\out\\a.md"}, "C:\\out\\b.png"]}
    text = f"完成。\nBEGIN_AGENT_TG_LEGACY_RESULT\n```json\n{json.dumps(body)}\n```\nEND_AGENT_TG_LEGACY_RESULT"
    manifest, error = threads.parse_manifest(text)
    assert error == "" and [item["path"] for item in manifest["files"]] == ["C:\\out\\a.md", "C:\\out\\b.png"]
    assert threads.parse_manifest("没有清单")[0] is None
    assert "JSON" in threads.parse_manifest("BEGIN_AGENT_TG_LEGACY_RESULT {bad END_AGENT_TG_LEGACY_RESULT")[1]
    assert threads.parse_manifest(None)[0] is None


def test_collect_manifest_files_copies_outside_files(tmp_path: Path) -> None:
    output = tmp_path / "out"
    output.mkdir()
    inside = output / "review.md"
    inside.write_text("r", encoding="utf-8")
    outside = tmp_path / "elsewhere" / "figure.png"
    outside.parent.mkdir()
    outside.write_bytes(b"png")
    manifest = {"files": [{"path": str(inside), "kind": "review"}, {"path": str(outside), "kind": "image"},
                          {"path": str(tmp_path / "missing.txt")}, {"path": "relative.txt"}]}
    verified, missing = threads.collect_manifest_files(manifest, output)
    assert [Path(item["path"]).name for item in verified] == ["review.md", "figure.png"]
    assert verified[1]["copied_from"] == str(outside) and (output / "figure.png").is_file()
    assert len(missing) == 2


def test_session_index_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "codex"
    (home / "sessions" / "2026").mkdir(parents=True)
    (home / "session_index.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in (
            {"id": T1, "thread_name": "旧名字", "updated_at": "2026-09-01"},
            {"id": T1, "thread_name": "新名字", "updated_at": "2026-09-02"},
        )) + "\n",
        encoding="utf-8",
    )
    (home / "sessions" / "2026" / f"rollout-2026-09-02T00-00-00-{T1}.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(home))
    info = threads.resolve_thread("新名字")
    assert info.id == T1 and info.rollout and info.rollout.name.endswith(f"{T1}.jsonl")


def test_thread_source_is_loaded_and_defaults_to_empty(codex_home, tmp_path: Path) -> None:
    import sqlite3

    codex_home.add(T1, "定时任务", [], thread_source="automation")
    codex_home.add(T2, "手动对话", [])
    loaded = {info.id: info.thread_source for info in threads.load_threads()}
    assert loaded == {T1: "automation", T2: "user"}

    old = tmp_path / "old-home"
    old.mkdir()
    with sqlite3.connect(old / "state_4.sqlite") as connection:
        connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, updated_at INTEGER,"
                           " source TEXT, cwd TEXT, title TEXT, archived INTEGER)")
        connection.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?)", (T3, "", 1_790_000_000, "vscode", "", "t", 0))
    (info,) = threads.load_threads(old)
    assert (info.id, info.thread_source, info.is_pinned) == (T3, "", False)


def test_strip_verbatim() -> None:
    assert threads.strip_verbatim("\\\\?\\C:\\projects") == "C:\\projects"
    assert threads.strip_verbatim("\\\\?\\UNC\\server\\share") == "\\\\server\\share"
    assert threads.strip_verbatim("C:\\projects") == "C:\\projects"


@pytest.mark.parametrize(("answers", "expected"), [
    ({0: ('问题"一"\\路径\n<&>', '  回答"一"\\路径\n<&> \t')},
     [("call_test1", 0, '回答"一"\\路径\n<&>')]),
    ({2: ("第二个问题", "  第二答复  "), 0: ("第一个问题", "\n第一答复\t")},
     [("call_test1", 0, "第一答复"), ("call_test1", 2, "第二答复")]),
])
def test_question_reply_round_trip(answers, expected) -> None:
    assert threads.question_reply_entries(threads.build_question_reply("call_test1", answers)) == expected


def test_question_reply_builder_matches_desktop_bytes() -> None:
    expected = (
        '<send_user_message_question_reply>\n'
        r'[{"questionItemId":"[\"request_user_input_async\",\"call_test1\",0]",'
        r'"question":"中文\"标题\\路径","answer":"答复\"选项\\目录"}]'
        '\n</send_user_message_question_reply>'
    )
    actual = threads.build_question_reply("call_test1", {0: ('中文"标题\\路径', '答复"选项\\目录')})
    assert actual.encode("utf-8") == expected.encode("utf-8")
    assert not actual.endswith("\n")
    assert threads.question_reply_entries(expected + "\n") == [("call_test1", 0, '答复"选项\\目录')]


def _question_reply_payload(payload: object) -> str:
    return (threads.QUESTION_REPLY_OPEN + "\n" + json.dumps(payload, ensure_ascii=False)
            + "\n" + threads.QUESTION_REPLY_CLOSE)


@pytest.mark.parametrize("as_list", [False, True])
def test_question_reply_entries_accepts_object_or_list_and_spaced_item_id(as_list: bool) -> None:
    entry = {"questionItemId": '["request_user_input_async", "call_test1", 2]',
             "question": '问题"二"\\路径\n<&>', "answer": "  中文\n<&>  ", "extra": 123}
    payload = [entry] if as_list else entry
    assert threads.question_reply_entries(" \n" + _question_reply_payload(payload) + "\t ") == [
        ("call_test1", 2, "  中文\n<&>  ")]


@pytest.mark.parametrize(("prefix", "suffix"), [("[tg:abc] ", ""), ("", "额外后缀")])
def test_question_reply_entries_rejects_text_outside_envelope(prefix: str, suffix: str) -> None:
    envelope = threads.build_question_reply("call_test1", {0: ("问题", "回答")})
    assert threads.question_reply_entries(prefix + envelope + suffix) == []


@pytest.mark.parametrize("changes", [
    {"questionItemId": '["request_user_input", "call_test1", 0]'},
    {"questionItemId": '["request_user_input_async", 123, 0]'},
    {"questionItemId": '["request_user_input_async", "call_test1", "0"]'},
    {"questionItemId": '["request_user_input_async", "call_test1", 0.0]'},
    {"questionItemId": '["request_user_input_async", "call_test1", true]'},
    {"questionItemId": '["request_user_input_async", "call_test1", false]'},
    {"questionItemId": '["request_user_input_async", "call_test1"]'},
    {"questionItemId": '["request_user_input_async", "call_test1", 0, 1]'},
    {"questionItemId": '{"tool": "request_user_input_async"}'},
    {"questionItemId": 'null'},
    {"questionItemId": 'invalid JSON'},
    {"questionItemId": ["request_user_input_async", "call_test1", 0]},
    {"question": None},
    {"answer": None},
    {"answer": 123},
])
def test_question_reply_entries_rejects_invalid_entry_fields(changes: dict) -> None:
    entry = {"questionItemId": '["request_user_input_async", "call_test1", 0]',
             "question": "问题", "answer": "回答"}
    assert threads.question_reply_entries(_question_reply_payload([entry | changes])) == []


@pytest.mark.parametrize("payload", [[], {}, None, True, "回答", 123, ["回答"], [{}]])
def test_question_reply_entries_rejects_invalid_payload_shapes(payload: object) -> None:
    assert threads.question_reply_entries(_question_reply_payload(payload)) == []


@pytest.mark.parametrize("missing", ["questionItemId", "question", "answer"])
def test_question_reply_entries_rejects_missing_fields(missing: str) -> None:
    entry = {"questionItemId": '["request_user_input_async", "call_test1", 0]',
             "question": "问题", "answer": "回答"}
    del entry[missing]
    assert threads.question_reply_entries(_question_reply_payload(entry)) == []


def test_question_reply_entries_rejects_whole_list_if_any_entry_is_invalid() -> None:
    entry = {"questionItemId": '["request_user_input_async", "call_test1", 0]',
             "question": "问题", "answer": "回答"}
    assert threads.question_reply_entries(_question_reply_payload([entry, {"answer": "缺少字段"}])) == []


@pytest.mark.parametrize("text", [
    "",
    "普通消息",
    "<send_user_message_question_reply>invalid JSON</send_user_message_question_reply>",
    "<send_user_message_question_reply>[]",
    "[]</send_user_message_question_reply>",
])
def test_question_reply_entries_rejects_invalid_json_or_missing_envelope(text: str) -> None:
    assert threads.question_reply_entries(text) == []


def test_question_reply_builder_strips_answers_and_preserves_titles() -> None:
    title = ' \n问题"标题"\\<&>\t '
    envelope = threads.build_question_reply("call_test1", {0: (title, " \n回答\n第二行\t ")})
    payload = json.loads(envelope[len(threads.QUESTION_REPLY_OPEN):-len(threads.QUESTION_REPLY_CLOSE)])
    assert payload[0]["question"] == title
    assert payload[0]["answer"] == "回答\n第二行"


@pytest.mark.parametrize("answers", [{}, {0: ("问题", "")}, {0: ("问题", " \n\t ")},
                                      {0: ("问题一", "回答"), 2: ("问题二", " ")}])
def test_question_reply_builder_rejects_empty_answers(answers) -> None:
    with pytest.raises(ValueError, match="empty"):
        threads.build_question_reply("call_test1", answers)


def test_question_reply_entries_accepts_empty_answer_and_negative_integer_index() -> None:
    entry = {"questionItemId": '["request_user_input_async", "call_test1", -1]',
             "question": "问题", "answer": ""}
    assert threads.question_reply_entries(_question_reply_payload(entry)) == [("call_test1", -1, "")]


@pytest.mark.parametrize("marker", ["tg:Ab3_-x", "tg:x", "tg:" + "a" * 64, ""])
def test_delegated_reply_unescapes_envelope_and_returns_marker(marker: str) -> None:
    envelope = threads.build_question_reply("call_test1", {0: ("问题\n<&>", '回答<标签> & "引号"\\路径')})
    text = f"[{marker}] \n\t{envelope}" if marker else envelope
    output = delegated("a", " \n" + text + "\n \t")["payload"]["item"]["output"]
    assert "&lt;send_user_message_question_reply&gt;" in output
    assert "&lt;标签&gt; &amp;" in output
    assert threads.delegated_reply(output) == (envelope, marker)


@pytest.mark.parametrize("text", ["[tg:] 回答", "[tg:" + "a" * 65 + "] 回答", "[tg:a!] 回答",
                                 "前缀 [tg:abc] 回答"])
def test_delegated_reply_preserves_invalid_or_nonleading_markers(text: str) -> None:
    output = delegated("a", text)["payload"]["item"]["output"]
    assert threads.delegated_reply(output) == (text, "")


@pytest.mark.parametrize("output", ["", "普通输出 &lt;标签&gt;", "<input>未闭合", "</input>"])
def test_delegated_reply_requires_input_element(output: str) -> None:
    assert threads.delegated_reply(output) is None


def test_unescape_delegated_uses_desktop_order_and_only_three_entities() -> None:
    escaped = "&lt;x&gt; &amp; &amp;lt; &amp;gt; &amp;amp; &quot; &#60; &apos;"
    assert threads.unescape_delegated(escaped) == '<x> & &lt; &gt; &amp; &quot; &#60; &apos;'


def test_delegated_input_unescapes_in_turn_collector_and_delivery_tracker() -> None:
    text = "[agent_telegram:atg-9] a<b & c>d"
    record = delegated("a", text)
    assert "a&lt;b &amp; c&gt;d" in record["payload"]["item"]["output"]
    records = [started("a"), record]
    collector = threads.TurnCollector()
    collector.feed(records)
    assert collector.turns["a"].user_text == text
    tracker = threads.DeliveryTracker("[agent_telegram:atg-9]")
    tracker.feed(records)
    assert tracker.turn_id == "a" and tracker.turn.user_text == text


def test_delegated_input_keeps_whole_output_fallback() -> None:
    record = delegated("a", "占位")
    record["payload"]["item"]["output"] = "[agent_telegram:atg-9] 原样 &lt;标签&gt;"
    collector = threads.TurnCollector()
    collector.feed([started("a"), record])
    assert collector.turns["a"].user_text == "[agent_telegram:atg-9] 原样 &lt;标签&gt;"


def test_parse_manifest_reads_the_agent_telegram_markers() -> None:
    body = {"status": "ok", "summary": "s", "files": ["C:\\out\\a.md"]}
    manifest, error = threads.parse_manifest(f"完成。\nBEGIN_AGENT_TG_RESULT\n{json.dumps(body)}\nEND_AGENT_TG_RESULT")
    assert error == "" and manifest["files"] == [{"path": "C:\\out\\a.md"}]
    later = f"BEGIN_AGENT_TG_LEGACY_RESULT {{\"summary\": \"old\"}} END_AGENT_TG_LEGACY_RESULT\nBEGIN_AGENT_TG_RESULT {json.dumps(body)} END_AGENT_TG_RESULT"
    assert threads.parse_manifest(later)[0]["summary"] == "s"
    legacy_last = f"BEGIN_AGENT_TG_RESULT {json.dumps(body)} END_AGENT_TG_RESULT\nBEGIN_AGENT_TG_LEGACY_RESULT {{\"summary\": \"old\"}} END_AGENT_TG_LEGACY_RESULT"
    assert threads.parse_manifest(legacy_last)[0]["summary"] == "old"
    # A begin marker only closes with its own end marker.
    assert threads.parse_manifest("BEGIN_AGENT_TG_LEGACY_RESULT {} END_AGENT_TG_RESULT")[0] is None
    assert threads.parse_manifest("BEGIN_AGENT_TG_RESULT {} END_AGENT_TG_LEGACY_RESULT")[0] is None

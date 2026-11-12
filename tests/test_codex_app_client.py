from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from agent_telegram import codex_app_client as client

FAKE_SERVER = Path(__file__).with_name("fake_app_tools_server.py")
THREAD = "01a07078-0000-7000-8000-0000000000e1"
GOOD_PIPE = "codex-app-tools-00000000-0000-0000-0000-000000000001"
BAD_PIPE = "codex-browser-use-00000000-0000-0000-0000-000000000bad"
LEGACY_GOOD_PIPE = "codex-browser-use-00000000-0000-0000-0000-000000000002"


@pytest.fixture
def fake_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    log = tmp_path / "app-tools.log"
    monkeypatch.setenv("FAKE_APP_TOOLS_LOG", str(log))
    monkeypatch.setattr(client, "find_node", lambda: Path(sys.executable))
    monkeypatch.setattr(client, "find_server", lambda: FAKE_SERVER)
    monkeypatch.setattr(client, "list_pipes", lambda: [BAD_PIPE, GOOD_PIPE])
    return log


def _logged(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_list_pipes_accepts_dedicated_app_tools_and_prefers_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client.os, "listdir", lambda _root: [LEGACY_GOOD_PIPE, GOOD_PIPE, BAD_PIPE])
    pipes = client.list_pipes()
    assert pipes[0] == GOOD_PIPE
    assert set(pipes[1:]) == {BAD_PIPE, LEGACY_GOOD_PIPE}


def test_send_message_skips_pipes_without_thread_tools(fake_app: Path) -> None:
    message = '请用 GPT 审稿 "main.pdf"\n路径 C:\\paper\\main.pdf'
    result = client.send_message(THREAD, message)
    assert result["pipe"] == GOOD_PIPE and json.loads(result["result_text"])["tool"] == "send_message_to_thread"

    calls = [entry for entry in _logged(fake_app) if entry.get("method") == "tools/call"]
    good_calls = [entry["params"] for entry in calls if entry["pipe"].endswith(GOOD_PIPE)]
    assert [call["name"] for call in good_calls] == ["read_thread", "navigate_to_codex_page", "send_message_to_thread"]
    sent = good_calls[-1]
    assert sent["arguments"] == {"threadId": THREAD, "prompt": message, "hostId": "local"}
    assert sent["_meta"]["openai/threadId"] == THREAD
    assert sent["_meta"]["x-codex-turn-metadata"]["thread_id"] == THREAD
    assert not any(entry["pipe"].endswith(BAD_PIPE) for entry in calls)


def test_send_without_navigation_leaves_the_visible_conversation(fake_app: Path) -> None:
    client.send_message(THREAD, "继续", navigate=False)
    calls = [entry["params"] for entry in _logged(fake_app)
             if entry.get("method") == "tools/call" and entry["pipe"].endswith(GOOD_PIPE)]
    assert [call["name"] for call in calls] == ["read_thread", "send_message_to_thread"]
    assert calls[-1]["arguments"] == {"threadId": THREAD, "prompt": "继续", "hostId": "local"}


def test_model_overrides_are_forwarded(fake_app: Path) -> None:
    client.send_message(THREAD, "hi", model="gpt-6-sol", thinking="high")
    sent = [entry["params"] for entry in _logged(fake_app) if entry.get("method") == "tools/call"][-1]
    assert sent["arguments"] == {"threadId": THREAD, "prompt": "hi", "hostId": "local", "model": "gpt-6-sol",
                                 "thinking": "high"}


def test_working_pipe_is_cached_and_tried_first(fake_app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client.send_message(THREAD, "first")
    fake_app.write_text("", encoding="utf-8")
    client.send_message(THREAD, "second")
    initialize = [entry for entry in _logged(fake_app) if entry.get("method") == "initialize"]
    assert initialize[0]["pipe"].endswith(GOOD_PIPE)


def test_cached_legacy_pipe_does_not_shadow_dedicated_pipe(fake_app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client, "list_pipes", lambda: [LEGACY_GOOD_PIPE, GOOD_PIPE])
    monkeypatch.setattr(client, "_cached_pipe", lambda: LEGACY_GOOD_PIPE)
    result = client.send_message(THREAD, "继续", navigate=False)
    assert result["pipe"] == GOOD_PIPE


def test_open_session_without_thread_only_checks_tool_listing(fake_app: Path) -> None:
    with client.open_session(timeout_s=5) as session:
        assert session.pipe == GOOD_PIPE
    assert not [entry for entry in _logged(fake_app) if entry.get("method") == "tools/call"]


def test_caller_host_override_is_local_to_app_tools_child(fake_app: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    monkeypatch.setenv("CODEX_APP_TOOLS_CALLER_HOST_ID", "other-host")
    with client.open_session(timeout_s=5, caller_host_id="local") as session:
        assert session.env["CODEX_APP_TOOLS_CALLER_HOST_ID"] == "local"
    assert os.environ["CODEX_APP_TOOLS_CALLER_HOST_ID"] == "other-host"


def test_tool_calls_carry_executor_metadata(fake_app: Path) -> None:
    with client.open_session(timeout_s=5) as session:
        with pytest.raises(client.AppToolsError, match="thread metadata"):
            session.call_tool("list_threads", {"limit": 1})
        assert session.call_tool("list_threads", {"limit": 1}, thread_id=THREAD)["content"]


@pytest.mark.parametrize(("status", "busy"), [("active", True), ("idle", False), ("notLoaded", False)])
def test_busy_check_reads_the_thread_once(fake_app: Path, monkeypatch: pytest.MonkeyPatch, status: str,
                                          busy: bool) -> None:
    monkeypatch.setenv("FAKE_THREAD_STATUS", status)
    assert client.app_reports_busy(THREAD) is busy
    reads = [entry for entry in _logged(fake_app)
             if entry.get("method") == "tools/call" and entry["pipe"].endswith(GOOD_PIPE)]
    assert [entry["params"]["name"] for entry in reads] == ["read_thread"]  # the probe answer is reused


def test_busy_check_is_unknown_without_a_usable_pipe(monkeypatch: pytest.MonkeyPatch, fake_app: Path) -> None:
    monkeypatch.setattr(client, "list_pipes", lambda: [BAD_PIPE])
    assert client.app_reports_busy(THREAD, timeout_s=0.5) is None


def test_tool_errors_raise(fake_app: Path) -> None:
    with pytest.raises(client.AppToolsError, match="rejected"):
        client.send_message(THREAD, "FAIL")


def test_no_usable_pipe(monkeypatch: pytest.MonkeyPatch, fake_app: Path) -> None:
    monkeypatch.setattr(client, "list_pipes", lambda: [BAD_PIPE])
    with pytest.raises(client.AppToolsError, match="没有找到可用"):
        client.open_session(timeout_s=0.5)

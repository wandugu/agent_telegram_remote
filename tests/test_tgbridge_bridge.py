from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_telegram import codex_threads
from agent_telegram.tgbridge import bridge as bridge_module
from agent_telegram.tgbridge import codex_ipc, progress
from agent_telegram.tgbridge.bridge import Bridge, BridgeLog, parse_command, topic_link
from agent_telegram.tgbridge.config import Config
from agent_telegram.tgbridge.model import (ANTIGRAVITY, CLAUDE, CODEX, ApprovalEvent, AttentionEvent, ProgressEvent, Question, QuestionEvent, Quota,
                                       QuotaWindow, SessionView, TurnEvent, session_key)
from agent_telegram.tgbridge.state import BridgeState
from agent_telegram.tgbridge.redact import redact_patterns
from agent_telegram.tgbridge.telegram_api import Conflict, TelegramError

CHAT = -1001234567890
OWNER = 42
BOT_ID = 900
THREAD = "019a0000-0000-7000-8000-000000000001"
SESSION = "11111111-2222-4333-8444-555555555555"
ANTIGRAVITY_SESSION = "ag-desktop-001"


class FakeAPI:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.next_message = 100
        self.next_thread = 500
        self.fail_topics = False
        self.topic_network_error = False
        self.edit_topic_error: Exception | None = None
        self.edit_topic_attempts = 0
        self.members = 3

    def _record(self, call: str, **fields: Any) -> dict[str, Any]:
        self.calls.append((call, fields))
        return fields

    def sent(self, name: str = "send_message") -> list[dict[str, Any]]:
        return [fields for call, fields in self.calls if call == name]

    def get_me(self) -> dict[str, Any]:
        return {"id": BOT_ID, "username": "pc1_bot", "is_bot": True}

    def webhook_info(self) -> dict[str, Any]:
        return {"url": ""}

    def get_chat(self, chat_id: int) -> dict[str, Any]:
        return {"id": chat_id, "is_forum": True}

    def set_commands(self, commands: Any, *, scope: Any = None) -> None:
        self._record("set_commands")

    def send_message(self, chat_id: int, text: str, **options: Any) -> dict[str, Any]:
        self.next_message += 1
        self._record("send_message", chat_id=chat_id, text=text, message_id=self.next_message, **options)
        return {"message_id": self.next_message}

    def edit_message(self, chat_id: int, message_id: int, text: str, **options: Any) -> dict[str, Any]:
        self._record("edit_message", message_id=message_id, text=text, **options)
        return {}

    def edit_buttons(self, chat_id: int, message_id: int, buttons: Any = None) -> None:
        self._record("edit_buttons", message_id=message_id)

    def send_document(self, chat_id: int, filename: str, data: bytes, **options: Any) -> dict[str, Any]:
        self.next_message += 1
        self._record("send_document", filename=filename, data=data, **options)
        return {"message_id": self.next_message}

    def answer_callback(self, callback_id: str, text: str = "", **options: Any) -> None:
        self._record("answer_callback", text=text)

    def create_topic(self, chat_id: int, name: str) -> int:
        if self.fail_topics:
            raise TelegramError("Bad Request: the chat is not a forum", code=400)
        if self.topic_network_error:
            raise TelegramError("network error", code=None)
        self.next_thread += 1
        self._record("create_topic", name=name)
        return self.next_thread

    def edit_topic(self, chat_id: int, thread_id: int, name: str) -> None:
        self.edit_topic_attempts += 1
        if self.edit_topic_error is not None:
            raise self.edit_topic_error
        self._record("edit_topic", thread_id=thread_id, name=name)

    def close_topic(self, chat_id: int, thread_id: int) -> None:
        self._record("close_topic", thread_id=thread_id)

    def reopen_topic(self, chat_id: int, thread_id: int) -> None:
        self._record("reopen_topic", thread_id=thread_id)

    def leave_chat(self, chat_id: int) -> None:
        self._record("leave_chat", chat_id=chat_id)

    def member_count(self, chat_id: int) -> int:
        return self.members

    def close(self) -> None:
        self._record("close")

    def get_updates(self, offset: Any, **options: Any) -> list[dict[str, Any]]:
        return []


class FakePresence:
    def __init__(self, at_pc: bool = False) -> None:
        self.value = at_pc

    def at_pc(self) -> bool:
        return self.value


class FakeCodex:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, str]] = []
        self.busy_value: bool | None = False
        self.answered: list[tuple[str, str, dict[str, list[str]]]] = []
        self.answer_error: Exception | None = None
        self.async_answered: list[dict[str, Any]] = []
        self.async_answer_error: Exception | None = None
        self.async_answer_result: dict[str, Any] = {"via": "ipc", "turn_id": "t1", "other_turn": False}
        self.async_state: dict[str, Any] | None = {
            "turn_id": "t1", "turn_running": True, "turn_last": True, "thread_busy": True,
            "answered": {}, "answer_sources": {}, "waiting": True,
        }
        self.job_result: dict[str, Any] = {"state": {"status": "running", "phase": "running_turn"}, "result": None}
        self.implemented: list[tuple[str, str, str]] = []
        self.limits: Quota | None = None
        self.title = "论文审稿"
        self.approvals: list[dict[str, Any]] = []
        self.approval_error: Exception | None = None

    def quota(self) -> Quota | None:
        return self.limits

    def sessions(self, limit: int) -> list[SessionView]:
        return [SessionView(CODEX, THREAD, self.title, r"C:\projects\paper", "running", time.time() - 30, live=True)]

    def view(self, thread_id: str) -> SessionView | None:
        return self.sessions(1)[0] if thread_id == THREAD else None

    def busy(self, thread_id: str) -> bool | None:
        return self.busy_value

    def submit_send(self, thread_id: str, text: str, *, nonce: str, if_busy: str = "wait") -> str:
        self.sent.append((thread_id, text, nonce, if_busy))
        return "atg-261001-120000-codex-abcdef"

    def job(self, job_id: str) -> dict[str, Any]:
        return self.job_result

    def desktop_running(self) -> bool:
        return True

    def read_turns(self, thread_id: str, k: int) -> list[dict[str, Any]]:
        return [{"status": "completed", "user_text": "问题", "final_message": "回答", "commentary": []}]

    def full_text(self, thread_id: str) -> str | None:
        return "完整回答"

    def answer_question(self, thread_id: str, call_id: str, answers: dict[str, list[str]]) -> None:
        if self.answer_error is not None:
            raise self.answer_error
        self.answered.append((thread_id, call_id, answers))

    def decide_approval(self, thread_id: str, **fields: Any) -> None:
        self.approvals.append({"thread_id": thread_id, **fields})
        if self.approval_error is not None:
            raise self.approval_error

    def answer_async_question(self, thread_id: str, call_id: str, turn_id: str,
                              answers: dict[int, tuple[str, str]], *, client_id: str, marker: str) -> dict[str, Any]:
        self.async_answered.append({"thread_id": thread_id, "call_id": call_id, "turn_id": turn_id,
                                    "answers": answers, "client_id": client_id, "marker": marker})
        if self.async_answer_error is not None:
            raise self.async_answer_error
        return self.async_answer_result

    def async_question_state(self, thread_id: str, call_id: str) -> dict[str, Any] | None:
        return self.async_state

    def implement_plan(self, thread_id: str, turn_id: str, *, nonce: str) -> str:
        self.implemented.append((thread_id, turn_id, nonce))
        return "atg-261001-120000-codex-plan01"


class FakeClaude:
    def __init__(self) -> None:
        self.delivered: list[tuple[str, str]] = []
        self.priorities: list[str] = []
        self.state = "running"
        self.registry_status = "busy"  # what ~/.claude/sessions says about SESSION
        self.registry_kind = "interactive"
        self.registry_changed_at = 0.0  # statusUpdatedAt (epoch s); 0 = unknown
        self.hooked_sessions = {SESSION}  # sessions whose hooks reported to the bridge
        self.limits: Quota | None = None
        self.title = "重构脚本"

    def quota(self) -> Quota | None:
        return self.limits

    def sessions(self, limit: int) -> list[SessionView]:
        return [SessionView(CLAUDE, SESSION, self.title, r"C:\projects\code", self.state, time.time() - 5, live=True)]

    def view(self, session_id: str) -> SessionView | None:
        return self.sessions(1)[0] if session_id == SESSION else None

    def live(self, session_id: str) -> Any:
        if session_id != SESSION:
            return None
        return SimpleNamespace(status=self.registry_status, kind=self.registry_kind,
                               status_updated_at=self.registry_changed_at)

    def inbox_for(self, session_id: str) -> tuple[str, str] | None:
        return ("\\\\.\\pipe\\LOCAL\\cc-msg-" + "a" * 32, "token") if session_id == SESSION else None

    def deliver(self, session_id: str, text: str, *, priority: str = "next") -> dict[str, Any]:
        self.delivered.append((session_id, text))
        self.priorities.append(priority)
        return {"transcript": "", "offset": 0}

    def check_delivered(self, session_id: str, marker: str, info: dict[str, Any]) -> bool:
        return any(marker in text for _, text in self.delivered)

    def on_hook(self, payload: dict[str, Any]) -> None:
        pass

    def hooked(self, session_id: str) -> bool:
        return session_id in self.hooked_sessions

    def poll(self) -> None:
        pass

    def permission_mode(self, session_id: str) -> str:
        return "default"


class FakeAntigravity:
    def __init__(self) -> None:
        self.read_calls: list[tuple[str, int]] = []
        self.full_calls: list[str] = []
        self.poll_calls = 0
        self.last_error: str | None = None
        self.title = "Antigravity 设计"

    def sessions(self, limit: int) -> list[SessionView]:
        return [SessionView(ANTIGRAVITY, ANTIGRAVITY_SESSION, self.title, r"C:\proj\ag",
                            "idle", time.time() - 10, live=False, kind="desktop")]

    def view(self, conversation_id: str) -> SessionView | None:
        return self.sessions(1)[0] if conversation_id == ANTIGRAVITY_SESSION else None

    def read_turns(self, conversation_id: str, k: int) -> list[dict[str, Any]]:
        self.read_calls.append((conversation_id, k))
        return [{"status": "completed", "user_text": "设计问题", "final_message": "设计回答", "commentary": []}]

    def full_text(self, conversation_id: str) -> str | None:
        self.full_calls.append(conversation_id)
        return "Antigravity 完整回答"

    def quota(self) -> None:
        return None

    def poll(self) -> None:
        self.poll_calls += 1

    def status(self) -> dict[str, Any]:
        return {"stores": ["desktop", "cli"], "conversations": 1, "last_error": self.last_error}


@pytest.fixture(autouse=True)
def fast_permission_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_module, "PERMISSION_POLL_S", 0.01)
    monkeypatch.setattr(bridge_module, "ATTENTION_CARD_WAIT_STEPS", 1)


@pytest.fixture
def setup(tmp_path: Path):
    cfg = Config(machine="pc1", chat_id=CHAT, allowed_user_ids=[OWNER], roots=[str(tmp_path)])
    state = BridgeState(tmp_path / "state.json")
    api = FakeAPI()
    presence = FakePresence(False)
    log = BridgeLog(tmp_path / "logs")
    now = [1_790_800_000.0]
    bridge = Bridge(cfg, state, api, redact=lambda text: text.replace("SECRET", "‹已脱敏›"), presence=presence,
                    log=log, clock=lambda: now[0])
    bridge.me = api.get_me()
    codex, claude = FakeCodex(), FakeClaude()
    bridge.attach_sources(codex=codex, claude=claude, antigravity=FakeAntigravity())
    return bridge, api, state, codex, claude, presence, now


def _message(text: str, *, user: int = OWNER, thread_id: int | None = None, chat: int = CHAT, date: float | None = None,
             message_id: int = 7, **extra: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"message_id": message_id, "chat": {"id": chat, "type": "supergroup"},
                               "from": {"id": user, "is_bot": False, "first_name": "Ann"}, "text": text,
                               "date": int(date if date is not None else 1_790_800_000)}
    if thread_id is not None:
        message.update(message_thread_id=thread_id, is_topic_message=True)
    message.update(extra)
    return message


def _run_threads() -> None:
    for thread in threading.enumerate():
        if thread.name == "tgbridge-exec":
            thread.join(timeout=5)


def _drain(bridge: Bridge) -> None:
    while True:
        item = bridge.outbox.get(0.01)
        if item is None:
            return
        item[2]()


def _card_nonce(api: FakeAPI) -> str:
    buttons = api.sent()[-1]["buttons"]
    return buttons[0][0][1].split(":", 1)[1]



def _run_title_sync(bridge: Bridge, now: list[float], delta: float) -> None:
    now[0] += delta
    bridge._housekeeping()
    _drain(bridge)


def test_housekeeping_syncs_antigravity_title_without_a_new_turn(setup) -> None:
    bridge, api, state, _codex, _claude, _presence, now = setup
    antigravity = bridge.antigravity
    key = session_key(ANTIGRAVITY, ANTIGRAVITY_SESSION)
    bridge.on_event(TurnEvent(ANTIGRAVITY, ANTIGRAVITY_SESSION, antigravity.title, r"C:\\proj\\ag",
                              "completed", final_text="完成"))
    _drain(bridge)
    before = state.topic_for(key)
    assert before is not None
    created_at, active_at = before["created_at"], before["active_at"]

    # Establish the first one-minute deadline before changing the app title.
    bridge._housekeeping()
    _drain(bridge)
    antigravity.title = "General Greeting"
    _run_title_sync(bridge, now, bridge_module.TOPIC_TITLE_SYNC_S - 1)
    assert api.sent("edit_topic") == []

    _run_title_sync(bridge, now, 2)
    edits = api.sent("edit_topic")
    assert len(edits) == 1
    assert edits[0]["name"] == "pc1·Antigravity·General Greeting"
    after = state.topic_for(key)
    assert after is not None
    assert after["name"] == edits[0]["name"]
    assert after["created_at"] == created_at
    assert after["active_at"] == active_at

    _run_title_sync(bridge, now, bridge_module.TOPIC_TITLE_SYNC_S)
    assert len(api.sent("edit_topic")) == 1


def test_housekeeping_syncs_codex_title_without_a_new_turn(setup) -> None:
    bridge, api, state, codex, _claude, _presence, now = setup
    key = session_key(CODEX, THREAD)
    bridge.on_event(TurnEvent(CODEX, THREAD, codex.title, r"C:\\projects\\paper", "completed", final_text="完成"))
    _drain(bridge)
    before = state.topic_for(key)
    assert before is not None

    bridge._housekeeping()
    _drain(bridge)
    codex.title = "新的审稿标题"
    _run_title_sync(bridge, now, bridge_module.TOPIC_TITLE_SYNC_S)
    edits = api.sent("edit_topic")
    assert len(edits) == 1
    assert edits[0]["name"] == "pc1·Codex·新的审稿标题"
    after = state.topic_for(key)
    assert after is not None
    assert after["name"] == edits[0]["name"]
    assert after["created_at"] == before["created_at"]
    assert after["active_at"] == before["active_at"]


def test_title_sync_skips_closed_idle_missing_and_empty_titles(setup) -> None:
    bridge, api, state, codex, claude, _presence, now = setup
    closed_key = session_key(ANTIGRAVITY, ANTIGRAVITY_SESSION)
    idle_key = session_key(CODEX, THREAD)
    empty_key = session_key(CLAUDE, SESSION)
    missing_key = session_key(CODEX, "missing-thread")
    state.set_topic(closed_key, 601, "old closed")
    state.set_topic_closed(closed_key, True)
    state.set_topic(idle_key, 602, "old idle")
    state._topics[idle_key]["active_at"] = now[0] - bridge.cfg.active_window_h * 3600.0 - 1
    state.set_topic(empty_key, 603, "old empty")
    state.set_topic(missing_key, 604, "old missing")
    claude.title = ""
    bridge._housekeeping()
    _drain(bridge)
    codex.title = "new idle"
    _run_title_sync(bridge, now, bridge_module.TOPIC_TITLE_SYNC_S)
    assert api.sent("edit_topic") == []
    assert state.topic_for(closed_key)["name"] == "old closed"
    assert state.topic_for(idle_key)["name"] == "old idle"
    assert state.topic_for(empty_key)["name"] == "old empty"
    assert state.topic_for(missing_key)["name"] == "old missing"


def test_title_sync_does_not_retry_a_failed_name_but_tries_a_new_name(setup) -> None:
    bridge, api, state, codex, _claude, _presence, now = setup
    key = session_key(CODEX, THREAD)
    bridge.on_event(TurnEvent(CODEX, THREAD, codex.title, r"C:\\projects\\paper", "completed", final_text="完成"))
    _drain(bridge)
    bridge._housekeeping()
    _drain(bridge)
    codex.title = "第一次改名"
    api.edit_topic_error = TelegramError("temporary failure", code=500)
    _run_title_sync(bridge, now, bridge_module.TOPIC_TITLE_SYNC_S)
    assert api.edit_topic_attempts == 1
    assert state.topic_for(key)["name"] == "pc1·Codex·论文审稿"
    _run_title_sync(bridge, now, bridge_module.TOPIC_TITLE_SYNC_S)
    assert api.edit_topic_attempts == 1
    codex.title = "第二次改名"
    _run_title_sync(bridge, now, bridge_module.TOPIC_TITLE_SYNC_S)
    assert api.edit_topic_attempts == 2


def test_title_sync_redacts_token_shaped_titles(setup) -> None:
    bridge, api, _state, _codex, _claude, _presence, now = setup
    antigravity = bridge.antigravity
    key = session_key(ANTIGRAVITY, ANTIGRAVITY_SESSION)
    bridge.on_event(TurnEvent(ANTIGRAVITY, ANTIGRAVITY_SESSION, antigravity.title, r"C:\\proj\\ag",
                              "completed", final_text="完成"))
    _drain(bridge)
    bridge._housekeeping()
    _drain(bridge)
    bridge.redact = redact_patterns
    antigravity.title = "token 123456:abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMN"
    _run_title_sync(bridge, now, bridge_module.TOPIC_TITLE_SYNC_S)
    edits = api.sent("edit_topic")
    assert len(edits) == 1
    assert "123456:" not in edits[0]["name"]
    assert "‹已脱敏›" in edits[0]["name"]


def test_parse_command_and_topic_link() -> None:
    assert parse_command("/list@pc1_bot 5") == ("list", "pc1_bot", "5")
    assert parse_command("/stop") == ("stop", "", "")
    assert parse_command("hello") is None
    assert topic_link(-1001234567890, 55) == "https://t.me/c/1234567890/55"


def test_turn_push_creates_topic_and_routes_replies(setup) -> None:
    bridge, api, state, *_ = setup
    event = TurnEvent(CODEX, THREAD, "论文审稿", r"C:\projects\paper", "completed", final_text="结果 SECRET",
                      started_at=1_790_799_000.0, completed_at=1_790_799_600.0)
    bridge.on_event(event)
    _drain(bridge)
    assert api.sent("create_topic")[0]["name"].startswith("pc1·Codex·")
    push = api.sent()[-1]
    assert "‹已脱敏›" in push["text"] and "SECRET" not in push["text"]
    assert push["thread_id"] == 501 and push["buttons"][0][0][1] == f"full:{session_key(CODEX, THREAD)}"
    assert state.key_for_thread(501) == session_key(CODEX, THREAD)
    # A second turn reuses the topic.
    bridge.on_event(event)
    _drain(bridge)
    assert len(api.sent("create_topic")) == 1


def test_antigravity_turn_push_is_view_only_without_plan_or_quota(setup) -> None:
    bridge, api, state, *_ = setup
    bridge.on_event(TurnEvent(ANTIGRAVITY, ANTIGRAVITY_SESSION, "设计结果", r"C:\proj\ag", "completed",
                              final_text="Antigravity 完成", plan=True))
    _drain(bridge)
    topic = api.sent("create_topic")[0]
    push = api.sent()[-1]
    assert "Antigravity" in topic["name"]
    assert "Antigravity 完成" in push["text"]
    assert "📊" not in push["text"]
    assert push["buttons"] == [[("📄 全文", f"full:{session_key(ANTIGRAVITY, ANTIGRAVITY_SESSION)}"),
                                 ("🕘 最近3轮", f"read:{session_key(ANTIGRAVITY, ANTIGRAVITY_SESSION)}")]]
    assert state.key_for_thread(501) == session_key(ANTIGRAVITY, ANTIGRAVITY_SESSION)


def test_antigravity_progress_is_pushed(setup) -> None:
    bridge, api, *_ = setup
    bridge.on_event(ProgressEvent(ANTIGRAVITY, ANTIGRAVITY_SESSION, "设计中", r"C:\proj\ag", ["正在分析"]))
    _drain(bridge)
    assert "Antigravity" in api.sent("create_topic")[0]["name"]
    assert "正在分析" in api.sent()[-1]["text"]


def test_list_includes_antigravity_sessions(setup) -> None:
    bridge, api, *_ = setup
    bridge.handle_update({"update_id": 1, "message": _message("/list")})
    assert "Antigravity" in api.sent()[-1]["text"]


def test_read_and_full_route_to_antigravity_source(setup) -> None:
    bridge, api, *_ = setup
    antigravity = bridge.antigravity
    key = session_key(ANTIGRAVITY, ANTIGRAVITY_SESSION)
    bridge._cmd_read(_message("/read"), key, 2)
    bridge._cmd_full(_message("/full"), key)
    assert antigravity.read_calls == [(ANTIGRAVITY_SESSION, 2)]
    assert antigravity.full_calls == [ANTIGRAVITY_SESSION]
    assert "Antigravity 完整回答" in api.sent()[-1]["text"]


def test_antigravity_replies_and_stop_are_refused_without_confirmation(setup) -> None:
    bridge, api, state, *_ = setup
    bridge.on_event(TurnEvent(ANTIGRAVITY, ANTIGRAVITY_SESSION, "只读", r"C:\proj\ag", "completed",
                              final_text="结果"))
    _drain(bridge)
    thread_id = int(state.topic_for(session_key(ANTIGRAVITY, ANTIGRAVITY_SESSION))["thread_id"])
    bridge.handle_update({"update_id": 1, "message": _message("回复内容", thread_id=thread_id)})
    bridge.handle_update({"update_id": 2, "message": _message("/stop", thread_id=thread_id)})
    texts = [item["text"] for item in api.sent()]
    assert texts[-2:] == ["Antigravity 对话只能查看，请在 Antigravity 里回复。",
                           "Antigravity 对话只能查看，请在 Antigravity 里回复。"]
    assert not bridge.pending


def test_status_shows_antigravity_source_and_error(setup) -> None:
    bridge, api, *_ = setup
    bridge.antigravity.last_error = "store damaged"
    bridge.handle_update({"update_id": 1, "message": _message("/status")})
    text = api.sent()[-1]["text"]
    assert "Antigravity" in text and "desktop" in text and "1 个对话" in text and "store damaged" in text
    assert bridge.status()["antigravity"] == {"stores": ["desktop", "cli"], "conversations": 1,
                                              "last_error": "store damaged"}


def test_poll_loop_step_calls_antigravity(setup) -> None:
    bridge, *_ = setup
    bridge._poll_antigravity()
    assert bridge.antigravity.poll_calls == 1


def test_antigravity_question_event_does_not_create_card(setup) -> None:
    bridge, api, *_ = setup
    bridge.on_event(QuestionEvent(ANTIGRAVITY, ANTIGRAVITY_SESSION, "问题", r"C:\proj\ag", "call-1",
                                  questions=[Question("q", "问题")]))
    _drain(bridge)
    assert not bridge.questions
    assert not api.sent()


def test_muted_and_excluded_sessions(setup) -> None:
    bridge, api, state, *_ = setup
    bridge.cfg.content_exclude = [r"C:\projects\secret"]
    bridge.on_event(TurnEvent(CODEX, THREAD, "机密", r"C:\projects\secret\x", "completed", final_text="机密内容"))
    _drain(bridge)
    push = api.sent()[-1]
    assert "机密内容" not in push["text"] and push["buttons"] is None
    state.set_muted(session_key(CODEX, THREAD), True)
    count = len(api.sent())
    bridge.on_event(TurnEvent(CODEX, THREAD, "机密", r"C:\projects\paper", "completed", final_text="x"))
    _drain(bridge)
    assert len(api.sent()) == count


def test_quiet_when_at_pc(setup) -> None:
    bridge, api, _state, _codex, _claude, presence, _now = setup
    presence.value = True
    bridge.on_event(TurnEvent(CLAUDE, SESSION, "重构脚本", r"C:\projects\code", "completed", final_text="ok"))
    _drain(bridge)
    assert api.sent()[-1]["silent"] is True


def test_send_to_codex_needs_confirmation(setup) -> None:
    bridge, api, state, codex, *_ = setup
    state.set_topic(session_key(CODEX, THREAD), 501, "pc1·Codex·论文审稿")
    bridge.handle_update({"update_id": 1, "message": _message("继续改第三节", thread_id=501)})
    card = api.sent()[-1]
    assert "继续改第三节" in card["text"] and codex.sent == []
    nonce = _card_nonce(api)
    bridge.handle_update({"update_id": 2, "callback_query": {
        "id": "cb", "data": f"ok:{nonce}", "from": {"id": OWNER}, "message": {"message_id": card["message_id"],
                                                                             "chat": {"id": CHAT}}}})
    _run_threads()
    assert codex.sent and codex.sent[0][0] == THREAD and codex.sent[0][2].startswith("[tg:")
    assert codex.sent[0][3] == "wait"
    # The nonce is single use.
    bridge.handle_update({"update_id": 3, "callback_query": {
        "id": "cb2", "data": f"ok:{nonce}", "from": {"id": OWNER}, "message": {"message_id": 101, "chat": {"id": CHAT}}}})
    _run_threads()
    assert len(codex.sent) == 1


def test_busy_codex_offers_send_now(setup) -> None:
    bridge, api, state, codex, *_ = setup
    codex.busy_value = True
    state.set_topic(session_key(CODEX, THREAD), 501, "t")
    bridge.handle_update({"update_id": 1, "message": _message("插一句", thread_id=501)})
    buttons = api.sent()[-1]["buttons"]
    nonce = buttons[0][1][1].split(":", 1)[1]
    assert buttons[0][1][1].startswith("now:")
    bridge.handle_update({"update_id": 2, "callback_query": {
        "id": "cb", "data": f"now:{nonce}", "from": {"id": OWNER}, "message": {"message_id": 101, "chat": {"id": CHAT}}}})
    _run_threads()
    assert codex.sent[0][3] == "send"


def test_send_to_claude_uses_inbox_and_confirms_delivery(setup) -> None:
    bridge, api, state, _codex, claude, *_ = setup
    state.set_topic(session_key(CLAUDE, SESSION), 502, "t")
    bridge.handle_update({"update_id": 1, "message": _message("跑一下测试", thread_id=502)})
    nonce = _card_nonce(api)
    bridge.handle_update({"update_id": 2, "callback_query": {
        "id": "cb", "data": f"ok:{nonce}", "from": {"id": OWNER}, "message": {"message_id": 101, "chat": {"id": CHAT}}}})
    _run_threads()
    assert claude.delivered and "跑一下测试" in claude.delivered[0][1] and claude.delivered[0][1].startswith("[tg:")
    assert claude.priorities == ["later"]  # the session is running: the first button queues
    bridge._check_deliveries()
    _drain(bridge)
    assert any("已排进会话的队列" in call["text"] for call in api.sent("edit_message"))
    assert bridge.deliveries == []


def test_busy_claude_offers_queue_and_steer(setup) -> None:
    bridge, api, state, _codex, claude, *_ = setup
    state.set_topic(session_key(CLAUDE, SESSION), 502, "t")
    bridge.handle_update({"update_id": 1, "message": _message("改用方案 B", thread_id=502)})
    card = api.sent()[-1]
    assert [label for label, _ in card["buttons"][0]] == ["📥 排队：本轮结束后发", "↪️ 引导：插进这一轮"]
    assert "排队" in card["text"] and "引导" in card["text"]
    nonce = card["buttons"][0][1][1].split(":", 1)[1]
    bridge.handle_update({"update_id": 2, "callback_query": {
        "id": "cb", "data": f"now:{nonce}", "from": {"id": OWNER}, "message": {"message_id": 101, "chat": {"id": CHAT}}}})
    _run_threads()
    assert claude.priorities == ["next"]
    bridge._check_deliveries()
    _drain(bridge)
    assert any(call["text"] == "✅ 已送达会话。" for call in api.sent("edit_message"))


def test_idle_claude_gets_a_plain_send(setup) -> None:
    bridge, api, state, _codex, claude, *_ = setup
    claude.state = "idle"
    state.set_topic(session_key(CLAUDE, SESSION), 502, "t")
    bridge.handle_update({"update_id": 1, "message": _message("继续", thread_id=502)})
    assert [label for label, _ in api.sent()[-1]["buttons"][0]] == ["✅ 发送", "✖ 取消"]
    _confirm(bridge, api)
    assert claude.priorities == ["next"]


def test_a_question_without_questions_gets_no_card(setup) -> None:
    bridge, api, *_ = setup
    assert bridge.claude_permission({"session_id": SESSION, "tool_name": "AskUserQuestion", "tool_input": {}}) == {}
    assert api.sent() == []


def test_cancel_and_expiry(setup) -> None:
    bridge, api, state, codex, _claude, _presence, now = setup
    state.set_topic(session_key(CODEX, THREAD), 501, "t")
    bridge.handle_update({"update_id": 1, "message": _message("a", thread_id=501)})
    nonce = _card_nonce(api)
    bridge.handle_update({"update_id": 2, "callback_query": {
        "id": "cb", "data": f"no:{nonce}", "from": {"id": OWNER}, "message": {"message_id": 101, "chat": {"id": CHAT}}}})
    assert "已取消" in api.sent("edit_message")[-1]["text"]
    bridge.handle_update({"update_id": 3, "message": _message("b", thread_id=501)})
    nonce = _card_nonce(api)
    now[0] += 3600
    bridge.handle_update({"update_id": 4, "callback_query": {
        "id": "cb", "data": f"ok:{nonce}", "from": {"id": OWNER}, "message": {"message_id": 102, "chat": {"id": CHAT}}}})
    _run_threads()
    assert codex.sent == []


def test_old_messages_are_not_sent(setup) -> None:
    bridge, api, state, *_ = setup
    state.set_topic(session_key(CODEX, THREAD), 501, "t")
    bridge.handle_update({"update_id": 1, "message": _message("旧消息", thread_id=501, date=1_790_800_000 - 3600)})
    assert "太旧" in api.sent()[-1]["text"]


def test_intruder_locks_writes(setup) -> None:
    bridge, api, state, codex, *_ = setup
    bridge.handle_update({"update_id": 1, "message": _message("hi", user=77)})
    assert state.locked
    _drain(bridge)
    assert "非白名单" in api.sent()[-1]["text"]
    state.set_topic(session_key(CODEX, THREAD), 501, "t")
    bridge.handle_update({"update_id": 2, "message": _message("do it", thread_id=501)})
    assert "锁定" in api.sent()[-1]["text"] and codex.sent == []


def test_foreign_chats_and_bots_are_ignored(setup) -> None:
    bridge, api, state, *_ = setup
    bridge.handle_update({"update_id": 1, "message": _message("/list", chat=-100999)})
    assert api.sent("leave_chat")
    count = len(api.sent())
    bridge.handle_update({"update_id": 2, "message": {**_message("/list"), "from": {"id": 5, "is_bot": True}}})
    bridge.handle_update({"update_id": 3, "message": {**_message("/list"), "sender_chat": {"id": CHAT}}})
    assert len(api.sent()) == count and not state.locked


def test_commands_for_the_other_bot_are_ignored(setup) -> None:
    bridge, api, *_ = setup
    bridge.handle_update({"update_id": 1, "message": _message("/list@pc2_bot")})
    assert api.sent() == []
    bridge.handle_update({"update_id": 2, "message": _message("/list")})
    assert "最近" in api.sent()[-1]["text"]
    bridge.handle_update({"update_id": 3, "message": _message("/new")})
    assert api.sent()[-1]["buttons"][0][0][1] == "mach:new"


def test_unlock_needs_confirmation(setup) -> None:
    bridge, api, state, *_ = setup
    state.set_locked(True, "test")
    bridge.handle_update({"update_id": 1, "message": _message("/unlock@pc1_bot")})
    nonce = _card_nonce(api)
    assert state.locked
    bridge.handle_update({"update_id": 2, "callback_query": {
        "id": "cb", "data": f"ok:{nonce}", "from": {"id": OWNER}, "message": {"message_id": 101, "chat": {"id": CHAT}}}})
    _run_threads()
    assert not state.locked


def test_stop_flag_answers_pretooluse(setup) -> None:
    bridge, api, state, *_ = setup
    state.set_topic(session_key(CLAUDE, SESSION), 502, "t")
    assert bridge.claude_pretooluse({"session_id": SESSION}) == {}
    bridge.handle_update({"update_id": 1, "message": _message("/stop", thread_id=502)})
    nonce = _card_nonce(api)
    bridge.handle_update({"update_id": 2, "callback_query": {
        "id": "cb", "data": f"ok:{nonce}", "from": {"id": OWNER}, "message": {"message_id": 101, "chat": {"id": CHAT}}}})
    _run_threads()
    assert bridge.claude_pretooluse({"session_id": SESSION})["continue"] is False
    bridge.claude_event({"event": "Stop", "session_id": SESSION})
    bridge._drain_hook_events()
    assert bridge.claude_pretooluse({"session_id": SESSION}) == {}


def test_codex_stop_is_refused(setup) -> None:
    bridge, api, state, *_ = setup
    state.set_topic(session_key(CODEX, THREAD), 501, "t")
    bridge.handle_update({"update_id": 1, "message": _message("/stop", thread_id=501)})
    assert "不支持" in api.sent()[-1]["text"]


REMOTE_THREAD = "019a0000-0000-7000-8000-0000000000e1"


class FakeRemote:
    def __init__(self) -> None:
        self.closed = False

    def status(self) -> dict[str, Any]:
        return {"threads": 2, "hosts": ["lab1", "lab2"], "unavailable": ["lab2"], "last_poll": 0.0,
                "last_error": "lab2: TimeoutError: SECRET"}

    def close(self) -> None:
        self.closed = True


def test_remote_codex_threads_are_view_only(setup, monkeypatch) -> None:
    bridge, api, state, codex, *_ = setup
    remote = SessionView(CODEX, REMOTE_THREAD, "lab1·demo-task", "/home/demo", "idle", time.time() - 60,
                         kind="remote:lab1")
    monkeypatch.setattr(codex, "view", lambda thread_id: remote if thread_id == REMOTE_THREAD else None)
    state.set_topic(session_key(CODEX, REMOTE_THREAD), 777, "pc1·Codex·lab1·demo-task")
    bridge.handle_update({"update_id": 1, "message": _message("继续", thread_id=777)})
    assert "只能查看进度" in api.sent()[-1]["text"] and codex.sent == []
    bridge.handle_update({"update_id": 2, "message": _message("/stop", thread_id=777)})
    assert "不支持" in api.sent()[-1]["text"]
    bridge.handle_update({"update_id": 3, "message": _message("/full", thread_id=777)})
    assert api.sent()[-1]["text"] == "完整回答"


def test_status_reports_remote_codex(setup) -> None:
    bridge, api, _state, codex, *_ = setup
    assert "codex_remote" not in bridge.status()
    codex.remote = FakeRemote()
    assert bridge.status()["codex_remote"]["threads"] == 2
    bridge.handle_update({"update_id": 1, "message": _message("/status")})
    text = api.sent()[-1]["text"]
    assert "远程 Codex（只读）：2 个对话，主机 lab1、lab2" in text and "连不上：lab2" in text
    assert "SECRET" not in text


def test_remote_loop_backs_off_after_errors_and_closes_the_session(setup, monkeypatch, tmp_path) -> None:
    bridge, _api, _state, codex, *_ = setup
    codex.remote = FakeRemote()
    monkeypatch.setattr(bridge_module, "REMOTE_FIRST_POLL_S", 0.0)
    monkeypatch.setattr(bridge_module, "REMOTE_MIN_POLL_S", 0.001)
    bridge.cfg.codex_remote_poll_s = 0.001
    calls: list[float] = []
    waits: list[float] = []
    real_wait = bridge.stopping.wait

    def wait(delay: float) -> bool:
        waits.append(delay)
        return real_wait(0)

    def poll_remote() -> None:
        calls.append(time.monotonic())
        if len(calls) >= 4:
            bridge.stopping.set()
        if len(calls) <= 2:
            raise TimeoutError("Codex app is slow")

    monkeypatch.setattr(bridge.stopping, "wait", wait)
    codex.poll_remote = poll_remote
    bridge._remote_loop()
    assert len(calls) == 4 and codex.remote.closed
    assert waits[1] == pytest.approx(0.002) and waits[2] == pytest.approx(0.004) and waits[3] == pytest.approx(0.001)
    log_path = tmp_path / "logs" / "bridge.log"
    log = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    assert "读取远程 Codex 对话出错" not in log  # brief failures stay out of the log


def test_permission_relay_allow(setup) -> None:
    bridge, api, *_ = setup
    result: dict[str, Any] = {}

    def ask() -> None:
        result.update(bridge.claude_permission({"session_id": SESSION, "tool_name": "Bash",
                                                "tool_input": {"command": "rm -rf build"}, "cwd": r"C:\projects\code",
                                                "permission_mode": "default"}))

    thread = threading.Thread(target=ask)
    thread.start()
    assert _wait_for(lambda: bridge.permissions and api.sent())
    nonce = next(iter(bridge.permissions))
    card = api.sent()[-1]
    assert "rm -rf build" in card["text"]
    bridge.handle_update({"update_id": 1, "callback_query": {
        "id": "cb", "data": f"perm:a:{nonce}", "from": {"id": OWNER}, "message": {"message_id": 101, "chat": {"id": CHAT}}}})
    thread.join(timeout=10)
    assert result["hookSpecificOutput"]["decision"]["behavior"] == "allow"


class Asked:
    """A PermissionRequest hook call running on its own thread, as the hook server runs it."""

    def __init__(self, bridge: Bridge, payload: dict[str, Any] | None = None, **options: Any) -> None:
        self.result: dict[str, Any] | None = None
        body = {"session_id": SESSION, "tool_name": "Bash", "tool_input": {"command": "npm test"},
                "cwd": r"C:\projects\code", "permission_mode": "default", **(payload or {})}
        self.thread = threading.Thread(target=self._run, args=(bridge, body, options), daemon=True)
        self.thread.start()

    def _run(self, bridge: Bridge, body: dict[str, Any], options: dict[str, Any]) -> None:
        self.result = bridge.claude_permission(body, **options)

    def done(self) -> dict[str, Any] | None:
        self.thread.join(timeout=10)
        assert not self.thread.is_alive()
        return self.result


def _wait_for(predicate: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _tap(bridge: Bridge, data: str, update_id: int, *, user: int = OWNER) -> None:
    bridge.handle_update({"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "data": data, "from": {"id": user},
        "message": {"message_id": 101, "chat": {"id": CHAT}}}})


def _allowed(result: dict[str, Any] | None) -> bool:
    return bool(result) and result["hookSpecificOutput"]["decision"]["behavior"] == "allow"


def test_permission_at_pc_sends_a_silent_card_and_notices_the_pc_answer(setup) -> None:
    bridge, api, _state, _codex, claude, presence, _now = setup
    presence.value = True
    claude.registry_status = "waiting"
    asked = Asked(bridge)
    assert _wait_for(lambda: bridge.permissions and api.sent())
    card = api.sent()[-1]
    assert card["silent"] is True and "npm test" in card["text"]
    assert [button[1].split(":")[1] for row in card["buttons"] for button in row] == ["a", "d", "A", "l"]
    time.sleep(0.1)
    assert asked.thread.is_alive()  # Claude's dialog races the hook: the card stays open meanwhile
    claude.registry_status = "busy"  # answered in the dialog
    assert asked.done() == {}
    _drain(bridge)
    assert "已在电脑上处理" in api.sent("edit_message")[-1]["text"]


def test_pc_answer_before_the_first_look_still_closes_the_card(setup) -> None:
    bridge, api, _state, _codex, claude, presence, now = setup
    presence.value = True
    claude.registry_status, claude.registry_changed_at = "busy", now[0] + 0.5  # answered at once
    asked = Asked(bridge)
    assert _wait_for(lambda: api.sent())
    presence.value = False  # leaving afterwards must not bring a reminder for it
    assert asked.done() == {}
    _drain(bridge)
    assert "已在电脑上处理" in api.sent("edit_message")[-1]["text"]
    assert not any("还在等待审批" in item["text"] for item in api.sent())


def test_a_session_that_never_shows_waiting_closes_the_card_after_a_while(setup) -> None:
    bridge, api, _state, _codex, claude, _presence, now = setup
    claude.registry_status, claude.registry_changed_at = "busy", now[0] - 100  # e.g. another hook decided
    asked = Asked(bridge)
    assert _wait_for(lambda: api.sent())
    time.sleep(0.1)
    assert asked.thread.is_alive()
    now[0] += bridge_module.PERMISSION_NO_DIALOG_S
    assert asked.done() == {}


def test_subagent_request_ignores_the_registry(setup) -> None:
    bridge, api, _state, _codex, claude, _presence, _now = setup
    claude.registry_status = "waiting"  # another dialog of the session
    asked = Asked(bridge, {"agent_id": "agent-1"})
    assert _wait_for(lambda: api.sent())
    time.sleep(0.05)
    claude.registry_status = "busy"
    time.sleep(0.1)
    assert asked.thread.is_alive()  # still waiting for Telegram: its own dialog has not opened yet
    _tap(bridge, f"perm:a:{next(iter(bridge.permissions))}", 1)
    assert _allowed(asked.done())


def test_print_mode_session_keeps_the_old_rules(setup) -> None:
    bridge, api, _state, _codex, claude, presence, now = setup
    claude.registry_kind = "sdk"  # e.g. a claude -p run the bridge did not start: no dialog races the hook
    presence.value = True
    assert bridge.claude_permission({"session_id": SESSION, "tool_name": "Bash", "tool_input": {}}) == {}
    assert api.sent() == []
    presence.value = False
    asked = Asked(bridge, hook_timeout_s=6 * 3600 + 120)
    assert _wait_for(lambda: api.sent())
    now[0] += bridge_module.PERMISSION_WAIT_S
    assert asked.done() == {}
    _drain(bridge)
    assert "等待超时" in api.sent("edit_message")[-1]["text"]


def test_locking_hands_open_cards_to_the_pc(setup) -> None:
    bridge, api, *_ = setup
    asked = Asked(bridge)
    assert _wait_for(lambda: api.sent())
    bridge.handle_update({"update_id": 1, "message": _message("/lock")})
    assert asked.done() == {}
    _drain(bridge)
    assert "写操作已锁定" in api.sent("edit_message")[-1]["text"]


def test_full_input_is_not_sent_during_a_security_lock(setup) -> None:
    bridge, api, state, *_ = setup
    bridge.redact_light = lambda text: text.replace("sk-secretsecretsecret", "‹已脱敏›")
    asked = Asked(bridge, {"tool_input": {"command": "curl -H sk-secretsecretsecret x"}})
    assert _wait_for(lambda: api.sent())
    nonce = next(iter(bridge.permissions))
    state.set_locked(True, "intruder")
    _tap(bridge, f"perm:v:{nonce}", 1)
    assert "安全原因" in api.sent("answer_callback")[-1]["text"] and not api.sent("send_document")
    _tap(bridge, f"perm:d:{nonce}", 2)
    assert asked.done()["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_cards_left_open_by_a_restart_are_marked_at_the_next_start(setup) -> None:
    bridge, api, state, *_ = setup
    bridge._remember_card(55, True)
    bridge._remember_card(56, True)
    bridge._remember_card(56, False)
    assert state.get("open_permission_cards") == [55]
    bridge._mark_stale_cards()
    _drain(bridge)
    edit = api.sent("edit_message")[-1]
    assert edit["message_id"] == 55 and "已失效" in edit["text"] and state.get("open_permission_cards") is None


def test_expired_grants_are_not_counted_and_allows_count_on_their_own_turn(setup) -> None:
    bridge, api, _state, _codex, _claude, _presence, now = setup
    bridge.allow_all[SESSION] = bridge_module.AllowAll(SESSION, session_key(CLAUDE, SESSION), OWNER, now[0], now[0] + 100)
    assert bridge._auto_allow(SESSION)
    now[0] += 10
    assert bridge._auto_allow(SESSION)
    bridge.on_event(TurnEvent(CLAUDE, SESSION, "重构脚本", r"C:\projects\code", "completed", final_text="ok",
                              completed_at=now[0] - 5))
    _drain(bridge)
    assert "自动允许了 1 次" in api.sent()[-1]["text"]
    assert bridge.status()["allow_all"] == 1
    now[0] += 100
    assert bridge.status()["allow_all"] == 0 and not bridge.allow_all
    assert bridge._revoke_allow_all(SESSION, "/ask") == 0


def test_a_loud_alert_under_a_grant_is_still_sent(setup) -> None:
    bridge, api, _state, _codex, _claude, _presence, now = setup
    bridge.allow_all[SESSION] = bridge_module.AllowAll(SESSION, session_key(CLAUDE, SESSION), OWNER, now[0], now[0] + 100)
    bridge.on_event(AttentionEvent(CLAUDE, SESSION, "重构脚本", r"C:\projects\code", "permission prompt"))
    _drain(bridge)
    assert api.sent() == []
    bridge.on_event(AttentionEvent(CLAUDE, SESSION, "重构脚本", r"C:\projects\code", "permission prompt", loud=True))
    _drain(bridge)
    assert "全部允许没有接住" in api.sent()[-1]["text"]


def test_waiting_alert_names_the_real_reason(setup) -> None:
    bridge, api, state, *_ = setup
    bridge.cfg.content_exclude = [r"C:\projects\secret"]
    bridge.on_event(AttentionEvent(CLAUDE, SESSION, "机密", r"C:\projects\secret\x", "permission prompt"))
    _drain(bridge)
    assert "content_exclude" in api.sent()[-1]["text"]
    state.set_locked(True, bridge_module.OWNER_LOCK)
    bridge.on_event(AttentionEvent(CLAUDE, SESSION, "重构脚本", r"C:\projects\code", "permission prompt"))
    _drain(bridge)
    assert "写操作已锁定" in api.sent()[-1]["text"]


def test_subagent_request_at_pc_goes_to_the_pc_at_once(setup) -> None:
    bridge, api, _state, _codex, _claude, presence, _now = setup
    presence.value = True
    assert bridge.claude_permission({"session_id": SESSION, "tool_name": "Bash", "tool_input": {},
                                     "agent_id": "agent-1"}) == {}
    assert api.sent() == []


def test_subagent_request_returns_to_the_pc_when_someone_is_back(setup) -> None:
    bridge, api, _state, _codex, _claude, presence, _now = setup
    asked = Asked(bridge, {"agent_id": "agent-1"})
    assert _wait_for(lambda: bridge.permissions and api.sent())
    assert api.sent()[-1]["silent"] is False
    presence.value = True
    assert asked.done() == {}
    _drain(bridge)
    assert "已交给电脑处理" in api.sent("edit_message")[-1]["text"]


def test_reminder_once_nobody_is_at_the_pc(setup) -> None:
    bridge, api, _state, _codex, claude, presence, _now = setup
    presence.value = True
    claude.registry_status = "waiting"
    asked = Asked(bridge)
    assert _wait_for(lambda: bridge.permissions and api.sent())
    card = api.sent()[-1]
    presence.value = False

    def reminded() -> bool:
        _drain(bridge)
        return any("还在等待审批" in item["text"] for item in api.sent())

    assert _wait_for(reminded)
    reminder = [item for item in api.sent() if "还在等待审批" in item["text"]]
    assert len(reminder) == 1 and reminder[0]["reply_to"] == card["message_id"] and reminder[0]["silent"] is False
    _tap(bridge, f"perm:a:{next(iter(bridge.permissions))}", 1)
    assert _allowed(asked.done())


def test_allow_all_takes_two_taps_then_allows_the_session_by_itself(setup) -> None:
    bridge, api, state, *_ = setup
    asked = Asked(bridge)
    assert _wait_for(lambda: bridge.permissions and api.sent())
    nonce = next(iter(bridge.permissions))
    _tap(bridge, f"perm:Y:{nonce}", 1)
    assert "按钮已失效" in api.sent("answer_callback")[-1]["text"]
    _tap(bridge, f"perm:A:{nonce}", 2)
    edit = api.sent("edit_message")[-1]
    assert edit["buttons"] == [[("⚠️ 确认全部允许", f"perm:Y:{nonce}"), ("↩️ 返回", f"perm:b:{nonce}")]]
    assert "之后这个会话的所有权限请求" in edit["text"] and asked.thread.is_alive()
    _tap(bridge, f"perm:b:{nonce}", 3)
    assert api.sent("edit_message")[-1]["buttons"][0][0][1] == f"perm:a:{nonce}"
    _tap(bridge, f"perm:A:{nonce}", 4)
    _tap(bridge, f"perm:Y:{nonce}", 5)
    assert _allowed(asked.done())
    _drain(bridge)
    notice = [item for item in api.sent() if "已开启全部允许" in item["text"]]
    assert notice and notice[-1]["buttons"] == [[("🔒 恢复逐条审批", f"aa:off:{SESSION}")]]
    # Later requests of the session are allowed without a card.
    count = len(api.sent())
    assert _allowed(bridge.claude_permission({"session_id": SESSION, "tool_name": "Write",
                                              "tool_input": {"file_path": "x"}}))
    assert len(api.sent()) == count
    bridge.on_event(TurnEvent(CLAUDE, SESSION, "重构脚本", r"C:\projects\code", "completed", final_text="ok"))
    _drain(bridge)
    assert "自动允许了 1 次" in api.sent()[-1]["text"]
    # /ask in the topic goes back to asking.
    state.set_topic(session_key(CLAUDE, SESSION), 502, "t")
    bridge.handle_update({"update_id": 9, "message": _message("/ask", thread_id=502)})
    assert "已恢复逐条审批" in api.sent()[-1]["text"] and not bridge.allow_all


def test_allow_all_settles_the_other_open_requests_of_the_session(setup) -> None:
    bridge, *_ = setup
    first, second = Asked(bridge), Asked(bridge, {"tool_name": "Write"})
    assert _wait_for(lambda: len(bridge.permissions) == 2)
    nonce = next(iter(bridge.permissions))
    _tap(bridge, f"perm:A:{nonce}", 1)
    _tap(bridge, f"perm:Y:{nonce}", 2)
    assert _allowed(first.done()) and _allowed(second.done())


def test_allow_all_is_off_while_locked_revoked_by_lock_and_expires(setup) -> None:
    bridge, api, state, _codex, _claude, _presence, now = setup
    bridge.allow_all[SESSION] = bridge_module.AllowAll(SESSION, session_key(CLAUDE, SESSION), OWNER, now[0], now[0] + 100)
    state.set_locked(True, bridge_module.OWNER_LOCK)
    assert bridge.claude_permission({"session_id": SESSION, "tool_name": "Bash", "tool_input": {}}) == {}
    state.set_locked(False, "")
    assert bridge._allow_all_for(SESSION) is not None
    bridge.handle_update({"update_id": 1, "message": _message("/lock")})
    assert "取消了 1 个会话的全部允许" in api.sent()[-1]["text"] and not bridge.allow_all
    state.set_locked(False, "")
    bridge.allow_all[SESSION] = bridge_module.AllowAll(SESSION, session_key(CLAUDE, SESSION), OWNER, now[0], now[0] + 100)
    now[0] += 101
    assert bridge._allow_all_for(SESSION) is None and not bridge.allow_all
    bridge.allow_all[SESSION] = bridge_module.AllowAll(SESSION, session_key(CLAUDE, SESSION), OWNER, now[0], now[0] + 100)
    bridge._lock_writes("intruder")
    assert not bridge.allow_all


def test_revoke_button(setup) -> None:
    bridge, api, _state, _codex, _claude, _presence, now = setup
    bridge.allow_all[SESSION] = bridge_module.AllowAll(SESSION, session_key(CLAUDE, SESSION), OWNER, now[0], now[0] + 100)
    _tap(bridge, f"aa:off:{SESSION}", 1)
    assert api.sent("answer_callback")[-1]["text"] == "已恢复逐条审批" and not bridge.allow_all
    assert "恢复逐条审批" in api.sent("edit_message")[-1]["text"]


def test_permission_withdrawn_when_claude_hangs_up(setup) -> None:
    bridge, api, *_ = setup
    gone = threading.Event()
    asked = Asked(bridge, gone=gone)
    assert _wait_for(lambda: bridge.permissions and api.sent())
    gone.set()
    assert asked.done() == {}
    _drain(bridge)
    assert "请求已撤回" in api.sent("edit_message")[-1]["text"]


def test_permission_waits_until_shortly_before_the_hook_times_out(setup) -> None:
    bridge, api, _state, _codex, _claude, _presence, now = setup
    assert Bridge._permission_wait_s(None) == bridge_module.PERMISSION_WAIT_S == 540
    assert Bridge._permission_wait_s(6 * 3600 + 120) == 6 * 3600
    assert Bridge._permission_wait_s(100) == 60 and Bridge._permission_wait_s(5) == 10
    asked = Asked(bridge, hook_timeout_s=100)
    assert _wait_for(lambda: bridge.permissions and api.sent())
    now[0] += 61
    assert asked.done() == {}
    _drain(bridge)
    assert "等待超时" in api.sent("edit_message")[-1]["text"]


def test_waiting_alert_is_folded_into_the_card(setup) -> None:
    bridge, api, *_ = setup
    asked = Asked(bridge)
    assert _wait_for(lambda: bridge.permissions and api.sent())
    count = len(api.sent())
    bridge.on_event(AttentionEvent(CLAUDE, SESSION, "重构脚本", r"C:\projects\code", "permission prompt"))
    _drain(bridge)
    assert len(api.sent()) == count
    bridge.on_event(AttentionEvent(CLAUDE, SESSION, "重构脚本", r"C:\projects\code", "permission prompt", loud=True))
    _drain(bridge)
    assert len(api.sent()) == count + 1 and "还在等待审批" in api.sent()[-1]["text"]
    _tap(bridge, f"perm:d:{next(iter(bridge.permissions))}", 1)
    assert asked.done()["hookSpecificOutput"]["decision"]["behavior"] == "deny"


def test_waiting_alert_without_a_card_says_why(setup) -> None:
    bridge, api, *_ = setup
    bridge.on_event(AttentionEvent(CLAUDE, SESSION, "重构脚本", r"C:\projects\code", "permission prompt"))
    _drain(bridge)
    assert "没有发到 Telegram" in api.sent()[-1]["text"]
    bridge.on_event(AttentionEvent(CLAUDE, SESSION, "重构脚本", r"C:\projects\code", "question"))
    _drain(bridge)
    assert "没有发到 Telegram" not in api.sent()[-1]["text"]


def test_hook_server_keeps_a_long_permission_request_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    import json
    import socket
    import struct
    import urllib.request

    from agent_telegram.tgbridge import hook_server

    monkeypatch.setattr(hook_server, "KEEPALIVE_S", 0.05)

    class Handler:
        def __init__(self) -> None:
            self.release = threading.Event()
            self.timeouts: list[float | None] = []
            self.gone: list[threading.Event] = []

        def claude_permission(self, payload: dict[str, Any], *, gone: threading.Event | None = None,
                              hook_timeout_s: float | None = None) -> dict[str, Any]:
            self.timeouts.append(hook_timeout_s)
            assert gone is not None
            self.gone.append(gone)
            self.release.wait(5)
            return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {"behavior": "allow"}}}

        def claude_event(self, payload: dict[str, Any]) -> None: ...

        def claude_pretooluse(self, payload: dict[str, Any]) -> dict[str, Any]:
            return {}

        def status(self) -> dict[str, Any]:
            return {}

    handler = Handler()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = hook_server.HookServer(port, "tok", handler)
    server.start()
    try:
        body = json.dumps({"session_id": SESSION}).encode()
        request = urllib.request.Request(f"http://127.0.0.1:{port}/claude/permission", data=body, headers={
            "Content-Type": "application/json", "X-TgBridge-Token": "tok", "X-TgBridge-Timeout": "21720"})
        threading.Timer(0.4, handler.release.set).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=5) as response:
            text = response.read().decode("utf-8")
        assert text.startswith(" ") and json.loads(text.strip())["hookSpecificOutput"]["decision"]["behavior"] == "allow"
        assert handler.timeouts == [21720.0]
        # Claude Code hanging up mid-wait reaches the handler as `gone`.
        handler.release.clear()
        with socket.create_connection(("127.0.0.1", port)) as client:
            client.sendall(b"POST /claude/permission HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                           b"X-TgBridge-Token: tok\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            assert client.recv(64).startswith(b"HTTP/1.1 200")
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        assert _wait_for(lambda: len(handler.gone) == 2 and handler.gone[1].is_set())
        handler.release.set()
    finally:
        server.stop()


def test_permission_denied_when_locked(setup) -> None:
    bridge, api, state, *_ = setup
    state.set_locked(True, "x")
    assert bridge.claude_permission({"session_id": SESSION, "tool_name": "Bash", "tool_input": {}}) == {}


def test_update_ids_are_processed_once(setup) -> None:
    bridge, api, state, codex, *_ = setup
    updates = [{"update_id": 10, "message": _message("/list")}]
    api.get_updates = lambda offset, **options: updates  # type: ignore[method-assign]
    assert bridge.poll_updates_once(timeout_s=0) is None
    assert bridge.poll_updates_once(timeout_s=0) is None
    assert len(api.sent()) == 1 and state.update_offset == 11


def test_topic_failure_falls_back_to_general(setup) -> None:
    bridge, api, *_ = setup
    api.fail_topics = True
    bridge.on_event(TurnEvent(CODEX, THREAD, "t", r"C:\x", "failed", error="boom"))
    _drain(bridge)
    assert api.sent()[-1]["thread_id"] is None


def test_hook_server_routes_and_guards(setup) -> None:
    import json
    import socket
    import urllib.error
    import urllib.request

    from agent_telegram.tgbridge.hook_server import TOKEN_HEADER, HookServer

    bridge, *_ = setup
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = HookServer(port, "tok", bridge)
    server.start()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        def post(path: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, dict[str, Any]]:
            request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json", **headers})
            try:
                with opener.open(request, timeout=5) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        assert post("/claude/pretooluse", {"session_id": SESSION}, {})[0] == 403
        assert post("/claude/pretooluse", {"session_id": SESSION}, {TOKEN_HEADER: "tok", "Origin": "https://evil"})[0] == 403
        assert post("/claude/pretooluse", {"session_id": SESSION}, {TOKEN_HEADER: "tok"}) == (200, {})
        bridge.stop_flags[SESSION] = bridge.clock()
        assert post("/claude/pretooluse", {"session_id": SESSION}, {TOKEN_HEADER: "tok"})[1]["continue"] is False
        assert post("/claude/event", {"event": "Stop", "session_id": SESSION}, {TOKEN_HEADER: "tok"}) == (200, {})
        assert bridge.hook_events
        with opener.open(f"http://127.0.0.1:{port}/status", timeout=5) as response:
            assert json.loads(response.read())["service"] == "tgbridge"
        # A second server cannot take the port.
        with pytest.raises(OSError):
            HookServer(port, "tok", bridge).start()
    finally:
        server.stop()


def test_log_redacts(tmp_path: Path) -> None:
    log = BridgeLog(tmp_path, lambda text: text.replace("TOKEN123", "‹已脱敏›"))
    log.line("value TOKEN123")
    log.audit("x", detail="TOKEN123")
    assert "TOKEN123" not in (tmp_path / "bridge.log").read_text(encoding="utf-8")
    assert "TOKEN123" not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


def test_module_constants() -> None:
    assert bridge_module.PERMISSION_WAIT_S < 580


def _confirm(bridge: Bridge, api: FakeAPI, *, user: int = OWNER, update_id: int = 90, choice: str = "ok") -> None:
    card = api.sent()[-1]
    nonce = card["buttons"][0][0][1].split(":", 1)[1]
    bridge.handle_update({"update_id": update_id, "callback_query": {
        "id": "cb", "data": f"{choice}:{nonce}", "from": {"id": user},
        "message": {"message_id": card["message_id"], "chat": {"id": CHAT}}}})
    _run_threads()


def test_confirmation_is_bound_to_the_asking_user(setup) -> None:
    bridge, api, state, codex, *_ = setup
    bridge.cfg.allowed_user_ids = [OWNER, 43]
    state.set_topic(session_key(CODEX, THREAD), 501, "t")
    bridge.handle_update({"update_id": 1, "message": _message("hello", thread_id=501)})
    _confirm(bridge, api, user=43)
    assert codex.sent == [] and bridge.pending  # still waiting for the owner
    card = api.sent()[-1]
    nonce = card["buttons"][0][0][1].split(":", 1)[1]
    bridge.handle_update({"update_id": 3, "callback_query": {
        "id": "cb", "data": f"ok:{nonce}", "from": {"id": OWNER}, "message": {"message_id": 999, "chat": {"id": CHAT}}}})
    _run_threads()
    assert codex.sent == []  # not on the original card


def test_one_unfinished_write_per_session(setup) -> None:
    bridge, api, state, *_ = setup
    state.set_topic(session_key(CLAUDE, SESSION), 502, "t")
    bridge.handle_update({"update_id": 1, "message": _message("一", thread_id=502)})
    bridge.handle_update({"update_id": 2, "message": _message("二", thread_id=502)})
    assert "还没处理完" in api.sent()[-1]["text"]


def test_permission_with_hidden_parts_needs_full_view(setup) -> None:
    bridge, api, *_ = setup

    class Light:
        def __call__(self, text: str) -> str:
            return text

        def light(self, text: str) -> str:
            return text.replace("sk-secretsecretsecret", "‹已脱敏›")

    bridge.redact = Light()
    bridge.redact_light = bridge.redact.light
    result: dict[str, Any] = {}
    thread = threading.Thread(target=lambda: result.update(bridge.claude_permission(
        {"session_id": SESSION, "tool_name": "Bash", "tool_input": {"command": "curl -H sk-secretsecretsecret x"}})))
    thread.start()
    assert _wait_for(lambda: bridge.permissions and api.sent())
    nonce = next(iter(bridge.permissions))
    card = api.sent()[-1]
    assert card["buttons"][0][0][1] == f"perm:v:{nonce}" and "sk-secret" not in card["text"]
    base = {"id": "cb", "from": {"id": OWNER}, "message": {"message_id": card["message_id"], "chat": {"id": CHAT}}}
    bridge.handle_update({"update_id": 1, "callback_query": {**base, "data": f"perm:a:{nonce}"}})
    assert "请先查看全文" in api.sent("answer_callback")[-1]["text"] and not result
    bridge.handle_update({"update_id": 2, "callback_query": {**base, "data": f"perm:v:{nonce}"}})
    assert api.sent("send_document")
    bridge.handle_update({"update_id": 3, "callback_query": {**base, "data": f"perm:a:{nonce}"}})
    thread.join(timeout=10)
    assert result["hookSpecificOutput"]["decision"]["behavior"] == "allow"


def test_permission_skipped_for_excluded_sessions(setup) -> None:
    bridge, api, *_ = setup
    bridge.cfg.content_exclude = [r"C:\projects\code"]
    assert bridge.claude_permission({"session_id": SESSION, "tool_name": "Write", "cwd": r"C:\other",
                                     "tool_input": {"content": "TOP-SECRET"}}) == {}
    assert api.sent() == []


def test_remote_permission_mode_is_capped() -> None:
    assert Bridge._remote_mode("auto", bypass_allowed=False) == "acceptEdits"
    assert Bridge._remote_mode("bypassPermissions", bypass_allowed=False) == "acceptEdits"
    assert Bridge._remote_mode("bypassPermissions", bypass_allowed=True) == "bypassPermissions"
    assert Bridge._remote_mode("plan", bypass_allowed=False) == "plan"
    assert Bridge._remote_mode("manual", bypass_allowed=False) == "default"


def test_headless_resume_refused_when_session_is_live(setup) -> None:
    bridge, *_ = setup
    with pytest.raises(RuntimeError, match="电脑上打开"):
        bridge._start_headless(SESSION, "继续", resume=True, cwd=None)
    assert not bridge._starting


def test_stop_flag_expires_after_the_grace(setup) -> None:
    bridge, _api, _state, _codex, _claude, _presence, now = setup
    bridge.stop_flags[SESSION] = now[0]
    assert bridge.claude_pretooluse({"session_id": SESSION})["continue"] is False
    now[0] += bridge_module.STOP_HIT_GRACE_S + 1
    assert bridge.claude_pretooluse({"session_id": SESSION}) == {}
    assert SESSION not in bridge.stop_flags


def test_topic_creation_pauses_only_on_permanent_errors(setup) -> None:
    bridge, api, _state, _codex, _claude, _presence, now = setup
    api.topic_network_error = True
    for _ in range(4):
        assert bridge._topic(session_key(CODEX, THREAD), "t", CODEX) is None
    api.topic_network_error = False
    assert bridge._topic(session_key(CODEX, THREAD), "t", CODEX) == 501
    api.fail_topics = True
    assert bridge._topic(session_key(CLAUDE, SESSION), "c", CLAUDE) is None
    api.fail_topics = False
    assert bridge._topic(session_key(CLAUDE, SESSION), "c", CLAUDE) is None  # paused
    now[0] += bridge_module.TOPIC_PAUSE_S + 1
    assert bridge._topic(session_key(CLAUDE, SESSION), "c", CLAUDE) == 502


def test_security_lock_pushes_titles_only(setup) -> None:
    bridge, api, state, *_ = setup
    state.set_locked(True, "非白名单用户 77 在群里发言")
    bridge.on_event(TurnEvent(CODEX, THREAD, "t", r"C:\x", "completed", final_text="机密结果"))
    _drain(bridge)
    push = api.sent()[-1]
    assert "机密结果" not in push["text"] and push["buttons"] is None
    state.set_locked(True, "owner /lock")
    bridge.on_event(TurnEvent(CODEX, THREAD, "t", r"C:\x", "completed", final_text="普通结果"))
    _drain(bridge)
    assert "普通结果" in api.sent()[-1]["text"]


def test_unlock_refused_while_extra_members(setup) -> None:
    bridge, api, state, *_ = setup
    state.set_locked(True, "x")
    api.members = 5
    bridge.handle_update({"update_id": 1, "message": _message("/unlock@pc1_bot")})
    _confirm(bridge, api)
    assert state.locked


def test_proxy_follows_clash(setup, monkeypatch: pytest.MonkeyPatch) -> None:
    bridge, api, *_ = setup
    made: list[str | None] = []
    bridge.api_factory = lambda proxy: made.append(proxy) or FakeAPI()
    bridge.proxy = None
    bridge.cfg.proxy = None
    monkeypatch.setattr(bridge_module, "resolve_proxy", lambda cfg: "http://127.0.0.1:7897")
    bridge._network_trouble()
    assert made == ["http://127.0.0.1:7897"] and bridge.proxy == "http://127.0.0.1:7897"
    assert api.sent("close")
    bridge._network_trouble()
    assert len(made) == 1


def test_revoked_token_stops_the_bridge(setup) -> None:
    bridge, api, *_ = setup

    def unauthorized(offset: Any, **options: Any) -> list[dict[str, Any]]:
        raise TelegramError("Unauthorized", code=401)

    api.get_updates = unauthorized  # type: ignore[method-assign]
    bridge.poll_updates_once(timeout_s=0)
    assert bridge.stopping.is_set() and bridge.exit_code == 3


def test_adopted_runs_are_released(setup, monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_telegram.tgbridge import headless

    bridge, _api, state, *_ = setup
    state.put("headless_runs", {SESSION: {"pid": 123, "created": 1, "log_path": "x.log", "started_at": 1.0,
                                          "cwd": "C:\\", "resume": True}})
    alive = [True]
    monkeypatch.setattr(headless, "run_alive", lambda run: alive[0])
    monkeypatch.setattr(headless, "parse_result", lambda path, **options: {"is_error": False})
    bridge._adopt_runs()
    assert SESSION in bridge.runs and bridge.deliveries
    alive[0] = False
    bridge._check_deliveries()
    assert SESSION not in bridge.runs and not bridge.deliveries


def test_cli_imports_and_status(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from agent_telegram.tgbridge import cli

    monkeypatch.setattr(cli, "running_status", lambda cfg, timeout=3.0: None)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "token：未设置" in out and "没有运行" in out


def test_switching_bots_resets_the_update_offset(tmp_path: Path) -> None:
    state = BridgeState(tmp_path / "state.json")
    assert state.adopt_bot(900) is False
    state.mark_update(5000)
    state.remember_message(77, "codex:x")
    assert state.adopt_bot(900) is False and state.update_offset == 5001
    assert state.adopt_bot(901) is True
    assert state.update_offset == 0 and not state.seen_update(5000) and state.key_for_message(77) is None
    assert BridgeState(tmp_path / "state.json").get("bot_id") == 901


def test_expected_bots_counts_machines(setup) -> None:
    bridge, api, state, *_ = setup
    api.members = 4  # the owner and three machine bots
    bridge._check_members()
    assert state.locked
    state.set_locked(False, "x")
    bridge.cfg.expected_bots = 3
    bridge._member_count_alerted = None
    bridge._check_members()
    assert not state.locked


def test_hook_server_ignores_clients_that_hang_up(setup) -> None:
    import socket
    import struct

    from agent_telegram.tgbridge.hook_server import HookServer

    bridge, *_ = setup
    logged: list[str] = []
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = HookServer(port, "tok", bridge, log=lambda text, level: logged.append(level))
    server.start()
    try:
        client = socket.create_connection(("127.0.0.1", port))
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("hh", 1, 0))  # close with a reset
        client.close()
        time.sleep(0.5)
    finally:
        server.stop()
    assert "ERROR" not in logged


# --- mid-turn messages ---------------------------------------------------------------------


def _said(texts: list[str], *, asks: bool = False, kind: str = "text", cwd: str = r"C:\projects\code") -> ProgressEvent:
    return ProgressEvent(CLAUDE, SESSION, "重构脚本", cwd, texts, kind, asks)


def test_progress_message_is_edited_flushed_and_renewed(setup) -> None:
    bridge, api, _state, _codex, _claude, presence, now = setup
    bridge.on_event(_said(["我先读代码 SECRET"]))
    _drain(bridge)
    first = api.sent()[-1]
    assert "进行中" in first["text"] and "我先读代码" in first["text"] and "SECRET" not in first["text"]
    assert first["silent"] is True and first["thread_id"] == 501
    bridge.on_event(_said(["改好了第一处"]))
    _drain(bridge)
    assert len(api.sent()) == 1 and api.sent("edit_message") == []  # within the edit interval: waits
    now[0] += bridge_module.PROGRESS_EDIT_MIN_S
    bridge._flush_progress()
    _drain(bridge)
    edit = api.sent("edit_message")[-1]
    assert edit["message_id"] == first["message_id"] and "我先读代码" in edit["text"] and "改好了第一处" in edit["text"]
    # Addressed to the user: a new message, loud while nobody is at the PC.
    bridge.on_event(_said(["请用 demo 账号登录 Figma"], asks=True, kind="narration"))
    _drain(bridge)
    asked = api.sent()[-1]
    assert "请用 demo 账号登录" in asked["text"] and asked["silent"] is False and asked["message_id"] != first["message_id"]
    # The turn's end flushes what waited and the next turn starts a new message.
    bridge.on_event(_said(["收尾中"]))
    bridge.on_event(TurnEvent(CLAUDE, SESSION, "重构脚本", r"C:\projects\code", "completed", final_text="完成"))
    _drain(bridge)
    assert any("收尾中" in call["text"] for call in api.sent("edit_message")) and not bridge.progress_cards
    assert "完成" in api.sent()[-1]["text"]
    bridge.on_event(_said(["下一轮开始"]))
    _drain(bridge)
    assert "下一轮开始" in api.sent()[-1]["text"]
    # A long gap also starts a new message (which notifies).
    now[0] += bridge.cfg.progress_new_message_s
    count = len(api.sent())
    bridge.on_event(_said(["十分钟后"]))
    _drain(bridge)
    assert len(api.sent()) == count + 1


def test_progress_question_is_loud_only_away_from_the_pc(setup) -> None:
    bridge, api, _state, _codex, _claude, presence, _now = setup
    presence.value = True
    bridge.on_event(_said(["放到哪个目录？"], asks=True, kind="question"))
    _drain(bridge)
    sent = api.sent()[-1]
    assert sent["text"].startswith("❓") and sent["silent"] is True


def test_progress_respects_mute_exclusion_locks_and_the_switch(setup) -> None:
    bridge, api, state, *_ = setup
    state.set_muted(session_key(CLAUDE, SESSION), True)
    bridge.on_event(_said(["静音中"]))
    _drain(bridge)
    state.set_muted(session_key(CLAUDE, SESSION), False)
    bridge.cfg.content_exclude = [r"C:\projects\secret"]
    bridge.on_event(_said(["机密"], cwd=r"C:\projects\secret\x"))
    _drain(bridge)
    state.set_locked(True, "intruder")
    bridge.on_event(_said(["锁定中"]))
    _drain(bridge)
    state.set_locked(False, "")
    bridge.cfg.progress_enabled = False
    bridge.on_event(_said(["关掉了"]))
    _drain(bridge)
    assert api.sent() == []


def test_progress_message_keeps_the_newest_texts(setup) -> None:
    bridge, api, _state, _codex, _claude, _presence, now = setup
    bridge.on_event(_said(["开头"]))
    _drain(bridge)
    for index in range(12):
        now[0] += bridge_module.PROGRESS_EDIT_MIN_S
        bridge.on_event(_said([f"第 {index} 段 " + "长" * 900]))
        _drain(bridge)
    text = api.sent("edit_message")[-1]["text"]
    assert "第 11 段" in text and "开头" not in text and "…" in text
    assert len(text.encode("utf-16-le")) // 2 <= bridge_module.PROGRESS_LIMIT
    card = next(iter(bridge.progress_cards.values()))
    assert len(card.texts) <= 4


def test_a_new_progress_message_first_sends_what_waited_on_the_old_one(setup) -> None:
    bridge, api, *_ = setup
    bridge.on_event(_said(["AAA"]))
    bridge.on_event(_said(["BBB"]))  # waits for the edit interval
    bridge.on_event(_said(["CCC 请确认？"], asks=True))
    _drain(bridge)
    assert any("BBB" in call["text"] for call in api.sent("edit_message"))
    assert "CCC" in api.sent()[-1]["text"]


def test_waiting_progress_text_is_not_sent_after_a_lock_mute_or_hidden_turn(setup) -> None:
    bridge, api, state, _codex, _claude, _presence, now = setup
    bridge.on_event(_said(["AAA"]))
    bridge.on_event(_said(["WORKDETAIL"]))
    _drain(bridge)
    bridge._lock_writes("intruder")
    now[0] += bridge_module.PROGRESS_EDIT_MIN_S
    bridge._flush_progress()
    _drain(bridge)
    assert not any("WORKDETAIL" in call["text"] for call in api.sent("edit_message"))
    state.set_locked(False, "")
    bridge.on_event(_said(["AAA2"]))
    bridge.on_event(_said(["MUTEDDETAIL"]))
    _drain(bridge)
    state.set_muted(session_key(CLAUDE, SESSION), True)
    bridge.on_event(TurnEvent(CLAUDE, SESSION, "重构脚本", r"C:\projects\code", "completed", final_text="done"))
    _drain(bridge)
    assert not any("MUTEDDETAIL" in call["text"] for call in api.sent("edit_message")) and not bridge.progress_cards
    state.set_muted(session_key(CLAUDE, SESSION), False)
    bridge.cfg.content_exclude = [r"C:\projects\secret"]
    bridge.on_event(_said(["AAA3"]))
    bridge.on_event(_said(["HIDDENDETAIL"]))
    bridge.on_event(TurnEvent(CLAUDE, SESSION, "机密", r"C:\projects\secret\x", "completed", final_text="done"))
    _drain(bridge)
    assert not any("HIDDENDETAIL" in call["text"] for call in api.sent("edit_message"))


def test_one_queued_flush_per_progress_message(setup) -> None:
    bridge, api, _state, _codex, _claude, _presence, now = setup
    bridge.on_event(_said(["AAA"]))
    bridge.on_event(_said(["BBB"]))
    _drain(bridge)
    now[0] += bridge_module.PROGRESS_EDIT_MIN_S
    for _ in range(5):
        bridge._flush_progress()
    assert len(bridge.outbox) == 1
    _drain(bridge)
    assert len(api.sent("edit_message")) == 1


def test_a_failed_progress_edit_is_tried_again(setup, monkeypatch) -> None:
    bridge, api, _state, _codex, _claude, _presence, now = setup
    bridge.on_event(_said(["AAA"]))
    bridge.on_event(_said(["BBB"]))
    _drain(bridge)
    original = api.edit_message
    failures = [TelegramError("network down", code=None)]

    def flaky(*args: Any, **options: Any) -> Any:
        if failures:
            raise failures.pop()
        return original(*args, **options)

    monkeypatch.setattr(api, "edit_message", flaky)
    now[0] += bridge_module.PROGRESS_EDIT_MIN_S
    bridge._flush_progress()
    _drain(bridge)
    assert api.sent("edit_message") == []
    now[0] += bridge_module.PROGRESS_EDIT_MIN_S
    bridge._flush_progress()
    _drain(bridge)
    assert "BBB" in api.sent("edit_message")[-1]["text"]


# --- quiet transient network problems ---------------------------------------------------------


def test_log_trouble_is_quiet_until_it_lasts_and_reports_the_recovery(tmp_path: Path, monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: clock[0])
    log = BridgeLog(tmp_path)
    log.trouble("net", "连不上")
    clock[0] += 60
    log.trouble("net", "连不上")
    log.recovered("net", "恢复了")
    assert not (tmp_path / "bridge.log").exists()  # a one-minute blip leaves no trace
    log.trouble("net", "连不上")
    clock[0] += 130
    log.trouble("net", "连不上")
    clock[0] += 30
    log.trouble("net", "连不上")  # logged once per hour at most
    clock[0] += 10
    log.recovered("net", "恢复了")
    text = (tmp_path / "bridge.log").read_text(encoding="utf-8")
    assert text.count("连不上（已持续 2 分钟，仍在重试）") == 1 and "恢复了（中断了约 3 分钟）" in text


def test_dropped_long_polls_stay_out_of_the_log(setup) -> None:
    bridge, api, *_ = setup
    failures = [TelegramError("Server disconnected without sending a response.", code=None),
                TelegramError("Bad Gateway", code=502)]

    def get_updates(offset: Any, **options: Any) -> list[dict[str, Any]]:
        if failures:
            raise failures.pop()
        return []

    api.get_updates = get_updates  # type: ignore[method-assign]
    assert bridge.poll_updates_once(timeout_s=0) == 0.0
    assert bridge.poll_updates_once(timeout_s=0) == 0.0
    assert bridge.poll_updates_once(timeout_s=0) is None
    log = bridge.log.text_path
    assert not log.exists() or "getUpdates" not in log.read_text(encoding="utf-8")
    # A real refusal is still logged at once.
    api.get_updates = lambda offset, **options: (_ for _ in ()).throw(TelegramError("Bad Request", code=400))  # type: ignore[method-assign]
    bridge.poll_updates_once(timeout_s=0)
    assert "getUpdates 失败" in log.read_text(encoding="utf-8")


# --- question cards ------------------------------------------------------------------------------

ASK_INPUT = {"questions": [
    {"question": "用哪种方案？", "header": "方案", "multiSelect": False,
     "options": [{"label": "A 方案", "description": "改动小"}, {"label": "B 方案", "description": "更彻底"}]},
    {"question": "哪些模块要改？", "header": "模块", "multiSelect": True,
     "options": [{"label": "解析", "description": ""}, {"label": "推送", "description": ""},
                 {"label": "测试", "description": ""}]},
]}
PLAN_INPUT = {"plan": "# 计划\n1. 改解析\n2. 加测试", "planFilePath": "C:\\proj\\.claude\\plans\\p.md"}
CODEX_QUESTIONS = [Question("scope", "先做哪一部分？", "范围", [("P1 优先（推荐）", "先打基础"), ("并行", "一起做")]),
                   Question("data", "数据从哪来？", "数据", [("新采集", "")])]


def _question_card(bridge: Bridge) -> Any:
    assert _wait_for(lambda: bridge.questions)
    card = next(iter(bridge.questions.values()))
    assert _wait_for(lambda: card.message_id is not None)
    return card


def _tap_q(bridge: Bridge, card: Any, op: str, update_id: int, *, version: int | None = None) -> str:
    """Tap the card's button for ``op`` as the card shows it now (or as it showed at ``version``)."""

    data = f"q:{op}:{card.nonce}" if op in ("v", "l") else \
        f"q:{op}.{card.version if version is None else version}:{card.nonce}"
    bridge.handle_update({"update_id": update_id, "callback_query": {
        "id": f"cb{update_id}", "data": data, "from": {"id": OWNER},
        "message": {"message_id": card.message_id, "chat": {"id": CHAT}}}})
    return bridge.api.sent("answer_callback")[-1]["text"]


def _reply_to(card: Any, text: str, update_id: int) -> dict[str, Any]:
    return {"update_id": update_id,
            "message": _message(text, reply_to_message={"message_id": card.message_id, "from": {"id": BOT_ID}})}


def _codex_question(request_id: str = "call_a", **fields: Any) -> QuestionEvent:
    fields.setdefault("questions", list(CODEX_QUESTIONS))
    return QuestionEvent(CODEX, THREAD, "论文审稿", r"C:\proj\paper", request_id, "t1", **fields)


def test_claude_question_card_answers_through_updated_input(setup) -> None:
    bridge, api, *_ = setup
    asked = Asked(bridge, {"tool_name": "AskUserQuestion", "tool_input": ASK_INPUT})
    card = _question_card(bridge)
    sent = api.sent()[-1]
    assert "用哪种方案？" in sent["text"] and "A. A 方案 — 改动小" in sent["text"] and sent["silent"] is False
    assert [row[0][1] for row in sent["buttons"][:2]] == [f"q:o0.0:{card.nonce}", f"q:o1.0:{card.nonce}"]
    _tap_q(bridge, card, "o1", 1)  # B 方案, on to the multi-select question
    for update_id, choice in enumerate(("o0", "o2", "o2", "o2"), 2):  # 解析, 测试, untick 测试, tick it again
        _tap_q(bridge, card, choice, update_id)
    assert "☑️ C. 测试" in [row[0][0] for row in api.sent("edit_message")[-1]["buttons"][:3]]
    _tap_q(bridge, card, "n", 6)  # 选好了: every question visited
    assert card.review and api.sent("edit_message")[-1]["buttons"][0][0][1] == f"q:s.{card.version}:{card.nonce}"
    _tap_q(bridge, card, "s", 7)
    decision = asked.done()["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "allow" and decision["updatedInput"]["questions"] == ASK_INPUT["questions"]
    assert decision["updatedInput"]["answers"] == {"用哪种方案？": "B 方案", "哪些模块要改？": "解析, 测试"}
    _drain(bridge)
    final = api.sent("edit_message")[-1]
    assert "已在 Telegram 上回答" in final["text"] and final["buttons"] is None and not bridge.questions


def test_a_reply_to_the_question_card_is_a_typed_answer(setup) -> None:
    bridge, api, *_ = setup
    asked = Asked(bridge, {"tool_name": "AskUserQuestion", "tool_input": {"questions": ASK_INPUT["questions"][:1]}})
    card = _question_card(bridge)
    bridge.handle_update(_reply_to(card, "都不要，先调研", 1))
    assert card.review and card.answers == [["都不要，先调研"]] and card.typed == [True]
    assert "已填进卡片" in api.sent()[-1]["text"] and "✍️ 已填：都不要，先调研" in api.sent("edit_message")[-1]["text"]
    _tap_q(bridge, card, "s", 2)
    assert asked.done()["hookSpecificOutput"]["decision"]["updatedInput"]["answers"] == {"用哪种方案？": "都不要，先调研"}


def test_plan_card_approves_without_sending_the_plan_back(setup) -> None:
    bridge, api, *_ = setup
    asked = Asked(bridge, {"tool_name": "ExitPlanMode", "tool_input": {**PLAN_INPUT, "allowedPrompts": []}})
    card = _question_card(bridge)
    sent = api.sent()[-1]
    assert "1. 改解析" in sent["text"] and sent["buttons"][0][0] == ("✅ 批准计划", f"q:a.0:{card.nonce}")
    _tap_q(bridge, card, "a", 1)
    assert asked.done()["hookSpecificOutput"]["decision"] == {"behavior": "allow",
                                                               "updatedInput": {"allowedPrompts": []}}
    _drain(bridge)
    assert "已批准计划" in api.sent("edit_message")[-1]["text"]


def test_plan_card_keeps_planning_with_the_owners_change_requests(setup) -> None:
    bridge, api, *_ = setup
    asked = Asked(bridge, {"tool_name": "ExitPlanMode", "tool_input": PLAN_INPUT})
    card = _question_card(bridge)
    bridge.handle_update(_reply_to(card, "第 2 步先别做", 1))
    assert card.feedback == "第 2 步先别做"
    assert api.sent("edit_message")[-1]["buttons"][0][1][0] == "✏️ 带着意见继续规划"
    _tap_q(bridge, card, "k", 2)
    decision = asked.done()["hookSpecificOutput"]["decision"]
    assert decision["behavior"] == "deny" and "第 2 步先别做" in decision["message"]


def test_question_answered_on_the_pc_closes_the_card(setup) -> None:
    bridge, api, _state, _codex, claude, presence, _now = setup
    presence.value = True
    claude.registry_status = "waiting"
    asked = Asked(bridge, {"tool_name": "AskUserQuestion", "tool_input": ASK_INPUT})
    _question_card(bridge)
    assert api.sent()[-1]["silent"] is True
    time.sleep(0.1)  # the card watched the dialog open
    claude.registry_status = "busy"  # answered in Claude's own dialog
    assert asked.done() == {}
    _drain(bridge)
    assert "已在电脑上处理" in api.sent("edit_message")[-1]["text"]


def test_questions_that_cannot_go_to_telegram_stay_on_the_pc(setup) -> None:
    bridge, api, _state, _codex, _claude, presence, _now = setup
    presence.value = True
    assert bridge.claude_permission({"session_id": SESSION, "tool_name": "AskUserQuestion", "tool_input": ASK_INPUT,
                                     "agent_id": "agent-1"}) == {}  # a sub-agent's dialog may wait for the hook
    presence.value = False
    bridge.cfg.content_exclude = [r"C:\projects"]
    assert bridge.claude_permission({"session_id": SESSION, "tool_name": "ExitPlanMode", "tool_input": PLAN_INPUT,
                                     "cwd": r"C:\projects\code"}) == {}
    assert api.sent() == []


def test_locking_and_the_pc_button_hand_question_cards_to_the_pc(setup) -> None:
    bridge, api, *_ = setup
    asked = Asked(bridge, {"tool_name": "AskUserQuestion", "tool_input": ASK_INPUT})
    card = _question_card(bridge)
    _tap_q(bridge, card, "o0", 1)
    bridge.handle_update({"update_id": 2, "message": _message("/lock")})
    assert asked.done() == {}
    assert bridge.claude_permission({"session_id": SESSION, "tool_name": "ExitPlanMode",
                                     "tool_input": PLAN_INPUT}) == {}  # locked: no card at all


def test_allow_all_leaves_open_questions_waiting(setup) -> None:
    bridge, api, *_ = setup
    question = Asked(bridge, {"tool_name": "AskUserQuestion", "tool_input": ASK_INPUT})
    card = _question_card(bridge)
    permission = Asked(bridge)
    assert _wait_for(lambda: len(bridge.permissions) == 2)
    nonce = next(key for key, wait in bridge.permissions.items() if wait.tool == "Bash")
    _tap(bridge, f"perm:A:{nonce}", 1)
    _tap(bridge, f"perm:Y:{nonce}", 2)
    assert _allowed(permission.done())
    assert question.thread.is_alive()  # a question still needs its answers
    _tap(bridge, f"perm:a:{card.nonce}", 3)  # a permission button cannot answer a question
    assert question.thread.is_alive()
    _tap_q(bridge, card, "l", 4)
    assert question.done() == {}


def test_question_text_gives_way_to_the_card(setup) -> None:
    bridge, api, *_ = setup
    asked = Asked(bridge, {"tool_name": "AskUserQuestion", "tool_input": ASK_INPUT})
    card = _question_card(bridge)
    text = "用哪种方案？\n「A 方案」 / 「B 方案」\n\n（请在电脑上回答）"
    digest = progress.interactive_digest("AskUserQuestion", ASK_INPUT)
    bridge.on_event(ProgressEvent(CLAUDE, SESSION, "重构脚本", r"C:\projects\code", ["先读了代码", text], kind="question",
                                  asks_user=True, items=[("text", "先读了代码", ""), ("question", text, digest)]))
    bridge.on_event(AttentionEvent(CLAUDE, SESSION, "重构脚本", r"C:\projects\code", "input needed"))
    _drain(bridge)
    pushes = [item["text"] for item in api.sent()[1:]]
    assert len(pushes) == 1 and "先读了代码" in pushes[0] and "请在电脑上回答" not in pushes[0]
    assert api.sent()[-1]["silent"] is True  # no longer a question: the card was the loud one
    _tap_q(bridge, card, "l", 1)
    assert asked.done() == {}


def test_codex_question_card_answers_through_codex(setup) -> None:
    bridge, api, _state, codex, *_ = setup
    bridge.on_event(_codex_question())
    _drain(bridge)
    card = next(iter(bridge.questions.values()))
    sent = api.sent()[-1]
    assert "先做哪一部分？" in sent["text"] and "A. P1 优先（推荐） — 先打基础" in sent["text"] and sent["silent"] is False
    assert not any("交给电脑" in button[0] for row in sent["buttons"] for button in row)
    _tap_q(bridge, card, "o1", 1)
    bridge.handle_update(_reply_to(card, "自己采", 2))
    _tap_q(bridge, card, "s", 3)
    _tap_q(bridge, card, "s", 4)  # a second tap sends nothing more
    _run_threads()
    assert codex.answered == [(THREAD, "call_a", {"scope": ["并行"], "data": ["自己采"]})]
    assert "已发给 Codex" in api.sent("edit_message")[-1]["text"]
    bridge.on_event(_codex_question(closed=True, questions=[], answer="范围: 并行"))
    _drain(bridge)
    final = api.sent("edit_message")[-1]
    assert "Codex 收到的答案：\n范围: 并行" in final["text"] and final["buttons"] is None and not bridge.questions


def test_codex_question_answered_on_the_pc(setup) -> None:
    bridge, api, *_ = setup
    bridge.on_event(_codex_question())
    bridge.on_event(_codex_question())  # reported again (e.g. a repeated event): one card
    bridge.on_event(_codex_question(closed=True, questions=[], answer="范围: P1 优先（推荐）"))
    _drain(bridge)
    assert len([item for item in api.sent() if "先做哪一部分" in item["text"]]) == 1
    final = api.sent("edit_message")[-1]
    assert "已在电脑上回答" in final["text"] and "P1 优先" in final["text"] and final["buttons"] is None


def test_codex_answer_failure_offers_another_try(setup) -> None:
    bridge, api, _state, codex, *_ = setup
    codex.answer_error = ValueError("Codex Desktop 里没有窗口负责这个对话")
    bridge.on_event(_codex_question(questions=CODEX_QUESTIONS[:1]))
    _drain(bridge)
    card = next(iter(bridge.questions.values()))
    _tap_q(bridge, card, "o0", 1)
    _tap_q(bridge, card, "s", 2)
    _run_threads()
    edit = api.sent("edit_message")[-1]
    assert "没能发给 Codex" in edit["text"] and edit["buttons"][0][0][1] == f"q:s.{card.version}:{card.nonce}"
    codex.answer_error = None
    _tap_q(bridge, card, "s", 3)
    _run_threads()
    assert codex.answered == [(THREAD, "call_a", {"scope": ["P1 优先（推荐）"]})] and card.state == "sent"


def test_an_answer_after_a_failed_send_is_not_credited_to_the_pc(setup) -> None:
    bridge, api, _state, codex, *_ = setup
    codex.answer_error = ValueError("Codex Desktop 没有及时响应")  # it may have arrived all the same
    bridge.on_event(_codex_question(questions=CODEX_QUESTIONS[:1]))
    _drain(bridge)
    card = next(iter(bridge.questions.values()))
    _tap_q(bridge, card, "o0", 1)
    _tap_q(bridge, card, "s", 2)
    _run_threads()
    bridge.on_event(_codex_question(closed=True, questions=[], answer="范围: P1 优先（推荐）"))
    _drain(bridge)
    final = api.sent("edit_message")[-1]["text"]
    assert "Codex 收到的答案：\n范围: P1 优先（推荐）" in final and "已在电脑上回答" not in final


def test_codex_questions_that_cannot_be_answered_here_get_a_notice(setup) -> None:
    bridge, api, state, *_ = setup
    bridge.on_event(_codex_question(questions=[], excluded=True))
    _drain(bridge)
    assert "content_exclude" in api.sent()[-1]["text"] and not api.sent()[-1].get("buttons")
    state.set_locked(True, bridge_module.OWNER_LOCK)
    bridge.on_event(_codex_question("call_b"))
    _drain(bridge)
    assert "写操作已锁定" in api.sent()[-1]["text"] and not bridge.questions


def test_locking_closes_codex_question_cards(setup) -> None:
    bridge, api, *_ = setup
    bridge.on_event(_codex_question())
    _drain(bridge)
    card = next(iter(bridge.questions.values()))
    bridge.handle_update({"update_id": 1, "message": _message("/lock")})
    _drain(bridge)
    assert not bridge.questions and "写操作已锁定" in api.sent("edit_message")[-1]["text"]
    _tap_q(bridge, card, "o0", 2)
    assert "已经处理过" in api.sent("answer_callback")[-1]["text"]


def test_codex_question_reminder_once_nobody_is_at_the_pc(setup) -> None:
    bridge, api, _state, _codex, _claude, presence, _now = setup
    presence.value = True
    bridge.on_event(_codex_question())
    _drain(bridge)
    assert api.sent()[-1]["silent"] is True
    bridge._remind_questions()
    _drain(bridge)
    count = len(api.sent())
    presence.value = False
    for _ in range(2):
        bridge._remind_questions()
        _drain(bridge)
    assert len(api.sent()) == count + 1 and "还在等你回答问题" in api.sent()[-1]["text"]


def test_plain_text_while_a_question_waits_says_how_to_answer(setup) -> None:
    bridge, api, state, *_ = setup
    state.set_topic(session_key(CODEX, THREAD), 501, "t")
    bridge.on_event(_codex_question())
    _drain(bridge)
    bridge.handle_update({"update_id": 1, "message": _message("继续", thread_id=501)})
    assert "正在等你回答问题" in api.sent()[-1]["text"]


def test_a_codex_plan_is_implemented_after_a_confirmation(setup) -> None:
    bridge, api, _state, codex, *_ = setup
    bridge.on_event(TurnEvent(CODEX, THREAD, "论文审稿", r"C:\projects\paper", "completed", final_text="# 计划",
                              turn_id="t1", plan=True))
    _drain(bridge)
    push = api.sent()[-1]
    assert "实施此计划" in push["text"] and push["buttons"][-1][0][1].startswith("pl:")
    bridge.handle_update({"update_id": 1, "callback_query": {
        "id": "cb1", "data": push["buttons"][-1][0][1], "from": {"id": OWNER},
        "message": {"message_id": push["message_id"], "chat": {"id": CHAT}, "message_thread_id": push["thread_id"],
                    "is_topic_message": True}}})
    card = api.sent()[-1]
    assert "实施这个计划" in card["text"] and codex.implemented == []
    nonce = card["buttons"][0][0][1].split(":", 1)[1]
    bridge.handle_update({"update_id": 2, "callback_query": {
        "id": "cb2", "data": f"ok:{nonce}", "from": {"id": OWNER},
        "message": {"message_id": card["message_id"], "chat": {"id": CHAT}}}})
    _run_threads()
    assert codex.implemented and codex.implemented[0][:2] == (THREAD, "t1")
    assert codex.implemented[0][2].startswith("[tg:") and [item.kind for item in bridge.deliveries] == ["codex_job"]
    # A turn without a plan offers nothing.
    bridge.on_event(TurnEvent(CODEX, THREAD, "论文审稿", r"C:\projects\paper", "completed", final_text="好", turn_id="t2"))
    _drain(bridge)
    assert not any(button[1].startswith("pl:") for row in api.sent()[-1]["buttons"] for button in row)


def test_a_plan_without_its_text_is_read_from_the_plan_file(setup, tmp_path) -> None:
    bridge, api, *_ = setup
    plans = tmp_path / "plans"
    plans.mkdir()
    (plans / "p.md").write_text("# 计划\n1. 从文件读", encoding="utf-8")
    asked = Asked(bridge, {"tool_name": "ExitPlanMode", "tool_input": {"planFilePath": str(plans / "p.md")}})
    card = _question_card(bridge)
    assert "1. 从文件读" in api.sent()[-1]["text"]
    _tap_q(bridge, card, "a", 1)
    assert asked.done()["hookSpecificOutput"]["decision"]["updatedInput"] == {}
    (tmp_path / "other.md").write_text("x", encoding="utf-8")
    assert bridge._plan_file(str(tmp_path / "other.md")) == "" and bridge._plan_file(None) == ""


def test_cards_sent_before_the_stale_check_stay_open(setup) -> None:
    bridge, api, state, *_ = setup
    bridge._remember_card(55, True)
    bridge.on_event(_codex_question())  # the sources poll before start() looks at old cards
    _drain(bridge)
    card = next(iter(bridge.questions.values()))
    bridge._mark_stale_cards()
    _drain(bridge)
    assert [edit["message_id"] for edit in api.sent("edit_message")] == [55]
    assert state.get("open_permission_cards") == [card.message_id]


def test_a_tap_on_buttons_the_card_no_longer_shows_changes_nothing(setup) -> None:
    bridge, api, *_ = setup
    bridge.on_event(_codex_question())
    _drain(bridge)
    card = next(iter(bridge.questions.values()))
    _tap_q(bridge, card, "o0", 1)  # question 1: P1, on to question 2
    assert _tap_q(bridge, card, "o1", 2, version=0) == "按钮已过期：请点卡片上现在的按钮"  # a second tap on old buttons
    assert card.answers == [["P1 优先（推荐）"], []] and card.index == 1


def test_an_answer_that_left_the_card_stays_as_it_was_sent(setup) -> None:
    bridge, api, *_ = setup
    asked = Asked(bridge, {"tool_name": "AskUserQuestion", "tool_input": {"questions": ASK_INPUT["questions"][:1]}})
    card = _question_card(bridge)
    _tap_q(bridge, card, "o0", 1)
    version = card.version
    _tap_q(bridge, card, "s", 2)
    assert "已经处理过" in _tap_q(bridge, card, "r", 3, version=version)
    bridge.handle_update(_reply_to(card, "改主意了", 4))
    assert asked.done()["hookSpecificOutput"]["decision"]["updatedInput"]["answers"] == {"用哪种方案？": "A 方案"}


def test_a_long_plan_is_approved_only_after_reading_it_all(setup) -> None:
    bridge, api, *_ = setup
    plan = "# 计划\n" + "".join(f"{step}. 修改第 {step} 个模块，并补上测试和文档\n" for step in range(1, 400))
    asked = Asked(bridge, {"tool_name": "ExitPlanMode", "tool_input": {"plan": plan}})
    card = _question_card(bridge)
    sent = api.sent()[-1]
    labels = [button[0] for row in sent["buttons"] for button in row]
    assert "✅ 批准计划" not in labels and "📄 查看全文" in labels and "查看全文" in sent["text"]
    assert "请先点 [📄 查看全文]" in _tap_q(bridge, card, "a", 1)
    assert _tap_q(bridge, card, "v", 2) == "已发送全文"
    document = api.sent("send_document")[-1]
    assert document["filename"] == "plan.md" and "399. 修改第 399 个模块" in document["data"].decode("utf-8")
    assert api.sent("edit_message")[-1]["buttons"][0][0][0] == "✅ 批准计划"
    _tap_q(bridge, card, "a", 3)
    assert asked.done()["hookSpecificOutput"]["decision"] == {"behavior": "allow", "updatedInput": {}}


def test_a_plan_with_nothing_to_show_stays_on_the_pc(setup) -> None:
    bridge, api, *_ = setup
    assert bridge.claude_permission({"session_id": SESSION, "tool_name": "ExitPlanMode",
                                     "tool_input": {"plan": "  "}}) == {}
    assert api.sent() == [] and not bridge.questions and not bridge.permissions


def test_question_cards_hide_what_pushes_hide(setup) -> None:
    bridge, api, *_ = setup
    bridge.redact_light = lambda text: text  # approval cards' light redaction must not apply here
    bridge.on_event(_codex_question(questions=[Question("key", "用 SECRET 吗？", "", [("用 SECRET", "")])]))
    _drain(bridge)
    card = next(iter(bridge.questions.values()))
    sent = api.sent()[-1]
    assert "SECRET" not in sent["text"] and "‹已脱敏›" in sent["text"]
    assert all("SECRET" not in button[0] for row in sent["buttons"] for button in row)
    bridge._question_full(card)
    assert b"SECRET" not in api.sent("send_document")[-1]["data"]


def test_a_closed_card_shows_only_what_was_sent(setup) -> None:
    bridge, api, *_ = setup
    bridge.on_event(_codex_question())
    _drain(bridge)
    card = next(iter(bridge.questions.values()))
    _tap_q(bridge, card, "o1", 1)  # picked on Telegram, never sent
    bridge.on_event(_codex_question(closed=True, questions=[], answer="范围: P1 优先（推荐）"))
    _drain(bridge)
    final = api.sent("edit_message")[-1]["text"]
    assert "💻 已在电脑上回答：\n范围: P1 优先（推荐）" in final and "已选" not in final


def test_a_reply_to_a_closed_card_is_not_sent_to_the_session(setup) -> None:
    bridge, api, state, codex, *_ = setup
    state.set_topic(session_key(CODEX, THREAD), 501, "t")
    bridge.on_event(_codex_question())
    _drain(bridge)
    card = next(iter(bridge.questions.values()))
    bridge.on_event(_codex_question(closed=True, questions=[]))
    _drain(bridge)
    update = _reply_to(card, "并行", 1)
    update["message"].update(message_thread_id=501, is_topic_message=True)
    bridge.handle_update(update)
    assert "卡片已经关闭了" in api.sent()[-1]["text"] and not bridge.pending and codex.sent == []


def test_waiting_alerts_fold_only_into_cards_of_their_kind(setup) -> None:
    bridge, api, *_ = setup
    question = Asked(bridge, {"tool_name": "AskUserQuestion", "tool_input": ASK_INPUT})
    card = _question_card(bridge)
    count = len(api.sent())
    bridge.on_event(AttentionEvent(CLAUDE, SESSION, "重构脚本", r"C:\proj\code", "permission prompt", loud=True))
    _drain(bridge)
    alert = api.sent()[-1]
    assert len(api.sent()) == count + 1 and "等待处理" in alert["text"] and "没有发到 Telegram" in alert["text"]
    bridge.on_event(AttentionEvent(CLAUDE, SESSION, "重构脚本", r"C:\proj\code", "input needed", loud=True))
    _drain(bridge)
    nudge = api.sent()[-1]
    assert "还在等你回答问题" in nudge["text"] and nudge["reply_to"] == card.message_id
    _tap_q(bridge, card, "l", 1)
    assert question.done() == {}


def test_question_text_stays_when_no_card_goes_out(setup) -> None:
    bridge, api, _state, _codex, claude, *_ = setup
    bridge.cfg.content_exclude = [r"C:\proj\secret"]
    assert bridge.claude_permission({"session_id": SESSION, "tool_name": "AskUserQuestion", "tool_input": ASK_INPUT,
                                     "cwd": r"C:\proj\secret"}) == {}  # no card for an excluded folder
    bridge.cfg.content_exclude = []
    digest = progress.interactive_digest("AskUserQuestion", ASK_INPUT)
    text = "用哪种方案？（请在电脑上回答）"
    bridge.on_event(ProgressEvent(CLAUDE, SESSION, "重构脚本", r"C:\proj\code", [text], kind="question", asks_user=True,
                                  items=[("question", text, digest)]))
    _drain(bridge)
    assert "请在电脑上回答" in api.sent()[-1]["text"] and api.sent()[-1]["silent"] is False
    # A session whose hooks never reported gets no card either: its text goes out without waiting.
    claude.hooked_sessions = set()
    other = progress.interactive_digest("AskUserQuestion", {"questions": [{"question": "另一个？"}]})
    started = time.monotonic()
    bridge.on_event(ProgressEvent(CLAUDE, SESSION, "重构脚本", r"C:\proj\code", ["另一个？"], kind="question",
                                  asks_user=True, items=[("question", "另一个？", other)]))
    _drain(bridge)
    assert "另一个？" in api.sent()[-1]["text"] and time.monotonic() - started < 0.4


def test_status_counts_questions_once_and_writes_under_way(setup) -> None:
    bridge, api, _state, codex, *_ = setup
    question = Asked(bridge, {"tool_name": "AskUserQuestion", "tool_input": ASK_INPUT})
    card = _question_card(bridge)
    status = bridge.status()
    assert (status["pending_permissions"], status["pending_questions"], status["executing"]) == (0, 1, 0)
    _tap_q(bridge, card, "l", 1)
    assert question.done() == {}
    release = threading.Event()
    codex.answer_question = lambda *_args: release.wait(5)  # type: ignore[method-assign]
    bridge.on_event(_codex_question(questions=CODEX_QUESTIONS[:1]))
    _drain(bridge)
    card = next(iter(bridge.questions.values()))
    _tap_q(bridge, card, "o0", 2)
    _tap_q(bridge, card, "s", 3)
    assert bridge.status()["executing"] == 1  # an Install now waits for the answer to reach Codex
    release.set()
    _run_threads()
    assert bridge.status()["executing"] == 0


def test_a_card_closed_while_it_was_being_sent_goes_out_closed(setup) -> None:
    bridge, api, state, *_ = setup
    original = api.send_message

    def send_then_lock(chat_id: int, text: str, **options: Any) -> dict[str, Any]:
        sent = original(chat_id, text, **options)
        if "先做哪一部分" in text:
            bridge._settle_open_cards("test")  # writes got locked by another thread meanwhile
        return sent

    api.send_message = send_then_lock  # type: ignore[method-assign]
    bridge.on_event(_codex_question())
    _drain(bridge)
    card_id = api.sent()[-1]["message_id"]
    final = api.sent("edit_message")[-1]
    assert final["message_id"] == card_id and "写操作已锁定" in final["text"] and final["buttons"] is None
    assert not bridge.questions and card_id not in (state.get("open_permission_cards") or [])


def test_a_late_delivery_edit_never_reopens_a_closed_card(setup) -> None:
    bridge, api, _state, codex, *_ = setup
    bridge.on_event(_codex_question(questions=CODEX_QUESTIONS[:1]))
    _drain(bridge)
    card = next(iter(bridge.questions.values()))

    def answered_on_the_pc_meanwhile(*_args: Any) -> None:
        bridge._close_codex_question(_codex_question(closed=True, questions=[], answer="范围: 并行"))

    codex.answer_question = answered_on_the_pc_meanwhile  # type: ignore[method-assign]
    _tap_q(bridge, card, "o0", 1)
    _tap_q(bridge, card, "s", 2)
    _run_threads()
    _drain(bridge)
    final = api.sent("edit_message")[-1]
    assert "Codex 收到的答案：\n范围: 并行" in final["text"] and final["buttons"] is None


def test_a_reminder_for_a_card_closed_meanwhile_is_dropped(setup) -> None:
    bridge, api, _state, _codex, _claude, presence, _now = setup
    presence.value = True
    bridge.on_event(_codex_question())
    _drain(bridge)
    presence.value = False
    bridge._remind_questions()  # queued ...
    bridge._settle_open_cards("test")  # ... and writes got locked before it went out
    count = len(api.sent())
    _drain(bridge)
    assert len(api.sent()) == count and "写操作已锁定" in api.sent("edit_message")[-1]["text"]


WEEKLY = QuotaWindow("本周", 5.0, None, 7 * 86400.0)


def test_every_push_ends_with_the_plan_limits_left(setup) -> None:
    bridge, api, state, codex, claude, _presence, now = setup
    codex_limits = Quota(CODEX, [WEEKLY], now[0] - 30)
    claude_limits = Quota(CLAUDE, [QuotaWindow("5h", 8.0, None, 5 * 3600.0), QuotaWindow("本周", 39.0, None, 7 * 86400.0)],
                          now[0] - 600, source="desktop")
    bridge.on_event(TurnEvent(CODEX, THREAD, "论文审稿", r"C:\proj\paper", "completed", final_text="好",
                              quota=codex_limits))
    bridge.on_event(TurnEvent(CLAUDE, SESSION, "重构脚本", r"C:\proj\code", "completed", final_text="完成",
                              quota=claude_limits))
    bridge.on_event(TurnEvent(CLAUDE, SESSION, "重构脚本", r"C:\proj\code", "completed", final_text="没有记录"))
    _drain(bridge)
    first, second, third = (item["text"] for item in api.sent()[-3:])
    assert first.endswith("<i>📊 Codex 额度：本周剩 95%</i>")
    assert "<i>📊 Claude 额度：5h 剩 92% · 本周剩 61%（Claude Desktop " in second and second.endswith("的记录）</i>")
    assert "📊" not in third
    bridge.cfg.quota_enabled = False
    bridge.on_event(TurnEvent(CODEX, THREAD, "论文审稿", r"C:\proj\paper", "completed", final_text="好",
                              quota=codex_limits))
    _drain(bridge)
    assert "📊" not in api.sent()[-1]["text"]
    bridge.cfg.quota_enabled = True
    state.set_locked(True, "非白名单用户 99 在群里发言")  # a security lock: titles only
    bridge.on_event(TurnEvent(CODEX, THREAD, "论文审稿", r"C:\proj\paper", "completed", final_text="好",
                              quota=codex_limits))
    _drain(bridge)
    assert "📊" not in api.sent()[-1]["text"]


def test_status_shows_the_plan_limits(setup) -> None:
    bridge, api, _state, codex, claude, _presence, now = setup
    bridge.handle_update({"update_id": 1, "message": _message("/status")})
    assert "额度：还没有数据" in api.sent()[-1]["text"]
    codex.limits = Quota(CODEX, [WEEKLY], now[0])
    claude.quota = lambda: (_ for _ in ()).throw(OSError("unreadable"))  # type: ignore[method-assign]
    bridge.handle_update({"update_id": 2, "message": _message("/status")})
    text = api.sent()[-1]["text"]
    assert "📊 Codex 额度：本周剩 95%" in text and "Claude 额度" not in text


def _conflict(offset: Any, **options: Any) -> list[dict[str, Any]]:
    raise Conflict("Conflict: terminated by other getUpdates request", code=409, method="getUpdates")


def test_a_single_409_warns_and_a_repeated_one_locks(setup, tmp_path: Path) -> None:
    bridge, api, state, _codex, _claude, _presence, now = setup
    api.get_updates = _conflict  # type: ignore[method-assign]
    assert bridge.poll_updates_once() == bridge_module.CONFLICT_RETRY_S
    _drain(bridge)
    assert not state.locked
    assert "409 Conflict" in api.sent()[-1]["text"] and api.sent()[-1]["silent"]
    now[0] += bridge_module.CONFLICT_CONFIRM_S - 1
    assert bridge.poll_updates_once() == 60.0
    _drain(bridge)
    assert state.locked and "409" in state.locked_reason
    assert "写操作已锁定" in api.sent()[-1]["text"]
    now[0] += 60
    bridge.poll_updates_once()
    log = (tmp_path / "logs" / "bridge.log").read_text(encoding="utf-8")
    assert log.count("getUpdates 409") == 2 and "已锁定写操作" in log  # every 409 leaves a line


def test_409s_further_apart_than_the_confirm_window_do_not_lock(setup) -> None:
    bridge, api, state, _codex, _claude, _presence, now = setup
    api.get_updates = _conflict  # type: ignore[method-assign]
    bridge.poll_updates_once()
    now[0] += bridge_module.CONFLICT_CONFIRM_S + 1
    assert bridge.poll_updates_once() == bridge_module.CONFLICT_RETRY_S
    assert not state.locked


def test_title_sync_retries_a_name_after_a_network_error(setup) -> None:
    bridge, api, state, codex, _claude, _presence, now = setup
    key = session_key(CODEX, THREAD)
    bridge.on_event(TurnEvent(CODEX, THREAD, codex.title, r"C:\proj\paper", "completed", final_text="完成"))
    _drain(bridge)
    codex.title = "改名"
    api.edit_topic_error = TelegramError("网络错误（ConnectError）")
    _run_title_sync(bridge, now, bridge_module.TOPIC_TITLE_SYNC_S)
    api.edit_topic_error = None
    _run_title_sync(bridge, now, bridge_module.TOPIC_TITLE_SYNC_S)
    assert api.edit_topic_attempts == 2
    assert state.topic_for(key)["name"] == "pc1·Codex·改名"


# Card retries run on the hook's existing polling loop. These tests replace only each
# request's Event.wait: every poll advances the fake clock and runs its scripted PC/tap action.
def _card_retry_polls(monkeypatch: pytest.MonkeyPatch, now: list[float], on_poll: Any = None,
                      *, step_s: float = 2.0) -> list[Any]:
    make_wait = bridge_module.PermissionWait
    waits: list[Any] = []

    def create(*args: Any, **kwargs: Any) -> Any:
        wait = make_wait(*args, **kwargs)
        polls = 0

        def poll(_timeout: float) -> bool:
            nonlocal polls
            if not wait.event.is_set():
                polls += 1
                assert polls <= 400, "card wait did not settle under its deterministic script"
                now[0] += step_s
                if on_poll is not None:
                    on_poll(wait, polls)
            return wait.event.is_set()

        monkeypatch.setattr(wait.event, "wait", poll)
        waits.append(wait)
        return wait

    monkeypatch.setattr(bridge_module, "PermissionWait", create)
    return waits


def _card_audits(monkeypatch: pytest.MonkeyPatch, bridge: Bridge) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    monkeypatch.setattr(bridge.log, "audit", lambda event, **fields: records.append({"event": event, **fields}))
    return records


def _card_send_failures(monkeypatch: pytest.MonkeyPatch, bridge: Bridge,
                        failures: list[Exception]) -> list[dict[str, Any]]:
    send = bridge._send
    attempts: list[dict[str, Any]] = []
    pending = list(failures)

    def scripted(html: str, **options: Any) -> dict[str, Any]:
        buttons = options.get("buttons") or []
        if any(button[1].startswith(("perm:", "q:")) for row in buttons for button in row):
            attempts.append({"text": html, "at": bridge.clock(), **options})
            if pending:
                raise pending.pop(0)
        return send(html, **options)

    monkeypatch.setattr(bridge, "_send", scripted)
    return attempts


def _permission_payload(**fields: Any) -> dict[str, Any]:
    return {"session_id": SESSION, "tool_name": "Bash", "tool_input": {"command": "npm test"},
            "cwd": r"C:\projects\code", **fields}


@pytest.mark.parametrize("code", [None, 429, 503])
def test_permission_card_retries_then_allows_with_audited_attempts(setup, monkeypatch, code) -> None:
    bridge, api, _state, _codex, claude, presence, now = setup
    claude.registry_status = "waiting"
    audits = _card_audits(monkeypatch, bridge)
    attempts = _card_send_failures(monkeypatch, bridge, [TelegramError("temporary outage", code=code)])
    network_checks: list[bool] = []
    monkeypatch.setattr(bridge, "_network_trouble", lambda: network_checks.append(True))

    def poll(wait: Any, count: int) -> None:
        if count == 1:
            assert wait.message_id is None and wait.card_failed and wait.attempts == 1
            assert bridge.permissions[wait.nonce] is wait
            presence.value = True  # each attempt recomputes silent/reminded from current presence
        elif wait.message_id is not None:
            _tap(bridge, f"perm:a:{wait.nonce}", 1)

    waits = _card_retry_polls(monkeypatch, now, poll)
    assert _allowed(bridge.claude_permission(_permission_payload()))
    assert len(attempts) == 2 and attempts[1]["at"] - attempts[0]["at"] == 2
    assert attempts[0]["silent"] is False and attempts[1]["silent"] is True
    assert waits[0].reminded is False and api.sent()[-1]["silent"] is True
    assert [record["attempts"] for record in audits if record["event"] == "permission_asked"] == [2]
    assert next(record for record in audits if record["event"] == "permission_decided")["why"] == ""
    assert network_checks == ([True] if code is None else [])
    assert not bridge.permissions


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("error", [TelegramError("Bad Request", code=400), ValueError("invalid keyboard")])
def test_permission_card_permanent_send_failure_falls_back_at_once(setup, monkeypatch, owned, error) -> None:
    bridge, api, *_rest, now = setup
    audits = _card_audits(monkeypatch, bridge)
    attempts = _card_send_failures(monkeypatch, bridge, [error])
    if owned:
        monkeypatch.setattr(bridge, "_owned_run", lambda _session_id: object())

    def no_poll(_wait: Any, _count: int) -> None:
        pytest.fail("a permanent card failure must not enter the retry loop")

    _card_retry_polls(monkeypatch, now, no_poll)
    result = bridge.claude_permission(_permission_payload())
    assert result["hookSpecificOutput"]["decision"]["behavior"] == "deny" if owned else result == {}
    failed = next(record for record in audits if record["event"] == "card_send_failed")
    assert failed["attempts"] == 1 and failed["code"] == getattr(error, "code", None)
    assert failed["outcome"] == ("deny" if owned else "local")
    assert len(attempts) == 1 and not api.sent() and not bridge.permissions


def test_permission_retry_stops_when_the_registry_shows_a_pc_answer(setup, monkeypatch) -> None:
    bridge, api, _state, _codex, claude, _presence, now = setup
    claude.registry_status = "waiting"
    audits = _card_audits(monkeypatch, bridge)
    attempts = _card_send_failures(monkeypatch, bridge, [TelegramError("network", code=None)] * 10)

    def poll(_wait: Any, count: int) -> None:
        if count >= 2:
            claude.registry_status = "busy"

    _card_retry_polls(monkeypatch, now, poll)
    assert bridge.claude_permission(_permission_payload()) == {}
    assert len(attempts) == 2 and not api.sent() and not api.sent("edit_message")
    decided = next(record for record in audits if record["event"] == "permission_decided")
    assert decided["decision"] == "answered" and decided["why"] == "left_waiting"


def test_owned_permission_retries_past_the_card_window_until_its_deadline(setup, monkeypatch) -> None:
    bridge, api, _state, _codex, _claude, _presence, now = setup
    start = now[0]
    monkeypatch.setattr(bridge_module, "CARD_RETRY_S", 6.0)
    monkeypatch.setattr(bridge_module, "PERMISSION_WAIT_S", 20.0)
    monkeypatch.setattr(bridge, "_owned_run", lambda _session_id: object())
    audits = _card_audits(monkeypatch, bridge)
    attempts = _card_send_failures(monkeypatch, bridge, [TelegramError("network", code=None)] * 20)
    _card_retry_polls(monkeypatch, now)
    result = bridge.claude_permission(_permission_payload(), hook_timeout_s=100)
    assert result["hookSpecificOutput"]["decision"]["behavior"] == "deny"
    assert [item["at"] - start for item in attempts] == [0, 2, 6, 14]
    assert now[0] - start == 20 and not api.sent()
    failed = next(record for record in audits if record["event"] == "card_send_failed")
    assert failed["attempts"] == 4 and failed["code"] is None and failed["outcome"] == "deny"


def test_owned_permission_permanent_failure_on_retry_keeps_the_connection_denial_reason(setup, monkeypatch) -> None:
    bridge, api, *_rest, now = setup
    monkeypatch.setattr(bridge, "_owned_run", lambda _session_id: object())
    audits = _card_audits(monkeypatch, bridge)
    attempts = _card_send_failures(monkeypatch, bridge, [TelegramError("network", code=None),
                                                       TelegramError("Bad Request", code=400)])
    _card_retry_polls(monkeypatch, now)
    result = bridge.claude_permission(_permission_payload())
    assert result["hookSpecificOutput"]["decision"] == {"behavior": "deny", "message": "无法联系 Telegram 审批"}
    assert len(attempts) == 2 and not api.sent()
    failed = next(record for record in audits if record["event"] == "card_send_failed")
    assert (failed["attempts"], failed["code"], failed["outcome"]) == (2, 400, "deny")


def test_interactive_permission_returns_to_pc_after_the_card_retry_window(setup, monkeypatch) -> None:
    bridge, api, _state, _codex, claude, _presence, now = setup
    start = now[0]
    claude.registry_status = "waiting"
    monkeypatch.setattr(bridge_module, "CARD_RETRY_S", 10.0)
    audits = _card_audits(monkeypatch, bridge)
    attempts = _card_send_failures(monkeypatch, bridge, [TelegramError("network", code=None)] * 20)
    _card_retry_polls(monkeypatch, now)
    assert bridge.claude_permission(_permission_payload(), hook_timeout_s=1000) == {}
    assert [item["at"] - start for item in attempts] == [0, 2, 6]
    assert now[0] - start == 10 and not api.sent()
    failed = next(record for record in audits if record["event"] == "card_send_failed")
    assert failed["outcome"] == "local" and failed["attempts"] == 3


def test_permission_retry_uses_the_full_backoff_ladder(setup, monkeypatch) -> None:
    bridge, _api, _state, _codex, claude, _presence, now = setup
    claude.registry_status = "waiting"
    start = now[0]
    attempts = _card_send_failures(monkeypatch, bridge, [TelegramError("network", code=None)] * 6)

    def poll(wait: Any, _count: int) -> None:
        if wait.message_id is not None:
            _tap(bridge, f"perm:a:{wait.nonce}", 1)

    _card_retry_polls(monkeypatch, now, poll)
    assert _allowed(bridge.claude_permission(_permission_payload()))
    assert [item["at"] - start for item in attempts] == [0, 2, 6, 14, 30, 60, 90]


def test_attention_alert_survives_an_unsent_retry_and_is_covered_after_success(setup, monkeypatch) -> None:
    bridge, api, _state, _codex, claude, _presence, now = setup
    claude.registry_status = "waiting"
    _card_send_failures(monkeypatch, bridge, [TelegramError("network", code=None)])
    attention = AttentionEvent(CLAUDE, SESSION, "重构脚本", r"C:\projects\code", "permission prompt")

    def poll(wait: Any, count: int) -> None:
        if count == 1:
            assert wait.message_id is None and wait.card_failed
            bridge._push_attention(attention)
            assert len(api.sent()) == 1
            assert "等待处理" in api.sent()[-1]["text"]
            assert "卡片暂时发不出去，桥在重试；也可在电脑上处理。" in api.sent()[-1]["text"]
        elif wait.message_id is not None:
            sent_count = len(api.sent())
            bridge._push_attention(attention)
            assert len(api.sent()) == sent_count  # the sent card covers the ordinary alert again
            _tap(bridge, f"perm:a:{wait.nonce}", 1)

    _card_retry_polls(monkeypatch, now, poll)
    assert _allowed(bridge.claude_permission(_permission_payload()))
    assert len(api.sent()) == 2


@pytest.mark.parametrize("description", ["ReadTimeout", "ReadError", "RemoteProtocolError"])
def test_retry_after_a_possible_telegram_delivery_adds_a_duplicate_note(setup, monkeypatch, description) -> None:
    bridge, api, _state, _codex, claude, _presence, now = setup
    claude.registry_status = "waiting"
    audits = _card_audits(monkeypatch, bridge)
    attempts = _card_send_failures(monkeypatch, bridge, [TelegramError(f"network ({description})", code=None)])

    def poll(wait: Any, _count: int) -> None:
        if wait.message_id is not None:
            _tap(bridge, f"perm:a:{wait.nonce}", 1)

    _card_retry_polls(monkeypatch, now, poll)
    assert _allowed(bridge.claude_permission(_permission_payload()))
    note = "<i>若上方有同样的卡片，以此为准。</i>"
    assert note not in attempts[0]["text"] and note in attempts[1]["text"] and note in api.sent()[-1]["text"]
    assert len([record for record in audits if record["event"] == "card_resent_maybe_duplicate"]) == 1


@pytest.mark.parametrize("tool_name,tool_input", [("AskUserQuestion", ASK_INPUT), ("ExitPlanMode", PLAN_INPUT)])
def test_claude_question_retry_is_not_open_until_its_card_is_sent(setup, monkeypatch, tool_name, tool_input) -> None:
    bridge, api, _state, _codex, claude, _presence, now = setup
    claude.registry_status = "waiting"
    audits = _card_audits(monkeypatch, bridge)
    attempts = _card_send_failures(monkeypatch, bridge, [TelegramError("network", code=None)])
    digest = progress.interactive_digest(tool_name, tool_input)

    def poll(wait: Any, count: int) -> None:
        if count == 1:
            assert wait.card_failed and wait.message_id is None
            assert not bridge.questions and bridge._open_question(wait.key) is None
            assert bridge.status()["pending_questions"] == 0
            assert bridge._question_hooks[(SESSION, digest)][1] == "failed"
            assert bridge._card_shows(SESSION, digest) is False
            bridge.handle_update({"update_id": 1, "message": _message("先调研", thread_id=wait.thread_id)})
            assert bridge.pending  # ordinary topic text still asks to send it to the session
            assert not any("已填进卡片" in item["text"] for item in api.sent())
        elif wait.message_id is not None:
            card = bridge.questions[wait.nonce]
            assert card.message_id == wait.message_id
            assert card.answers == [[] for _ in card.questions] and card.feedback == ""
            assert bridge.status()["pending_questions"] == 1
            assert bridge._question_hooks[(SESSION, digest)][1] == "card"
            _tap_q(bridge, card, "l", 2)

    _card_retry_polls(monkeypatch, now, poll)
    result = bridge.claude_permission(_permission_payload(tool_name=tool_name, tool_input=tool_input))
    assert result == {} and len(attempts) == 2
    assert [record["attempts"] for record in audits if record["event"] == "question_asked"] == [2]
    assert not bridge.questions and not bridge.permissions


@pytest.mark.parametrize("error", [TelegramError("Bad Request", code=400), ValueError("invalid keyboard")])
def test_codex_question_permanent_push_failure_sends_a_plain_note(setup, monkeypatch, error) -> None:
    bridge, api, *_ = setup
    audits = _card_audits(monkeypatch, bridge)
    if isinstance(error, TelegramError):
        attempts = _card_send_failures(monkeypatch, bridge, [error])
    else:
        attempts = []

        def invalid_keyboard(_card: Any) -> Any:
            raise error

        monkeypatch.setattr(bridge, "_question_view", invalid_keyboard)
    bridge._push_question(_codex_question())
    assert not bridge.questions
    assert len(attempts) == (1 if isinstance(error, TelegramError) else 0)
    assert len(api.sent()) == 1 and api.sent()[0]["buttons"] is None
    assert api.sent()[0]["text"] == "❓ <b>pc1·Codex·论文审稿</b> 在等你回答问题（卡片发不出去，请在电脑上回答）。"
    failure = next(record for record in audits if record["event"] == "question_push_failed")
    assert failure["code"] == getattr(error, "code", None)


@pytest.mark.parametrize("code", [None, 429, 503])
def test_codex_question_transient_push_failure_propagates_for_the_send_loop(setup, monkeypatch, code) -> None:
    bridge, api, *_ = setup
    audits = _card_audits(monkeypatch, bridge)
    error = TelegramError("temporary outage", code=code)
    attempts = _card_send_failures(monkeypatch, bridge, [error])
    with pytest.raises(TelegramError) as failure:
        bridge._push_question(_codex_question())
    assert failure.value is error and len(attempts) == 1
    assert not bridge.questions and not api.sent()
    assert not any(record["event"] == "question_push_failed" for record in audits)


def test_codex_question_plain_fallback_is_attempted_once_if_it_also_fails(setup, monkeypatch) -> None:
    bridge, api, *_ = setup
    audits = _card_audits(monkeypatch, bridge)
    attempts: list[dict[str, Any]] = []

    def fail(html: str, **options: Any) -> Any:
        attempts.append({"text": html, **options})
        raise TelegramError("Bad Request", code=400) if len(attempts) == 1 else TelegramError("network", code=None)

    monkeypatch.setattr(bridge, "_send", fail)
    bridge._push_question(_codex_question())
    assert len(attempts) == 2 and attempts[1].get("buttons") is None
    assert "卡片发不出去，请在电脑上回答" in attempts[1]["text"]
    assert not bridge.questions and not api.sent()
    assert len([record for record in audits if record["event"] == "question_push_failed"]) == 1
    assert "发送问题提示失败" in bridge.log.text_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("error", [TelegramError("Bad Request", code=400), ValueError("invalid topic")])
def test_codex_question_permanent_topic_failure_falls_back_to_a_plain_note_in_general(setup, monkeypatch, error) -> None:
    bridge, api, *_ = setup
    audits = _card_audits(monkeypatch, bridge)

    def fail_topic(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    monkeypatch.setattr(bridge, "_topic", fail_topic)
    bridge._push_question(_codex_question())
    assert not bridge.questions and len(api.sent()) == 1
    sent = api.sent()[0]
    assert sent["thread_id"] is None and sent["buttons"] is None
    assert "卡片发不出去，请在电脑上回答" in sent["text"]
    assert next(record for record in audits if record["event"] == "question_push_failed")["code"] == getattr(error, "code", None)


def test_permission_topic_failure_retries_creation_before_sending_the_card(setup, monkeypatch) -> None:
    bridge, api, _state, _codex, claude, _presence, now = setup
    claude.registry_status = "waiting"
    audits = _card_audits(monkeypatch, bridge)
    create_topic = bridge._topic
    topics: list[float] = []

    def topic(*args: Any, **kwargs: Any) -> int | None:
        topics.append(bridge.clock())
        if len(topics) == 1:
            raise TelegramError("network", code=None)
        return create_topic(*args, **kwargs)

    def poll(wait: Any, count: int) -> None:
        if count == 1:
            assert wait.thread_id is None and wait.card_failed and not api.sent()
        elif wait.message_id is not None:
            _tap(bridge, f"perm:a:{wait.nonce}", 1)

    monkeypatch.setattr(bridge, "_topic", topic)
    _card_retry_polls(monkeypatch, now, poll)
    assert _allowed(bridge.claude_permission(_permission_payload()))
    assert len(topics) == 2 and topics[1] - topics[0] == 2 and len(api.sent()) == 1
    assert next(record for record in audits if record["event"] == "permission_asked")["attempts"] == 2


def test_the_first_card_send_in_flight_already_covers_the_attention_alert(setup, monkeypatch) -> None:
    bridge, api, _state, _codex, claude, _presence, now = setup
    claude.registry_status = "waiting"
    send = bridge._send

    def initial_send(html: str, **options: Any) -> dict[str, Any]:
        wait = next(iter(bridge.permissions.values()))
        assert wait.message_id is None and wait.card_failed is False
        bridge._push_attention(AttentionEvent(CLAUDE, SESSION, "重构脚本", r"C:\projects\code", "permission prompt"))
        assert not api.sent()
        return send(html, **options)

    def poll(wait: Any, _count: int) -> None:
        _tap(bridge, f"perm:a:{wait.nonce}", 1)

    monkeypatch.setattr(bridge, "_send", initial_send)
    _card_retry_polls(monkeypatch, now, poll)
    assert _allowed(bridge.claude_permission(_permission_payload()))
    assert len(api.sent()) == 1


@pytest.mark.parametrize("tool_name,tool_input", [("Bash", {"command": "npm test"}), ("AskUserQuestion", ASK_INPUT)])
@pytest.mark.parametrize("why", ["left_waiting", "changed_after_arrival", "no_dialog_15s", "back_at_pc"])
def test_pc_card_decisions_audit_the_registry_or_presence_reason(setup, monkeypatch, tool_name, tool_input, why) -> None:
    bridge, _api, _state, _codex, claude, presence, now = setup
    audits = _card_audits(monkeypatch, bridge)
    claude.registry_status = "waiting" if why == "left_waiting" else "busy"
    if why == "changed_after_arrival":
        claude.registry_changed_at = now[0] + 0.5
    if why == "back_at_pc":
        claude.registry_kind = "sdk"

    def poll(_wait: Any, count: int) -> None:
        if why == "left_waiting" and count >= 2:
            claude.registry_status = "busy"
        elif why == "back_at_pc":
            presence.value = True

    _card_retry_polls(monkeypatch, now, poll)
    assert bridge.claude_permission(_permission_payload(tool_name=tool_name, tool_input=tool_input)) == {}
    event = "question_decided" if tool_name == "AskUserQuestion" else "permission_decided"
    decision = next(record for record in audits if record["event"] == event)
    assert decision["why"] == why
    assert decision["decision"] == ("local" if why == "back_at_pc" else "answered")


@pytest.mark.parametrize("tool_name,tool_input", [("Bash", {"command": "SECRET"}), ("AskUserQuestion", ASK_INPUT)])
@pytest.mark.parametrize("reason", ["unbound", "locked", "at_pc_subagent", "at_pc_no_dialog", "excluded"])
def test_skipped_claude_cards_audit_the_reason_without_input(setup, monkeypatch, tool_name, tool_input, reason) -> None:
    bridge, api, state, _codex, claude, presence, _now = setup
    audits = _card_audits(monkeypatch, bridge)
    payload = _permission_payload(tool_name=tool_name, tool_input=tool_input)
    if reason == "unbound":
        payload["session_id"] = ""
    elif reason == "locked":
        state.set_locked(True, bridge_module.OWNER_LOCK)
    elif reason == "at_pc_subagent":
        presence.value = True
        payload["agent_id"] = "agent-1"
    elif reason == "at_pc_no_dialog":
        presence.value = True
        claude.registry_kind = "sdk"
    elif reason == "excluded":
        bridge.cfg.content_exclude = [r"C:\projects"]
    assert bridge.claude_permission(payload) == {} and not api.sent()
    skipped = [record for record in audits if record["event"] == "card_skipped"]
    assert len(skipped) == 1 and skipped[0]["reason"] == reason
    assert skipped[0]["session_id"] == payload["session_id"] and skipped[0]["tool"] == tool_name
    assert "tool_input" not in skipped[0] and "SECRET" not in str(skipped)


@pytest.mark.parametrize("tool_name", ["AskUserQuestion", "ExitPlanMode"])
def test_empty_claude_question_cards_audit_no_questions(setup, monkeypatch, tool_name) -> None:
    bridge, api, *_ = setup
    audits = _card_audits(monkeypatch, bridge)
    assert bridge.claude_permission(_permission_payload(tool_name=tool_name, tool_input={})) == {}
    assert not api.sent() and not bridge.questions and not bridge.permissions
    skipped = next(record for record in audits if record["event"] == "card_skipped")
    assert skipped["reason"] == "no_questions" and skipped["tool"] == tool_name


ASYNC_QUESTIONS = [Question("0", "选择方案 SECRET？", options=[("方案 SECRET", ""), ("并行", "")]),
                   Question("1", "请补充时间安排？")]
ASYNC_JOB = "atg-261001-120000-codex-async1"


def _async_question(request_id: str = "async_call", **fields: Any) -> QuestionEvent:
    fields.setdefault("mode", "async")
    fields.setdefault("questions", list(ASYNC_QUESTIONS))
    return _codex_question(request_id, **fields)


def _async_card(bridge: Bridge, **fields: Any) -> Any:
    bridge.on_event(_async_question(**fields))
    _drain(bridge)
    return next(card for card in bridge.questions.values() if card.kind == "codex_async")


def _submit_async_choice(bridge: Bridge, card: Any) -> None:
    update_id = 1
    while not card.review:
        if card.questions[card.index].options:
            _tap_q(bridge, card, "o0", update_id)
        else:
            bridge.handle_update(_reply_to(card, "明天发布", update_id))
        update_id += 1
    _tap_q(bridge, card, "s", update_id)
    _run_threads()
    _drain(bridge)


def test_codex_async_card_shows_options_and_the_optionless_reply_hint(setup, monkeypatch) -> None:
    bridge, api, *_ = setup
    audits = _card_audits(monkeypatch, bridge)
    card = _async_card(bridge)
    sent = api.sent()[-1]
    assert card.mode == "async" and card.kind == "codex_async"
    assert sent["buttons"][0][0][1] == f"q:o0.0:{card.nonce}"
    assert sent["buttons"][0][0][0] == "A. 方案 ‹已脱敏›"
    assert "SECRET" not in sent["text"] and "选择方案 ‹已脱敏›？" in sent["text"]
    assert "回复" in sent["text"] and "没有选项" in sent["text"]
    assert "跳过" not in sent["text"] and "请回答每一个问题后再发送" in sent["text"]
    assert next(record for record in audits if record["event"] == "question_pushed")["mode"] == "async"
    _tap_q(bridge, card, "o1", 1)
    assert "回复这张卡片" in api.sent("edit_message")[-1]["text"]


def test_codex_async_option_submission_keeps_original_text_and_persists_identity(setup, monkeypatch) -> None:
    bridge, api, state, codex, _claude, _presence, now = setup
    audits = _card_audits(monkeypatch, bridge)
    card = _async_card(bridge, questions=ASYNC_QUESTIONS[:1])
    _submit_async_choice(bridge, card)
    assert len(codex.async_answered) == 1 and not codex.answered
    answer = codex.async_answered[0]
    assert answer == {"thread_id": THREAD, "call_id": "async_call", "turn_id": "t1",
                      "answers": {0: ("选择方案 SECRET？", "方案 SECRET")},
                      "client_id": card.client_id, "marker": f"[{card.marker}]"}
    assert card.client_id and card.marker == "tg:" + card.nonce[:6]
    assert card.state == "sent" and card.tried and card.sent_at == now[0] and card.via == "ipc"
    assert "已插进正在运行的这一轮，等待 Codex 记录" in api.sent("edit_message")[-1]["text"]
    assert not api.sent("edit_message")[-1].get("buttons")
    assert state.get("async_answers") == [{"session_id": THREAD, "call_id": "async_call", "indices": [0],
                                          "via": "ipc", "client_id": card.client_id, "marker": card.marker,
                                          "job_id": "", "at": now[0]}]
    persisted = BridgeState(state.path)
    assert persisted.get("async_answers") == state.get("async_answers")
    sent = next(record for record in audits if record["event"] == "question_async_sent")
    envelope = codex_threads.build_question_reply("async_call", answer["answers"])
    assert sent["via"] == "ipc" and sent["count"] == 1 and sent["text_sha"] == bridge_module.text_digest(envelope)
    assert next(record for record in audits if record["event"] == "question_submitted")["mode"] == "async"
    _tap_q(bridge, card, "s", 4)
    _run_threads()
    assert len(codex.async_answered) == 1


def test_codex_async_ipc_new_turn_footer(setup) -> None:
    bridge, api, _state, codex, *_ = setup
    codex.async_answer_result = {"via": "ipc", "turn_id": "t2", "other_turn": True}
    card = _async_card(bridge, questions=ASYNC_QUESTIONS[:1])
    _submit_async_choice(bridge, card)
    assert card.state == "sent" and "答案进入了 Codex 的新一轮，等待记录" in api.sent("edit_message")[-1]["text"]


@pytest.mark.parametrize("source", ["client", "marker", "other"])
def test_codex_async_close_credits_the_actual_answer_source(setup, monkeypatch, source) -> None:
    bridge, api, state, _codex, *_ = setup
    audits = _card_audits(monkeypatch, bridge)
    card = _async_card(bridge, questions=ASYNC_QUESTIONS[:1])
    _submit_async_choice(bridge, card)
    attribution = {"client": f"client:{card.client_id}", "marker": card.marker, "other": "client:someone-else"}[source]
    bridge.on_event(_async_question(closed=True, questions=[], by="answered", answer="选择方案 SECRET？: 方案 SECRET",
                                    answered={0: "方案 SECRET"}, answer_sources={0: attribution}))
    _drain(bridge)
    final = api.sent("edit_message")[-1]
    expected = "已在电脑上回答" if source == "other" else "✅ 已从 Telegram 回答"
    assert expected in final["text"] and "SECRET" not in final["text"] and "‹已脱敏›" in final["text"]
    assert final["buttons"] is None and card.closed and not bridge.questions
    assert state.get("async_answers", []) == []
    closed = next(record for record in audits if record["event"] == "question_closed")
    assert closed["mode"] == "async" and closed["by"] == "answered"


@pytest.mark.parametrize("by,outcome", [
    ("superseded", "对话已经继续，这个问题作废"),
    ("gone", "对话已归档或删除"),
    ("expired", "超过 6 小时没有回答，这个问题作废"),
    ("restart", "Codex Desktop 重启或退出了，这个问题作废"),
    ("desktop_gone", "Codex Desktop 重启或退出了，这个问题作废"),
])
def test_codex_async_close_reason(setup, monkeypatch, by, outcome) -> None:
    bridge, api, _state, *_ = setup
    audits = _card_audits(monkeypatch, bridge)
    card = _async_card(bridge)
    bridge.on_event(_async_question(closed=True, questions=[], by=by))
    _drain(bridge)
    assert outcome in api.sent("edit_message")[-1]["text"] and card.closed and not bridge.questions
    assert next(record for record in audits if record["event"] == "question_closed")["by"] == by


def test_codex_async_reply_submits_typed_text_with_the_original_index(setup) -> None:
    bridge, api, _state, codex, *_ = setup
    card = _async_card(bridge, questions=[Question("3", "请原样回答 SECRET？")])
    bridge.handle_update(_reply_to(card, "明天 SECRET 发布", 1))
    assert card.review and card.answers == [["明天 SECRET 发布"]] and card.typed == [True]
    assert "回复这张卡片" in api.sent()[0]["text"]
    _tap_q(bridge, card, "s", 2)
    _run_threads()
    _drain(bridge)
    assert codex.async_answered[0]["answers"] == {3: ("请原样回答 SECRET？", "明天 SECRET 发布")}
    assert card.state == "sent" and "SECRET" not in api.sent("edit_message")[-1]["text"]


def test_codex_async_refuses_skip_and_partial_submission(setup) -> None:
    bridge, _api, _state, codex, *_ = setup
    card = _async_card(bridge)
    assert "请回答每一个问题后再发送" in _tap_q(bridge, card, "n", 1)
    assert card.index == 0 and not card.review
    assert not any("跳过" in label for row in bridge._question_buttons(card) for label, _ in row)
    _tap_q(bridge, card, "o0", 2)
    # A forged submit cannot send the first answer without answering the second question.
    card.review = True
    assert "请回答每一个问题后再发送" in _tap_q(bridge, card, "s", 3)
    _run_threads()
    assert card.state == "open" and not codex.async_answered


def _approval(**fields: Any) -> ApprovalEvent:
    values: dict[str, Any] = {
        "agent": CODEX, "session_id": THREAD, "title": "运行检查", "cwd": r"C:\projects\paper",
        "request_id": 17, "method": codex_ipc.COMMAND_APPROVAL_METHOD, "item_id": "command-1",
        "turn_id": "turn-1", "approval_id": "approval-1", "digest": "params-digest",
        "kind": "command", "command": "python build.py", "command_cwd": r"C:\projects\paper",
        "reason": "检查构建", "network": {"host": "example.com"},
        "extra_permissions": {"read": ["inputs"]}, "files": [],
    }
    values.update(fields)
    return ApprovalEvent(**values)


def _approval_card(bridge: Bridge, **fields: Any) -> Any:
    bridge.on_event(_approval(**fields))
    _drain(bridge)
    return next(iter(bridge.codex_approvals.values()))


def _tap_approval(bridge: Bridge, card: Any, op: str, *, user: int = OWNER,
                  version: int | None = None) -> str:
    bridge.handle_update({"update_id": 777, "callback_query": {
        "id": "approval-callback", "data": f"ca:{op}.{card.version if version is None else version}:{card.nonce}",
        "from": {"id": user}, "message": {"message_id": card.message_id, "chat": {"id": CHAT}}}})
    return bridge.api.sent("answer_callback")[-1]["text"]


@pytest.mark.parametrize("kind", ["command", "file"])
def test_codex_approval_card_has_only_single_request_buttons(setup, monkeypatch, kind) -> None:
    bridge, api, state, *_ = setup
    audits = _card_audits(monkeypatch, bridge)
    card = _approval_card(bridge, kind=kind, method=codex_ipc.FILE_APPROVAL_METHOD if kind == "file"
                          else codex_ipc.COMMAND_APPROVAL_METHOD, files=["src/app.py"] if kind == "file" else [])
    sent = api.sent()[-1]
    labels = [label for row in sent["buttons"] for label, _ in row]
    assert labels == ["✅ 允许", "⛔ 拒绝", "💻 交给电脑"]
    assert all(len(data.encode()) <= 64 for row in sent["buttons"] for _, data in row)
    assert "Codex 命令" in sent["text"] if kind == "command" else "Codex 文件修改" in sent["text"]
    assert "example.com" in sent["text"] and "inputs" in sent["text"]
    assert state.get("open_permission_cards") == [card.message_id]
    assert bridge.status()["pending_approvals"] == 1
    record = next(record for record in audits if record["event"] == "codex_approval_pushed")
    assert record["kind"] == kind and not record["silent"]
    assert "python build.py" not in str(audits)


@pytest.mark.parametrize("fields", [
    {"command": "run SECRET"}, {"command": "x" * 10000}, {"title": "SECRET"}, {"cwd": "SECRET"},
])
def test_codex_approval_requires_view_for_hidden_or_truncated_input(setup, fields) -> None:
    bridge, api, *_ = setup
    card = _approval_card(bridge, **fields)
    assert card.needs_view and not card.viewed
    assert api.sent()[-1]["buttons"][0][0][0] == "📄 查看全文"
    assert _tap_approval(bridge, card, "a") == "请先查看全文"
    assert _tap_approval(bridge, card, "v") == "已发送全文"
    assert card.viewed and api.sent("send_document")
    assert api.sent("edit_message")[-1]["buttons"][0][0][0] == "✅ 允许"
    assert "SECRET" not in str(api.calls)
    assert "按钮已过期" in _tap_approval(bridge, card, "a", version=0)


def test_codex_approval_exact_secret_with_quotes_requires_view(setup) -> None:
    bridge, api, *_ = setup
    secret = 'embedded "quoted" secret'
    bridge.redact_light = lambda text: text.replace(secret, "[hidden]")
    card = _approval_card(bridge, command="execute " + secret)
    assert card.needs_view and "[hidden]" in api.sent()[-1]["text"]
    assert "quoted" not in api.sent()[-1]["text"]


@pytest.mark.parametrize("op,decision,outcome", [("a", "accept", "✅ 已从 Telegram 允许"),
                                                ("d", "decline", "⛔ 已从 Telegram 拒绝")])
def test_codex_approval_decision_uses_counted_worker_and_closes_once(setup, monkeypatch, op, decision, outcome) -> None:
    bridge, api, state, codex, *_ = setup
    audits = _card_audits(monkeypatch, bridge)
    card = _approval_card(bridge)
    started = []
    start = bridge._start_exec

    def checked_start(target, *args, **kwargs):
        assert bridge.status()["executing"] == 1
        assert kwargs == {"_reserved": True}
        started.append(target)
        start(target, *args, **kwargs)

    monkeypatch.setattr(bridge, "_start_exec", checked_start)
    assert _tap_approval(bridge, card, op) == "正在发送"
    _run_threads()
    _drain(bridge)
    assert started and codex.approvals == [{"thread_id": THREAD, "method": codex_ipc.COMMAND_APPROVAL_METHOD,
        "item_id": "command-1", "turn_id": "turn-1", "approval_id": "approval-1", "digest": "params-digest",
        "request_id": 17, "decision": decision}]
    assert outcome in api.sent("edit_message")[-1]["text"] and not api.sent("edit_message")[-1]["buttons"]
    assert not bridge.codex_approvals and state.get("open_permission_cards") == []
    assert bridge.status()["executing"] == 0
    _tap_approval(bridge, card, op)
    _run_threads()
    assert len(codex.approvals) == 1
    assert next(record for record in audits if record["event"] == "codex_approval_decided")["decision"] == decision
    assert next(record for record in audits if record["event"] == "codex_approval_closed")["by"] == "telegram"


def test_codex_approval_leave_to_pc_is_single_use_without_decision(setup) -> None:
    bridge, api, _state, codex, *_ = setup
    card = _approval_card(bridge)
    assert _tap_approval(bridge, card, "l") == "已交给电脑"
    assert "已经处理过" in _tap_approval(bridge, card, "a")
    _drain(bridge)
    assert "已交给电脑处理" in api.sent("edit_message")[-1]["text"]
    assert not codex.approvals and not bridge.codex_approvals


@pytest.mark.parametrize("error,state,outcome", [
    (codex_ipc.AlreadyAnswered(mine=False), "closed", "已在电脑上处理"),
    (codex_ipc.OutcomeUnknown(), "sent", "可能已送达 Codex，正在确认"),
    (codex_ipc.CodexIpcError("失败 SECRET"), "open", "❌ 失败 ‹已脱敏›"),
])
def test_codex_approval_ipc_errors_have_safe_outcomes(setup, monkeypatch, error, state, outcome) -> None:
    bridge, api, _state, codex, *_ = setup
    audits = _card_audits(monkeypatch, bridge)
    codex.approval_error = error
    card = _approval_card(bridge)
    _tap_approval(bridge, card, "a")
    _run_threads()
    _drain(bridge)
    assert card.state == state and outcome in api.sent("edit_message")[-1]["text"]
    if state == "sent":
        assert not api.sent("edit_message")[-1]["buttons"]
        _tap_approval(bridge, card, "a")
        assert len(codex.approvals) == 1
        bridge.on_event(_approval(closed=True, by="pc"))
        _drain(bridge)
        assert not bridge.codex_approvals
    if state == "open":
        assert api.sent("edit_message")[-1]["buttons"][0][0][0] == "✅ 允许"
        assert next(record for record in audits if record["event"] == "codex_approval_failed")["error"] == "CodexIpcError"


@pytest.mark.parametrize("by,outcome", [("pc", "已在电脑上处理"), ("turn_ended", "这一轮已结束，审批作废"),
    ("desktop_gone", "Codex Desktop 重启或退出了，审批作废"), ("gone", "对话已归档或删除")])
def test_codex_approval_source_close_reason(setup, monkeypatch, by, outcome) -> None:
    bridge, api, state, *_ = setup
    audits = _card_audits(monkeypatch, bridge)
    card = _approval_card(bridge)
    bridge.on_event(_approval(closed=True, by=by))
    _drain(bridge)
    assert card.state == "closed" and not bridge.codex_approvals
    assert outcome in api.sent("edit_message")[-1]["text"]
    assert not state.get("open_permission_cards")
    assert next(record for record in audits if record["event"] == "codex_approval_closed")["by"] == by


def test_codex_approval_lock_closes_on_send_thread_and_refuses_taps(setup) -> None:
    bridge, api, _state, codex, *_ = setup
    card = _approval_card(bridge)
    edits = list(api.sent("edit_message"))
    bridge._lock_writes("test security lock")
    assert api.sent("edit_message") == edits
    assert "写操作已锁定" in _tap_approval(bridge, card, "d")
    _drain(bridge)
    assert "🔒 写操作已锁定，请在电脑上处理" in api.sent("edit_message")[-1]["text"]
    assert not bridge.codex_approvals and not codex.approvals


def test_codex_approval_restart_stale_marking_preserves_live_cards(setup) -> None:
    bridge, api, state, *_ = setup
    card = _approval_card(bridge)
    state.put("open_permission_cards", [55, card.message_id])
    bridge._mark_stale_cards()
    _drain(bridge)
    assert state.get("open_permission_cards") == [card.message_id]
    assert any(edit["message_id"] == 55 and "卡片已失效" in edit["text"] for edit in api.sent("edit_message"))
    assert not any(edit["message_id"] == card.message_id for edit in api.sent("edit_message"))


@pytest.mark.parametrize("muted", [False, True])
def test_codex_approval_presence_and_mute_control_one_reminder(setup, muted) -> None:
    bridge, api, state, _codex, _claude, presence, _now = setup
    presence.value = True
    if muted:
        state.set_muted(session_key(CODEX, THREAD), True)
    card = _approval_card(bridge)
    assert api.sent()[-1]["silent"]
    presence.value = False
    bridge._remind_codex_approvals()
    _drain(bridge)
    reminders = [sent for sent in api.sent() if "还在等待审批" in sent["text"]]
    assert len(reminders) == (0 if muted else 1)
    bridge._remind_codex_approvals()
    _drain(bridge)
    assert [sent for sent in api.sent() if "还在等待审批" in sent["text"]] == reminders
    assert card.reminded


@pytest.mark.parametrize("reason", ["excluded", "locked"])
def test_codex_approval_content_is_hidden_when_excluded_or_locked(setup, reason) -> None:
    bridge, api, state, *_ = setup
    if reason == "locked":
        bridge._lock_writes("test")
    bridge.on_event(_approval(command="sensitive-command", excluded=reason == "excluded"))
    _drain(bridge)
    text = api.sent()[-1]["text"]
    assert "在等待审批" in text and "请在电脑上处理" in text
    assert ("content_exclude" if reason == "excluded" else "写操作已锁定") in text
    assert "sensitive-command" not in str(api.calls) and not bridge.codex_approvals


def test_codex_approval_permanent_push_failure_falls_back_once(setup, monkeypatch) -> None:
    bridge, api, *_ = setup
    audits = _card_audits(monkeypatch, bridge)
    send = bridge._send

    def fail_card(html, **options):
        if options.get("buttons"):
            raise TelegramError("can't send card", code=400)
        return send(html, **options)

    monkeypatch.setattr(bridge, "_send", fail_card)
    bridge._push_codex_approval(_approval(command="sensitive-command"))
    assert not bridge.codex_approvals
    assert len(api.sent()) == 1 and "卡片发不出去，请在电脑上处理" in api.sent()[0]["text"]
    assert "sensitive-command" not in str(api.calls)
    assert next(record for record in audits if record["event"] == "approval_push_failed")["kind"] == "command"


def test_codex_approval_transient_push_failure_can_retry(setup, monkeypatch) -> None:
    bridge, api, *_ = setup
    send = bridge._send
    failures = [TelegramError("network", code=None)]

    def fail_once(html, **options):
        if options.get("buttons") and failures:
            raise failures.pop()
        return send(html, **options)

    monkeypatch.setattr(bridge, "_send", fail_once)
    with pytest.raises(TelegramError):
        bridge._push_codex_approval(_approval())
    assert not bridge.codex_approvals
    bridge._push_codex_approval(_approval())
    assert len(bridge.codex_approvals) == 1 and len(api.sent()) == 1


def test_codex_approval_cannot_be_answered_by_claude_allow_all(setup) -> None:
    bridge, api, _state, codex, *_ = setup
    card = _approval_card(bridge)
    assert "处理过" in bridge._on_permission_choice(card.nonce, "Y", {"id": OWNER})
    assert "按钮已失效" in _tap_approval(bridge, card, "Y")
    assert not codex.approvals and card.state == "open" and not bridge.allow_all


def test_codex_approval_rejects_unknown_user_and_duplicate_snapshot(setup) -> None:
    bridge, api, _state, codex, *_ = setup
    card = _approval_card(bridge)
    count = len(api.sent())
    bridge.on_event(_approval())
    _drain(bridge)
    assert len(api.sent()) == count
    assert _tap_approval(bridge, card, "a", user=99) == "无权操作"
    assert card.state == "open" and not codex.approvals


def test_codex_approval_pc_close_during_write_keeps_telegram_outcome(setup, monkeypatch) -> None:
    bridge, api, _state, codex, *_ = setup
    started, release = threading.Event(), threading.Event()

    def wait_for_close(thread_id, **fields):
        codex.approvals.append({"thread_id": thread_id, **fields})
        started.set()
        assert release.wait(5)

    monkeypatch.setattr(codex, "decide_approval", wait_for_close)
    card = _approval_card(bridge)
    try:
        _tap_approval(bridge, card, "a")
        assert started.wait(5)
        bridge.on_event(_approval(closed=True, by="pc"))
        _drain(bridge)
        assert card.state == "sending" and card.pending_by == "pc"
    finally:
        release.set()
        _run_threads()
    _drain(bridge)
    assert card.state == "closed" and "已从 Telegram 允许" in api.sent("edit_message")[-1]["text"]


@pytest.mark.parametrize("already_tapped", [False, True])
def test_codex_approval_lock_unlock_never_revives_old_nonce_or_queued_write(setup, monkeypatch, already_tapped) -> None:
    bridge, api, state, codex, *_ = setup
    queued = []
    start = bridge._start_exec
    monkeypatch.setattr(bridge, "_start_exec", lambda target, *args, **kwargs: queued.append((target, args, kwargs)))
    card = _approval_card(bridge)
    if already_tapped:
        _tap_approval(bridge, card, "a")
        assert card.state == "sending" and bridge.status()["executing"] == 1
    bridge._lock_writes("owner lock")
    assert card.invalidated
    state.set_locked(False)
    if not already_tapped:
        assert "写操作已锁定" in _tap_approval(bridge, card, "a")
        assert not queued
    for target, args, kwargs in queued:
        start(target, *args, **kwargs)
    _run_threads()
    _drain(bridge)
    assert not codex.approvals and not bridge.codex_approvals and bridge.status()["executing"] == 0
    assert "写操作已锁定，请在电脑上处理" in api.sent("edit_message")[-1]["text"]


@pytest.mark.parametrize("by", ["pc", "turn_ended", "desktop_gone", "gone"])
def test_codex_approval_source_close_revokes_worker_that_has_not_started(setup, monkeypatch, by) -> None:
    bridge, api, _state, codex, *_ = setup
    queued = []
    start = bridge._start_exec
    monkeypatch.setattr(bridge, "_start_exec", lambda target, *args, **kwargs: queued.append((target, args, kwargs)))
    card = _approval_card(bridge)
    _tap_approval(bridge, card, "a")
    bridge.on_event(_approval(closed=True, by=by))
    _drain(bridge)
    assert card.state == "closed"
    for target, args, kwargs in queued:
        start(target, *args, **kwargs)
    _run_threads()
    _drain(bridge)
    assert not codex.approvals and bridge.status()["executing"] == 0


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_codex_async_app_tools_delivery_failure_queues_the_card_edit(setup, status) -> None:
    bridge, api, state, codex, *_ = setup
    codex.async_answer_result = {"via": "app_tools", "job_id": ASYNC_JOB}
    card = _async_card(bridge, questions=ASYNC_QUESTIONS[:1])
    _submit_async_choice(bridge, card)
    assert card.state == "sent" and card.via == "app_tools" and card.job_id == ASYNC_JOB
    assert "这一轮已结束，答案作为新消息发出，等待 Codex 记录" in api.sent("edit_message")[-1]["text"]
    assert len(bridge.deliveries) == 1 and bridge.deliveries[0].kind == "codex_async"
    assert bridge.deliveries[0].job_id == ASYNC_JOB and state.get("async_answers")[0]["job_id"] == ASYNC_JOB
    codex.job_result = {"state": {"status": status}, "result": {"error": "发送 SECRET 失败"}}
    edits, version = list(api.sent("edit_message")), card.version
    bridge._check_deliveries()  # poll thread: Telegram I/O waits for the send thread
    assert api.sent("edit_message") == edits and card.state == "sent" and card.version == version
    _drain(bridge)
    assert card.state == "open" and card.version == version + 1
    assert "❌" in api.sent("edit_message")[-1]["text"] and "发送 ‹已脱敏› 失败" in api.sent("edit_message")[-1]["text"]
    assert api.sent("edit_message")[-1]["buttons"][0][0][0] == "📨 发送答案"
    assert not bridge.deliveries


@pytest.mark.parametrize("status,phase,tracked", [("running", "running_turn", False),
                                                ("succeeded", "delivered", False),
                                                ("running", "delivered", True)])
def test_codex_async_job_delivery_stops_tracking_once_delivered(setup, status, phase, tracked) -> None:
    # A delivered answer is the source's to attribute: the bridge must not stay busy for the
    # whole turn the answer started (Install/Restart wait for deliveries).
    bridge, api, _state, codex, *_ = setup
    codex.async_answer_result = {"via": "app_tools", "job_id": ASYNC_JOB}
    card = _async_card(bridge, questions=ASYNC_QUESTIONS[:1])
    _submit_async_choice(bridge, card)
    codex.job_result = {"state": {"status": status, "phase": phase}, "result": None}
    edits = list(api.sent("edit_message"))
    bridge._check_deliveries()
    _drain(bridge)
    assert len(bridge.deliveries) == (1 if tracked else 0)
    assert card.state == "sent" and api.sent("edit_message") == edits
    bridge.on_event(_async_question(closed=True, questions=[], by="answered", answer="方案 SECRET",
                                    answered={0: "方案 SECRET"}, answer_sources={0: card.marker}))
    _drain(bridge)
    bridge._check_deliveries()
    assert not bridge.deliveries


@pytest.mark.parametrize("error,footer", [(codex_ipc.OutcomeUnknown(), "可能已送达 Codex，正在确认"),
                                       (codex_ipc.AlreadyAnswered(mine=True), "上次的答案已送达")])
def test_codex_async_uncertain_or_duplicate_ipc_answer_stays_sent(setup, monkeypatch, error, footer) -> None:
    bridge, api, state, codex, *_ = setup
    audits = _card_audits(monkeypatch, bridge)
    codex.async_answer_error = error
    card = _async_card(bridge, questions=ASYNC_QUESTIONS[:1])
    _submit_async_choice(bridge, card)
    assert card.state == "sent" and card.tried and footer in api.sent("edit_message")[-1]["text"]
    assert not api.sent("edit_message")[-1].get("buttons") and not bridge.deliveries
    if isinstance(error, codex_ipc.OutcomeUnknown):
        assert next(record for record in audits if record["event"] == "question_async_sent")["via"] == "ipc_unconfirmed"
    assert state.get("async_answers")[0]["client_id"] == card.client_id


def test_codex_async_answer_by_someone_else_waits_for_source_close(setup) -> None:
    bridge, api, _state, codex, *_ = setup
    codex.async_answer_error = codex_ipc.AlreadyAnswered(mine=False)
    card = _async_card(bridge, questions=ASYNC_QUESTIONS[:1])
    _submit_async_choice(bridge, card)
    assert bridge.questions[card.nonce] is card and "这个问题已经有人回答了" in api.sent("edit_message")[-1]["text"]
    assert not card.closed


@pytest.mark.parametrize("error", [ValueError("对话已经继续"), codex_ipc.CodexIpcError("窗口没有响应")])
def test_codex_async_rejected_answer_reopens_and_audits_failure(setup, monkeypatch, error) -> None:
    bridge, api, _state, codex, *_ = setup
    audits = _card_audits(monkeypatch, bridge)
    codex.async_answer_error = error
    card = _async_card(bridge, questions=ASYNC_QUESTIONS[:1])
    _submit_async_choice(bridge, card)
    assert card.state == "open" and "❌" in api.sent("edit_message")[-1]["text"]
    assert str(error) in api.sent("edit_message")[-1]["text"]
    assert next(record for record in audits if record["event"] == "question_failed")["mode"] == "async"


@pytest.mark.parametrize("running", [False, True])
def test_codex_async_unconfirmed_answer_reopens_only_after_the_turn_ended(setup, monkeypatch, running) -> None:
    bridge, api, _state, codex, _claude, _presence, now = setup
    audits = _card_audits(monkeypatch, bridge)
    codex.async_answer_error = codex_ipc.OutcomeUnknown()
    card = _async_card(bridge, questions=ASYNC_QUESTIONS[:1])
    _submit_async_choice(bridge, card)
    assert codex.async_state is not None
    codex.async_state.update(turn_running=running, thread_busy=running)
    version, edits = card.version, list(api.sent("edit_message"))
    now[0] += 121
    bridge._housekeeping()
    assert card.state == "sent" and api.sent("edit_message") == edits
    _drain(bridge)
    if running:
        assert card.state == "sent" and card.version == version and api.sent("edit_message") == edits
        assert not any(record["event"] == "question_async_unconfirmed" for record in audits)
    else:
        assert card.state == "open" and card.version == version + 1
        assert "没有确认送达，可以再点一次 [📨 发送答案]" in api.sent("edit_message")[-1]["text"]
        assert next(record for record in audits if record["event"] == "question_async_unconfirmed")["mode"] == "async"


@pytest.mark.parametrize("st", [None, {"turn_running": False, "waiting": False}])
def test_codex_async_unconfirmed_answer_leaves_closing_to_the_source(setup, st) -> None:
    bridge, api, _state, codex, _claude, _presence, now = setup
    card = _async_card(bridge, questions=ASYNC_QUESTIONS[:1])
    _submit_async_choice(bridge, card)
    codex.async_state = st
    edits = list(api.sent("edit_message"))
    now[0] += 121
    bridge._housekeeping()
    _drain(bridge)
    assert card.state == "sent" and api.sent("edit_message") == edits


def test_codex_async_active_job_prevents_unconfirmed_reopening(setup) -> None:
    bridge, api, _state, codex, _claude, _presence, now = setup
    codex.async_answer_result = {"via": "app_tools", "job_id": ASYNC_JOB}
    card = _async_card(bridge, questions=ASYNC_QUESTIONS[:1])
    _submit_async_choice(bridge, card)
    assert codex.async_state is not None
    codex.async_state.update(turn_running=False, thread_busy=False)
    edits = list(api.sent("edit_message"))
    now[0] += 121
    bridge._housekeeping()
    _drain(bridge)
    assert card.state == "sent" and api.sent("edit_message") == edits and len(bridge.deliveries) == 1


def test_codex_async_persisted_answer_repush_starts_sent_and_tried(setup) -> None:
    bridge, api, state, _codex, _claude, _presence, now = setup
    record = {"session_id": THREAD, "call_id": "async_call", "indices": [0], "via": "ipc_unconfirmed",
              "client_id": "prior-client", "marker": "tg:Ab3x9_", "job_id": "", "at": now[0] - 20}
    state.put("async_answers", [record])
    card = _async_card(bridge)
    assert card.state == "sent" and card.tried and card.client_id == "prior-client" and card.marker == "tg:Ab3x9_"
    assert card.sent_at == record["at"] and card.via == "ipc_unconfirmed"
    assert "📨 答案已发出，等待 Codex 记录…" in api.sent()[-1]["text"] and not api.sent()[-1].get("buttons")


def test_codex_async_muted_topic_is_silent_and_never_reminds(setup) -> None:
    bridge, api, state, _codex, _claude, presence, _now = setup
    state.set_muted(session_key(CODEX, THREAD), True)
    card = _async_card(bridge)
    assert api.sent()[-1]["silent"] is True and card.reminded
    sent = list(api.sent())
    presence.value = True
    bridge._remind_questions()
    presence.value = False
    bridge._remind_questions()
    _drain(bridge)
    assert api.sent() == sent


@pytest.mark.parametrize("reason", ["locked", "excluded", "no_questions"])
def test_codex_async_unanswerable_question_sends_only_the_existing_notice(setup, reason) -> None:
    bridge, api, state, *_ = setup
    fields: dict[str, Any] = {}
    if reason == "locked":
        state.set_locked(True, bridge_module.OWNER_LOCK)
    elif reason == "excluded":
        fields["excluded"] = True
    else:
        fields["questions"] = []
    bridge.on_event(_async_question(**fields))
    _drain(bridge)
    expected = {"locked": "写操作已锁定", "excluded": "content_exclude", "no_questions": "问题的格式认不出来"}[reason]
    assert not bridge.questions and expected in api.sent()[-1]["text"] and not api.sent()[-1].get("buttons")


@pytest.mark.parametrize("error", [TelegramError("Bad Request", code=400), ValueError("invalid keyboard")])
def test_codex_async_permanent_push_failure_keeps_the_plain_note_fallback(setup, monkeypatch, error) -> None:
    bridge, api, *_ = setup
    audits = _card_audits(monkeypatch, bridge)
    if isinstance(error, TelegramError):
        _card_send_failures(monkeypatch, bridge, [error])
    else:
        def invalid_keyboard(_card: Any) -> Any:
            raise error

        monkeypatch.setattr(bridge, "_question_view", invalid_keyboard)
    bridge._push_question(_async_question())
    assert not bridge.questions and len(api.sent()) == 1 and api.sent()[0]["buttons"] is None
    assert "卡片发不出去，请在电脑上回答" in api.sent()[0]["text"]
    assert next(record for record in audits if record["event"] == "question_push_failed")["mode"] == "async"


@pytest.mark.parametrize("mode", ["async", "blocking"])
def test_plain_message_wait_note_applies_only_to_blocking_codex_questions(setup, mode) -> None:
    bridge, api, state, *_ = setup
    key = session_key(CODEX, THREAD)
    state.set_topic(key, 501, "t")
    bridge.on_event(_async_question() if mode == "async" else _codex_question())
    _drain(bridge)
    bridge._request_send(_message("先继续调查", thread_id=501), key, "先继续调查")
    assert bool(bridge.pending)
    assert ("要等问题答完" in api.sent()[-1]["text"]) is (mode == "blocking")


def test_codex_async_delivery_closed_before_a_queued_failure_edit_stays_closed(setup) -> None:
    bridge, api, _state, codex, *_ = setup
    codex.async_answer_result = {"via": "app_tools", "job_id": ASYNC_JOB}
    card = _async_card(bridge, questions=ASYNC_QUESTIONS[:1])
    _submit_async_choice(bridge, card)
    codex.job_result = {"state": {"status": "failed", "error": "late failure"}, "result": None}
    bridge._check_deliveries()  # queue a failure, then the source closes the card first
    bridge._push_question(_async_question(closed=True, questions=[], by="answered", answer="方案 SECRET",
                                          answer_sources={0: f"client:{card.client_id}"}))
    final = api.sent("edit_message")[-1]
    assert "✅ 已从 Telegram 回答" in final["text"]
    _drain(bridge)
    assert card.closed and not bridge.questions and api.sent("edit_message")[-1] == final
    assert not any("late failure" in edit["text"] for edit in api.sent("edit_message"))


def test_codex_async_close_while_the_answer_is_in_flight_does_not_reopen_or_persist_it(setup, monkeypatch) -> None:
    bridge, api, state, codex, *_ = setup
    entered, release = threading.Event(), threading.Event()
    answer = codex.answer_async_question

    def wait_for_close(*args: Any, **kwargs: Any) -> dict[str, Any]:
        entered.set()
        assert release.wait(5), "the test did not release its synthetic async send"
        return answer(*args, **kwargs)

    monkeypatch.setattr(codex, "answer_async_question", wait_for_close)
    card = _async_card(bridge, questions=ASYNC_QUESTIONS[:1])
    _tap_q(bridge, card, "o0", 1)
    _tap_q(bridge, card, "s", 2)
    try:
        assert entered.wait(2)
        assert bridge.status()["executing"] == 1 and card.state == "sending"
        bridge.on_event(_async_question(closed=True, questions=[], by="answered", answer="方案 SECRET",
                                        answer_sources={0: f"client:{card.client_id}"}))
        _drain(bridge)
        final = api.sent("edit_message")[-1]
        assert "✅ 已从 Telegram 回答" in final["text"] and card.closed
    finally:
        release.set()
        _run_threads()
    _drain(bridge)
    assert bridge.status()["executing"] == 0 and not bridge.deliveries and not bridge.questions
    assert not state.get("async_answers", []) and api.sent("edit_message")[-1] == final


def test_codex_async_answer_persistence_keeps_only_the_newest_200_without_answer_text(setup) -> None:
    bridge, _api, state, _codex, _claude, _presence, now = setup
    older = [{"session_id": THREAD, "call_id": f"older-{index}", "indices": [0], "via": "ipc",
              "client_id": f"client-{index}", "marker": "tg:abcdef", "job_id": "", "at": now[0] - 201 + index}
             for index in range(200)]
    state.put("async_answers", older)
    card = _async_card(bridge, questions=ASYNC_QUESTIONS[:1])
    _submit_async_choice(bridge, card)
    saved = state.get("async_answers")
    assert len(saved) == 200 and saved[0] == older[1] and saved[-1]["call_id"] == "async_call"
    assert "SECRET" not in str(saved) and "选择方案" not in str(saved)


def test_codex_async_answer_persistence_failure_does_not_reopen_a_sent_answer(setup, monkeypatch) -> None:
    bridge, api, state, _codex, *_ = setup
    card = _async_card(bridge, questions=ASYNC_QUESTIONS[:1])
    put = state.put

    def cannot_save(name: str, value: Any) -> None:
        if name == "async_answers":
            raise OSError("synthetic state disk error")
        put(name, value)

    monkeypatch.setattr(state, "put", cannot_save)
    _submit_async_choice(bridge, card)
    assert card.state == "sent" and "已插进正在运行的这一轮" in api.sent("edit_message")[-1]["text"]
    assert "synthetic state disk error" in bridge.log.text_path.read_text(encoding="utf-8")


def test_blocking_codex_question_audits_keep_the_blocking_mode(setup, monkeypatch) -> None:
    bridge, _api, *_ = setup
    audits = _card_audits(monkeypatch, bridge)
    bridge.on_event(_codex_question(questions=CODEX_QUESTIONS[:1]))
    _drain(bridge)
    card = next(iter(bridge.questions.values()))
    _tap_q(bridge, card, "o0", 1)
    _tap_q(bridge, card, "s", 2)
    _run_threads()
    bridge.on_event(_codex_question(closed=True, questions=[], by="answered", answer="范围: P1 优先（推荐）"))
    _drain(bridge)
    for name in ("question_pushed", "question_submitted", "question_answered", "question_closed"):
        assert next(record for record in audits if record["event"] == name)["mode"] == "blocking"
    assert next(record for record in audits if record["event"] == "question_closed")["by"] == "answered"


def test_a_blocking_question_wait_note_survives_an_earlier_async_card(setup) -> None:
    bridge, api, state, *_ = setup
    key = session_key(CODEX, THREAD)
    state.set_topic(key, 501, "t")
    _async_card(bridge)
    bridge.on_event(_codex_question("blocking_call"))
    _drain(bridge)
    bridge._request_send(_message("先继续调查", thread_id=501), key, "先继续调查")
    assert "要等问题答完" in api.sent()[-1]["text"]


@pytest.mark.parametrize("mode", ["async", "blocking"])
def test_codex_question_confirmation_counts_as_executing_during_telegram_edit(setup, monkeypatch, mode) -> None:
    bridge, _api, _state, codex, *_ = setup
    event = _async_question(questions=ASYNC_QUESTIONS[:1]) if mode == "async" else \
        _codex_question(questions=CODEX_QUESTIONS[:1])
    bridge.on_event(event)
    _drain(bridge)
    card = next(iter(bridge.questions.values()))
    edit = bridge._edit_open_card
    counts: list[int] = []

    def inspect_sending_edit(target: Any, html: str, buttons: Any = None) -> None:
        if target is card and card.state == "sending":
            counts.append(bridge.status()["executing"])
        edit(target, html, buttons)

    monkeypatch.setattr(bridge, "_edit_open_card", inspect_sending_edit)
    _tap_q(bridge, card, "o0", 1)
    _tap_q(bridge, card, "s", 2)
    _run_threads()
    _drain(bridge)
    assert counts == [1]  # the confirmed write already exists while Telegram edits the card
    assert bridge.status()["executing"] == 0 and card.state == "sent"
    assert len(codex.async_answered if mode == "async" else codex.answered) == 1


@pytest.mark.parametrize("mode", ["async", "blocking"])
def test_codex_question_telegram_edit_failure_does_not_lose_a_confirmed_send(setup, monkeypatch, mode) -> None:
    bridge, _api, _state, codex, *_ = setup
    event = _async_question(questions=ASYNC_QUESTIONS[:1]) if mode == "async" else \
        _codex_question(questions=CODEX_QUESTIONS[:1])
    bridge.on_event(event)
    _drain(bridge)
    card = next(iter(bridge.questions.values()))
    edit = bridge._edit_open_card
    counts: list[int] = []

    def fail_sending_edit(target: Any, html: str, buttons: Any = None) -> None:
        if target is card and card.state == "sending":
            counts.append(bridge.status()["executing"])
            raise RuntimeError("synthetic sending-card edit failed")
        edit(target, html, buttons)

    monkeypatch.setattr(bridge, "_edit_open_card", fail_sending_edit)
    _tap_q(bridge, card, "o0", 1)
    try:
        _tap_q(bridge, card, "s", 2)
    except RuntimeError as exc:
        assert str(exc) == "synthetic sending-card edit failed"
    _run_threads()
    _drain(bridge)
    assert counts == [1] and bridge.status()["executing"] == 0
    assert len(codex.async_answered if mode == "async" else codex.answered) == 1 and card.state == "sent"


def test_a_lock_close_keeps_the_saved_async_answer_until_the_source_closes(setup) -> None:
    bridge, _api, state, codex, *_ = setup
    codex.async_answer_result = {"via": "ipc", "turn_id": "t1", "other_turn": False}
    card = _async_card(bridge, questions=ASYNC_QUESTIONS[:1])
    _submit_async_choice(bridge, card)
    assert bridge._saved_async_answer(card.session_id, card.request_id) is not None
    bridge._close_codex_cards()
    _drain(bridge)
    # The question still waits in Codex: after a restart its card must start as sent, not open.
    assert bridge._saved_async_answer(card.session_id, card.request_id) is not None
    bridge.on_event(_async_question(closed=True, questions=[], by="answered", answer="方案",
                                    answered={0: "方案"}, answer_sources={0: f"client:{card.client_id}"}))
    _drain(bridge)
    assert bridge._saved_async_answer(card.session_id, card.request_id) is None


def test_a_backlog_of_handled_updates_below_the_offset_is_confirmed(setup) -> None:
    # observed, 2026-10-11..15: ids restarted lower, Telegram ignored the old higher offset and
    # returned the same oldest 100 updates on every poll, so new taps never arrived.
    bridge, api, state, *_ = setup
    state.mark_update(951_800_136)
    old = [{"update_id": 313_218_000 + index, "message": _message("/list")} for index in range(3)]
    for update in old:
        state.mark_update(update["update_id"])
    state._offset = 951_800_137  # what the old code left behind
    offsets: list[int | None] = []
    batches = [old, old[2:] + [{"update_id": 313_218_003, "message": _message("/list")}]]

    def get_updates(offset, **options):
        offsets.append(offset)
        return batches.pop(0) if batches else []

    api.get_updates = get_updates  # type: ignore[method-assign]
    assert bridge.poll_updates_once(timeout_s=0) is None
    assert state.update_offset == 313_218_003 and not api.sent()  # seen ones confirmed, not handled again
    assert bridge.poll_updates_once(timeout_s=0) is None
    assert offsets == [951_800_137, 313_218_003]
    assert len(api.sent()) == 1 and state.update_offset == 313_218_004  # the new update was handled

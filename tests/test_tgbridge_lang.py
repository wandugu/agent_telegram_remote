from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent_telegram import i18n
from agent_telegram.tgbridge import bridge as bridge_module, config as config_module
from agent_telegram.tgbridge.bridge import Bridge, BridgeLog, OWNER_LOCK
from agent_telegram.tgbridge.config import Config
from agent_telegram.tgbridge.model import CODEX, ApprovalEvent, Question, QuestionEvent, TurnEvent, session_key
from agent_telegram.tgbridge.state import BridgeState
from agent_telegram.tgbridge.telegram_api import TelegramError
from test_tgbridge_bridge import CHAT, OWNER, BOT_ID, THREAD, FakeAPI, FakeCodex, FakePresence, _message, _drain

CJK = re.compile(r"[㐀-鿿]")


class LanguageAPI(FakeAPI):
    def set_commands(self, commands, *, scope=None):
        self._record("set_commands", commands=commands, scope=scope)


@pytest.fixture
def bridge(tmp_path):
    # Reuse the bridge suite's fake API/source, with English session content.
    with i18n.use("zh"):
        cfg = Config(machine="pc1", chat_id=CHAT, allowed_user_ids=[OWNER])
        api = LanguageAPI()
        instance = Bridge(cfg, BridgeState(tmp_path / "state.json"), api, redact=lambda text: text,
                          presence=FakePresence(False), log=BridgeLog(tmp_path / "logs"),
                          clock=lambda: 1_790_800_000.0, synchronous=True)
        instance.me = api.get_me()
        codex = FakeCodex()
        codex.title = "Review"
        instance.attach_sources(codex=codex)
        yield instance


def send(bridge, text, **fields):
    bridge.handle_update({"message": _message(text, **fields)})


def tap(bridge, data, *, user=OWNER, bot=BOT_ID, chat=CHAT):
    bridge.handle_update({"callback_query": {
        "id": "language-button", "from": {"id": user}, "data": data,
        "message": {"message_id": 101, "chat": {"id": chat}, "from": {"id": bot}},
    }})


def config_path():
    path = config_module.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_lang_broadcast_query_is_one_silent_line_with_machine_buttons(bridge):
    # No topic, reply, or @bot target: this must reach every machine's bot.
    assert "lang" in bridge_module.BROADCAST_COMMANDS
    send(bridge, "/lang")
    result = bridge.api.sent()[-1]
    assert result["silent"] and "pc1" in result["text"] and "zh" in result["text"]
    assert "\n" not in result["text"]
    assert result["buttons"] == [[("English", "lang:en:42"), ("中文", "lang:zh:42")]]
    assert not bridge.pending and not config_module.config_path().exists()


@pytest.mark.parametrize(("value", "expected"), [
    ("en", "en"), ("ENGLISH", "en"), ("zh", "zh"), ("CN", "zh"),
    ("zh-CN", "zh"), ("ZH_cn", "zh"), ("中文", "zh"),
])
def test_lang_broadcast_switch_aliases_persist_and_confirm_in_new_language(bridge, value, expected):
    i18n.set_language("zh" if expected == "en" else "en")
    send(bridge, "/lang " + value)
    assert i18n.current() == bridge.cfg.language == expected
    assert json.loads(config_path().read_text(encoding="utf-8")) == {"language": expected}
    result = bridge.api.sent()[-1]
    assert result["silent"] and expected in result["text"]
    assert bool(CJK.search(result["text"])) == (expected == "zh")
    assert not bridge.pending and bridge.status()["executing"] == 0
    audit = [json.loads(line) for line in bridge.log.audit_path.read_text(encoding="utf-8").splitlines()]
    entry = next(row for row in audit if row["event"] == "language_changed")
    assert (entry["language"], entry["machine"], entry["user_id"]) == (expected, "pc1", OWNER)


def test_lang_rereads_disk_and_preserves_every_other_key(bridge):
    path = config_path()
    original = {"language": "zh", "chat_id": -1009, "allowed_user_ids": [71], "proxy": "direct",
                "future_key": {"keep": [False, 0, "untouched"]}, "recent_n": 17}
    path.write_text(json.dumps(original), encoding="utf-8")
    bridge.cfg.recent_n = 3
    send(bridge, "/lang en")
    assert json.loads(path.read_text(encoding="utf-8")) == {**original, "language": "en"}


def test_lang_reregisters_the_same_scoped_menu_in_the_new_language(bridge):
    bridge._set_commands()
    old = bridge.api.sent("set_commands")[-1]
    send(bridge, "/lang en")
    new = bridge.api.sent("set_commands")[-1]
    assert old["scope"] == new["scope"] == {"type": "chat", "chat_id": CHAT}
    assert dict(old["commands"])["help"] == "帮助"
    assert dict(new["commands"])["help"] == "Help" and "lang" in dict(new["commands"])
    assert all(not CJK.search(description) for _, description in new["commands"])


@pytest.mark.parametrize("reason", [OWNER_LOCK, "security lock"])
def test_lang_works_while_locked_without_unlock_or_confirmation(bridge, reason):
    bridge.state.set_locked(True, reason)
    send(bridge, "/lang")
    assert bridge.api.sent()[-1]["buttons"]
    send(bridge, "/lang en")
    assert i18n.current() == "en" and bridge.state.locked
    assert bridge.state.locked_reason == reason and not bridge.pending


@pytest.mark.parametrize("value", ["fr", "en zh", "unknown"])
def test_lang_unknown_value_replies_with_usage_without_changing_state(bridge, value):
    send(bridge, "/lang " + value)
    assert "/lang [en|zh]" in bridge.api.sent()[-1]["text"]
    assert i18n.current() == "zh" and not config_module.config_path().exists()
    assert not bridge.api.sent("set_commands") and not bridge.pending


@pytest.mark.parametrize("language", ["en", "zh"])
def test_lang_buttons_switch_only_this_machine_and_work_while_locked(bridge, language):
    i18n.set_language("zh" if language == "en" else "en")
    bridge.state.set_locked(True, "security lock")
    send(bridge, "/lang")
    data = next(data for row in bridge.api.sent()[-1]["buttons"] for _, data in row
                if data.startswith("lang:" + language + ":"))
    tap(bridge, data)
    assert i18n.current() == language
    assert json.loads(config_path().read_text(encoding="utf-8"))["language"] == language
    assert bridge.api.sent("answer_callback") and bridge.state.locked and not bridge.pending


@pytest.mark.parametrize("kind", ["message", "callback"])
def test_lang_non_whitelisted_user_cannot_switch(bridge, kind):
    if kind == "message":
        send(bridge, "/lang en", user=99)
    else:
        tap(bridge, "lang:en:99", user=99)
    assert i18n.current() == "zh" and not config_module.config_path().exists()
    assert not bridge.api.sent("set_commands")


@pytest.mark.parametrize("kind", ["foreign_chat", "other_bot", "other_owner"])
def test_lang_keeps_callback_chat_bot_and_requester_ownership_checks(bridge, kind):
    bridge.cfg.allowed_user_ids.append(43)
    options = {"foreign_chat": {"chat": -1009}, "other_bot": {"bot": 901},
               "other_owner": {"user": 43}}[kind]
    tap(bridge, "lang:en:42", **options)
    assert i18n.current() == "zh" and not config_module.config_path().exists()
    assert not bridge.api.sent("set_commands")


@pytest.mark.parametrize("kind", ["foreign_chat", "other_bot", "bot_sender", "via_bot"])
def test_lang_keeps_message_validation(bridge, kind):
    options = {"foreign_chat": {"chat": -1009}, "other_bot": {},
               "bot_sender": {"from": {"id": OWNER, "is_bot": True}}, "via_bot": {"via_bot": {"id": 5}}}[kind]
    send(bridge, "/lang@other_bot en" if kind == "other_bot" else "/lang en", **options)
    assert i18n.current() == "zh" and not config_module.config_path().exists()
    assert not bridge.api.sent("set_commands")


def test_lang_unknown_button_value_gets_usage(bridge):
    tap(bridge, "lang:fr:42")
    assert "/lang [en|zh]" in bridge.api.sent()[-1]["text"]
    assert i18n.current() == "zh"


@pytest.mark.parametrize("content", ["{broken", "[]"])
def test_lang_damaged_config_is_not_overwritten_or_reported_as_success(bridge, content):
    path = config_path()
    path.write_text(content, encoding="utf-8")
    send(bridge, "/lang en")
    assert path.read_text(encoding="utf-8") == content and i18n.current() == "zh"
    assert not bridge.api.sent("set_commands")


def test_lang_menu_failure_does_not_lose_the_saved_language(bridge, monkeypatch):
    def failed(*args, **kwargs):
        raise TelegramError("offline")
    monkeypatch.setattr(bridge.api, "set_commands", failed)
    send(bridge, "/lang en")
    assert i18n.current() == "en"
    assert json.loads(config_path().read_text(encoding="utf-8"))["language"] == "en"
    assert "Language changed to en" in bridge.api.sent()[-1]["text"]


@pytest.mark.parametrize("command", ["help", "list", "status", "lock", "unlock", "unknown"])
def test_english_command_output_contains_no_cjk(bridge, command):
    with i18n.use("en"):
        send(bridge, "/" + command + "@pc1_bot")
        sent = bridge.api.sent()
        assert sent
        if command == "help":
            assert "/lang" in sent[-1]["text"]
        if command == "unlock":
            assert bridge.pending and sent[-1]["buttons"]
        assert_no_cjk(bridge.api)


def assert_no_cjk(api):
    for method, fields in api.calls:
        if method in {"send_message", "edit_message", "answer_callback", "create_topic"}:
            for name in ("text", "name"):
                assert not CJK.search(fields.get(name, "")), (method, fields)
            for row in fields.get("buttons") or []:
                for label, _ in row:
                    assert not CJK.search(label), label


def test_english_send_confirmation_card(bridge):
    key = session_key(CODEX, THREAD)
    bridge.state.set_topic(key, 501, "Review")
    with i18n.use("en"):
        send(bridge, "Please run the checks", thread_id=501)
        assert bridge.pending and bridge.api.sent()[-1]["buttons"]
        assert_no_cjk(bridge.api)


def test_english_turn_push_and_plan_buttons(bridge):
    with i18n.use("en"):
        bridge.on_event(TurnEvent(CODEX, THREAD, "Review", "C:/proj/example", "completed",
                                  final_text="The checks pass.", plan=True, turn_id="t1"))
        _drain(bridge)
        assert bridge.api.sent() and bridge.api.sent()[-1]["buttons"]
        assert_no_cjk(bridge.api)


@pytest.mark.parametrize("kind", ["command", "file"])
def test_english_approval_card(bridge, kind):
    with i18n.use("en"):
        bridge.on_event(ApprovalEvent(CODEX, THREAD, "Build", "C:/proj/example", 17,
                        "item/commandExecution/requestApproval", "item-1", "t1", None, "digest", kind,
                        command="python build.py", reason="Run checks", files=["result.txt"]))
        _drain(bridge)
        assert bridge.codex_approvals and bridge.api.sent()[-1]["buttons"]
        assert_no_cjk(bridge.api)


@pytest.mark.parametrize("mode", ["blocking", "async"])
def test_english_question_card(bridge, mode):
    with i18n.use("en"):
        bridge.on_event(QuestionEvent(CODEX, THREAD, "Review", "C:/proj/example", "call-1", "t1",
                        questions=[Question("0", "Which option?", "Choice", [("A", "First")])], mode=mode))
        _drain(bridge)
        assert bridge.questions and bridge.api.sent()[-1]["buttons"]
        assert_no_cjk(bridge.api)


def test_english_outcomes_are_resolved_after_import(bridge):
    with i18n.use("en"):
        assert all(not CJK.search(text) for text in bridge_module._permission_outcomes().values())
        assert all(not CJK.search(text) for text in bridge_module._question_outcomes().values())

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from conftest import thinking_signature
from test_tgbridge_claude_source import NOW, assistant, text, tool_result

from agent_telegram.tgbridge import progress


def narration(value: str) -> dict[str, Any]:
    return {"type": "thinking", "thinking": value, "signature": thinking_signature("narration")}


def thinking(value: str) -> dict[str, Any]:
    return {"type": "thinking", "thinking": value, "signature": thinking_signature("thinking")}


# --- progress.py --------------------------------------------------------------------------


def test_block_kind_reads_the_tag_and_fails_closed() -> None:
    assert progress.block_kind(thinking_signature("narration")) == "narration"
    assert progress.block_kind(thinking_signature("thinking")) == "thinking"
    for bad in ("", None, "!!!not base64", base64.b64encode(b"\xff\xff\xff").decode(),
                thinking_signature("narration")[:12], base64.b64encode(bytes([0x12, 0x05, 0x0a, 0x05, 0x61, 0x62, 0x63])).decode()):
        assert progress.block_kind(bad) is None


@pytest.mark.parametrize(("value", "expected"), [
    ("我已打开登录页，请用 demo@example.com 登录。", True),
    ("接下来需要你确定目录结构。", True),
    ("Could you log in to Figma?", True),
    ("这样可以吗？", True),
    ("请求失败了，我换一种方法。", False),
    ("测试下拉框能否稳定弹出。", False),
    ("Let me check the logs.", False),
    ("我再确认一下配置有没有生效。", False),
    ("界面上显示 `请检查网络连接`，说明代理没起来。", False),
    ("弹窗写着“请先登录”，我换个账号试试。", False),
])
def test_asks_user(value: str, expected: bool) -> None:
    assert progress.asks_user(value) is expected


def test_claude_items_keep_only_what_the_user_sees_mid_turn() -> None:
    mid = lambda *blocks: assistant("m", list(blocks), "tool_use", NOW)  # noqa: E731

    def seen(record: dict[str, Any]) -> list[tuple[str, str]]:
        return [(kind, value) for kind, value, _digest in progress.claude_items(record)]

    assert progress.claude_items(mid(text("先读代码"))) == [("text", "先读代码", "")]
    assert seen(mid(narration("请登录"))) == [("narration", "请登录")]
    assert seen(mid(thinking("private reasoning"))) == []
    assert seen(mid({"type": "thinking", "thinking": "no signature"})) == []
    assert seen(mid({"type": "tool_use", "id": "u", "name": "SendUserMessage",
                     "input": {"message": "结果是 42"}})) == [("message", "结果是 42")]
    question = {"type": "tool_use", "id": "q", "name": "AskUserQuestion", "input": {"questions": [
        {"question": "放到哪个目录？", "options": [{"label": "D:\\out"}, {"label": "C:\\out"}]}]}}
    digest = progress.interactive_digest("AskUserQuestion", question["input"])
    assert digest and progress.claude_items(mid(question)) == [
        ("question", "放到哪个目录？\n「D:\\out」 / 「C:\\out」\n\n（请在电脑上回答）", digest)]
    # The hook's input names the same call: it carries the same questions.
    assert progress.interactive_digest("AskUserQuestion", {**question["input"], "answers": {}}) == digest
    plan = {"type": "tool_use", "id": "p", "name": "ExitPlanMode", "input": {"plan": "1. 改代码"}}
    assert progress.claude_items(mid(plan)) == [("question", "计划等你批准（请在电脑上处理）：\n1. 改代码",
                                                 progress.interactive_digest("ExitPlanMode", {"plan": "1. 改代码"}))]
    assert progress.interactive_digest("ExitPlanMode", {"plan": "1. 改代码"}) != digest
    assert progress.interactive_digest("Bash", {"command": "ls"}) == ""
    assert seen(mid({"type": "tool_use", "id": "b", "name": "Bash", "input": {}})) == []
    assert seen(assistant("m", [text("最终答复")], "end_turn", NOW)) == []
    assert seen(assistant("m", [text("子代理")], "tool_use", NOW, side=True)) == []
    assert seen(tool_result(NOW)) == []


def test_summarize_puts_the_most_pressing_kind_first() -> None:
    assert progress.summarize([("text", "看了代码"), ("message", "好了")]) == ("message", True)
    assert progress.summarize([("narration", "接着改")]) == ("narration", False)
    assert progress.summarize([("text", "要不要继续？")]) == ("text", True)


def test_ask_questions_reads_choices_and_typed_questions() -> None:
    questions = progress.ask_questions({"questions": [
        {"question": "放到哪个目录？", "header": "目录", "multiSelect": False,
         "options": [{"label": "D:\\out", "description": "外接盘"}, {"label": "C:\\out"}, {"description": "无标签"}]},
        {"question": "哪些模块？", "multiSelect": True, "options": [{"label": "解析"}, {"label": "推送"}]},
        {"question": "截止日期？", "kind": "text", "title": "日期"},
        {"question": "   "},
        "不是对象",
    ]})
    assert [(item.id, item.header, item.multi) for item in questions] == [
        ("放到哪个目录？", "目录", False), ("哪些模块？", "", True), ("截止日期？", "日期", False)]
    assert questions[0].options == [("D:\\out", "外接盘"), ("C:\\out", "")] and questions[2].options == []
    assert progress.ask_questions({"questions": "x"}) == [] and progress.ask_questions(None) == []
    many = progress.ask_questions({"questions": [{"question": f"问题 {index}"} for index in range(9)]})
    assert len(many) == progress.MAX_QUESTIONS

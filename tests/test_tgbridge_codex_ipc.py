from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Callable
from typing import Any

import pytest

from agent_telegram.codex_threads import build_question_reply
from agent_telegram.tgbridge import codex_ipc
from agent_telegram.tgbridge.codex_ipc import CodexIpcError, IpcClient

THREAD = "01a07078-0000-7000-8000-0000000000c1"


@pytest.fixture(autouse=True)
def no_real_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("IPC tests must use FakeRouter, never the real pipe")

    monkeypatch.setattr(codex_ipc, "PipeTransport", refuse)


class FakeRouter:
    """The router and the window that owns THREAD, as an IpcClient's transport sees them."""

    def __init__(self, states: dict[str, dict[str, Any]] | None = None, *, results: dict[str, Any] | None = None,
                 errors: dict[str, str] | None = None, init_error: str | None = None,
                 no_reply: set[str] | None = None,
                 after_request: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.states = states or {}
        self.results = results or {}
        self.errors = errors or {}
        self.init_error = init_error
        self.no_reply = no_reply or set()
        self.after_request = after_request
        self.inbox = bytearray()
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.now = 1000.0
        self.chunk = 7  # small pieces: frames arrive split

    def clock(self) -> float:
        return self.now

    def push(self, message: dict[str, Any]) -> None:
        self.inbox += codex_ipc.encode(message)

    def write(self, data: bytes) -> None:
        while data:
            (size,) = struct.unpack("<I", data[:4])
            message = json.loads(data[4 : 4 + size])
            data = data[4 + size :]
            self.sent.append(message)
            self._handle(message)

    def _handle(self, message: dict[str, Any]) -> None:
        kind, method = message.get("type"), message.get("method")
        if kind == "request" and method == "initialize":
            if self.init_error:
                self.push({"type": "response", "requestId": message["requestId"], "resultType": "error",
                           "error": self.init_error})
                return
            self.push({"type": "response", "requestId": message["requestId"], "resultType": "success",
                       "method": "initialize", "result": {"clientId": "client-1"}})
            # What other clients do when one connects: ask everyone something, announce their follows.
            self.push({"type": "client-discovery-request", "requestId": "d1", "request": {"method": "ide-context"}})
            self.push({"type": "request", "requestId": "r1", "method": "ide-context", "params": {}})
            self.push({"type": "broadcast", "method": "thread-stream-following-changed", "version": 1,
                       "params": {"conversationId": "other", "hostId": "local", "following": True}})
        elif kind == "broadcast" and method == codex_ipc.FOLLOWING_CHANGED:
            params = message["params"]
            if params["following"] and params["conversationId"] in self.states:
                self.push({"type": "broadcast", "method": codex_ipc.STATE_CHANGED, "sourceClientId": "owner",
                           "version": 11, "targetClientIds": ["client-1"],
                           "params": {"conversationId": params["conversationId"], "hostId": "local",
                                      "change": {"type": "snapshot", "revision": 3,
                                                 "conversationState": self.states[params["conversationId"]]}}})
        elif kind == "request":
            if self.after_request is not None:
                self.after_request(message)
            if method in self.no_reply:
                return
            if method in self.errors:
                self.push({"type": "response", "requestId": message["requestId"], "resultType": "error",
                           "error": self.errors[method]})
            else:
                self.push({"type": "response", "requestId": message["requestId"], "resultType": "success",
                           "method": method, "result": self.results.get(method, {"ok": True})})

    def read(self, timeout_s: float) -> bytes:
        if self.closed:
            raise CodexIpcError("closed")
        if not self.inbox:
            self.now += timeout_s  # nothing arrives: time passes
            return b""
        data = bytes(self.inbox[: self.chunk])
        del self.inbox[: self.chunk]
        return data

    def close(self) -> None:
        self.closed = True

    def connect(self) -> IpcClient:
        client = IpcClient(self, clock=self.clock)
        client.initialize()
        return client

    def of(self, kind: str, method: str) -> list[dict[str, Any]]:
        return [item for item in self.sent if item.get("type") == kind and item.get("method") == method]


def _state(*requests: dict[str, Any], mode: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"id": THREAD, "requests": list(requests),
            "latestCollaborationMode": mode or {"mode": "default", "settings": {"model": "m", "reasoning_effort": "high",
                                                                               "developer_instructions": None}}}


def _ask(request_id: Any, item_id: str, question_ids: list[str], turn_id: str = "t1") -> dict[str, Any]:
    return {"method": codex_ipc.USER_INPUT_METHOD, "id": request_id,
            "params": {"threadId": THREAD, "turnId": turn_id, "itemId": item_id,
                       "questions": [{"id": qid, "question": qid} for qid in question_ids]}}


def test_answer_user_input_follows_the_thread_and_answers_its_request() -> None:
    router = FakeRouter({THREAD: _state(_ask(17, "call_a", ["scope", "data"]))})
    codex_ipc.answer_user_input(THREAD, call_id="call_a", answers={"scope": ["并行"], "data": []},
                                connect=router.connect)
    (init,) = router.of("request", "initialize")
    assert init["params"] == {"clientType": "tgbridge"} and init["sourceClientId"] == "initializing-client"
    follows = [item["params"]["following"] for item in router.of("broadcast", codex_ipc.FOLLOWING_CHANGED)]
    assert follows == [True, False]  # followed only until the snapshot came
    (submit,) = router.of("request", codex_ipc.SUBMIT_USER_INPUT)
    assert submit["version"] == 1 and submit["sourceClientId"] == "client-1" and "targetClientId" not in submit
    assert submit["params"] == {"conversationId": THREAD, "requestId": 17,  # the id exactly as the owner keeps it
                                "response": {"answers": {"scope": {"answers": ["并行"]}}}}
    # The router's questions to every client were answered: this client handles nothing.
    assert {"type": "client-discovery-response", "requestId": "d1", "response": {"canHandle": False}} in router.sent
    assert {"type": "response", "requestId": "r1", "resultType": "error", "error": "no-handler-for-request"} in router.sent
    assert router.closed


def test_the_request_is_found_by_its_item_id_only() -> None:
    # Another call asking the same questions (an earlier or a parallel one) is never answered instead.
    twins = _state(_ask("a", "call_0", ["scope"]), _ask("b", "call_a", ["scope"]))
    assert codex_ipc.find_user_input_request(twins, call_id="call_a")["id"] == "b"
    assert codex_ipc.find_user_input_request(twins, call_id="call_b") is None
    assert codex_ipc.find_user_input_request(twins, call_id="") is None
    plan = _state({"method": codex_ipc.PLAN_METHOD, "id": "implement-plan:t1", "params": {"itemId": "call_a"}})
    assert codex_ipc.find_user_input_request(plan, call_id="call_a") is None  # another kind of request


def test_a_question_no_longer_pending_is_not_answered() -> None:
    router = FakeRouter({THREAD: _state()})
    with pytest.raises(CodexIpcError, match="已经没有这个待回答的问题"):
        codex_ipc.answer_user_input(THREAD, call_id="call_a", answers={"scope": ["并行"]}, connect=router.connect)
    assert router.of("request", codex_ipc.SUBMIT_USER_INPUT) == [] and router.closed


def test_router_errors_are_explained() -> None:
    router = FakeRouter({THREAD: _state(_ask(1, "call_a", ["scope"]))},
                        errors={codex_ipc.SUBMIT_USER_INPUT: "no-client-found"})
    with pytest.raises(CodexIpcError, match="没有窗口负责这个对话"):
        codex_ipc.answer_user_input(THREAD, call_id="call_a", answers={"scope": ["并行"]}, connect=router.connect)
    router = FakeRouter({THREAD: _state(_ask(1, "call_a", ["scope"]))},
                        results={codex_ipc.SUBMIT_USER_INPUT: {"ok": False}})
    with pytest.raises(CodexIpcError, match="没有确认收到"):
        codex_ipc.answer_user_input(THREAD, call_id="call_a", answers={"scope": ["并行"]}, connect=router.connect)


def test_a_thread_no_window_owns_times_out_and_is_unfollowed() -> None:
    router = FakeRouter({})
    client = router.connect()
    with pytest.raises(CodexIpcError, match="没有窗口打开这个对话"):
        client.snapshot(THREAD, timeout_s=3)
    assert [item["params"]["following"] for item in router.of("broadcast", codex_ipc.FOLLOWING_CHANGED)] == [True,
                                                                                                         False]


def test_a_refused_connection_and_a_broken_frame() -> None:
    router = FakeRouter(init_error="nope")
    with pytest.raises(CodexIpcError, match="拒绝了连接：nope"):
        router.connect()
    router = FakeRouter({THREAD: _state()})
    client = router.connect()
    router.inbox += struct.pack("<I", 0)
    with pytest.raises(CodexIpcError, match="无效的帧"):
        client.snapshot("someone-else", timeout_s=1)


PLAN_MODE = {"mode": "plan", "settings": {"model": "m", "reasoning_effort": "xhigh",
                                          "developer_instructions": "# Plan Mode"}}


def _offer(turn_id: str, plan: str = "# 计划\n1. 改代码") -> dict[str, Any]:
    return {"method": codex_ipc.PLAN_METHOD, "id": f"implement-plan:{turn_id}",
            "params": {"threadId": THREAD, "turnId": turn_id, "planContent": plan}}


def test_plan_implementation_switches_a_plan_thread_to_default_mode() -> None:
    router = FakeRouter({THREAD: _state(_offer("t1"), mode=PLAN_MODE)},
                        results={codex_ipc.UPDATE_THREAD_SETTINGS: {"applied": True}})
    assert codex_ipc.prepare_plan_implementation(THREAD, turn_id="t1", connect=router.connect) == "# 计划\n1. 改代码"
    (update,) = router.of("request", codex_ipc.UPDATE_THREAD_SETTINGS)
    assert update["version"] == 2
    assert update["params"] == {"conversationId": THREAD, "threadSettings": {"collaborationMode": {
        "mode": "default", "settings": {"model": "m", "reasoning_effort": "xhigh", "developer_instructions": None}}}}


def test_plan_implementation_checks_the_offer_and_the_switch() -> None:
    router = FakeRouter({THREAD: _state(_offer("t1"))})  # already in Default mode: nothing to switch
    assert codex_ipc.prepare_plan_implementation(THREAD, turn_id="t1", connect=router.connect).startswith("# 计划")
    assert router.of("request", codex_ipc.UPDATE_THREAD_SETTINGS) == []
    router = FakeRouter({THREAD: _state(_offer("t0"), mode=PLAN_MODE)})
    with pytest.raises(CodexIpcError, match="没有在等着实施这个计划"):
        codex_ipc.prepare_plan_implementation(THREAD, turn_id="t1", connect=router.connect)
    assert router.of("request", codex_ipc.UPDATE_THREAD_SETTINGS) == []
    router = FakeRouter({THREAD: _state(_offer("t1"), mode=PLAN_MODE)},
                        results={codex_ipc.UPDATE_THREAD_SETTINGS: {"applied": False}})
    with pytest.raises(CodexIpcError, match="没有把对话切回默认模式"):
        codex_ipc.prepare_plan_implementation(THREAD, turn_id="t1", connect=router.connect)
    router = FakeRouter({THREAD: _state(_offer("t1", plan="  "), mode=PLAN_MODE)})
    with pytest.raises(CodexIpcError, match="计划是空的"):
        codex_ipc.prepare_plan_implementation(THREAD, turn_id="t1", connect=router.connect)


def test_a_refused_plan_leaves_the_mode_alone() -> None:
    router = FakeRouter({THREAD: _state(_offer("t1"), mode=PLAN_MODE)},
                        results={codex_ipc.UPDATE_THREAD_SETTINGS: {"applied": True}})
    seen: list[str] = []

    def refuse(plan: str) -> None:
        seen.append(plan)
        raise ValueError("计划太长")

    with pytest.raises(ValueError, match="计划太长"):
        codex_ipc.prepare_plan_implementation(THREAD, turn_id="t1", check=refuse, connect=router.connect)
    assert seen == ["# 计划\n1. 改代码"] and router.of("request", codex_ipc.UPDATE_THREAD_SETTINGS) == []


def test_an_initialize_answer_of_another_shape_is_refused() -> None:
    router = FakeRouter({THREAD: _state()})
    client = IpcClient(router, clock=router.clock)
    router._handle = lambda message: router.push({"type": "response", "requestId": message["requestId"],  # type: ignore[method-assign]
                                                  "resultType": "success", "result": ["client-1"]})
    with pytest.raises(CodexIpcError, match="拒绝了连接"):
        client.initialize()


def test_describe_error_keeps_unknown_codes() -> None:
    assert codex_ipc.describe_error("request-version-mismatch").startswith("Codex Desktop 的内部协议版本不匹配")
    assert codex_ipc.describe_error("boom") == "boom" and codex_ipc.describe_error(None) == "未知错误"


CLIENT_ID = "e2257ace-0000-4000-8000-000000000001"
CWD = r"C:\proj\workspace"


def _reply(call_id: str = "call_a", index: int = 0, answer: str = "并行") -> str:
    return build_question_reply(call_id, {index: ("执行方式", answer)})


def _turn(*items: dict[str, Any], turn_id: str = "t1", status: str = "inProgress") -> dict[str, Any]:
    return {"turnId": turn_id, "status": status,
            "items": [{"type": "agentMessage", "id": "call_a", "questions": [{"title": "执行方式"}]}]
            + list(items)}


def _with_turns(*items: dict[str, Any], canonical: bool = False,
                requests: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    state = _state(*(requests or []))
    if canonical:
        state["turns"] = []
        state["turnHistory"] = {"kind": "canonical", "history": {
            "entitiesByKey": {f"key-{index}": item for index, item in enumerate(items)},
            "islands": [{"entries": [{"value": f"key-{index}"} for index in range(len(items))]}]}}
    else:
        state["turns"] = list(items)
    return state


def _steer(router: FakeRouter, *, envelope: str | None = None,
           indices: list[int] | None = None) -> dict[str, Any]:
    return codex_ipc.steer_question_reply(
        THREAD, turn_id="t1", call_id="call_a", indices=[0] if indices is None else indices,
        envelope=_reply() if envelope is None else envelope, client_id=CLIENT_ID, cwd=CWD,
        connect=router.connect)


@pytest.mark.parametrize("canonical", [False, True])
def test_turn_helpers_follow_desktops_latest_turn_rule(canonical: bool) -> None:
    first = _turn(turn_id="t0", status="completed")
    active = _turn()
    placeholder = {"turnId": None, "status": "completed", "items": []}
    state = _with_turns(first, active, placeholder, canonical=canonical)
    assert codex_ipc.turns(state) == [first, active, placeholder]
    assert codex_ipc.latest_turn(state) == active
    assert codex_ipc.latest_turn(_with_turns(placeholder, canonical=canonical)) is None
    pending = {"turnId": None, "status": "inProgress", "items": []}
    assert codex_ipc.latest_turn(_with_turns(first, pending, canonical=canonical)) == pending


def test_canonical_turns_skip_unknown_entries_and_keep_island_order() -> None:
    first, second = _turn(turn_id="t0"), _turn()
    state = _state()
    state.update({"turns": [], "turnHistory": {"kind": "canonical", "history": {
        "entitiesByKey": {"a": first, "b": second},
        "islands": [{"entries": [{"value": "missing"}, {"value": "a"}]},
                    {"entries": [{"value": "b"}]}]}}})
    assert codex_ipc.turns(state) == [first, second]
    assert codex_ipc.latest_turn(state) == second


@pytest.mark.parametrize("canonical", [False, True])
def test_steer_question_reply_sends_only_the_desktop_answer_payload(
    monkeypatch: pytest.MonkeyPatch, canonical: bool,
) -> None:
    monkeypatch.setattr(codex_ipc.time, "time", lambda: 1234.567)
    state = _with_turns(_turn(), canonical=canonical)
    router = FakeRouter({THREAD: state}, results={codex_ipc.STEER_TURN: {"result": {"turnId": "t1"}}})
    assert _steer(router) == {"turn_id": "t1", "other_turn": False}
    (request,) = router.of("request", codex_ipc.STEER_TURN)
    assert request["version"] == codex_ipc.VERSIONS[codex_ipc.STEER_TURN] == 1
    assert request["sourceClientId"] == "client-1"
    assert "targetClientId" not in request
    params = request["params"]
    assert set(params) == {"conversationId", "clientUserMessageId", "input", "restoreMessage", "serviceTier",
                           "attachments", "additionalContext", "toolOutput"}
    assert params["conversationId"] == THREAD
    assert params["clientUserMessageId"] == CLIENT_ID
    assert params["input"] == [{"type": "text", "text": _reply(), "text_elements": []}]
    assert params["serviceTier"] is None
    assert params["attachments"] == []
    assert params["additionalContext"] is None
    assert params["toolOutput"] is None
    assert params["restoreMessage"] == {
        "id": CLIENT_ID, "text": _reply(), "createdAt": 1234567, "cwd": CWD,
        "context": {"prompt": _reply(), "turnTrigger": "send_user_message_async_question", "addedFiles": [],
                    "fileAttachments": [], "imageAttachments": [], "commentAttachments": [], "ideContext": None,
                    "workspaceRoots": [CWD], "collaborationMode": state["latestCollaborationMode"]},
        "responsesapiClientMetadata": None}
    assert params["clientUserMessageId"] == params["restoreMessage"]["id"]
    follows = router.of("broadcast", codex_ipc.FOLLOWING_CHANGED)
    assert [item["params"]["following"] for item in follows] == [True, False]
    assert all(item["params"]["hostId"] == "local" for item in follows)
    assert router.closed


@pytest.mark.parametrize("turn_list", [
    [_turn(), _turn(turn_id="t2")],
    [_turn(status="completed")],
    [{"turnId": "t1", "status": "inProgress", "items": []}],
    [{"turnId": "t1", "status": "inProgress", "items": [{"type": "userMessage", "id": "call_a"}]}],
    [],
])
def test_steering_refuses_an_ended_or_different_question_turn(turn_list: list[dict[str, Any]]) -> None:
    router = FakeRouter({THREAD: _with_turns(*turn_list)})
    with pytest.raises(codex_ipc.TurnEnded):
        _steer(router)
    assert router.of("request", codex_ipc.STEER_TURN) == []


@pytest.mark.parametrize("error", [
    "Cannot steer conversation x because its active turn already ended", "no active turn to steer",
])
def test_owner_ended_turn_errors_are_distinct(error: str) -> None:
    router = FakeRouter({THREAD: _with_turns(_turn())}, errors={codex_ipc.STEER_TURN: error})
    with pytest.raises(codex_ipc.TurnEnded):
        _steer(router)


@pytest.mark.parametrize("error", [
    "thread-follower-steer-turn-timeout", "request-timeout", "client-disconnected",
])
def test_sent_steering_router_errors_have_unknown_outcomes(error: str) -> None:
    router = FakeRouter({THREAD: _with_turns(_turn())}, errors={codex_ipc.STEER_TURN: error})
    with pytest.raises(codex_ipc.OutcomeUnknown) as caught:
        _steer(router)
    assert caught.value.code == error
    assert len(router.of("request", codex_ipc.STEER_TURN)) == 1


def test_a_sent_request_response_wait_timeout_has_an_unknown_outcome() -> None:
    router = FakeRouter({THREAD: _with_turns(_turn())}, no_reply={codex_ipc.STEER_TURN})
    with pytest.raises(codex_ipc.OutcomeUnknown, match="等待 Codex Desktop 处理超时") as caught:
        _steer(router)
    assert caught.value.code == "response-wait-timeout"
    assert len(router.of("request", codex_ipc.STEER_TURN)) == 1


def test_a_read_disconnect_after_steering_was_sent_has_an_unknown_outcome() -> None:
    router = FakeRouter({THREAD: _with_turns(_turn())}, no_reply={codex_ipc.STEER_TURN})
    original_read = router.read

    def disconnected(timeout_s: float) -> bytes:
        if router.of("request", codex_ipc.STEER_TURN):
            raise CodexIpcError("Codex Desktop 关闭了 IPC 连接", code="client-disconnected")
        return original_read(timeout_s)

    router.read = disconnected  # type: ignore[method-assign]
    with pytest.raises(codex_ipc.OutcomeUnknown) as caught:
        _steer(router)
    assert caught.value.code == "client-disconnected"
    assert len(router.of("request", codex_ipc.STEER_TURN)) == 1


def test_ipc_requests_keep_the_raw_error_code_and_existing_message() -> None:
    router = FakeRouter(errors={codex_ipc.SUBMIT_USER_INPUT: "no-client-found"})
    with router.connect() as client:
        with pytest.raises(CodexIpcError, match="没有窗口负责这个对话") as caught:
            client.request(codex_ipc.SUBMIT_USER_INPUT, {})
    assert caught.value.code == "no-client-found"


@pytest.mark.parametrize("error", [
    "An earlier turn submission is not yet confirmed", "Cannot steer conversation X without an active turn id",
    "Conversation X is not being streamed.",
])
def test_other_steering_errors_keep_their_error_code(error: str) -> None:
    router = FakeRouter({THREAD: _with_turns(_turn())}, errors={codex_ipc.STEER_TURN: error})
    with pytest.raises(CodexIpcError) as caught:
        _steer(router)
    assert type(caught.value) is CodexIpcError
    assert caught.value.code == error


@pytest.mark.parametrize("kind,status,client_id,mine", [
    ("steeringUserMessage", "accepted", CLIENT_ID, True),
    ("steeringUserMessage", "pending", CLIENT_ID, True),
    ("steeringUserMessage", "rejected", "someone-else", False),
    ("userMessage", None, "someone-else", False),
])
def test_existing_or_pending_answers_prevent_a_second_steer(
    kind: str, status: str | None, client_id: str, mine: bool,
) -> None:
    answer = {"type": kind, "status": status,
              "input" if kind == "steeringUserMessage" else "content": [{"type": "text", "text": _reply()}],
              "clientUserMessageId" if kind == "steeringUserMessage" else "clientId": client_id}
    turn = _turn(answer)
    assert codex_ipc.question_answers(turn, "call_a") == {
        0: {"answer": "并行", "client_id": client_id, "status": status, "kind": kind}}
    router = FakeRouter({THREAD: _with_turns(turn)})
    with pytest.raises(codex_ipc.AlreadyAnswered) as caught:
        _steer(router)
    assert caught.value.mine is mine
    assert router.of("request", codex_ipc.STEER_TURN) == []


def test_question_answers_require_exactly_one_text_part_and_the_named_call() -> None:
    matching = {"type": "userMessage", "clientId": "desktop",
                "content": [{"type": "text", "text": _reply(index=2, answer="稍后")}]}
    invalid = [
        {"type": "userMessage", "content": [{"type": "text", "text": _reply()},
                                             {"type": "text", "text": "extra"}]},
        {"type": "userMessage", "content": [{"type": "image", "text": _reply()}]},
        {"type": "steeringUserMessage", "input": [{"type": "text", "text": _reply("other")}]},
        {"type": "agentMessage", "content": [{"type": "text", "text": _reply()}]},
        {"type": "userMessage", "content": [{"type": "text", "text": "ordinary input"}]},
    ]
    assert codex_ipc.question_answers(_turn(*invalid, matching), "call_a") == {
        2: {"answer": "稍后", "client_id": "desktop", "status": None, "kind": "userMessage"}}


@pytest.mark.parametrize("submission", [
    {"input": [{"type": "text", "text": _reply()}], "clientUserMessageId": CLIENT_ID},
    {"text": _reply(), "clientId": CLIENT_ID},
    {"restoreMessage": {"id": CLIENT_ID, "context": {"prompt": _reply()}}},
])
def test_unconfirmed_answer_submissions_are_in_flight(submission: dict[str, Any]) -> None:
    state = _with_turns(_turn())
    state["unconfirmedTurnSubmissions"] = [submission]
    router = FakeRouter({THREAD: state})
    with pytest.raises(codex_ipc.AlreadyAnswered) as caught:
        _steer(router)
    assert caught.value.mine is True
    assert router.of("request", codex_ipc.STEER_TURN) == []


def test_unrecognised_unconfirmed_submissions_are_skipped() -> None:
    state = _with_turns(_turn())
    state["unconfirmedTurnSubmissions"] = [
        None, {"opaque": _reply()}, {"prompt": "plain input"},
        {"requestId": "submission-1", "method": "turn/steer", "stage": "sent", "clientUserMessageId": CLIENT_ID}]
    router = FakeRouter({THREAD: state}, results={codex_ipc.STEER_TURN: {"result": {"turnId": "t1"}}})
    assert _steer(router) == {"turn_id": "t1", "other_turn": False}


@pytest.mark.parametrize("result,turn_id", [
    ({"result": {"turnId": "t2"}}, "t2"), ({"turnId": "t2"}, "t2"),
])
def test_steering_reports_when_the_owner_used_a_different_turn(result: dict[str, Any], turn_id: str) -> None:
    router = FakeRouter({THREAD: _with_turns(_turn())}, results={codex_ipc.STEER_TURN: result})
    assert _steer(router) == {"turn_id": turn_id, "other_turn": True}


@pytest.mark.parametrize("result", [None, {"ok": True}, {"result": {"turnId": 17}}, {"result": []}])
def test_steering_requires_the_confirmed_turn_id(result: Any) -> None:
    router = FakeRouter({THREAD: _with_turns(_turn())}, results={codex_ipc.STEER_TURN: result})
    with pytest.raises(CodexIpcError):
        _steer(router)


@pytest.mark.parametrize("envelope,indices", [
    ("ordinary steering text", [0]), (_reply("other"), [0]), (_reply(index=1), [0]),
    (_reply(), []),
    (build_question_reply("call_a", {0: ("方式", "并行"), 1: ("数据", "公开")}), [0]),
])
def test_invalid_answer_envelopes_are_refused_before_any_request(envelope: str, indices: list[int]) -> None:
    router = FakeRouter({THREAD: _with_turns(_turn())})
    with pytest.raises(ValueError):
        _steer(router, envelope=envelope, indices=indices)
    assert router.of("request", codex_ipc.STEER_TURN) == []


def _approval(request_id: Any = 17, *, method: str | None = None,
              item_id: str = "cmd-1", turn_id: str = "t1", **params: Any) -> dict[str, Any]:
    return {"id": request_id, "method": method or codex_ipc.COMMAND_APPROVAL_METHOD,
            "params": {"threadId": THREAD, "turnId": turn_id, "itemId": item_id, **params}}


def _decide(router: FakeRouter, approval: dict[str, Any], *, decision: str = "accept",
            digest: str | None = None) -> None:
    params = approval["params"]
    codex_ipc.decide_approval(
        THREAD, method=approval["method"], item_id=params["itemId"], turn_id=params["turnId"],
        approval_id=params.get("approvalId"), digest=codex_ipc.approval_digest(approval) if digest is None else digest,
        decision=decision, connect=router.connect)


def test_find_approval_requests_filters_to_the_four_known_request_types() -> None:
    requests = [_approval(index, method=method) for index, method in enumerate([
        codex_ipc.COMMAND_APPROVAL_METHOD, codex_ipc.FILE_APPROVAL_METHOD,
        codex_ipc.PERMISSIONS_APPROVAL_METHOD, codex_ipc.ELICITATION_METHOD])]
    state = _state(*requests, _ask("input", "call_a", ["scope"]),
                   {"id": 99, "method": codex_ipc.COMMAND_APPROVAL_METHOD, "params": []})
    state["requests"].append(None)
    assert codex_ipc.find_approval_requests(state) == requests
    assert [type(item["id"]) for item in codex_ipc.find_approval_requests(state)] == [int] * 4


def test_approval_digest_covers_method_and_all_params_but_not_rpc_id() -> None:
    request = _approval(reason="检查", command=["python", "-V"])
    expected = hashlib.sha256(json.dumps({"method": request["method"], "params": request["params"]},
                                        sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
    assert codex_ipc.approval_digest(request) == expected
    assert codex_ipc.approval_digest({**request, "id": "17"}) == expected
    changed = {**request, "params": {**request["params"], "reason": "changed"}}
    assert codex_ipc.approval_digest(changed) != expected


def test_command_approval_details_show_the_verified_command_and_context_fields() -> None:
    network = {"host": "example.test", "protocol": "https"}
    permissions = {"network": {"enabled": True}}
    request = _approval(command=["python", "script.py", "--dry-run"], cwd=CWD, approvalId="approval-a",
                        reason="需要检查", networkApprovalContext=network,
                        additionalPermissions=permissions, availableDecisions=["accept", "decline"])
    assert codex_ipc.approval_details(_state(request), request) == {
        "kind": "command", "item_id": "cmd-1", "turn_id": "t1", "approval_id": "approval-a",
        "reason": "需要检查", "command": "python script.py --dry-run", "cwd": CWD, "network": network,
        "extra_permissions": permissions, "available_decisions": ["accept", "decline"], "files": []}


def test_file_approval_details_read_paths_from_canonical_turn_items() -> None:
    request = _approval(method=codex_ipc.FILE_APPROVAL_METHOD, item_id="files-1")
    state = _with_turns(
        _turn({"type": "fileChange", "id": "files-1", "changes": [{"path": "wrong-turn.py"}]},
              turn_id="t0", status="completed"),
        _turn({"type": "fileChange", "id": "other", "changes": [{"path": "wrong-item.py"}]},
              {"type": "fileChange", "id": "files-1", "changes": [
                  {"path": "src/main.py", "kind": "update"}, {"path": "tests/test_main.py", "kind": "add"}]}),
        canonical=True, requests=[request])
    details = codex_ipc.approval_details(state, request)
    assert details["kind"] == "file"
    assert details["files"] == ["src/main.py", "tests/test_main.py"]
    assert details["item_id"] == "files-1" and details["turn_id"] == "t1"
    assert details["command"] == ""
    assert details["network"] is None and details["extra_permissions"] is None
    assert details["available_decisions"] is None


def test_approval_cwd_falls_back_to_the_matching_canonical_turn_params() -> None:
    request = _approval()
    turn = _turn()
    turn["params"] = {"cwd": CWD}
    other = _turn(turn_id="t0", status="completed")
    other["params"] = {"cwd": r"C:\proj\other"}
    state = _with_turns(other, turn, canonical=True, requests=[request])
    assert codex_ipc.approval_details(state, request)["cwd"] == CWD


@pytest.mark.parametrize("method,kind", [
    ("item/permissions/requestApproval", "permissions"), ("mcpServer/elicitation/request", "elicitation"),
])
def test_other_approval_details_are_read_only_display_kinds(method: str, kind: str) -> None:
    request = _approval(method=method, command="echo ok")
    assert codex_ipc.approval_details(_state(request), request)["kind"] == kind


@pytest.mark.parametrize("request_id", [17, "17"])
@pytest.mark.parametrize("method,write", [
    ("item/commandExecution/requestApproval", "thread-follower-command-approval-decision"),
    ("item/fileChange/requestApproval", "thread-follower-file-approval-decision"),
])
def test_approval_decision_uses_the_states_exact_request_id_and_verifies_removal(
    request_id: int | str, method: str, write: str,
) -> None:
    request = _approval(request_id, method=method, approvalId="approval-a")
    state = _state(request)
    router = FakeRouter({THREAD: state}, after_request=lambda _message: state.update(requests=[]))
    _decide(router, request, decision="decline")
    (frame,) = router.of("request", write)
    assert frame["version"] == codex_ipc.VERSIONS[write] == 1
    assert frame["params"] == {"conversationId": THREAD, "requestId": request_id, "decision": "decline"}
    assert type(frame["params"]["requestId"]) is type(request_id)
    assert [item["params"]["following"] for item in router.of("broadcast", codex_ipc.FOLLOWING_CHANGED)] == [
        True, False, True, False]
    assert all(item["params"]["hostId"] == "local"
               for item in router.of("broadcast", codex_ipc.FOLLOWING_CHANGED))


def test_approval_rpc_id_type_is_taken_from_the_current_snapshot() -> None:
    old = _approval(17)
    current = _approval("17")
    state = _state(current)
    router = FakeRouter({THREAD: state}, after_request=lambda _message: state.update(requests=[]))
    _decide(router, old)
    (frame,) = router.of("request", codex_ipc.COMMAND_APPROVAL)
    assert frame["params"]["requestId"] == "17" and type(frame["params"]["requestId"]) is str


@pytest.mark.parametrize("changed", [
    {"itemId": "other-item"}, {"turnId": "other-turn"}, {"approvalId": "other-approval"},
])
def test_reused_rpc_id_does_not_authorize_a_different_approval(changed: dict[str, str]) -> None:
    original = _approval(17, approvalId="approval-a")
    current = {**original, "params": {**original["params"], **changed}}
    router = FakeRouter({THREAD: _state(current)})
    with pytest.raises(codex_ipc.AlreadyAnswered) as caught:
        _decide(router, original)
    assert caught.value.mine is False
    assert router.of("request", codex_ipc.COMMAND_APPROVAL) == []


def test_an_approval_with_a_changed_digest_is_not_decided() -> None:
    request = _approval(command="echo original")
    router = FakeRouter({THREAD: _state(request)})
    with pytest.raises(codex_ipc.AlreadyAnswered) as caught:
        _decide(router, request, digest="0" * 64)
    assert caught.value.mine is False
    assert router.of("request", codex_ipc.COMMAND_APPROVAL) == []


def test_a_missing_approval_is_already_answered() -> None:
    router = FakeRouter({THREAD: _state()})
    with pytest.raises(codex_ipc.AlreadyAnswered) as caught:
        _decide(router, _approval())
    assert caught.value.mine is False
    assert router.of("request", codex_ipc.COMMAND_APPROVAL) == []


def test_a_decision_not_offered_by_the_owner_is_refused() -> None:
    request = _approval(availableDecisions=["decline"])
    router = FakeRouter({THREAD: _state(request)})
    with pytest.raises(CodexIpcError):
        _decide(router, request)
    assert router.of("request", codex_ipc.COMMAND_APPROVAL) == []


@pytest.mark.parametrize("decision", ["acceptForSession", "yes", ""])
def test_approval_decisions_are_limited_to_accept_or_decline(decision: str) -> None:
    request = _approval()
    router = FakeRouter({THREAD: _state(request)})
    with pytest.raises(ValueError):
        _decide(router, request, decision=decision)
    assert router.of("request", codex_ipc.COMMAND_APPROVAL) == []


@pytest.mark.parametrize("method", ["item/permissions/requestApproval", "mcpServer/elicitation/request"])
def test_permission_and_elicitation_requests_cannot_be_decided(method: str) -> None:
    request = _approval(method=method)
    router = FakeRouter({THREAD: _state(request)})
    with pytest.raises(ValueError):
        _decide(router, request)
    assert [item for item in router.sent if item.get("type") == "request" and item.get("method") != "initialize"] == []


def test_an_ok_response_is_not_enough_when_the_approval_stays_pending() -> None:
    request = _approval()
    router = FakeRouter({THREAD: _state(request)})
    with pytest.raises(CodexIpcError, match="Codex Desktop 没有接受这个决定"):
        _decide(router, request)
    assert len(router.of("request", codex_ipc.COMMAND_APPROVAL)) == 1


@pytest.mark.parametrize("drop_reply", [False, True])
def test_a_timed_out_approval_succeeds_when_the_post_snapshot_shows_it_gone(drop_reply: bool) -> None:
    request = _approval()
    state = _state(request)
    router = FakeRouter(
        {THREAD: state}, errors={} if drop_reply else {codex_ipc.COMMAND_APPROVAL: "request-timeout"},
        no_reply={codex_ipc.COMMAND_APPROVAL} if drop_reply else set(),
        after_request=lambda _message: state.update(requests=[]))
    _decide(router, request)
    assert len(router.of("request", codex_ipc.COMMAND_APPROVAL)) == 1


def test_a_timed_out_approval_still_pending_has_an_unknown_outcome() -> None:
    request = _approval()
    router = FakeRouter({THREAD: _state(request)}, errors={codex_ipc.COMMAND_APPROVAL: "request-timeout"})
    with pytest.raises(codex_ipc.OutcomeUnknown) as caught:
        _decide(router, request)
    assert caught.value.code == "request-timeout"
    assert len(router.of("request", codex_ipc.COMMAND_APPROVAL)) == 1


def test_approval_post_check_uses_the_exact_id_type_and_item_id() -> None:
    request = _approval(17)
    state = _state(request)
    router = FakeRouter({THREAD: state}, after_request=lambda _message: state.update(requests=[_approval("17")]))
    _decide(router, request)
    assert state["requests"][0]["id"] == "17"


def test_an_approval_whose_post_check_fails_has_an_unknown_outcome() -> None:
    request = _approval()
    router = FakeRouter({THREAD: _state(request)})
    router.after_request = lambda _message: router.states.pop(THREAD, None)  # the owner stops answering
    with pytest.raises(codex_ipc.OutcomeUnknown):
        _decide(router, request)
    assert len(router.of("request", codex_ipc.COMMAND_APPROVAL)) == 1

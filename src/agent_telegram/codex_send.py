"""Hand a task to one Codex Desktop conversation and wait for its final answer."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_telegram.i18n import tr
from agent_telegram import codex_app_client, codex_threads, codex_watch
from agent_telegram.codex_threads import FOLLOWED_STATUSES, DeliveryTracker, ThreadInfo
from agent_telegram import delegation
from agent_telegram.jobs import now_iso
from agent_telegram.outputs import atomic_write_text, publish_text, require_absolute
from agent_telegram.worker import JobContext, JobFailure

POLL_S = 5.0
DELIVERY_TIMEOUT_S = 120.0
# Codex Desktop starts a retry turn within seconds after a transient turn failure
# while the conversation is on screen; codex_watch continues background ones after
# its backoff, and later failures in a row wait longer (see _retry_grace). A running
# codex_watch's log can extend the wait further (see _retry_over).
RETRY_GRACE_S = 360.0
# A running turn whose rollout stays silent this long is checked with Codex Desktop
# (long web tool waits write nothing, so silence alone proves nothing).
STALL_CHECK_S = 300.0
# if_busy='wait' asks Codex Desktop at most this often (each check starts a node process).
APP_RECHECK_S = 120.0
DEFAULT_TIMEOUT_S = 4 * 3600.0
MANIFEST_STATUSES = ("ok", "partial", "failed")
_WATCH = codex_watch.WatchConfig()

SendFunction = Callable[..., dict[str, Any]]
BusyCheck = Callable[[str], "bool | None"]
MainPid = Callable[[], "int | None"]


def compose_message(job_id: str, message: str, output_dir: Path | None, *, caller: str = "上游代理") -> str:  # i18n: data
    target = (
        # i18n: data-begin
        f"2. 把本次所有交付物（审稿意见、生成的图片、修改稿等）保存到这个目录：{output_dir}\n"
        "   目录不存在就创建；不要覆盖已有文件。\n"
        # i18n: data-end
        if output_dir
        else "2. 交付物保存在你认为合适的位置，并在清单里写出绝对路径。\n"  # i18n: data
    )
    return (
        # i18n: data-begin
        f"[agent-telegram:{job_id}] 以下任务由{delegation.spaced(caller)}通过 agent_telegram 转交。\n\n"
        f"{message.strip()}\n\n"
        "---\n"
        "【交付要求（agent_telegram 自动附加）】\n"
        "1. 需要 ChatGPT/Gemini 时（问答、审稿、画图），使用 agent_telegram 的 web_* MCP 工具完成："
        '提交时设置 wait_s=0，收到 job_id 后调用 web_job(action="wait", job_id=...) 等待；'
        "不要用 on_degraded=codex。本轮不再调度其他代理，需要帮手时只用你自带的子代理。\n"
        f"{target}"
        "3. 完成后，在最终回复的最后输出下面格式的交付清单（路径必须是绝对路径）：\n"
        "BEGIN_AGENT_TG_RESULT\n"
        '{"status": "ok", "summary": "一句话总结", "files": [{"path": "C:\\\\绝对路径\\\\文件", '
        '"kind": "review|image|reply|tex|other", "description": "说明"}], '
        '"chatgpt_conversations": ["https://chatgpt.com/c/..."], "notes": ""}\n'
        "END_AGENT_TG_RESULT\n"
        "   status 只能是 ok、partial 或 failed。\n"
        "4. 交付物只保存为文件：不要对交付目录执行 git add / commit / push（除非任务本身明确要求提交）。\n"
        # i18n: data-end
    )


def raw_message(message: str, nonce: str) -> str:
    """Raw mode: the message goes out verbatim, but it must start with its nonce."""

    return message if message.startswith(nonce) else f"{nonce} {message}"


def _current_rollout(thread_id: str, fallback: Path | None) -> Path | None:
    try:
        info = codex_threads.resolve_thread(thread_id, include_archived=True)
    except codex_threads.ThreadLookupError:
        return fallback
    return info.rollout or fallback


def _activity(info: ThreadInfo, busy_check: BusyCheck | None) -> dict[str, Any]:
    """Rollout-based busy state, confirmed by Codex Desktop unless the rollout says idle.

    Without busy_check the rollout's answer stands; a state nobody can tell counts as busy.
    """

    activity = codex_threads.thread_activity(info)
    busy = activity["busy"]
    if busy is not False and busy_check is not None:
        confirmed = activity["app_confirmed_busy"] = busy_check(info.id)
        if confirmed is not None:
            busy = confirmed  # False e.g. for a turn interrupted by an app restart
    if busy is None:
        activity["state_unknown"] = True  # no turn event near the rollout's end, no answer from the app
    activity["busy"] = busy is not False
    return activity


def _wait_until_idle(
    ctx: JobContext, info: ThreadInfo, *, deadline: float, poll_s: float, busy_check: BusyCheck,
    app_recheck_s: float,
) -> dict[str, Any]:
    ctx.phase("waiting_idle")
    next_app_check = time.monotonic() + app_recheck_s  # run_codex_send has just asked the app
    while True:
        time.sleep(poll_s)
        info = codex_threads.resolve_thread(info.id)
        now = time.monotonic()
        ask = now >= next_app_check
        if ask:
            next_app_check = now + app_recheck_s
        activity = _activity(info, busy_check if ask else None)
        if not activity["busy"]:
            return activity
        if now >= deadline:
            raise JobFailure(tr('Timed out waiting for the Codex conversation to become idle.'), thread=info.summary(), activity=activity)


def _codex_pid(main_pid: MainPid) -> int | None:
    try:
        return main_pid()
    except OSError:
        return None  # a failed process snapshot must not end a long-running job


def _retry_grace(tracker: DeliveryTracker, base: float) -> float:
    """Seconds to wait for a continuation after the chain's last turn failed or was interrupted."""

    turn = tracker.turn
    assert turn is not None
    if turn.status == "interrupted":
        return base
    if not codex_watch.classify(turn.error_info, turn.error)[0]:
        return 0.0  # neither Codex nor codex_watch continues usage-limit, policy or request errors
    failures = 0
    for item in reversed(tracker.turns):
        if item.status != "failed":
            break
        failures += 1
    # codex_watch waits longer after each consecutive failure (up to 300 s from the fifth).
    return base + _WATCH.delay(failures) - _WATCH.delay(1)


def _retry_over(tracker: DeliveryTracker, thread_id: str, waited: float, base: float) -> bool:
    """No continuation of the chain's failed or interrupted last turn is to be expected any more."""

    if tracker.superseded:
        return True  # a turn with someone's new input took over the conversation
    if waited < _retry_grace(tracker, base):
        return False
    turn = tracker.turn
    assert turn is not None
    # codex_watch may still be backing off or retrying a failed send; its log says until when.
    due = codex_watch.continuation_due(thread_id, turn.turn_id) if turn.status == "failed" else None
    return due is None or time.time() >= due


def _unfinished_error(tracker: DeliveryTracker, waited: float) -> str:
    turn = tracker.turn
    assert turn is not None
    if tracker.superseded:
        what = tr('was interrupted') if turn.status == "interrupted" else tr('failed ({v0})', v0=turn.error) if turn.error else tr('failed')
        return (tr("After this Codex turn {v0}, a turn with new input (from the user or another task) started in the conversation; this task will not be continued. Use Telegram /read to inspect it.", v0=what))
    if turn.status == "interrupted":
        return (tr("This Codex turn was interrupted (Codex Desktop restarted or exited), and no continuation turn appeared within {v0:.0f} seconds; continue in Codex and use Telegram /read to check the result.", v0=waited))
    resumable, reason = codex_watch.classify(turn.error_info, turn.error)
    if not resumable:
        return tr('This Codex turn failed ({v0}; no automatic retry): {v1}', v0=reason, v1=turn.error)
    return tr('This Codex turn failed (no successful automatic retry within {v0:.0f} seconds): {v1}', v0=waited, v1=turn.error)


def _manifest_warnings(manifest: dict[str, Any]) -> list[str]:
    status = manifest.get("status")
    if status == "ok":
        return []
    if status not in MANIFEST_STATUSES:
        return [tr('The delivery manifest status must be ok, partial or failed; got {v0!r}.', v0=status)]
    texts = (" ".join(str(manifest.get(key) or "").split())[:300] for key in ("summary", "notes"))
    detail = tr('; ').join(text for text in texts if text)
    head = tr('Codex reported that the task failed') if status == "failed" else tr('Codex reported that the task was only partially completed')
    head += tr(' (status={status})', status=status)
    return [tr('{head}: {detail}', head=head, detail=detail) if detail else tr('{head}.', head=head)]


def render_codex_reply(info: ThreadInfo, turn_id: str, final: str, sent_at: str, job_id: str) -> str:
    header = (
        "<!-- agent_telegram codex_send\n"
        f"job_id: {job_id}\n"
        f"thread_id: {info.id}\n"
        f"thread_name: {info.name}\n"
        f"turn_id: {turn_id}\n"
        f"sent_at: {sent_at}\n"
        f"completed_at: {now_iso()}\n"
        "-->\n\n"
    )
    return header + final.rstrip() + "\n"


def run_codex_send(
    ctx: JobContext,
    *,
    send: SendFunction | None = None,
    poll_s: float = POLL_S,
    delivery_timeout_s: float = DELIVERY_TIMEOUT_S,
    retry_grace_s: float = RETRY_GRACE_S,
    busy_check: BusyCheck | None = None,
    main_pid: MainPid | None = None,
    stall_check_s: float = STALL_CHECK_S,
    app_recheck_s: float = APP_RECHECK_S,
) -> dict[str, Any]:
    """Send one message to a Codex conversation and follow its turn to the final answer.

    ``params["raw"]``: send ``params["message"]`` as it is (prefixed with ``params["nonce"]``
    when it does not start with it), keep the conversation's model settings, leave the
    visible conversation alone, and skip the delivery contract, manifest and reply files.
    """

    params = ctx.params
    raw = bool(params.get("raw"))
    send = send or codex_app_client.send_message
    busy_check = busy_check or codex_app_client.app_reports_busy
    main_pid = main_pid or codex_watch.codex_main_pid
    started = time.monotonic()
    deadline = started + float(params.get("timeout_s") or DEFAULT_TIMEOUT_S)
    if raw and (not str(params.get("nonce") or "").strip() or not str(params.get("message") or "").strip()):
        raise JobFailure(tr('Raw mode requires a nonempty nonce and message.'))
    info = codex_threads.resolve_thread(params["thread_id"])
    output_dir = None
    if params.get("output_dir") and not raw:
        output_dir = require_absolute(params["output_dir"], what="output_dir")
        output_dir.mkdir(parents=True, exist_ok=True)

    activity = _activity(info, busy_check)
    if activity["busy"]:
        policy = params.get("if_busy") or "fail"
        if policy == "fail":
            state = (
                tr('Cannot determine whether this Codex conversation is idle (no turn event at the end of the rollout, and Codex Desktop did not respond).')
                if activity.get("state_unknown")
                else tr('This Codex conversation is still running the previous task.')
            )
            raise JobFailure(
                tr("{v0}Wait for it to finish, or use if_busy='wait' (queue); if_busy='send' inserts the message into the current turn (steering), without interrupting it. Even a stuck turn sees it only after its current tool call returns.", v0=state),
                thread=info.summary(),
                activity=activity,
            )
        if policy == "wait":
            activity = _wait_until_idle(ctx, info, deadline=deadline, poll_s=poll_s, busy_check=busy_check,
                                        app_recheck_s=app_recheck_s)
            info = codex_threads.resolve_thread(info.id)

    if raw:
        nonce = str(params["nonce"]).strip()
        text = raw_message(str(params["message"]), nonce)
        model = thinking = None  # keep the conversation's own settings
    else:
        nonce = f"[agent-telegram:{ctx.job_id}]"
        text = compose_message(ctx.job_id, params["message"], output_dir, caller=delegation.caller_name(params))
        model = params.get("model") or None
        thinking = params.get("thinking") or None
    atomic_write_text(ctx.directory / "message.txt", text)
    rollout = info.rollout
    offset = rollout.stat().st_size if rollout and rollout.is_file() else 0

    ctx.phase("sending", thread_id=info.id, thread_name=info.name, model=model, thinking=thinking)
    if raw:
        delivery = send(info.id, text, model=None, thinking=None, navigate=False)
    else:
        delivery = send(info.id, text, model=model, thinking=thinking)
    sent_at = now_iso()
    sent_clock = time.monotonic()
    ctx.phase("waiting_turn", sent_at=sent_at, pipe=delivery.get("pipe"))

    tracker = DeliveryTracker(nonce)
    reported_turn: str | None = None
    failed_since: float | None = None
    app_pid = _codex_pid(main_pid)
    seen_pid = app_pid is not None  # False where the Codex process cannot be identified
    next_liveness = sent_clock + stall_check_s
    while True:
        latest = _current_rollout(info.id, rollout)
        if latest != rollout:
            rollout, offset = latest, 0  # a new rollout segment starts at byte 0
        if rollout is not None and rollout.is_file():
            if rollout.stat().st_size < offset:
                offset, tracker = 0, DeliveryTracker(nonce)  # file was rewritten
            records, offset = codex_threads.read_records(rollout, offset)
            if records:
                next_liveness = time.monotonic() + stall_check_s
            tracker.feed(records)
        if tracker.turn_id and tracker.turn_id != reported_turn:
            reported_turn = tracker.turn_id
            ctx.phase("running_turn", turn_id=tracker.turn_id, turn_chain=tracker.chain)
        if tracker.done:
            break
        now = time.monotonic()
        turn = tracker.turn
        if turn is not None and turn.status == "running":
            # An app restart leaves the turn open in the rollout forever; ask the app when
            # Codex Desktop restarted or the rollout went quiet.
            pid = _codex_pid(main_pid)
            seen_pid = seen_pid or pid is not None
            if pid != app_pid or now >= next_liveness:
                app_pid, next_liveness = pid, now + stall_check_s
                alive = busy_check(info.id)
                if alive is False or (alive is None and pid is None and seen_pid):
                    tracker.mark_interrupted()
                    turn = tracker.turn
                now = time.monotonic()
        if turn is not None and turn.status in FOLLOWED_STATUSES:
            if failed_since is None:
                failed_since = now  # wait for a retry or continuation turn
                ctx.phase("waiting_retry", turn_id=turn.turn_id, turn_status=turn.status, turn_chain=tracker.chain)
            if _retry_over(tracker, info.id, now - failed_since, retry_grace_s):
                break
        elif failed_since is not None:
            failed_since, reported_turn = None, None  # running again; report running_turn next poll
        if tracker.turn_id is None and now - sent_clock > delivery_timeout_s:
            raise JobFailure(
                tr('The message was sent, but did not appear in the Codex conversation within {v0:.0f} seconds.', v0=delivery_timeout_s),
                thread=info.summary(),
                delivery=delivery,
            )
        if now >= deadline:
            raise JobFailure(
                tr("Timed out waiting for Codex to finish; it may still be running. Check later with Telegram /read."),
                thread=info.summary(),
                turn_id=tracker.turn_id,
            )
        time.sleep(poll_s)

    turn = tracker.turn
    assert turn is not None
    web_calls = tracker.web_calls
    details = {"turn_chain": tracker.chain, "web_calls": web_calls[-20:], "relay_calls": web_calls[-20:],
               "model": model, "thinking": thinking}
    if turn.status == "aborted":
        raise JobFailure(tr('This Codex turn was aborted: {v0}', v0=turn.abort_reason), **details)
    if turn.status in FOLLOWED_STATUSES:
        assert failed_since is not None
        raise JobFailure(_unfinished_error(tracker, time.monotonic() - failed_since), turn_status=turn.status,
                         error_kind=turn.error_info or None, superseded=tracker.superseded or None, **details)

    final = turn.final_message or ""
    if raw:
        return {
            "ok": True,
            "status": "succeeded",
            "thread": info.summary(),
            "turn_id": turn.turn_id,
            "turn_chain": tracker.chain,
            "final_message": final,
            "warnings": [],
        }
    ctx.phase("publishing", turn_id=turn.turn_id)
    manifest, manifest_error = codex_threads.parse_manifest(final)
    files, missing = codex_threads.collect_manifest_files(manifest, output_dir) if manifest else ([], [])
    warnings = _manifest_warnings(manifest) if manifest is not None else [manifest_error]
    task_status = manifest.get("status") if manifest is not None else None
    if missing:
        warnings.append(tr('{v0} files in the delivery manifest do not exist.', v0=len(missing)))
    target = output_dir or ctx.directory
    reply_path = publish_text(target / "codex-reply.md", render_codex_reply(info, turn.turn_id, final, sent_at, ctx.job_id))
    record = {
        "job_id": ctx.job_id,
        "thread": info.summary(),
        "turn_id": turn.turn_id,
        "turn_chain": tracker.chain,
        "model": model,
        "thinking": thinking,
        "sent_at": sent_at,
        "completed_at": now_iso(),
        "task_status": task_status,
        "manifest": manifest,
        "manifest_error": manifest_error or None,
        "files": files,
        "missing_files": missing,
        "web_calls": web_calls,
        "gpt_relay_calls": web_calls,  # legacy alias
        "warnings": warnings,
    }
    manifest_path = publish_text(
        target / "agent_telegram-manifest.json", json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    )
    return {
        "ok": True,
        "status": "succeeded",
        "thread": info.summary(),
        "turn_id": turn.turn_id,
        "turn_chain": tracker.chain,
        "model": model,
        "thinking": thinking,
        "final_message": final,
        "task_status": task_status,
        "manifest": manifest,
        "files": files,
        "missing_files": missing,
        "web_call_count": len(web_calls),
        "web_calls": web_calls[-10:],
        "gpt_relay_call_count": len(web_calls),  # legacy aliases
        "gpt_relay_calls": web_calls[-10:],
        "reply_path": str(reply_path),
        "manifest_path": str(manifest_path),
        "warnings": warnings,
    }

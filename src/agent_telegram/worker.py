"""Detached job runner: ``python -m agent_telegram.worker <job_id>``."""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_telegram import jobs

HEARTBEAT_S = 30.0
FINAL_STATE_ATTEMPTS = 3
FINAL_STATE_RETRY_S = 1.0


@dataclass
class JobContext:
    job_id: str
    kind: str
    params: dict[str, Any]
    directory: Path
    current_phase: str = ""

    @classmethod
    def load(cls, job_id: str) -> JobContext:
        request = jobs.load_request(job_id)
        return cls(
            job_id=job_id,
            kind=str(request["kind"]),
            params=dict(request.get("params") or {}),
            directory=jobs.job_dir(job_id),
        )

    def phase(self, name: str, **extra: Any) -> None:
        jobs.update_state(self.job_id, phase=name, **extra)
        self.current_phase = name
        self.log(f"phase={name}")

    def log(self, message: str) -> None:
        print(f"[{jobs.now_iso()}] {message}", flush=True)


class JobFailure(RuntimeError):
    """A handled failure whose details are copied into result.json."""

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.details = details


def _run_selftest(ctx: JobContext) -> dict[str, Any]:
    """Diagnostic kind: sleep, then succeed (or fail on request)."""

    seconds = float(ctx.params.get("seconds") or 0)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
    if ctx.params.get("fail"):
        raise JobFailure("selftest failure requested", requested=True)
    return {"ok": True, "status": "succeeded", "slept_s": seconds}


def handler_for(kind: str) -> Callable[[JobContext], dict[str, Any]]:
    if kind == "codex":
        from agent_telegram.codex_send import run_codex_send

        return run_codex_send
    if kind == "selftest":
        return _run_selftest
    raise ValueError(f"unknown job kind: {kind}")


def run_job(job_id: str, handler: Callable[[JobContext], dict[str, Any]] | None = None) -> int:
    ctx = JobContext.load(job_id)
    if (ctx.kind in jobs.WEB_JOB_KINDS and ctx.params.get("delegated_job")
            and jobs.load_state(job_id).get("status") in jobs.TERMINAL_STATUSES):
        # Cancellation can win while a newly spawned worker is importing.
        return 0
    if ctx.params.get("_dispatch_managed"):
        raise ValueError("Shared-dispatcher tasks are unsupported in this standalone bridge.")
    pid = os.getpid()
    jobs.update_state(
        job_id,
        status="running",
        phase="starting",
        pid=pid,
        pid_created=jobs.process_creation_time(pid),
        started_at=jobs.now_iso(),
    )
    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(HEARTBEAT_S):
            with contextlib.suppress(OSError):
                jobs.update_state(job_id, heartbeat_at=jobs.now_iso())

    threading.Thread(target=heartbeat, name="agent_telegram-heartbeat", daemon=True).start()
    try:
        result = (handler or handler_for(ctx.kind))(ctx)
        result.setdefault("ok", True)
        result.setdefault("status", "succeeded")
        warnings = list(result.get("warnings") or [])
        stop.set()
        jobs.write_result(job_id, result)
    except BaseException as exc:  # noqa: BLE001 - every failure must be recorded
        stop.set()
        _record_failure(ctx, exc)
        return 1
    # result.json already holds the success; a failed state update must never
    # replace it. refresh_state adopts the terminal result once this process exits.
    for attempt in range(FINAL_STATE_ATTEMPTS):
        try:
            jobs.update_state(job_id, status="succeeded", phase="done", finished_at=jobs.now_iso(), warnings=warnings)
            break
        except (OSError, ValueError) as exc:  # ValueError: unreadable state.json
            ctx.log(f"final state update failed ({attempt + 1}/{FINAL_STATE_ATTEMPTS}): {exc}")
            if attempt + 1 < FINAL_STATE_ATTEMPTS:
                time.sleep(FINAL_STATE_RETRY_S)
    return 0


def _record_failure(ctx: JobContext, exc: BaseException) -> None:
    ctx.log(traceback.format_exc())
    error = f"{type(exc).__name__}: {exc}"
    details = getattr(exc, "details", None)
    jobs.write_result(
        ctx.job_id,
        {
            "ok": False,
            "status": "failed",
            "error": error,
            "error_type": type(exc).__name__,
            **(details if isinstance(details, dict) else {}),
        },
    )
    jobs.update_state(ctx.job_id, status="failed", phase="failed", finished_at=jobs.now_iso(), error=error)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or not jobs.is_local_job_id(args[0]):
        print("usage: python -m agent_telegram.worker <job_id>", file=sys.stderr)
        return 2
    return run_job(args[0])


if __name__ == "__main__":
    raise SystemExit(main())

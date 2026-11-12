from __future__ import annotations

import time

import pytest

from agent_telegram import jobs, worker


def _wait_for(job_id: str, statuses: set[str], timeout_s: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = jobs.refresh_state(job_id)
        if state.get("status") in statuses:
            return state
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not reach {statuses}: {jobs.load_state(job_id)}")


def test_create_job_writes_request_and_state() -> None:
    job_id = jobs.create_job("selftest", {"seconds": 0})
    assert jobs.is_local_job_id(job_id)
    assert jobs.load_request(job_id)["params"] == {"seconds": 0}
    state = jobs.load_state(job_id)
    assert (state["status"], state["phase"], state["kind"]) == ("queued", "queued", "selftest")


def test_run_job_in_process_succeeds() -> None:
    job_id = jobs.create_job("selftest", {"seconds": 0})
    assert worker.run_job(job_id) == 0
    state = jobs.load_state(job_id)
    assert state["status"] == "succeeded" and state["pid"]
    assert jobs.load_result(job_id) == {"ok": True, "status": "succeeded", "slept_s": 0.0}


def test_failed_job_records_error_details() -> None:
    job_id = jobs.create_job("selftest", {"fail": True})
    assert worker.run_job(job_id) == 1
    assert jobs.load_state(job_id)["status"] == "failed"
    result = jobs.load_result(job_id)
    assert result["error_type"] == "JobFailure" and result["requested"] is True


def test_failed_final_state_update_keeps_the_successful_result(monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = jobs.create_job("selftest", {"seconds": 0})
    real_update = jobs.update_state

    def flaky_update(job: str, **fields):
        if fields.get("status") == "succeeded":
            raise PermissionError("simulated sharing violation")
        return real_update(job, **fields)

    monkeypatch.setattr(jobs, "update_state", flaky_update)
    monkeypatch.setattr(worker, "FINAL_STATE_RETRY_S", 0.0)
    assert worker.run_job(job_id, handler=lambda ctx: {"conversation_url": "https://chatgpt.com/c/abc"}) == 0
    assert jobs.load_result(job_id) == {"conversation_url": "https://chatgpt.com/c/abc", "ok": True, "status": "succeeded"}
    monkeypatch.setattr(jobs, "update_state", real_update)
    monkeypatch.setattr(jobs, "process_alive", lambda pid, created: False)
    assert jobs.refresh_state(job_id)["status"] == "succeeded"  # adopted once the worker is gone


def test_refresh_marks_dead_worker_orphaned(monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = jobs.create_job("selftest", {})
    jobs.update_state(job_id, status="running", pid=4_000_000, pid_created=1)
    monkeypatch.setattr(jobs, "process_alive", lambda pid, created: False)
    state = jobs.refresh_state(job_id)
    assert state["status"] == "orphaned" and "worker.log" in state["error"]


def test_refresh_adopts_result_written_before_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = jobs.create_job("selftest", {})
    jobs.update_state(job_id, status="running", pid=4_000_000, pid_created=1)
    jobs.write_result(job_id, {"ok": True, "status": "succeeded"})
    monkeypatch.setattr(jobs, "process_alive", lambda pid, created: False)
    assert jobs.refresh_state(job_id)["status"] == "succeeded"


def test_list_jobs_labels_codex_threads() -> None:
    jobs.create_job("codex", {"thread_id": "01a07078-0000-7000-8000-0000000000e1", "thread_name": "demo-测试"})
    jobs.create_job("ask", {"prompt": "hello"})
    listed = jobs.list_jobs(10)
    by_kind = {item["kind"]: item for item in listed}
    assert by_kind["codex"]["thread_name"] == "demo-测试"
    assert by_kind["ask"]["label"] == "hello"
    assert len(jobs.list_jobs(10, active_only=True)) == 2


def test_active_list_does_not_read_finished_requests_and_still_refreshes_workers(monkeypatch):
    finished = jobs.create_job("ask", {"prompt": "old request"})
    jobs.update_state(finished, status="succeeded")
    dead = jobs.create_job("selftest", {})
    jobs.update_state(dead, status="running", pid=4_000_000, pid_created=1)
    active = jobs.create_job("ask", {"prompt": "active request"})
    monkeypatch.setattr(jobs, "process_alive", lambda pid, created: False)
    original = jobs.load_request
    reads = []
    def read(job_id):
        assert job_id == active, "finished request must not be loaded by active-only health"
        reads.append(job_id)
        return original(job_id)
    monkeypatch.setattr(jobs, "load_request", read)
    result = jobs.list_jobs(10, active_only=True)
    assert [item["job_id"] for item in result] == [active]
    assert result[0]["label"] == "active request" and reads == [active]
    assert jobs.load_state(dead)["status"] == "orphaned"


def test_detached_worker_runs_and_cancel_terminates() -> None:
    quick = jobs.create_job("selftest", {"seconds": 0.2})
    jobs.spawn_worker(quick)
    assert _wait_for(quick, jobs.TERMINAL_STATUSES)["status"] == "succeeded"

    slow = jobs.create_job("selftest", {"seconds": 120})
    jobs.spawn_worker(slow)
    running = _wait_for(slow, {"running"})
    cancelled = jobs.cancel_job(slow)
    assert cancelled["status"] == "cancelled" and cancelled["worker_terminated"] is True
    deadline = time.monotonic() + 10
    while jobs.process_alive(running["pid"], running.get("pid_created")) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not jobs.process_alive(running["pid"], running.get("pid_created"))
    assert jobs.refresh_state(slow)["status"] == "cancelled"

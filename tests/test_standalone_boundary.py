"""The shared bridge does not ship or depend on the private MCP dispatcher."""
from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest

from agent_telegram import jobs, worker


def test_standalone_worker_records_native_phase_and_success(tmp_path: Path):
    ident = jobs.create_job("selftest", {"seconds": 0})
    ctx = worker.JobContext.load(ident)
    ctx.phase("isolated-test", item_count=1)
    assert jobs.load_state(ident)["phase"] == "isolated-test"
    assert worker.run_job(ident) == 0
    assert jobs.load_state(ident)["status"] == "succeeded"
    assert jobs.load_result(ident)["status"] == "succeeded"


def test_standalone_worker_rejects_shared_managed_job_before_execution():
    ident = jobs.create_job("selftest", {"_dispatch_managed": True})
    called = []
    with pytest.raises(ValueError, match="unsupported"):
        worker.run_job(ident, handler=lambda ctx: called.append(ctx) or {"ok": True})
    assert called == []
    assert jobs.load_state(ident)["status"] == "queued"


def test_standalone_has_no_optional_dispatcher_or_context_imports():
    from agent_telegram import codex_send
    for module in (worker, jobs, codex_send):
        source = inspect.getsource(module)
        assert "from .agent_dispatch" not in source
        assert "from .agent_context" not in source


def test_package_tests_use_isolated_profile(tmp_path: Path):
    for name in ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        assert Path(os.environ[name]).is_relative_to(tmp_path)

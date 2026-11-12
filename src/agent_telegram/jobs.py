"""File-backed job store and detached worker processes.

Every task runs in its own detached ``python -m agent_telegram.worker <job_id>``
process so that a restart of Claude Code (and therefore of this MCP server) does
not interrupt a long ChatGPT or Codex turn. Only the worker writes state.json
while it is alive; the server writes it only after the worker is gone
(cancel/orphan detection). The spawner records the process identity in a
separate spawn.json to avoid racing the worker's first state update.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from agent_telegram.i18n import tr
from agent_telegram.outputs import atomic_write_json, read_json
from agent_telegram.settings import PACKAGE_ROOT, jobs_dir, var_dir
from agent_telegram.paths import PACKAGE_ROOT, is_legacy_stored, relocate_stored

TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "orphaned"})
ACTIVE_STATUSES = frozenset({"queued", "running"})
WEB_JOB_KINDS = frozenset({"ask", "review", "draw", "image_iterate", "paper_loop", "web_operation"})
MAX_DELEGATED_WEB_JOBS = 2
JOB_ID_RE = re.compile(r"^atg-\d{6}-\d{6}-[a-z_]+-[0-9a-f]{6}$")
_KIND_RE = re.compile(r"^[a-z_]+$")
# A worker that has not reported a pid this long after creation never started.
_SPAWN_GRACE_S = 60.0

_STATE_LOCK = threading.Lock()


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def is_local_job_id(value: str) -> bool:
    return bool(JOB_ID_RE.fullmatch(value or ""))


def job_dir(job_id: str) -> Path:
    if not is_local_job_id(job_id):
        raise ValueError(tr('Not a local job ID (atg-…): {v0}', v0=job_id))
    return jobs_dir() / job_id


def _lineage_path(parent_id: str) -> Path:
    # Parent identifiers may come from different native backends; do not use
    # their raw values as filesystem names.
    key = hashlib.sha256(parent_id.encode("utf-8")).hexdigest()[:32]
    return var_dir() / "web-lineage" / key


def _delegated_web_parent(kind: str, params: dict[str, Any]) -> str:
    return str(params.get("delegated_job") or "").strip() if kind in WEB_JOB_KINDS else ""


def _delegated_web_jobs(parent_id: str) -> list[str]:
    """Read lineage without refreshing workers or entering the dispatch ledger."""
    root = jobs_dir()
    if not root.is_dir():
        return []
    result = []
    for entry in root.iterdir():
        if not is_local_job_id(entry.name):
            continue
        try:
            request = load_request(entry.name)
        except (OSError, ValueError):
            continue
        if _delegated_web_parent(str(request.get("kind") or ""), request.get("params") or {}) != parent_id:
            continue
        try:
            state = load_state(entry.name)
        except (OSError, ValueError):
            # A matching request with unreadable state cannot prove a stop.
            # Keep its quota/reservation and let cancellation retry it later.
            result.append(entry.name)
            continue
        if state.get("status") in ACTIVE_STATUSES or state.get("termination_confirmed") is False:
            result.append(entry.name)
    return result


def create_job(kind: str, params: dict[str, Any], *, before_publish: Callable[[Path, dict, dict], None] | None = None) -> str:
    parent_id = _delegated_web_parent(kind, params)
    if not parent_id:
        return _create_job(kind, params, before_publish=before_publish)
    from agent_telegram.locks import _exclusive_lock

    lineage = _lineage_path(parent_id)
    # Count and publish under one cross-process lock. A refused third submit
    # creates no queued job, worker or retry-index reservation.
    with _exclusive_lock(lineage.with_suffix(".lock")):
        if lineage.with_suffix(".cancelled").is_file():
            raise ValueError(tr('The parent agent job was cancelled; no more web jobs can be submitted.'))
        if len(_delegated_web_jobs(parent_id)) >= MAX_DELEGATED_WEB_JOBS:
            raise ValueError(tr('Each delegated agent job may run at most 2 web jobs at once; wait for or cancel an existing job before submitting another.'))
        return _create_job(kind, params, before_publish=before_publish)


def _create_job(kind: str, params: dict[str, Any], *, before_publish: Callable[[Path, dict, dict], None] | None = None) -> str:
    if not _KIND_RE.fullmatch(kind):
        raise ValueError(f"invalid job kind: {kind}")
    stamp = datetime.now().strftime("%y%m%d-%H%M%S")
    for _ in range(32):
        job_id = f"atg-{stamp}-{kind}-{secrets.token_hex(3)}"
        root = jobs_dir() / job_id
        try:
            root.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        break
    else:  # pragma: no cover - 16M ids per second would be needed
        raise RuntimeError("could not allocate a job id")
    created = now_iso()
    request = {"job_id": job_id, "kind": kind, "params": params, "created_at": created}
    state = {
            "job_id": job_id,
            "kind": kind,
            "status": "queued",
            "phase": "queued",
            "created_at": created,
            "updated_at": created,
        }
    parent_id = _delegated_web_parent(kind, params)
    if parent_id:
        state["delegated_job"] = parent_id
    if before_publish is not None:
        before_publish(root, request, state)
    atomic_write_json(root / "request.json", request)
    atomic_write_json(root / "state.json", state)
    return job_id


def load_request(job_id: str) -> dict[str, Any]:
    return read_json(job_dir(job_id) / "request.json")


def load_state(job_id: str) -> dict[str, Any]:
    return read_json(job_dir(job_id) / "state.json")


def update_state(job_id: str, **fields: Any) -> dict[str, Any]:
    with _STATE_LOCK:
        path = job_dir(job_id) / "state.json"
        state = read_json(path)
        state.update(fields)
        state["updated_at"] = now_iso()
        atomic_write_json(path, state)
    # Shared-dispatcher notifications have no consumer in this distribution.
    return state


def write_result(job_id: str, result: dict[str, Any]) -> None:
    atomic_write_json(job_dir(job_id) / "result.json", result)
    # Results remain in the native job store; no shared-dispatcher observer.


_RESULT_PATH_FIELDS = frozenset({
    "diff_path", "events_path", "prompt_path", "last_message_path", "reply_path",
    "saved_to", "manifest_path", "rejected_reply_path", "answer_file",
    "run_dir", "resume_run", "output_dir", "review_path", "review_markdown",
})
_RESULT_ARTIFACT_LISTS = frozenset({"files", "images", "staged_images", "published_images", "attachments"})
_RESULT_ARTIFACT_FIELDS = _RESULT_ARTIFACT_LISTS | {"path", "source_path", "normalized_path"}


def _relocate_result_paths(value: Any, *, field: str = "", artifact: bool = False) -> Any:
    """Expose usable result artifacts without changing the persisted result JSON.

    This is a reading/display projection, not an input to a content digest.
    Hash-bound pipeline requests, checkpoints and manifests keep their original
    strings and relocate only when opening a file. Restrict this projection to
    established job-result artifact fields rather than arbitrary ``*_path`` keys.
    """
    if isinstance(value, dict):
        return {key: _relocate_result_paths(item, field=key, artifact=artifact)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_relocate_result_paths(item, field=field, artifact=artifact or field in _RESULT_ARTIFACT_LISTS)
                for item in value]
    if isinstance(value, str) and (field in _RESULT_PATH_FIELDS or artifact and field in _RESULT_ARTIFACT_FIELDS):
        return str(relocate_stored(value)) if is_legacy_stored(value) else value
    return value


def load_result(job_id: str) -> dict[str, Any] | None:
    path = job_dir(job_id) / "result.json"
    return _relocate_result_paths(read_json(path)) if path.is_file() else None


# --- process identity -------------------------------------------------------

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class _ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _kernel32.Process32FirstW.restype = _kernel32.Process32NextW.restype = wintypes.BOOL

    _PROCESS_TERMINATE = 0x0001
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259

    def _creation_time(handle: Any) -> int | None:
        creation, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not _kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        return (creation.dwHighDateTime << 32) | creation.dwLowDateTime

    def process_creation_time(pid: int) -> int | None:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            return _creation_time(handle)
        finally:
            _kernel32.CloseHandle(handle)

    def process_alive(pid: int, created: int | None) -> bool:
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
                return False
            return created is None or _creation_time(handle) == created
        finally:
            _kernel32.CloseHandle(handle)

    def terminate_process(pid: int, created: int | None) -> bool:
        handle = _kernel32.OpenProcess(_PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            if created is not None and _creation_time(handle) != created:
                return False  # the pid was reused by another process
            return bool(_kernel32.TerminateProcess(handle, 1))
        finally:
            _kernel32.CloseHandle(handle)

else:  # pragma: no cover - the toolkit targets Windows; keep a portable fallback
    import signal

    def process_creation_time(pid: int) -> int | None:
        return None

    def process_alive(pid: int, created: int | None) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def terminate_process(pid: int, created: int | None) -> bool:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            return False
        return True


# --- worker lifecycle ---------------------------------------------------------


def _worker_command(job_id: str) -> list[str]:
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    if os.name == "nt" and windowless.is_file():
        executable = windowless
    return [str(executable), "-m", "agent_telegram.worker", job_id]


def spawn_worker(job_id: str) -> int:
    request = load_request(job_id)
    parent_id = _delegated_web_parent(str(request.get("kind") or ""), request.get("params") or {})
    if not parent_id:
        return _spawn_worker(job_id)
    from agent_telegram.locks import _exclusive_lock

    lineage = _lineage_path(parent_id)
    # Coordinate with cancellation as well as creation: a local request can be
    # published just before its parent is cancelled, but must not start later.
    with _exclusive_lock(lineage.with_suffix(".lock")):
        if lineage.with_suffix(".cancelled").is_file() or load_state(job_id).get("status") not in ACTIVE_STATUSES:
            raise ValueError(tr('The parent agent job or web job was cancelled; the web job cannot start.'))
        return _spawn_worker(job_id)


def _spawn_worker(job_id: str) -> int:
    root = job_dir(job_id)
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["AGENT_TG_VAR_DIR"] = str(var_dir())
    command = _worker_command(job_id)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | breakaway
    with (root / "worker.log").open("ab") as log:
        options: dict[str, Any] = {
            "cwd": str(PACKAGE_ROOT),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": log,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        if os.name != "nt":
            options["start_new_session"] = True
        try:
            process = subprocess.Popen(command, creationflags=flags, **options)
        except OSError:
            # Some hosts run MCP servers inside a job object that forbids breakaway.
            process = subprocess.Popen(command, creationflags=flags & ~breakaway, **options)
    atomic_write_json(
        root / "spawn.json",
        {"pid": process.pid, "pid_created": process_creation_time(process.pid), "spawned_at": now_iso()},
    )
    return process.pid


_SETTLED_LINEAGE_PATHS: set[Path] = set()


def cancel_delegated_web_jobs(parent_id: str) -> list[dict[str, Any]]:
    """Cancel a parent's active local web waits through ``web_job``'s path.

    This never calls a daemon submit/resume endpoint. Already submitted paper
    jobs keep their daemon-side uncertainty and cancellation rules unchanged.
    The fence also prevents a late child MCP request from starting another wait.
    """
    parent_id = str(parent_id or "").strip()
    if not parent_id:
        return []
    lineage = _lineage_path(parent_id)
    settled_marker = lineage.with_suffix(".settled")
    if settled_marker in _SETTLED_LINEAGE_PATHS:
        return []
    if settled_marker.is_file():
        if len(_SETTLED_LINEAGE_PATHS) > 4096:
            _SETTLED_LINEAGE_PATHS.clear()
        _SETTLED_LINEAGE_PATHS.add(settled_marker)
        return []
    from agent_telegram.locks import _exclusive_lock

    with _exclusive_lock(lineage.with_suffix(".lock")):
        if settled_marker.is_file():
            if len(_SETTLED_LINEAGE_PATHS) > 4096:
                _SETTLED_LINEAGE_PATHS.clear()
            _SETTLED_LINEAGE_PATHS.add(settled_marker)
            return []
        lineage.with_suffix(".cancelled").touch(exist_ok=True)
        job_ids = _delegated_web_jobs(parent_id)
        if not job_ids:
            settled_marker.touch(exist_ok=True)
            if len(_SETTLED_LINEAGE_PATHS) > 4096:
                _SETTLED_LINEAGE_PATHS.clear()
            _SETTLED_LINEAGE_PATHS.add(settled_marker)
            return []
    states = []
    failed = False
    for job_id in job_ids:
        try:
            states.append(cancel_job(job_id))
        except (OSError, ValueError):
            # One unreadable/racing child must not prevent stopping its siblings
            # or the native parent. The broker can retry this parent's sweep.
            failed = True
    with _exclusive_lock(lineage.with_suffix(".lock")):
        # The cancellation fence forbids later submissions and spawns, so a
        # settled parent never needs another full jobs-directory scan. Unknown
        # stops remain visible to future broker sweeps until confirmed.
        if not failed and not _delegated_web_jobs(parent_id):
            settled_marker.touch(exist_ok=True)
            if len(_SETTLED_LINEAGE_PATHS) > 4096:
                _SETTLED_LINEAGE_PATHS.clear()
            _SETTLED_LINEAGE_PATHS.add(settled_marker)
    return states


def _worker_identity(job_id: str, state: dict[str, Any]) -> tuple[int | None, int | None]:
    if state.get("pid"):
        return int(state["pid"]), state.get("pid_created")
    spawn = job_dir(job_id) / "spawn.json"
    if spawn.is_file():
        info = read_json(spawn)
        return int(info["pid"]), info.get("pid_created")
    return None, None


def process_tree_identities(pid: int, created: int | None) -> list[dict[str, int | None]] | None:
    """Snapshot owned descendants with creation times; None means unknown.

    The process snapshot is taken before termination. Keeping every identity
    lets cancellation detect a surviving grandchild after its parent exits.
    """
    if not process_alive(pid, created):
        return []
    if os.name != "nt":
        return None  # no portable process-tree proof
    handle = _kernel32.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        return None
    parents: dict[int, int] = {}
    entry = _ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        found = _kernel32.Process32FirstW(handle, ctypes.byref(entry))
        while found:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            found = _kernel32.Process32NextW(handle, ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(handle)
    if process_creation_time(pid) != created or created is None:
        return None
    owned = {pid}
    created_times: dict[int, int | None] = {pid: created}
    while True:
        children = set()
        for candidate, parent in parents.items():
            if parent not in owned or candidate in owned:
                continue
            child_created = process_creation_time(candidate)
            parent_created = created_times.get(parent)
            # An orphan whose former parent's PID was reused is not ours.
            if child_created is not None and parent_created is not None and child_created < parent_created:
                continue
            created_times[candidate] = child_created
            children.add(candidate)
        if children <= owned:
            break
        owned |= children
    return [{"pid": member, "created": created_times[member]} for member in sorted(owned)]


def tree_members_alive(members: list[dict[str, Any]]) -> bool:
    return any(process_alive(int(member["pid"]), member.get("created")) for member in members)


def _age_seconds(state: dict[str, Any]) -> float:
    try:
        created = datetime.fromisoformat(str(state.get("created_at")))
    except ValueError:
        return 0.0
    return (datetime.now().astimezone() - created).total_seconds()


def refresh_state(job_id: str) -> dict[str, Any]:
    """Load state; mark an active job orphaned when its worker is gone."""

    state = load_state(job_id)
    if state.get("status") not in ACTIVE_STATUSES:
        return state
    pid, created = _worker_identity(job_id, state)
    if pid is None:
        if _age_seconds(state) < _SPAWN_GRACE_S:
            return state
    elif process_alive(pid, created):
        return state
    state = load_state(job_id)  # the worker may have finished meanwhile
    if state.get("status") not in ACTIVE_STATUSES:
        return state
    result = load_result(job_id)
    if result is not None and result.get("status") in TERMINAL_STATUSES:
        return update_state(job_id, status=result["status"], phase="done", finished_at=now_iso())
    return update_state(
        job_id,
        status="orphaned",
        phase="worker_exited",
        finished_at=now_iso(),
        error=tr('The background worker process exited unexpectedly; see worker.log in the job directory.'),
        termination_confirmed=not bool(state.get("child_pid") and process_alive(int(state["child_pid"]), state.get("child_created"))) and not tree_members_alive(state.get("cancel_tree") or []),
    )


def cancel_job(job_id: str) -> dict[str, Any]:
    state = refresh_state(job_id)
    child_live = bool(state.get("child_pid") and process_alive(int(state["child_pid"]), state.get("child_created"))) or tree_members_alive(state.get("cancel_tree") or [])
    if state.get("status") in TERMINAL_STATUSES and not (child_live or state.get("termination_confirmed") is False):
        return state
    pid, created = _worker_identity(job_id, state)
    # Stop the owned process tree first. Job-object attachment may have failed
    # on an old job, so terminating only the worker is not enough.
    child_pid, child_created = state.get("child_pid"), state.get("child_created")
    previous_members = list(state.get("cancel_tree") or [])
    root_pid, root_created = (pid, created) if pid and pid != os.getpid() else (child_pid, child_created)
    observed = process_tree_identities(int(root_pid), root_created) if root_pid else []
    members = previous_members + [item for item in observed or [] if item not in previous_members]
    tree_known = observed is not None and (not state.get("cancel_tree_unknown") or bool(previous_members))
    tree_killed = True
    if child_pid and process_alive(int(child_pid), child_created):
        from agent_telegram.tgbridge.winsys import kill_tree
        tree_killed = kill_tree(int(child_pid), child_created)
    # A prior partial taskkill may have removed the root, leaving a descendant.
    for member in members:
        member_pid = int(member["pid"])
        if member_pid not in {pid, child_pid, os.getpid()} and process_alive(member_pid, member.get("created")):
            from agent_telegram.tgbridge.winsys import kill_tree
            tree_killed = kill_tree(member_pid, member.get("created")) and tree_killed
    terminated = terminate_process(pid, created) if pid else False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if not (pid and process_alive(pid, created)) and not (child_pid and process_alive(int(child_pid), child_created)) and not tree_members_alive(members):
            break
        time.sleep(0.05)
    confirmed = (tree_known or bool(state.get("job_object_bound"))) and tree_killed and not (pid and process_alive(pid, created)) and not (child_pid and process_alive(int(child_pid), child_created)) and not tree_members_alive(members)
    note = (
        # The worker holds each headless CLI in a kill-on-close job object, so
        # ending the worker ends the whole process tree too. Keep Codex's
        # established note stable for callers that match it.
        tr('The background Codex process was terminated; its file changes are preserved. Check the working directory.')
        if state.get("kind") == "codex_exec"
        else tr('The background Claude Code headless CLI process was terminated; its file changes are preserved. Check the working directory.')
        if state.get("kind") == "claude_exec"
        else tr('The background Pi headless CLI process was terminated; its file changes are preserved. Check the working directory.')
        if state.get("kind") == "pi_exec"
        else tr('The background DSH headless CLI process was terminated; its file changes are preserved. Check the working directory.')
        if state.get("kind") == "dsh_exec"
        else tr('Stopped waiting; work already started in the ChatGPT page or Codex conversation may continue to completion.')
    )
    return update_state(
        job_id,
        status="cancelled",
        phase="cancelled",
        finished_at=now_iso(),
        worker_terminated=terminated,
        termination_confirmed=confirmed,
        cancel_tree=members,
        cancel_tree_unknown=not tree_known,
        tree_kill_succeeded=tree_killed,
        note=note,
    )


def _label(kind: str, params: dict[str, Any]) -> dict[str, Any]:
    if kind == "codex":
        return {"thread_id": params.get("thread_id"), "thread_name": params.get("thread_name")}
    text = params.get("prompt") or params.get("file") or params.get("resume_run") or ""
    return {"label": str(text)[:80]}


_INACTIVE_WEB_JOB_DIRS: set[Path] = set()


def active_web_jobs() -> list[str]:
    """Return active local web job IDs (ask/review/draw/image_iterate/paper_loop/web_operation)."""
    root = jobs_dir()
    if not root.is_dir():
        return []
    active: list[str] = []
    for entry in root.iterdir():
        if entry in _INACTIVE_WEB_JOB_DIRS:
            continue
        if not is_local_job_id(entry.name) or not (entry / "state.json").is_file():
            continue
        try:
            state = load_state(entry.name)
            if state.get("kind") not in WEB_JOB_KINDS or state.get("status") not in ACTIVE_STATUSES:
                if len(_INACTIVE_WEB_JOB_DIRS) > 8192:
                    _INACTIVE_WEB_JOB_DIRS.clear()
                _INACTIVE_WEB_JOB_DIRS.add(entry)
                continue
            state = refresh_state(entry.name)
        except (OSError, ValueError):
            continue
        if state.get("status") in ACTIVE_STATUSES:
            active.append(entry.name)
        else:
            if len(_INACTIVE_WEB_JOB_DIRS) > 8192:
                _INACTIVE_WEB_JOB_DIRS.clear()
            _INACTIVE_WEB_JOB_DIRS.add(entry)
    return sorted(active)


def list_jobs(limit: int = 20, *, active_only: bool = False) -> list[dict[str, Any]]:
    root = jobs_dir()
    if not root.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name, reverse=True):
        if not is_local_job_id(entry.name) or not (entry / "state.json").is_file():
            continue
        try:
            state = refresh_state(entry.name)
            # Health only needs active jobs. Historical request bodies can be
            # large and must not consume its bounded probe deadline.
            if active_only and state.get("status") not in ACTIVE_STATUSES:
                continue
            request = load_request(entry.name)
        except (OSError, ValueError):
            continue
        items.append(
            {
                "job_id": entry.name,
                "kind": state.get("kind"),
                "status": state.get("status"),
                "phase": state.get("phase"),
                "created_at": state.get("created_at"),
                "finished_at": state.get("finished_at"),
                **_label(str(state.get("kind")), request.get("params") or {}),
            }
        )
        if len(items) >= limit:
            break
    return items

"""Bridge-owned `claude -p` runs: /new claude and follow-ups to sessions without a live process.

The bridge calls claude.exe by absolute path (never a .cmd/sh shim), passes the
prompt on stdin (never in argv), strips the parent Claude Code / bridge variables
from the environment, logs stream-json output to a file and groups the run in a
job object so /stop can end the whole tree. Runs are detached (own process group,
breakaway from the bridge's job) and survive a bridge restart; `to_dict` keeps
what is needed to find them again.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent_telegram.i18n import tr
from agent_telegram import jobs
from agent_telegram.tgbridge.winsys import JobObject, kill_tree

PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "bypassPermissions")

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_VERSION_RE = re.compile(r"v?(\d+(?:\.\d+)*)(?:[-+][0-9A-Za-z.-]*)?")
_SCRUB_NAMES = {"CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT"}
_SCRUB_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_AGENT_SDK_", "AGENT_TELEGRAM_", "TGBRIDGE_")
_SHIMS = ("claude.cmd", "claude", "claude.ps1")
_NPM_EXE = Path("node_modules") / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_RESULT_KEYS = ("is_error", "result", "session_id", "subtype", "total_cost_usd")
_RESULT_TAIL_BYTES = 16 * 1024 * 1024
_STDIN_TIMEOUT_S = 60.0

# Candidate priority when two installs carry the same version (higher wins).
_PRIO_DESKTOP_MSIX = 3
_PRIO_DESKTOP_APPDATA = 2
_PRIO_NPM = 1
_PRIO_PATH_EXE = 0


# --- locating claude.exe ------------------------------------------------------------------


def _version(text: str) -> tuple[int, ...] | None:
    match = _VERSION_RE.fullmatch(text.strip())
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _is_exe(path: Path) -> bool:
    try:
        return path.suffix.lower() == ".exe" and path.is_file()
    except OSError:
        return False


def _version_dirs(root: Path, priority: int) -> list[tuple[tuple[int, ...], int, Path]]:
    found: list[tuple[tuple[int, ...], int, Path]] = []
    try:
        children = list(root.iterdir())
    except OSError:
        return found
    for child in children:
        version = _version(child.name)
        exe = child / "claude.exe"
        if version is not None and _is_exe(exe):
            found.append((version, priority, exe))
    return found


def _desktop_candidates() -> list[tuple[tuple[int, ...], int, Path]]:
    found: list[tuple[tuple[int, ...], int, Path]] = []
    localappdata = os.environ.get("LOCALAPPDATA", "")
    if localappdata:
        try:
            packages = sorted(Path(localappdata, "Packages").glob("Claude_*"))
        except OSError:
            packages = []
        for package in packages:
            root = package / "LocalCache" / "Roaming" / "Claude" / "claude-code"
            found += _version_dirs(root, _PRIO_DESKTOP_MSIX)
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        found += _version_dirs(Path(appdata) / "Claude" / "claude-code", _PRIO_DESKTOP_APPDATA)
    return found


def _package_version(package_json: Path) -> tuple[int, ...]:
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    value = data.get("version") if isinstance(data, dict) else None
    return (_version(value) if isinstance(value, str) else None) or ()


def _path_candidates() -> list[tuple[tuple[int, ...], int, Path]]:
    """npm installs behind a `claude` shim on PATH, plus bare claude.exe files (version unknown)."""

    found: list[tuple[tuple[int, ...], int, Path]] = []
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        folder = Path(entry)
        if not entry or not folder.is_absolute():  # "." and friends would depend on the bridge's cwd
            continue
        try:
            has_shim = any((folder / name).is_file() for name in _SHIMS)
        except OSError:
            continue
        npm_exe = folder / _NPM_EXE
        if has_shim and _is_exe(npm_exe):
            found.append((_package_version(npm_exe.parent.parent / "package.json"), _PRIO_NPM, npm_exe))
        bare = folder / "claude.exe"
        if _is_exe(bare) and not _is_alias(bare):
            found.append(((), _PRIO_PATH_EXE, bare))
    return found


def _is_alias(path: Path) -> bool:
    """A 0-byte App Execution Alias (WindowsApps); it may launch a GUI app rather than the CLI."""

    try:
        return path.stat().st_size == 0
    except OSError:
        return True


def find_claude_exe(configured: str = "") -> Path | None:
    """The claude.exe to run: the configured .exe when it exists, else the highest installed version.

    Looks at Claude Desktop's bundled versions (MSIX real path, then %APPDATA%) and npm
    installs whose shim is on PATH. Never returns a .cmd/.ps1/sh shim.
    """

    if configured and configured.strip():
        pinned = Path(os.path.expandvars(configured.strip().strip('"'))).expanduser()
        if _is_exe(pinned):
            return pinned
    candidates = _desktop_candidates() + _path_candidates()
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


# --- environment and auth ------------------------------------------------------------------


def scrubbed_env(extra: dict | None = None) -> dict[str, str]:
    """os.environ without the parent session's Claude Code and bridge variables, marked headless."""

    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if upper in _SCRUB_NAMES or upper.startswith(_SCRUB_PREFIXES):
            continue
        env[key] = value
    env["AGENT_TELEGRAM_HEADLESS"] = "1"
    for key, value in (extra or {}).items():
        env[str(key)] = str(value)
    return env


def _json_object(text: str) -> dict | None:
    text = text.strip()
    for candidate in (text, text[text.find("{") : text.rfind("}") + 1] if "{" in text else ""):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def auth_status(exe: Path, *, run: Callable[..., Any] = subprocess.run) -> dict:
    """`claude auth status` as a dict; {"loggedIn": False, "error": ...} when it cannot be read."""

    try:
        completed = run(
            [str(exe), "auth", "status"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=30,
            env=scrubbed_env(),
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"loggedIn": False, "error": tr('claude auth status timed out')}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"loggedIn": False, "error": tr('Cannot run claude: {v0}', v0=type(exc).__name__)}
    stdout = completed.stdout or b""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", errors="replace")
    data = _json_object(stdout)
    if data is not None:
        data["loggedIn"] = bool(data.get("loggedIn"))
        return data
    stderr = completed.stderr or b""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    first = next((line.strip() for line in stderr.splitlines() if line.strip()), "")
    detail = tr(': {detail}', detail=first[:200]) if first else ""
    return {"loggedIn": False, "error": tr('claude auth status returned no JSON (exit code {v0}){v1}', v0=completed.returncode, v1=detail)}


# --- runs ----------------------------------------------------------------------------------


@dataclass
class HeadlessRun:
    session_id: str
    pid: int
    created: int | None
    log_path: Path
    started_at: float
    cwd: str
    resume: bool
    job: JobObject | None = field(default=None, repr=False, compare=False)
    process: subprocess.Popen | None = field(default=None, repr=False, compare=False)
    log_offset: int = 0  # log size before this run wrote to it (the log may be shared across runs)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "created": self.created,
            "log_path": str(self.log_path),
            "started_at": self.started_at,
            "cwd": self.cwd,
            "resume": self.resume,
            "log_offset": self.log_offset,
        }

    @classmethod
    def from_dict(cls, data: dict) -> HeadlessRun:
        """A run restored after a bridge restart: no job handle and no Popen object."""

        created = data.get("created")
        return cls(
            session_id=str(data["session_id"]),
            pid=int(data["pid"]),
            created=int(created) if created is not None else None,
            log_path=Path(data["log_path"]),
            started_at=float(data.get("started_at") or 0.0),
            cwd=str(data.get("cwd") or ""),
            resume=bool(data.get("resume")),
            log_offset=max(0, int(data.get("log_offset") or 0)),
        )


def _validate(exe: Path, cwd: Path, prompt: str, session_id: str, permission_mode: str) -> None:
    if not isinstance(session_id, str) or not _UUID_RE.fullmatch(session_id):
        raise ValueError(tr('Session id must be a UUID'))
    if permission_mode not in PERMISSION_MODES:
        raise ValueError(tr('Unsupported permission mode: {v0}', v0=permission_mode))
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError(tr('Prompt is empty'))
    if not cwd.is_dir():
        raise ValueError(tr('Working directory does not exist: {v0}', v0=cwd))
    if not _is_exe(exe):
        raise ValueError(tr('Cannot find claude.exe: {v0}', v0=exe))


def _feed_prompt(process: Any, data: bytes, timeout_s: float) -> None:
    """Write the prompt to the child's stdin and close it, giving up after timeout_s.

    A prompt larger than the pipe buffer blocks until claude reads it; a child that
    hangs before reading must not hang the bridge thread that started it. Raises
    TimeoutError, or the OSError of the write (child exited before reading).
    """

    failure: list[BaseException] = []

    def feed() -> None:
        try:
            process.stdin.write(data)
            process.stdin.close()
        except BaseException as exc:  # reported to the caller below
            failure.append(exc)

    writer = threading.Thread(target=feed, name="claude-prompt", daemon=True)
    writer.start()
    writer.join(timeout_s)
    if writer.is_alive():
        raise TimeoutError("claude.exe did not read its prompt")
    if failure:
        raise failure[0]


def _abort(process: Any, job: JobObject | None, created: int | None) -> None:
    """End a run that could not be started properly, including anything it already spawned."""

    if job is None or not job.terminate():
        kill_tree(process.pid, created)
    if job is not None:
        job.close()
    try:
        process.kill()
    except OSError:
        pass


def start_run(
    *,
    exe: Path,
    cwd: Path,
    prompt: str,
    session_id: str,
    resume: bool,
    permission_mode: str,
    log_path: Path,
    popen: Callable[..., Any] = subprocess.Popen,
) -> HeadlessRun:
    """Start `claude -p` detached and windowless; the prompt goes to stdin, output to log_path."""

    exe, cwd, log_path = Path(exe), Path(cwd), Path(log_path)
    _validate(exe, cwd, prompt, session_id, permission_mode)
    argv = [
        str(exe), "-p", "--output-format", "stream-json", "--verbose",
        "--permission-mode", permission_mode,
        "--resume" if resume else "--session-id", session_id,
    ]  # fmt: skip
    data = prompt.encode("utf-8", errors="replace")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    offset = log_path.stat().st_size if log_path.is_file() else 0
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    flags = _NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | breakaway
    with log_path.open("ab") as log:
        options: dict[str, Any] = {
            "cwd": str(cwd),
            "env": scrubbed_env(),
            "stdin": subprocess.PIPE,
            "stdout": log,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        try:
            process = popen(argv, creationflags=flags, **options)
        except OSError:
            if not breakaway:
                raise
            # The bridge may run inside a job object that forbids breakaway.
            process = popen(argv, creationflags=flags & ~breakaway, **options)
    job: JobObject | None = None
    try:
        job = JobObject()
        if not job.assign(process.pid):
            job.close()
            job = None
    except OSError:
        job = None
    created = jobs.process_creation_time(process.pid)
    try:
        _feed_prompt(process, data, _STDIN_TIMEOUT_S)
    except TimeoutError as exc:
        _abort(process, job, created)
        raise OSError(tr('The claude process did not read the prompt within {v0:g} seconds; it was terminated (see the log)', v0=_STDIN_TIMEOUT_S)) from exc
    except Exception as exc:  # a broken pipe, or anything else that kept the prompt from arriving
        _abort(process, job, created)
        raise OSError(tr('The claude process exited before reading the prompt (see the log)')) from exc
    return HeadlessRun(
        session_id=session_id,
        pid=process.pid,
        created=created,
        log_path=log_path,
        started_at=time.time(),
        cwd=str(cwd),
        resume=resume,
        job=job,
        process=process,
        log_offset=offset,
    )


def run_alive(run: HeadlessRun) -> bool:
    if run.process is not None and run.process.poll() is not None:
        return False
    return jobs.process_alive(run.pid, run.created)


def stop_run(run: HeadlessRun) -> bool:
    """End the run's whole process tree: its job object when this bridge holds one, else taskkill /T."""

    if run.job is not None and run.job.terminate():
        return True
    return kill_tree(run.pid, run.created)


def parse_result(log_path: Path, *, offset: int = 0, max_bytes: int = _RESULT_TAIL_BYTES) -> dict | None:
    """The last stream-json `result` record written at or after `offset`, reduced to its summary keys."""

    path = Path(log_path)
    try:
        size = path.stat().st_size
    except OSError:
        return None
    offset = max(0, int(offset or 0))
    if offset > size:  # the log was replaced; read it from the start
        offset = 0
    start = max(offset, size - max(1, max_bytes))
    cut = start > offset  # the tail may begin in the middle of a line
    try:
        with path.open("rb") as handle:
            handle.seek(start - 1 if cut else start)
            data = handle.read()
    except OSError:
        return None
    if cut:
        # Keep the first line only when the byte before it ends a line.
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline >= 0 else b""
    lines = data.splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith(b"{") or b'"result"' not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("type") == "result":
            return {key: record.get(key) for key in _RESULT_KEYS}
    return None

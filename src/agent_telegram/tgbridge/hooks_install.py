"""Install, inspect and remove the tgbridge hooks in Claude Code's settings.json.

Only the top-level ``hooks`` key of ``~/.claude/settings.json`` is changed, and only
the hook entries this module recognises as its own (``is_ours``); foreign hooks,
their matcher groups and every other key stay exactly as they were. The file is
rewritten defensively: it must parse to a JSON object first, the original bytes
are backed up to ``~/.claude/backups`` (newest three kept), the new content goes to
a temporary file in the same directory, the original is re-read and compared by
SHA-256 right before ``os.replace`` (a concurrent writer makes the whole operation
start over, at most three times), and after the replace the file is parsed again
and every key other than ``hooks`` must equal the pre-image (else the backup is
restored). Nothing here prints; error messages never contain file content, since
settings.json may hold access tokens.
"""

from __future__ import annotations

from agent_telegram.i18n import tr
from agent_telegram.fsutil import replace_atomic

import copy
import hashlib
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

HOOK_MODULE = "agent_telegram.tgbridge.hook_report"
# Persisted external ownership metadata only; never an import or launch target.
LEGACY_PACKAGE = "agent_telegram"
LEGACY_HOOK_MODULE = f"{LEGACY_PACKAGE}.tgbridge.hook_report"
HOOK_MODULES = frozenset({HOOK_MODULE, LEGACY_HOOK_MODULE})
TOKEN_HEADER = "X-TgBridge-Token"
TIMEOUT_HEADER = "X-TgBridge-Timeout"
COMMAND_EVENTS = ("SessionStart", "Stop", "StopFailure")
# Claude Code shows the permission dialog while this hook runs, so it may wait long for Telegram;
# the bridge reads the timeout from TIMEOUT_HEADER and answers 40 s before it (6 h at most).
PERMISSION_TIMEOUT_S = 6 * 3600 + 120
# event -> (hook server path, hook timeout in seconds)
HTTP_EVENTS = {"PreToolUse": ("/claude/pretooluse", 3), "PermissionRequest": ("/claude/permission", PERMISSION_TIMEOUT_S)}
COMMAND_TIMEOUT_S = 30
MAX_ATTEMPTS = 3
_BACKUP_TAG = ".bak-tgbridge-"
# Visible ASCII without "$": Claude Code interpolates $VAR / ${VAR} in http hook header values.
_TOKEN_RE = re.compile(r"^[\x21-\x23\x25-\x7e]+$")

Settings = dict[str, Any]
Transform = Callable[[Settings], Settings]


def default_pythonw() -> Path:
    """The windowless interpreter next to sys.executable (the venv's pythonw.exe) when present."""

    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        return exe
    sibling = exe.with_name("pythonw.exe")
    return sibling if sibling.is_file() else exe


def settings_path(claude_home: Path) -> Path:
    return claude_home / "settings.json"


def build_hooks(*, pythonw: Path, port: int, token: str, include_http: bool = True) -> dict[str, list[dict]]:
    """Our hook entries per event, one matcher group each (no matcher = every tool / every source).

    include_http=False leaves out the PreToolUse / PermissionRequest http hooks (while the
    bridge is not running they would delay every tool call).
    """

    command = {
        "type": "command",
        "command": str(pythonw),
        "args": ["-X", "utf8", "-P", "-m", HOOK_MODULE],
        "async": True,
        "timeout": COMMAND_TIMEOUT_S,
    }
    hooks: dict[str, list[dict]] = {event: [{"hooks": [dict(command, args=list(command["args"]))]}]
                                     for event in COMMAND_EVENTS}
    if include_http:
        for event, (route, timeout) in HTTP_EVENTS.items():
            headers = {TOKEN_HEADER: token}
            if event == "PermissionRequest":
                headers[TIMEOUT_HEADER] = str(timeout)
            hooks[event] = [{"hooks": [{
                "type": "http",
                "url": f"http://127.0.0.1:{port}{route}",
                "timeout": timeout,
                "headers": headers,
            }]}]
    return hooks


def is_ours(hook: Any) -> bool:
    """A hook entry installed by this module (recognised by content, not by position)."""

    if not isinstance(hook, dict):
        return False
    kind = hook.get("type")
    if kind == "command":
        args = hook.get("args")
        if isinstance(args, list) and any(module in args for module in HOOK_MODULES):
            return True
        command = hook.get("command")
        # The interpreter may still be packages\\agent_telegram\\.venv's pythonw.
        # Match the module, preserving unrelated hooks that use that interpreter.
        return isinstance(command, str) and bool(HOOK_MODULES.intersection(
            token.strip("\"'") for token in command.split()
        ))
    if kind == "http":
        url = hook.get("url")
        headers = hook.get("headers")
        if not isinstance(url, str) or not url.startswith("http://127.0.0.1:") or not isinstance(headers, dict):
            return False
        try:
            route = urlsplit(url).path
        except ValueError:
            return False
        return route.startswith("/claude/") and any(
            isinstance(name, str) and name.lower() == TOKEN_HEADER.lower() for name in headers
        )
    return False


def _inner_hooks(group: Any) -> list | None:
    inner = group.get("hooks") if isinstance(group, dict) else None
    return inner if isinstance(inner, list) else None


def _drop_ours(hooks: dict[str, Any], *, is_ours: Callable[[Any], bool] = is_ours) -> dict[str, Any]:
    """hooks without our entries; groups / event lists emptied by the removal are dropped."""

    result: dict[str, Any] = {}
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            result[event] = groups
            continue
        kept_groups: list[Any] = []
        removed = False
        for group in groups:
            inner = _inner_hooks(group)
            if inner is None:
                kept_groups.append(group)
                continue
            kept = [hook for hook in inner if not is_ours(hook)]
            if len(kept) == len(inner):
                kept_groups.append(group)
                continue
            removed = True
            if kept:
                kept_groups.append({**group, "hooks": kept})
        if kept_groups or not removed:
            result[event] = kept_groups
    return result


def merge(
    settings: Settings, ours: dict[str, list[dict]], *, is_ours: Callable[[Any], bool] = is_ours,
) -> Settings:
    """settings with our old entries removed everywhere and ``ours`` appended per event (pure)."""

    result = copy.deepcopy(settings)
    hooks = result.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(tr('The hooks field in settings.json is not a JSON object; no changes made.'))
    hooks = _drop_ours(hooks, is_ours=is_ours)
    for event, groups in ours.items():
        existing = hooks.get(event, [])
        if not isinstance(existing, list):
            raise ValueError(tr('settings.json hooks.{v0} is not an array; no changes made.', v0=event))
        hooks[event] = existing + copy.deepcopy(groups)
    if hooks or "hooks" in result:
        result["hooks"] = hooks
    return result


def strip(settings: Settings, *, is_ours: Callable[[Any], bool] = is_ours) -> Settings:
    """settings without our entries (pure); a hooks object emptied by the removal is dropped."""

    result = copy.deepcopy(settings)
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result
    cleaned = _drop_ours(hooks, is_ours=is_ours)
    if cleaned == hooks:
        return result
    if cleaned:
        result["hooks"] = cleaned
    else:
        del result["hooks"]
    return result


def _iter_hooks(settings: Any) -> Iterator[tuple[str, Any]]:
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    if not isinstance(hooks, dict):
        return
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            for hook in _inner_hooks(group) or []:
                yield event, hook


def describe(settings: Settings) -> dict[str, dict[str, int]]:
    """Number of hook entries per event: {"ours": {event: n}, "foreign": {event: n}}."""

    counts: dict[str, dict[str, int]] = {"ours": {}, "foreign": {}}
    for event, hook in _iter_hooks(settings):
        bucket = counts["ours" if is_ours(hook) else "foreign"]
        bucket[event] = bucket.get(event, 0) + 1
    return counts


# --- safe rewrite of settings.json -------------------------------------------------------


class _DuplicateKey(ValueError):
    pass


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _parse(raw: bytes, name: str) -> Settings:
    """The JSON object in raw; ValueError (never quoting the content) otherwise."""

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(tr('{v0} is not UTF-8 text; no changes made.', v0=name)) from None
    try:
        value = json.loads(text, object_pairs_hook=_object_without_duplicates)
    except _DuplicateKey:
        raise ValueError(tr('{v0} contains duplicate keys; rewriting it would lose one, so no changes were made.', v0=name)) from None
    except json.JSONDecodeError as exc:
        raise ValueError(tr('{v0} is not valid JSON (line {v1}, column {v2}); no changes made.', v0=name, v1=exc.lineno, v2=exc.colno)) from None
    if not isinstance(value, dict):
        raise ValueError(tr('The top level of {v0} is not a JSON object; no changes made.', v0=name))
    return value


def _read_bytes(path: Path, attempts: int = 20) -> bytes | None:
    """File content, None when it does not exist (retries a writer's brief sharing violation)."""

    for attempt in range(attempts):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)
    raise AssertionError("unreachable")


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _write_new(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _replace(source: Path, target: Path, attempts: int = 40) -> None:
    replace_atomic(source, target)


def _temp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.tgbridge-{uuid.uuid4().hex[:8]}.tmp")


def _atomic_write(path: Path, data: bytes) -> None:
    temp = _temp_path(path)
    try:
        _write_new(temp, data)
        _replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _backup_pattern(name: str) -> re.Pattern[str]:
    return re.compile(re.escape(name + _BACKUP_TAG) + r"(\d{8}-\d{6})(?:-(\d+))?$")


def _backup(backup_dir: Path, name: str, data: bytes) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for index in range(1000):
        candidate = backup_dir / f"{name}{_BACKUP_TAG}{stamp}{f'-{index}' if index else ''}"
        try:
            _write_new(candidate, data)
        except FileExistsError:
            continue
        return candidate
    raise FileExistsError(tr('No backup filename is available in {v0}.', v0=backup_dir))


def _rotate(backup_dir: Path, name: str, keep: int) -> None:
    pattern = _backup_pattern(name)
    found: list[tuple[tuple[str, int], Path]] = []
    try:
        entries = list(backup_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        match = pattern.match(entry.name)
        if match and entry.is_file():
            found.append(((match.group(1), int(match.group(2) or 0)), entry))
    found.sort(key=lambda item: item[0], reverse=True)
    for _, entry in found[max(keep, 0):]:
        try:
            entry.unlink()
        except OSError:
            pass


def _others(settings: Settings) -> Settings:
    return {key: value for key, value in settings.items() if key != "hooks"}


def apply(path: Path, transform: Transform, *, backup_dir: Path, keep: int = 3) -> bool:
    """Rewrite the JSON object at path through transform; False when nothing changes.

    A missing file counts as ``{}`` (created without a backup). Raises ValueError when the
    file is not a JSON object or transform touches keys other than ``hooks``, and
    RuntimeError when another writer keeps changing the file or the result fails the
    post-write check (the original is restored then).
    """

    if path.is_symlink():
        path = path.resolve(strict=True)  # rewrite the target and keep the link
    name = path.name
    for _attempt in range(MAX_ATTEMPTS):
        original = _read_bytes(path)
        before = {} if original is None else _parse(original, name)
        after = transform(copy.deepcopy(before))
        if not isinstance(after, dict):
            raise TypeError("transform must return a dict")
        if after == before:
            return False
        if _others(after) != _others(before):
            raise ValueError(tr('Only hooks in {v0} may be changed; other keys must stay the same. No changes made.', v0=name))
        try:
            data = (json.dumps(after, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        except UnicodeEncodeError:  # a lone surrogate from a \ud800 escape
            raise ValueError(tr('{v0} contains characters that cannot be encoded as UTF-8 (unpaired \\u surrogates); no changes made.', v0=name)) from None
        backup = None if original is None else _backup(backup_dir, name, original)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = _temp_path(path)
        try:
            _write_new(temp, data)
            if _digest(_read_bytes(path)) != _digest(original):
                if backup is not None:
                    backup.unlink(missing_ok=True)
                continue  # changed by someone else meanwhile: start over from the new content
            _replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        try:
            written = _read_bytes(path)
            ok = written is not None and _others(_parse(written, name)) == _others(before)
        except ValueError:
            ok = False
        if not ok:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_write(path, original)
            raise RuntimeError(tr('Validation of rewritten {v0} failed; the original file was restored.', v0=name))
        if backup is not None:
            _rotate(backup_dir, name, keep)
        return True
    raise RuntimeError(tr('Other programs repeatedly rewrote {v0} during the update ({v1} retries); no changes made.', v0=name, v1=MAX_ATTEMPTS))


# --- commands ------------------------------------------------------------------------------


def _check_install_args(pythonw: Path, port: int, token: str) -> None:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise ValueError(tr('The hook port must be an integer between 1 and 65535.'))
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        raise ValueError(tr('The hook token is empty or contains characters that cannot appear in an HTTP header.'))  # never echo the token
    if not pythonw.is_absolute() or not pythonw.is_file():
        raise ValueError(tr("Cannot find the hook's Python interpreter: {v0}", v0=pythonw))


def install(*, claude_home: Path, pythonw: Path, port: int, token: str, include_http: bool = True) -> bool:
    """Add (or refresh) our hooks; False when settings.json already has exactly these hooks."""

    _check_install_args(pythonw, port, token)
    ours = build_hooks(pythonw=pythonw, port=port, token=token, include_http=include_http)
    return apply(settings_path(claude_home), lambda settings: merge(settings, ours),
                 backup_dir=claude_home / "backups")


def remove(*, claude_home: Path) -> bool:
    """Remove our hooks; False when there were none (or no settings.json)."""

    return apply(settings_path(claude_home), strip, backup_dir=claude_home / "backups")


def status(*, claude_home: Path) -> dict[str, Any]:
    """Read-only summary of settings.json (never includes the token or other content)."""

    path = settings_path(claude_home)
    result: dict[str, Any] = {
        "path": str(path),
        "exists": False,
        "error": "",
        "ours": {},
        "foreign": {},
        "installed": False,
        "http_installed": False,
        "disable_all_hooks": False,
        "commands": [],
        "missing_commands": [],
        "ports": [],
        "outdated": [],  # events whose hooks an older version installed: run `hooks install` again
    }
    try:
        raw = _read_bytes(path)
    except OSError as exc:
        result["error"] = tr('Failed to read {v0}: {v1}', v0=path.name, v1=type(exc).__name__)
        return result
    if raw is None:
        return result
    result["exists"] = True
    try:
        settings = _parse(raw, path.name)
    except ValueError as exc:
        result["error"] = str(exc)
        return result
    counts = describe(settings)
    result.update(counts)
    result["installed"] = all(counts["ours"].get(event) for event in COMMAND_EVENTS)
    result["http_installed"] = all(counts["ours"].get(event) for event in HTTP_EVENTS)
    result["disable_all_hooks"] = settings.get("disableAllHooks") is True
    commands: set[str] = set()
    ports: set[int] = set()
    outdated: set[str] = set()
    for event, hook in _iter_hooks(settings):
        if not is_ours(hook):
            continue
        if hook.get("type") == "http" and event in HTTP_EVENTS:
            timeout = HTTP_EVENTS[event][1]
            headers = hook.get("headers") if isinstance(hook.get("headers"), dict) else {}
            if hook.get("timeout") != timeout or (
                    event == "PermissionRequest" and str(headers.get(TIMEOUT_HEADER)) != str(timeout)):
                outdated.add(event)
        if hook.get("type") == "command":
            # Only the exec form names an interpreter file; a shell-form command is a command line.
            if isinstance(hook.get("args"), list) and isinstance(hook.get("command"), str):
                commands.add(hook["command"])
        elif hook.get("type") == "http":
            try:
                port = urlsplit(hook["url"]).port
            except ValueError:
                port = None
            if port is not None:
                ports.add(port)
    result["commands"] = sorted(commands)
    result["missing_commands"] = [command for command in sorted(commands) if not Path(command).is_file()]
    result["ports"] = sorted(ports)
    result["outdated"] = sorted(outdated)
    return result

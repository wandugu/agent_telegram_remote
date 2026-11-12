"""Command line of the Telegram bridge.

    .\\.venv\\Scripts\\python.exe -m agent_telegram set-token          # paste the bot token (hidden input, stored with DPAPI)
    .\\.venv\\Scripts\\python.exe -m agent_telegram bind               # bind the group and the owner with a one-time code
    .\\.venv\\Scripts\\python.exe -m agent_telegram run                # the bridge itself (the scheduled task runs this via pythonw)
    .\\.venv\\Scripts\\python.exe -m agent_telegram status             # configuration, running bridge, hooks, recent log
    .\\.venv\\Scripts\\python.exe -m agent_telegram hooks install|remove|status
    .\\.venv\\Scripts\\python.exe -m agent_telegram test-push          # send a test message to the group
    .\\.venv\\Scripts\\python.exe -m agent_telegram notify --file F    # send a plain-text notice from another local tool
    .\\.venv\\Scripts\\python.exe -m agent_telegram notify --file F --photo IMG ...   # ... followed by images
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from dataclasses import fields
from typing import Any

from agent_telegram import distribution
from agent_telegram.i18n import LANGUAGES, set_language, tr

from agent_telegram import codex_watch
from agent_telegram.tgbridge import config as config_module
from agent_telegram.tgbridge import hooks_install
from agent_telegram.tgbridge.config import Config, claude_home, log_dir, tg_dir


def _status_url(cfg: Config) -> str:
    return f"http://127.0.0.1:{cfg.hook_port}/status"


def running_status(cfg: Config, timeout: float = 3.0) -> dict[str, Any] | None:
    """The running bridge's GET /status, or None when nothing answers."""

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(_status_url(cfg), timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("service") == "tgbridge" else None


def _api(cfg: Config, token: str) -> Any:
    from agent_telegram.tgbridge.telegram_api import BotAPI

    return BotAPI(token, proxy=config_module.resolve_proxy(cfg))


def _print(text: str = "") -> None:
    if sys.stdout is not None:
        print(text, flush=True)


def _print_warnings(cfg: Config) -> None:
    for warning in getattr(cfg, "warnings", []) or []:
        _print(tr('⚠️ Configuration: {v0}', v0=warning))


class _BridgeLogHandler(logging.Handler):
    """Route the sources' ``logging`` records (WARNING and up) into bridge.log; pythonw has no stderr."""

    def __init__(self, log: Any) -> None:
        super().__init__(logging.WARNING)
        self._log = log

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if record.exc_info:
                text += "\n" + logging.Formatter().formatException(record.exc_info)
            self._log.line(f"{record.name}: {text}", record.levelname)
        except Exception:  # noqa: BLE001 - logging must never break the bridge
            pass


# --- run ------------------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    set_language(Config.load().language or distribution.DEFAULT_LANGUAGE)
    from agent_telegram.gate import wait_until_open
    wait_until_open()
    from agent_telegram.settings import codex_home as _codex_home
    from agent_telegram.tgbridge.bridge import Bridge, BridgeLog
    from agent_telegram.tgbridge.hook_server import HookServer
    from agent_telegram.tgbridge.redact import Redactor, load_local_secrets
    from agent_telegram.tgbridge.state import BridgeState
    from agent_telegram.tgbridge.winsys import Presence

    config_module.ensure_dirs()
    lock = codex_watch.single_instance_lock(log_dir() / "bridge.lock")
    if lock is None:
        _print(tr('tgbridge is already running.'))
        return 0
    token = config_module.load_bot_token()
    cfg = Config.load()
    set_language(cfg.language or distribution.DEFAULT_LANGUAGE)
    redactor = Redactor()
    log = BridgeLog(log_dir(), redactor)
    for warning in getattr(cfg, "warnings", []) or []:
        log.line(tr('Configuration: {v0}', v0=warning), "WARNING")
    package_logger = logging.getLogger("agent_telegram")
    package_logger.addHandler(_BridgeLogHandler(log))
    package_logger.setLevel(logging.INFO)
    if not token or not cfg.bound:
        what = tr('No bot token: run set-token first') if not token else tr('No group or user bound yet: run bind first')
        log.line(tr('{what} (.\\.venv\\Scripts\\python.exe -m agent_telegram ...).', what=what), "ERROR")
        _print(what + tr('.'))
        _drop_hooks_nobody_serves(log)
        return 0  # a failing exit code would make the scheduled task restart it every minute
    hook_token = config_module.hook_token()
    assert hook_token is not None
    for value in [token, hook_token, *load_local_secrets(claude_home(), _codex_home())]:
        redactor.add_secret(value)
    state = BridgeState()
    from agent_telegram.tgbridge.telegram_api import BotAPI

    proxy = config_module.resolve_proxy(cfg)
    try:
        api = BotAPI(token, proxy=proxy)
    except Exception:  # noqa: BLE001 - pythonw has no stderr: the reason must reach bridge.log
        log.line(tr('Failed to create the Telegram client (proxy {v0}):\n{v1}', v0=proxy or tr('direct'), v1=traceback.format_exc()), "ERROR")
        return 1
    bridge = Bridge(cfg, state, api, redact=redactor, presence=Presence(cfg.presence_idle_s),
                    log=log, api_factory=lambda chosen: BotAPI(token, proxy=chosen), proxy=proxy)
    _install_excepthooks(log)
    codex = claude = antigravity = pi = dsh = None
    if cfg.codex_enabled:
        from agent_telegram.tgbridge.codex_source import CodexSource

        codex = CodexSource(cfg, bridge.on_event)
    if cfg.claude_enabled:
        from agent_telegram.tgbridge.claude_source import ClaudeSource
        from agent_telegram.tgbridge.config import SecretStore

        claude = ClaudeSource(cfg, bridge.on_event, secrets=SecretStore())
    if getattr(cfg, "antigravity_enabled", False):
        try:
            # Keep this import lazy: environments without Antigravity support still start the bridge.
            from agent_telegram.tgbridge.antigravity_source import AntigravitySource, default_home, find_stores

            stores = find_stores(default_home(cfg))
            if stores:
                antigravity = AntigravitySource(cfg, bridge.on_event)
        except Exception as exc:  # noqa: BLE001 - an optional source must not stop tgbridge
            log.line(tr('Failed to initialize the Antigravity source: {v0}: {v1}', v0=type(exc).__name__, v1=exc), "WARNING")
    if getattr(cfg, "pi_enabled", False):
        try:
            from agent_telegram.tgbridge.pi_source import PiSource, default_home as pi_default_home, _sessions_dirs as pi_sessions_dirs

            if pi_sessions_dirs(pi_default_home(cfg)):
                pi = PiSource(cfg, bridge.on_event)
        except Exception as exc:  # noqa: BLE001 - an optional source must not stop tgbridge
            log.line(tr('Failed to initialize the Pi source: {v0}: {v1}', v0=type(exc).__name__, v1=exc), "WARNING")
    if getattr(cfg, "dsh_enabled", False):
        try:
            from agent_telegram.tgbridge.dsh_source import DshSource, default_home as dsh_default_home

            if (dsh_default_home(cfg) / "sessions").is_dir():
                dsh = DshSource(cfg, bridge.on_event)
        except Exception as exc:  # noqa: BLE001 - an optional source must not stop tgbridge
            log.line(tr('Failed to initialize the DSH source: {v0}: {v1}', v0=type(exc).__name__, v1=exc), "WARNING")
    bridge.attach_sources(codex=codex, claude=claude, antigravity=antigravity, pi=pi, dsh=dsh)
    server = HookServer(cfg.hook_port, hook_token, bridge, log=log.line)
    try:
        server.start()
    except OSError as exc:
        # The single-instance lock is ours, so another program holds the port: never leave the
        # hooks talking to it (it would see tool inputs and could answer permission requests).
        log.line(tr('Another program is using 127.0.0.1:{v0} ({v1}); hooks removed.', v0=cfg.hook_port, v1=exc), "ERROR")
        _drop_hooks_nobody_serves(log)
        return 1
    log.line(tr('tgbridge started (pid {v0}), hook port {v1}, Telegram {v2}', v0=os.getpid(), v1=cfg.hook_port, v2=tr('via proxy ') + proxy if proxy else tr('direct')))
    _refresh_outdated_hooks(cfg, hook_token, log)
    code = 0
    try:
        code = bridge.run()
    except KeyboardInterrupt:
        pass
    except SystemExit as exc:
        code = int(exc.code or 0)
    except BaseException:
        log.line(tr('tgbridge exited unexpectedly:\n{v0}', v0=traceback.format_exc()), "ERROR")
        code = 1
    finally:
        bridge.stop()
        server.stop()
        log.line(tr('tgbridge stopped (exit code {v0})', v0=code))
    return code


def _refresh_outdated_hooks(cfg: Config, token: str, log: Any) -> None:
    """Rewrite our hooks when an older version installed them (other timeouts or headers).

    Only installed hooks are touched, and only once the hook server answers; the rest of
    settings.json stays as it is (hooks_install.apply checks that).
    """

    try:
        current = hooks_install.status(claude_home=claude_home())
        if current.get("http_installed") and current.get("outdated"):
            hooks_install.install(claude_home=claude_home(), pythonw=hooks_install.default_pythonw(),
                                  port=cfg.hook_port, token=token)
            log.line(tr('Updated hooks installed by an older version ({v0}).', v0=tr(', ').join(current['outdated'])))
    except Exception as exc:  # noqa: BLE001 - the old hooks keep working, with the old timeout
        log.line(tr('Failed to update older hooks: {v0}: {v1}', v0=type(exc).__name__, v1=exc), "WARNING")


def _drop_hooks_nobody_serves(log: Any) -> None:
    try:
        if hooks_install.remove(claude_home=claude_home()):
            log.line(tr('The bridge cannot run; tgbridge hooks were removed from ~/.claude/settings.json. Run hooks install again after fixing it.'),
                     "WARNING")
    except Exception as exc:  # noqa: BLE001
        log.line(tr('Failed to remove hooks: {v0}', v0=exc), "ERROR")


def _install_excepthooks(log: Any) -> None:
    """pythonw has no stderr: record uncaught exceptions of any thread in bridge.log."""

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit:
            return
        text = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        log.line(tr('Uncaught exception in thread {v0}:\n{v1}', v0=getattr(args.thread, 'name', '?'), v1=text), "ERROR")

    threading.excepthook = thread_hook


# --- set-token -------------------------------------------------------------------------------


def _apply_proxy_option(cfg: Config, value: str | None) -> bool:
    """--proxy auto|direct|URL: store it in the config (False on a bad value)."""

    if value is None:
        return True
    choice = value.strip()
    if choice.lower() == "auto":
        cfg.proxy = None
    elif choice.lower() == "direct":
        cfg.proxy = ""
    elif config_module._proxy_ok(choice):
        cfg.proxy = choice
    else:
        _print(tr('--proxy requires auto, direct or http://host:port (SOCKS is not supported).'))
        return False
    config_module.ensure_dirs()
    cfg.save()
    _print(tr('Proxy: {v0}', v0=config_module.resolve_proxy(cfg) or tr('direct')))
    return True


def cmd_set_token(args: argparse.Namespace) -> int:
    cfg = Config.load()
    if not _apply_proxy_option(cfg, args.proxy):
        return 2
    token = getpass.getpass(tr('Paste the bot token (input is hidden):')).strip()
    if not config_module.TOKEN_RE.fullmatch(token):
        _print(tr('Invalid format: a token looks like 123456789:AA... (the entire string from BotFather).'))
        return 2
    if not args.no_check:
        from agent_telegram.tgbridge.telegram_api import TelegramError

        try:
            me = _api(cfg, token).get_me()
        except TelegramError as exc:
            _print(tr('getMe failed with this token: {v0}\n(Is the proxy running? Use --proxy http://127.0.0.1:PORT to select a proxy, or --no-check to skip verification)', v0=exc))
            return 1
        cfg.bot_username = str(me.get("username") or "")
        _print(tr('Token is valid: @{v0}', v0=cfg.bot_username))
    config_module.ensure_dirs()
    config_module.save_bot_token(token)
    cfg.save()
    _print(tr('Saved with DPAPI encryption to {v0}.', v0=tg_dir()))
    if running_status(cfg) is not None:
        _print(tr('The bridge is running with the old token: run powershell -ExecutionPolicy Bypass -File .\\install_task.ps1 -Action Install to restart it.'))
    elif not cfg.bound:
        _print(tr('Next: .\\.venv\\Scripts\\python.exe -m agent_telegram bind'))
    return 0


# --- bind ------------------------------------------------------------------------------------


def cmd_bind(args: argparse.Namespace) -> int:
    from agent_telegram.tgbridge.telegram_api import TelegramError

    cfg = Config.load()
    if not _apply_proxy_option(cfg, args.proxy):
        return 2
    _print_warnings(cfg)
    token = config_module.load_bot_token()
    if not token:
        _print(tr('Run set-token first.'))
        return 2
    if running_status(cfg) is not None:
        _print(tr('The bridge is running (it will consume the messages bind needs). Stop it first: Stop-ScheduledTask -TaskName AgentTelegram-Bridge'))
        return 2
    api = _api(cfg, token)
    try:
        me = api.get_me()
        pending = api.get_updates(-1, timeout_s=0)
    except TelegramError as exc:
        _print(tr('Failed to connect to Telegram: {v0} (Is the proxy running? Use --proxy http://127.0.0.1:PORT to select a proxy)', v0=exc))
        return 1
    offset = pending[-1]["update_id"] + 1 if pending else None
    username = str(me.get("username") or "")
    code = f"{secrets.randbelow(10**6):06d}"
    _print(tr('Send this in the group (valid for 5 minutes):\n\n    /bind@{v0} {v1}\n', v0=username, v1=code))
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            updates = api.get_updates(offset, timeout_s=20, allowed_updates=["message"])
        except TelegramError as exc:
            _print(tr('getUpdates failed: {v0}; retrying…', v0=exc))
            time.sleep(3)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            text = str(message.get("text") or "").strip()
            parts = text.split()
            if len(parts) != 2 or parts[0].lower() not in ("/bind", f"/bind@{username.lower()}") or parts[1] != code:
                continue
            chat = message.get("chat") or {}
            user = message.get("from") or {}
            if chat.get("type") != "supergroup":
                _print(tr('This /bind was not sent in a supergroup (send it in a group with topics enabled).'))
                continue
            if user.get("is_bot") or message.get("sender_chat"):
                _print(tr('Ignored: the sender is a bot or anonymous administrator. Turn off anonymous admin mode and send from your own account.'))
                continue
            name = " ".join(str(user.get(part) or "") for part in ("first_name", "last_name")).strip()
            _print(tr('Group: {v0} (id {v1}, topics {v2})', v0=chat.get('title'), v1=chat.get('id'), v2=tr('enabled') if chat.get('is_forum') else tr('disabled!')))
            _print(tr('User: {v0} @{v1} (id {v2})', v0=name, v1=user.get('username') or '-', v2=user.get('id')))
            answer = input(tr('Confirm binding? [y/N] ')).strip().lower()
            if answer != "y":
                _print(tr('Cancelled.'))
                return 1
            cfg.chat_id = int(chat["id"])
            cfg.allowed_user_ids = [int(user["id"])]
            cfg.bot_username = username
            config_module.ensure_dirs()
            cfg.save()
            api.get_updates(offset, timeout_s=0)  # acknowledge the /bind update
            try:
                member = api.call("getChatMember", {"chat_id": cfg.chat_id, "user_id": me["id"]})
                if member.get("status") != "administrator" or not member.get("can_manage_topics"):
                    _print(tr('⚠️ This bot is not an administrator with Manage Topics permission yet: make it an administrator in the group and enable Manage Topics.'))
                api.send_message(cfg.chat_id, tr('✅ [{v0}] Bound to this group.', v0=cfg.machine), silent=True)
            except TelegramError as exc:
                _print(tr('Binding saved, but the permission check / confirmation message failed: {v0}', v0=exc))
            _print(tr('Bound. Next: powershell -ExecutionPolicy Bypass -File .\\install_task.ps1 -Action Install (in a non-elevated PowerShell)'))
            return 0
    _print(tr('No valid /bind received within 5 minutes; giving up.'))
    return 1


# --- hooks -----------------------------------------------------------------------------------


def cmd_hooks(args: argparse.Namespace) -> int:
    cfg = Config.load()
    home = claude_home()
    if args.hooks_action == "status":
        _print(json.dumps(hooks_install.status(claude_home=home), ensure_ascii=False, indent=2))
        _print(tr('Bridge: ') + (tr('running') if running_status(cfg) else tr('not running')))
        return 0
    if args.hooks_action == "remove":
        changed = hooks_install.remove(claude_home=home)
        _print(tr('Removed tgbridge hooks.') if changed else tr('There are no tgbridge hooks in settings.json.'))
        return 0
    if running_status(cfg) is None and not args.force:
        _print(tr('The bridge is not running: install and start the scheduled task first (powershell -ExecutionPolicy Bypass -File .\\install_task.ps1 -Action Install), otherwise every Claude tool call waits about 2 extra seconds. Use --force to install anyway.'))
        return 2
    token = config_module.hook_token()
    assert token is not None
    changed = hooks_install.install(claude_home=home, pythonw=hooks_install.default_pythonw(), port=cfg.hook_port,
                                    token=token)
    _print(tr('Hooks installed (Claude Code reloads them automatically; use /hooks in any session to check).') if changed else tr('Hooks are already up to date.'))
    return 0


# --- status / test-push -----------------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    cfg = Config.load()
    _print_warnings(cfg)
    token = config_module.load_bot_token()
    _print(tr('Machine: {v0}  bot: @{v1}  token: {v2}', v0=cfg.machine, v1=cfg.bot_username or '-', v2=tr('set') if token else tr('not set')))
    _print(tr('Group: {v0}  allowed users: {v1}  proxy: {v2}', v0=cfg.chat_id or tr('unbound'), v1=len(cfg.allowed_user_ids), v2=config_module.resolve_proxy(cfg) or tr('direct')))
    status = running_status(cfg)
    _print(tr('Bridge: ') + (json.dumps(status, ensure_ascii=False) if status else tr('not running')))
    try:
        hooks = hooks_install.status(claude_home=claude_home())
        _print(tr('hooks: ') + json.dumps(hooks, ensure_ascii=False))
    except (OSError, ValueError) as exc:
        _print(tr('hooks: failed to read ({v0})', v0=exc))
    log = log_dir() / "bridge.log"
    if log.is_file():
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        started = status.get("started_at") if status else None
        if isinstance(started, (int, float)):
            current = lines_since(lines, float(started))
            hidden = len(lines) - len(current)
            _print(tr('\nLogs since this start ({v0}{v1}):', v0=log, v1=tr('; {v0} earlier lines hidden', v0=hidden) if hidden else ''))
            lines = current
        else:
            _print(tr('\nRecent logs ({v0}):', v0=log))
        for line in lines[-args.lines:]:
            _print("  " + line)
    return 0


_LOG_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) ")


def lines_since(lines: list[str], started: float) -> list[str]:
    """bridge.log lines written since ``started`` (epoch); continuation lines follow their entry."""

    keep = False
    found: list[str] = []
    for line in lines:
        match = _LOG_TIME.match(line)
        if match:
            try:
                at = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
            except ValueError:
                at = 0.0
            keep = at >= started - 1  # the log's timestamps have whole seconds
        if keep:
            found.append(line)
    return found


def cmd_test_push(args: argparse.Namespace) -> int:
    from agent_telegram.tgbridge.telegram_api import TelegramError

    cfg = Config.load()
    token = config_module.load_bot_token()
    if not token or not cfg.bound:
        _print(tr('Run set-token and bind first.'))
        return 2
    try:
        _api(cfg, token).send_message(cfg.chat_id, tr('🧪 [{v0}] Test push', v0=cfg.machine), silent=True)
    except TelegramError as exc:
        _print(tr('Failed to send: {v0}', v0=exc))
        return 1
    _print(tr('Sent.'))
    return 0


def _content_blocked() -> bool:
    """Whether the bridge is locked for a security reason (pushes carry titles only).

    Reads state.json directly: BridgeState() would rename a damaged file under the
    running bridge. An unreadable or damaged state counts as locked (fail closed).
    """

    from agent_telegram.tgbridge.bridge import OWNER_LOCK
    from agent_telegram.tgbridge.state import STATE_FILE

    try:
        raw = config_module._read_bytes(tg_dir() / STATE_FILE)
        data = {} if raw is None else json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
    except (OSError, ValueError, RecursionError):
        return True
    return data.get("locked") is True and data.get("locked_reason") != OWNER_LOCK


NOTIFY_TITLE_ONLY = 5   # exit code of notify when the group is locked and only a title went out
NOTIFY_MAX_PHOTOS = 10  # --photo files per notice
_SOURCE_NOTIFY_TEXT_SENT = 'Text sent'   # printed before the --photo uploads start
NOTIFY_CAPTION_MAX = 200  # each image's caption: the notice's first line, cut to this, then the file name
NOTIFY_NAME_MAX = 100     # ... cut to this


def _notify_images(api: Any, chat_id: int, paths: list[str], text: str, redactor: Any) -> tuple[int, int]:
    """Send the --photo files after the notice ``text`` (already redacted); returns (sent, as documents).

    Images up to PHOTO_MAX_BYTES go as photos; larger ones, and those Telegram refuses as a
    photo, as documents. A file that cannot be read, is empty or is above DOCUMENT_MAX_BYTES is
    skipped and a failed upload is reported; both only print a line: the text already went out.
    They are sent silently: the text itself rang (or was --silent).
    """

    from agent_telegram.tgbridge.render import _INVISIBLE_RE, _one_line, _shorten
    from agent_telegram.tgbridge.telegram_api import DOCUMENT_MAX_BYTES, PHOTO_MAX_BYTES, TelegramError

    first = _shorten(_INVISIBLE_RE.sub("", _one_line(text.splitlines()[0])).strip(), NOTIFY_CAPTION_MAX)
    sent = documents = 0
    for path in paths:
        try:
            with open(path, "rb") as handle:
                data = handle.read(DOCUMENT_MAX_BYTES + 1)
        except OSError as exc:
            _print(tr('⚠️ Skipping image {v0}: {v1}', v0=path, v1=exc))
            continue
        if not data or len(data) > DOCUMENT_MAX_BYTES:
            _print(tr('⚠️ Skipping image {v0}: ', v0=path) + (tr('the file is empty') if not data else tr("exceeds Telegram's 50 MB limit")))
            continue
        # Telegram drops a photo's file name: the caption keeps it (redacted, like the text).
        name = _shorten(redactor(_INVISIBLE_RE.sub("", os.path.basename(path)).strip()), NOTIFY_NAME_MAX)
        name = name or "image"
        caption = f"{first}\n{name}" if first else name
        as_photo = len(data) <= PHOTO_MAX_BYTES
        try:
            if as_photo:
                try:
                    api.send_photo(chat_id, name, data, caption=caption, silent=True, parse_mode=None)
                except TelegramError as exc:
                    # 400/413: not usable as a photo (dimensions, format, size) or no right to send
                    # photos; a document may still go through. Network errors and the rest do not retry.
                    if exc.code not in (400, 413):
                        raise
                    _print(tr('Telegram rejected image {v0} ({v1}); sending it as a file instead.', v0=path, v1=exc.description))
                    as_photo = False
            if not as_photo:
                api.send_document(chat_id, name, data, caption=caption, silent=True, parse_mode=None)
                documents += 1
        except TelegramError as exc:
            _print(tr('⚠️ Image {v0} was not sent: {v1}', v0=path, v1=exc))
            continue
        sent += 1
    return sent, documents


def cmd_notify(args: argparse.Namespace) -> int:
    """Send a plain-text notice from another local tool to the group, then the --photo images.

    Exit codes: 0 sent, 1 Telegram error, 2 nothing to send / not set up / too many photos,
    5 only a redacted title was sent because the group is security-locked (no images then).
    The exit code is about the text: images that could not be read or sent are reported in
    the output but still exit 0.
    """

    photos = list(args.photo or [])
    if len(photos) > NOTIFY_MAX_PHOTOS:
        _print(tr('--photo accepts at most {v0} files ({v1} supplied).', v0=NOTIFY_MAX_PHOTOS, v1=len(photos)))
        return 2
    if args.file:
        try:
            with open(args.file, encoding="utf-8") as handle:
                text = handle.read()
        except OSError as exc:
            _print(tr('Failed to read: {v0}', v0=exc))
            return 2
    else:
        text = sys.stdin.read() if sys.stdin is not None else ""
    return _send_notice(text, photos=photos, silent=args.silent)


def notify_text(text: str, *, silent: bool = False, quiet: bool = False) -> int:
    """Use the CLI notify path in-process, including its redaction and security lock.

    Local schedulers use ``quiet=True`` so an unconfigured bridge is a silent
    no-op. Tokens are read only by the existing bridge configuration code.
    Return codes match ``cmd_notify``; no token or API object leaves this path.
    """
    return _send_notice(text, photos=[], silent=silent, quiet=quiet)


def _send_notice(text: str, *, photos: list[str], silent: bool, quiet: bool = False) -> int:
    from agent_telegram.settings import codex_home as _codex_home
    from agent_telegram.tgbridge.redact import Redactor, load_local_secrets
    from agent_telegram.tgbridge.render import _INVISIBLE_RE, MAX_TEXT, MAX_TOPIC_NAME, _one_line, _shorten
    from agent_telegram.tgbridge.telegram_api import TelegramError, _clip

    report = (lambda _text: None) if quiet else _print
    text = text.strip()
    if not text:
        report(tr('There is no content to send.'))
        return 2
    cfg = Config.load()
    token = config_module.load_bot_token()
    if not token or not cfg.bound:
        report(tr('Run set-token and bind first.'))
        return 2
    redactor = Redactor([token, *load_local_secrets(claude_home(), _codex_home())])
    api = _api(cfg, token)
    blocked = _content_blocked()
    if not blocked:
        # This machine's bridge may not be running (or not have checked yet): count the
        # members like Bridge._check_members does, and fail closed.
        try:
            blocked = api.member_count(cfg.chat_id) > len(cfg.allowed_user_ids) + cfg.expected_bots
        except TelegramError:
            blocked = True
    if blocked:
        # Like the bridge's pushes under a security lock: one short, redacted title line.
        first = _INVISIBLE_RE.sub("", _one_line(text.splitlines()[0])).strip()
        text = _shorten(redactor(first), MAX_TOPIC_NAME) + tr('\n(The group is security-locked; sending only the title)')
    else:
        text = redactor(text)
    try:
        api.send_message(cfg.chat_id, _clip(text, MAX_TEXT), silent=silent, parse_mode=None)
    except TelegramError as exc:
        report(tr('Failed to send: {v0}', v0=exc))
        return 1
    if blocked:
        report(tr('Only the title was sent (the group is security-locked)') + (tr('; {v0} images were not sent.', v0=len(photos)) if photos else tr('.')))
        return NOTIFY_TITLE_ONLY
    if not photos:
        report(tr('Sent.'))
        return 0
    # Callers that time out during the uploads can tell from this line that the text went out.
    report(tr('Text sent') + tr('; sending images next.'))
    sent, documents = _notify_images(api, cfg.chat_id, photos, text, redactor)
    summary = tr('Sent; images {v0}/{v1}', v0=sent, v1=len(photos)) + (tr(' ({v0} sent as files)', v0=documents) if documents else "")
    if sent < len(photos):
        summary += tr('; {v0} were not sent (see the messages above; text was sent, exit code remains 0)', v0=len(photos) - sent)
    report(summary + tr('.'))
    return 0


def _restart_hint() -> None:
    _print(tr('Restart the running bridge with {command}, or use /lang in Telegram to apply the language immediately.',
              command=distribution.RESTART_COMMAND))


def cmd_lang(args: argparse.Namespace) -> int:
    cfg = Config.load()
    if args.language is None:
        _print(tr('Language: {language}. Change it with: .\\.venv\\Scripts\\python.exe -m agent_telegram lang en|zh',
                  language=cfg.language or distribution.DEFAULT_LANGUAGE))
        return 0
    cfg.language = args.language
    cfg.save()
    set_language(args.language)
    _print(tr('Language saved: {language}.', language=args.language))
    _restart_hint()
    return 0


_PROTECTED_CONFIG = frozenset({'chat_id', 'allowed_user_ids', 'bot_username', 'warnings'})


def _parse_value(key: str, kind: str, value: str) -> Any:
    if key == 'proxy':
        if value.lower() in ('auto', 'null'):
            return None
        if value.lower() == 'direct':
            return ''
    if kind == 'bool':
        lowered = value.lower()
        if lowered in ('true', 'yes', 'on', '1'):
            return True
        if lowered in ('false', 'no', 'off', '0'):
            return False
        raise ValueError(tr('Expected a boolean: true/false, yes/no, on/off or 1/0.'))
    if kind == 'int':
        try:
            return int(value)
        except ValueError:
            raise ValueError(tr('Expected an integer.')) from None
    if kind == 'float':
        try:
            return float(value)
        except ValueError:
            raise ValueError(tr('Expected a number.')) from None
    if kind == 'list[str]':
        if value.lstrip().startswith('['):
            try:
                result = json.loads(value)
            except ValueError:
                raise ValueError(tr('Expected a comma-separated list or a JSON array of strings.')) from None
            if not config_module._type_ok(kind, result):
                raise ValueError(tr('Expected a comma-separated list or a JSON array of strings.'))
            return result
        return [item.strip() for item in value.split(',')] if value else []
    return value


def cmd_config(args: argparse.Namespace) -> int:
    cfg = Config.load()
    if args.config_action == 'show':
        _print_warnings(cfg)
        defaults = Config()
        defaults.machine = config_module.default_machine()
        defaults.language = distribution.DEFAULT_LANGUAGE
        effective = cfg.to_dict()
        effective['language'] = cfg.language or distribution.DEFAULT_LANGUAGE
        for key, value in effective.items():
            suffix = tr(' (default)') if value == getattr(defaults, key) else ''
            _print(f'{key} = {json.dumps(value, ensure_ascii=False)}{suffix}')
        return 0
    known = {item.name: item for item in fields(Config)}
    key = args.key
    if key not in known or key in _PROTECTED_CONFIG:
        _print(tr('Configuration key {key} cannot be changed here.', key=key))
        return 2
    if args.config_action == 'unset':
        setattr(cfg, key, getattr(Config(), key))
    else:
        try:
            value = _parse_value(key, str(known[key].type), args.value)
        except ValueError as exc:
            _print(str(exc))
            return 2
        probe = Config()
        probe._apply({key: value})
        if probe.warnings:
            for warning in probe.warnings:
                _print(warning)
            return 2
        setattr(cfg, key, getattr(probe, key))
    cfg.save()
    _print(tr('Configuration saved: {key}.', key=key))
    _restart_hint()
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build help text in the current language, without reading local secrets."""
    parser = argparse.ArgumentParser(prog=".\\.venv\\Scripts\\python.exe -m agent_telegram",
                                     description=tr('Connect local Codex / Claude Code conversations to a Telegram group.'))
    commands = parser.add_subparsers(dest="command", required=True)
    lang = commands.add_parser('lang', help=tr('Show or change the language'))
    lang.add_argument('language', nargs='?', choices=LANGUAGES)
    config = commands.add_parser('config', help=tr('Show or change configuration'))
    actions = config.add_subparsers(dest='config_action', required=True)
    actions.add_parser('show', help=tr('Show effective configuration and defaults'))
    config_set = actions.add_parser('set', help=tr('Validate and save a configuration value'))
    config_set.add_argument('key', metavar='KEY')
    config_set.add_argument('value', metavar='VALUE')
    config_unset = actions.add_parser('unset', help=tr('Restore a configuration value to its default'))
    config_unset.add_argument('key', metavar='KEY')
    commands.add_parser("run", help=tr('Run the bridge (the scheduled task invokes it with pythonw)'))
    proxy_help = tr('Telegram proxy: auto (default, detect automatically), direct or http://127.0.0.1:PORT; saved in config.json')
    token = commands.add_parser("set-token", help=tr('Save the bot token (DPAPI encrypted)'))
    token.add_argument("--proxy", help=proxy_help)
    token.add_argument("--no-check", action="store_true", help=tr('Skip the getMe token check'))
    bind = commands.add_parser("bind", help=tr('Bind the group and your account with a one-time code'))
    bind.add_argument("--proxy", help=proxy_help)
    hooks = commands.add_parser("hooks", help=tr('Install/remove Claude Code hooks'))
    hooks.add_argument("hooks_action", choices=("install", "remove", "status"))
    hooks.add_argument("--force", action="store_true", help=tr('Install even if the bridge is not running'))
    status = commands.add_parser("status", help=tr('Show configuration, running state and recent logs'))
    status.add_argument("--lines", type=int, default=15)
    commands.add_parser("test-push", help=tr('Send a test message to the group'))
    notify = commands.add_parser("notify", help=tr('Send plain text to the group (for other local tools; a security-locked group receives only a redacted title, exit code 5)'))
    notify.add_argument("--file", help=tr('UTF-8 text file; read stdin if omitted'))
    notify.add_argument("--silent", action="store_true", help=tr('Push silently (no notification sound)'))
    notify.add_argument("--photo", action="append", metavar="FILE",
                        help=tr('Images after the text, repeatable (at most {v0}, sent silently); files over 10 MB or rejected by Telegram are sent as documents, unreadable files are skipped; no images are sent to a security-locked group', v0=NOTIFY_MAX_PHOTOS))
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    # The help and parser errors must use the saved language too. Loading is
    # intentionally quiet here; individual commands decide when to show warnings.
    try:
        language = Config.load().language
    except OSError:
        language = ''
    set_language(language or distribution.DEFAULT_LANGUAGE)
    args = build_parser().parse_args(argv)
    if args.command == "run" or (args.command == "hooks" and args.hooks_action == "install"):
        pass  # The old-package cutover guard is excluded.
    handlers = {"run": cmd_run, "set-token": cmd_set_token, "bind": cmd_bind, "hooks": cmd_hooks,
                "status": cmd_status, "test-push": cmd_test_push, "notify": cmd_notify,
                "lang": cmd_lang, "config": cmd_config}
    return handlers[args.command](args)


def __getattr__(name: str) -> str:
    """Resolve legacy message exports at access time, including for the pending bridge."""
    if name == 'NOTIFY_TEXT_SENT':
        return tr('Text sent')
    raise AttributeError(name)


if __name__ == "__main__":
    raise SystemExit(main())

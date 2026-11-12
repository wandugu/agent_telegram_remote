"""Bridge configuration, local secrets and hardened state directories.

``var/tgbridge/config.json`` holds the binding (chat id, allowed users) and the
tunables; it never holds a secret. Secrets live next to it: the bot token and
the Claude Code inbox tokens are DPAPI-encrypted for the current user, the hook
token is a plain file because the stdlib-only hook reporter must read it. The
directory itself gets a protected DACL (current user + SYSTEM only).
"""

from __future__ import annotations

import copy
import getpass
import json
import math
import os
import re
import secrets
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from agent_telegram import distribution, settings
from agent_telegram.i18n import LANGUAGES, normalize, tr
from agent_telegram.fsutil import replace_atomic
from agent_telegram.paths import logs_dir
from agent_telegram.outputs import _rename_no_clobber, atomic_write_bytes, atomic_write_json

DEFAULT_PROXY = "http://127.0.0.1:7897"
TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,64}$")

BOT_TOKEN_FILE = "bot_token.dpapi"
HOOK_TOKEN_FILE = "hook_token.txt"
INBOX_TOKENS_FILE = "inbox_tokens.dpapi"
CONFIG_FILE = "config.json"

# Binds the DPAPI blobs to this bridge: another program running as the same user
# cannot decrypt them by just calling CryptUnprotectData without the entropy.
_DPAPI_ENTROPY = b"agent_telegram.tgbridge.v1"
_DPAPI_DESCRIPTION = "agent_telegram tgbridge"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
_HOOK_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
# httpx is installed without the socks extra: SOCKS-only proxies are not supported.
_PROXY_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443, "socks5": 1080, "socks5h": 1080}


# --- paths -------------------------------------------------------------------------------


def tg_dir() -> Path:
    return settings.var_dir() / "tgbridge"


def log_dir() -> Path:
    override = os.environ.get("TGBRIDGE_LOG_DIR")
    return Path(override) if override else settings.var_dir() / "logs"


def claude_home() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) if override else Path.home() / ".claude"


def config_path() -> Path:
    return tg_dir() / CONFIG_FILE


def default_machine() -> str:
    """Short machine name used in topic names: the host name without a "<user>-" prefix (ann-laptop -> laptop)."""

    name = socket.gethostname().strip().lower()
    try:
        user = getpass.getuser().strip().lower()
    except Exception:  # noqa: BLE001 - no user name in the environment
        user = ""
    return (name.removeprefix(f"{user}-") if user else name) or name or "pc"


def _strip_verbatim(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(_strip_verbatim(path.strip())))


def _read_bytes(path: Path, attempts: int = 5) -> bytes | None:
    """File content, None when missing. Retries the sharing violation a reader can hit
    on Windows while another process atomically replaces the file."""

    for attempt in range(attempts):
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05)
    return None


def _is_within(path: str, root: str) -> bool:
    """Component-wise prefix test on normalized paths (C:\\a matches C:\\a\\b, not C:\\ab)."""

    if not path.strip() or not root.strip():
        return False
    child, parent = _norm_path(path), _norm_path(root)
    if child == parent:
        return True
    return child.startswith(parent.rstrip("\\/") + os.sep)


# --- configuration -----------------------------------------------------------------------

# Accepted value ranges (inclusive); fields not listed only need the right type.
_LIMITS: dict[str, tuple[float | None, float | None]] = {
    "hook_port": (1, 65535),
    "recent_n": (1, 30),  # /list shows at most 30 (and buttons for the first 10)
    "expected_bots": (1, 20),
    "summary_chars": (1, 3800),  # a turn card must stay below Telegram's 4096 characters
    "claude_max_headless": (0, 16),
}  # float fields must be finite and strictly positive
_TYPE_NAMES = {
    "str": 'string',
    "str | None": 'string or null',
    "int": 'integer',
    "int | None": 'integer or null',
    "float": 'number',
    "bool": "true/false",
    "list[int]": 'array of integers',
    "list[str]": 'array of strings',
}


def _translated_type_names() -> dict[str, str]:
    return {
        'str': tr('string'),
        'str | None': tr('string or null'),
        'int': tr('integer'),
        'int | None': tr('integer or null'),
        'float': tr('number'),
        'bool': 'true/false',
        'list[int]': tr('array of integers'),
        'list[str]': tr('array of strings'),
    }


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _type_ok(kind: str, value: Any) -> bool:
    match kind:
        case "str":
            return isinstance(value, str)
        case "str | None":
            return value is None or isinstance(value, str)
        case "int":
            return _is_int(value)
        case "int | None":
            return value is None or _is_int(value)
        case "float":
            if not (_is_int(value) or isinstance(value, float)):
                return False
            try:
                return math.isfinite(float(value))
            except OverflowError:  # an integer literal beyond the float range
                return False
        case "bool":
            return isinstance(value, bool)
        case "list[int]":
            return isinstance(value, list) and all(_is_int(item) for item in value)
        case "list[str]":
            return isinstance(value, list) and all(isinstance(item, str) for item in value)
    raise TypeError(f"unsupported Config field type: {kind}")


def _range_ok(name: str, kind: str, value: Any) -> bool:
    if kind == "float":
        return value > 0
    low, high = _LIMITS.get(name, (None, None))
    if not _is_int(value):
        return True
    return (low is None or value >= low) and (high is None or value <= high)


def _proxy_ok(value: str | None) -> bool:
    if value is None or not value.strip():
        return True
    try:
        parts = urlsplit(value.strip())
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return False
    return parts.scheme.lower() in _PROXY_SCHEMES and bool(parts.hostname)


@dataclass
class Config:
    language: str = ""  # Empty means the distribution's default language.
    machine: str = ""  # "" -> default_machine() at load
    chat_id: int | None = None
    allowed_user_ids: list[int] = field(default_factory=list)
    bot_username: str = ""
    proxy: str | None = None  # None = auto (DEFAULT_PROXY when listening, else direct); "" = direct; else URL
    hook_port: int = 8767
    recent_n: int = 10
    active_window_h: float = 24.0
    topic_idle_close_h: float = 72.0
    summary_chars: int = 1500
    presence_idle_s: float = 180.0
    waiting_alert_s: float = 600.0
    write_max_age_s: float = 600.0
    codex_enabled: bool = True
    codex_include_automation: bool = False
    codex_anchor_thread: str = ""
    # Codex Desktop threads on SSH remote hosts (read through the local app); view only.
    codex_remote_enabled: bool = True
    codex_remote_hosts: list[str] = field(default_factory=list)  # ssh aliases or host ids; empty = all
    codex_remote_poll_s: float = 30.0
    # What an agent says while its turn still runs: one message per turn, edited as text arrives,
    # a new one after this long or when the agent addresses the user.
    progress_enabled: bool = True
    progress_new_message_s: float = 600.0
    # Each turn push ends with the agent's plan limits left (Codex from its rollouts, Claude from
    # Claude Desktop's local usage record); /status shows them too.
    quota_enabled: bool = True
    claude_enabled: bool = True
    claude_include_scheduled: bool = False
    claude_exe: str = ""
    claude_remote_bypass_allowed: bool = False
    claude_max_headless: int = 2
    # Antigravity conversations (desktop app and agy CLI), read from the local files under
    # antigravity_home ("" = ~/.gemini); view only, never written to.
    antigravity_enabled: bool = distribution.ANTIGRAVITY_ENABLED
    antigravity_home: str = ""
    pi_enabled: bool = distribution.PI_ENABLED
    pi_home: str = ""
    dsh_enabled: bool = distribution.DSH_ENABLED
    dsh_home: str = ""
    # Bots expected in the group (one per machine); more members than owners + bots lock writes.
    expected_bots: int = 2
    # /new claude may only use these dirs (and their direct children).
    roots: list[str] = field(default_factory=lambda: [str(Path.home())])
    # cwd prefixes whose content is never pushed (metadata only).
    content_exclude: list[str] = field(default_factory=list)
    # Problems found by load(); shown by the CLI, never saved.
    warnings: list[str] = field(default_factory=list, compare=False, repr=False)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """Read config.json; a missing file gives the defaults.

        Unknown keys are ignored (a warning unless the key starts with "_", for
        comments); a value of the wrong type or out of range falls back to the
        default with a warning. A file that is not a UTF-8 JSON object gives the
        defaults (unbound, so the bridge stays idle) with a warning; save() keeps
        a copy of it as <name>.corrupt before overwriting it.
        """

        path = Path(path) if path else config_path()
        cfg = cls()
        raw = _read_bytes(path)
        if raw is not None:
            data = _parse_config(raw)
            if data is None:
                cfg.warnings.append(
                    tr('{v0} is not a valid JSON object; all defaults are in use (unbound). The original file will be saved as {v1}.corrupt before the next save', v0=path.name, v1=path.name)
                )
            else:
                cfg._apply(data)
        cfg.machine = cfg.machine.strip() or default_machine()
        return cfg

    def _apply(self, data: dict[str, Any]) -> None:
        known = {item.name: item for item in fields(self) if item.name != "warnings"}
        for name, value in data.items():
            item = known.get(name)
            if item is None:
                if not name.startswith("_"):
                    self.warnings.append(tr('Unknown configuration key {v0} in config.json; ignored', v0=name))
                continue
            kind = str(item.type)
            if not _type_ok(kind, value):
                self.warnings.append(tr('config.json field {v0} must be {v1}; using the default', v0=name, v1=_translated_type_names()[kind]))
                continue
            if name == "language" and value != "":
                value = normalize(value)
                if value not in LANGUAGES:
                    self.warnings.append(tr('config.json language must be en, zh or an empty string; using the default'))
                    continue
            if not _range_ok(name, kind, value) or (name == "proxy" and not _proxy_ok(value)):
                self.warnings.append(tr('Invalid value for config.json field {v0}; using the default', v0=name))
                continue
            if kind == "float":
                value = float(value)
            setattr(self, name, copy.deepcopy(value))

    def to_dict(self) -> dict[str, Any]:
        data = {item.name: copy.deepcopy(getattr(self, item.name)) for item in fields(self) if item.name != "warnings"}
        return dict(sorted(data.items()))

    def save(self, path: Path | None = None) -> None:
        path = Path(path) if path else config_path()
        _keep_damaged_config(path)
        atomic_write_json(path, self.to_dict())

    @property
    def bound(self) -> bool:
        return self.chat_id is not None and bool(self.allowed_user_ids)

    def excluded(self, cwd: str | os.PathLike[str]) -> bool:
        """True when cwd is (inside) one of content_exclude: push metadata only."""

        cwd = os.fspath(cwd) if cwd else ""
        if not cwd:
            return False
        return any(_is_within(cwd, prefix) for prefix in self.content_exclude)


def _parse_config(raw: bytes) -> dict[str, Any] | None:
    """The JSON object in raw (a UTF-8 BOM is fine), None when it is not one."""

    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, RecursionError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        return None
    return data if isinstance(data, dict) else None


def _keep_damaged_config(path: Path) -> None:
    """Copy a config file that does not parse to <name>.corrupt before save() replaces it."""

    try:
        raw = path.read_bytes()
    except OSError:
        return
    if _parse_config(raw) is None:
        atomic_write_bytes(path.with_name(path.name + ".corrupt"), raw)


# --- proxy -------------------------------------------------------------------------------


def proxy_listening(url: str, timeout: float = 0.5) -> bool:
    """True when something accepts TCP connections at the proxy URL's host:port."""

    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return False
    if not host:
        return False
    try:
        with socket.create_connection((host, port or _DEFAULT_PORTS.get(parts.scheme.lower(), 80)), timeout=timeout):
            return True
    except OSError:
        return False


def _env_proxies() -> list[str]:
    found = []
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        value = (os.environ.get(name) or "").strip()
        if value and value not in found:
            found.append(value)
    return found


def _system_proxy() -> str | None:
    """The Windows (WinINET) proxy that tools like Clash or v2rayN switch on, as an http:// URL."""

    if os.name != "nt":
        return None
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            if not winreg.QueryValueEx(key, "ProxyEnable")[0]:
                return None
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "").strip()
    except OSError:
        return None
    if not server:
        return None
    if "=" in server:  # per-protocol form: "http=host:port;https=host:port"
        parts = dict(item.split("=", 1) for item in server.split(";") if "=" in item)
        server = parts.get("https") or parts.get("http") or ""
    if not server:
        return None
    return server if "://" in server else f"http://{server}"


def resolve_proxy(cfg: Config) -> str | None:
    """Proxy URL for the Telegram client, or None for a direct connection.

    With ``proxy`` null (auto): Clash Verge's default port, then HTTPS_PROXY/HTTP_PROXY, then the
    Windows system proxy, each only when something listens there; otherwise direct.
    """

    if cfg.proxy is not None:
        return cfg.proxy.strip() or None
    candidates = [DEFAULT_PROXY, *_env_proxies()]
    system = _system_proxy()
    if system:
        candidates.append(system)
    for candidate in candidates:
        if _proxy_ok(candidate) and proxy_listening(candidate):
            return candidate
    return None


# --- DPAPI -------------------------------------------------------------------------------


def _win32crypt() -> Any:
    try:
        import win32crypt  # type: ignore[import-untyped]
    except ImportError as error:
        raise OSError(tr('DPAPI is available only on Windows with pywin32 installed')) from error
    return win32crypt


def protect(data: bytes) -> bytes:
    """Encrypt for the current Windows user (DPAPI); OSError when unavailable or failing."""

    win32crypt = _win32crypt()
    try:
        return bytes(
            win32crypt.CryptProtectData(
                bytes(data), _DPAPI_DESCRIPTION, _DPAPI_ENTROPY, None, None, _CRYPTPROTECT_UI_FORBIDDEN
            )
        )
    except Exception as error:  # pywintypes.error is not an OSError
        raise OSError(tr('DPAPI encryption failed: {v0}', v0=getattr(error, 'strerror', '') or type(error).__name__)) from None


def unprotect(blob: bytes) -> bytes:
    """Decrypt a protect() blob; OSError for another user's, another machine's or a damaged blob."""

    win32crypt = _win32crypt()
    try:
        _description, data = win32crypt.CryptUnprotectData(
            bytes(blob), _DPAPI_ENTROPY, None, None, _CRYPTPROTECT_UI_FORBIDDEN
        )
    except Exception as error:
        raise OSError(tr('DPAPI decryption failed: {v0}', v0=getattr(error, 'strerror', '') or type(error).__name__)) from None
    return bytes(data)


# --- secrets -----------------------------------------------------------------------------


def save_bot_token(token: str) -> None:
    token = (token or "").strip()
    if not TOKEN_RE.fullmatch(token):
        # Never echo the value: it may be a real token with a typo.
        raise ValueError(tr('Invalid bot token format: expected the “digits:string” provided by BotFather'))
    harden_dir(tg_dir())
    atomic_write_bytes(tg_dir() / BOT_TOKEN_FILE, protect(token.encode("utf-8")))


def load_bot_token() -> str | None:
    """The saved bot token; None when missing, undecryptable or malformed."""

    try:
        blob = (tg_dir() / BOT_TOKEN_FILE).read_bytes()
        token = unprotect(blob).decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return token if TOKEN_RE.fullmatch(token) else None


def _read_hook_token(path: Path) -> tuple[bool, str | None]:
    """(readable, token): readable is False when the file is missing or cannot be opened;
    token is None unless the content is a valid hook token."""

    try:
        raw = path.read_bytes()
    except OSError:
        return False, None
    try:
        token = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        return True, None
    return True, (token if _HOOK_TOKEN_RE.fullmatch(token) else None)


def hook_token(*, create: bool = True) -> str | None:
    """Shared secret of the hook endpoints (tg_dir()/hook_token.txt), created on demand.

    Creation never replaces a valid token another process wrote first, and the
    value returned is always the one read back from the file, so the bridge and
    the installed hooks agree.
    """

    path = tg_dir() / HOOK_TOKEN_FILE
    for _ in range(5):
        _readable, token = _read_hook_token(path)
        if token or not create:
            return token
        harden_dir(path.parent)
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        temp.write_text(secrets.token_urlsafe(32), encoding="utf-8")  # no newline: it goes into an HTTP header
        try:
            if not _rename_no_clobber(temp, path):
                readable, token = _read_hook_token(path)
                if readable and token is None:
                    replace_atomic(temp, path)  # the file holds no valid token (hand-edited, truncated)
                # Otherwise another process created it, or it is momentarily unreadable: look again.
        except PermissionError:  # a hook is reading the file right now
            time.sleep(0.05)
        finally:
            temp.unlink(missing_ok=True)
    return _read_hook_token(path)[1]


class SecretStore:
    """DPAPI-encrypted JSON object of small secret records (Claude Code inbox tokens).

    Thread-safe; loaded lazily and written on every change. An unreadable file is
    set aside as ``.corrupt`` and the store starts empty (hooks report the
    tokens again).
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or tg_dir() / INBOX_TOKENS_FILE
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] | None = None
        self._hardened = False

    def _load(self) -> dict[str, dict[str, Any]]:
        if self._data is not None:
            return self._data
        data: dict[str, dict[str, Any]] = {}
        blob = _read_bytes(self.path)
        if blob is not None:
            try:
                loaded = json.loads(unprotect(blob).decode("utf-8"))
                if not isinstance(loaded, dict):
                    raise ValueError("not an object")
                data = {str(key): value for key, value in loaded.items() if isinstance(value, dict)}
            except (OSError, UnicodeDecodeError, ValueError):
                _keep_corrupt(self.path)
                data = {}
        self._data = data
        return data

    def _commit(self, data: dict[str, dict[str, Any]]) -> None:
        """Write data, then make it current: a failed write leaves the store unchanged,
        so retrying the same put() writes again instead of being skipped as a no-op."""

        if not self._hardened:
            harden_dir(self.path.parent)
            self._hardened = True
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
        atomic_write_bytes(self.path, protect(payload))
        self._data = data

    def get(self, key: str) -> dict | None:
        with self._lock:
            value = self._load().get(key)
            return copy.deepcopy(value) if value is not None else None

    def put(self, key: str, value: dict) -> None:
        if not isinstance(key, str) or not key:
            raise ValueError("SecretStore key must be a non-empty string")
        if not isinstance(value, dict):
            raise TypeError("SecretStore values must be dicts")
        value = json.loads(json.dumps(value, ensure_ascii=False))  # JSON-safe copy (TypeError otherwise)
        with self._lock:
            data = self._load()
            if data.get(key) == value:
                return
            self._commit({**data, key: value})

    def drop(self, key: str) -> None:
        with self._lock:
            data = self._load()
            if key not in data:
                return
            self._commit({name: item for name, item in data.items() if name != key})

    def keys(self) -> list[str]:
        with self._lock:
            return list(self._load())


def _keep_corrupt(path: Path) -> None:
    """Keep an unreadable state file as <name>.corrupt for inspection (best effort)."""

    try:
        os.replace(path, path.with_name(path.name + ".corrupt"))
    except OSError:
        pass


# --- directory ACL -----------------------------------------------------------------------


def _is_link(path: Path) -> bool:
    """Symlink or junction (any reparse point); Path.is_junction needs Python 3.12."""

    try:
        if path.is_symlink():
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def harden_dir(path: Path) -> bool:
    """Create path and give it a protected DACL: current user + SYSTEM, full control,
    inherited by files and subfolders. False on any failure (and for links)."""

    try:
        path.mkdir(parents=True, exist_ok=True)
        if _is_link(path):
            return False
        if os.name != "nt":
            path.chmod(0o700)
            return True
        import ntsecuritycon  # type: ignore[import-untyped]
        import win32api  # type: ignore[import-untyped]
        import win32security  # type: ignore[import-untyped]

        process_token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
        try:
            user_sid = win32security.GetTokenInformation(process_token, win32security.TokenUser)[0]
        finally:
            process_token.Close()
        system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
        inheritance = ntsecuritycon.OBJECT_INHERIT_ACE | ntsecuritycon.CONTAINER_INHERIT_ACE
        dacl = win32security.ACL()
        for sid in (user_sid, system_sid):
            dacl.AddAccessAllowedAceEx(win32security.ACL_REVISION_DS, inheritance, ntsecuritycon.FILE_ALL_ACCESS, sid)
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
        return True
    except Exception:
        return False


def ensure_dirs() -> None:
    """Create and harden the state directory and the log directory."""

    harden_dir(tg_dir())
    harden_dir(log_dir())

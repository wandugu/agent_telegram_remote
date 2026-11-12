from __future__ import annotations

import json
import os
import socket
import threading
from dataclasses import fields
from pathlib import Path

import pytest

from agent_telegram.tgbridge import config, state
from agent_telegram.tgbridge.config import Config, SecretStore
from agent_telegram.tgbridge.state import MAX_MESSAGES, MAX_SEEN_UPDATES, BridgeState

windows_only = pytest.mark.skipif(os.name != "nt", reason="DPAPI and DACLs need Windows")

VALID_TOKEN = "123456789:" + "AbC-d_" * 6  # 36 characters after the colon


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake_dpapi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reversible stand-in for DPAPI so store logic runs without touching the user's keys."""

    def protect(data: bytes) -> bytes:
        return b"FAKE" + bytes(reversed(data))

    def unprotect(blob: bytes) -> bytes:
        if not blob.startswith(b"FAKE"):
            raise OSError("DPAPI 解密失败：fake")
        return bytes(reversed(blob[4:]))

    monkeypatch.setattr(config, "protect", protect)
    monkeypatch.setattr(config, "unprotect", unprotect)
    monkeypatch.setattr(config, "harden_dir", lambda path: path.mkdir(parents=True, exist_ok=True) or True)


def write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


# --- paths -------------------------------------------------------------------------------


def test_paths_follow_the_environment(isolated_var: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.tg_dir() == isolated_var / "tgbridge"
    assert config.config_path() == isolated_var / "tgbridge" / "config.json"

    monkeypatch.setenv("TGBRIDGE_LOG_DIR", str(tmp_path / "logs"))
    assert config.log_dir() == tmp_path / "logs"
    monkeypatch.delenv("TGBRIDGE_LOG_DIR")
    assert config.log_dir() == isolated_var / "logs"

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    assert config.claude_home() == tmp_path / "claude"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert config.claude_home() == Path.home() / ".claude"


@pytest.mark.parametrize(
    ("hostname", "expected"),
    [("ANN-LAPTOP", "laptop"), ("ann-pc2", "pc2"), ("Desktop-7", "desktop-7"), ("ann-", "ann-"), ("joann-pc", "joann-pc")],
)
def test_default_machine_strips_the_user_prefix(hostname: str, expected: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.socket, "gethostname", lambda: hostname)
    monkeypatch.setattr(config.getpass, "getuser", lambda: "Ann")
    assert config.default_machine() == expected


def test_default_machine_without_a_user_name(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_user() -> str:
        raise OSError("no user")

    monkeypatch.setattr(config.socket, "gethostname", lambda: "Ann-Laptop")
    monkeypatch.setattr(config.getpass, "getuser", no_user)
    assert config.default_machine() == "ann-laptop"


# --- Config ------------------------------------------------------------------------------


def test_every_config_field_type_is_validated() -> None:
    defaults = Config()
    for item in fields(Config):
        assert config._type_ok(str(item.type), getattr(defaults, item.name)), item.name


def test_missing_config_gives_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.socket, "gethostname", lambda: "ann-pc1")
    monkeypatch.setattr(config.getpass, "getuser", lambda: "ann")
    cfg = Config.load()
    assert cfg.machine == "pc1"
    assert cfg.chat_id is None and cfg.allowed_user_ids == []
    assert cfg.proxy is None and cfg.hook_port == 8767 and cfg.recent_n == 10
    assert cfg.roots == [str(Path.home())] and cfg.content_exclude == []
    assert cfg.warnings == []
    assert not cfg.bound
    assert not config.config_path().exists()


def test_config_defaults_are_not_shared() -> None:
    first, second = Config(), Config()
    first.roots.append("D:\\x")
    first.allowed_user_ids.append(1)
    assert second.roots == [str(Path.home())] and second.allowed_user_ids == []


def test_config_round_trip_with_sorted_keys_and_no_warnings(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "config.json"
    cfg = Config(
        machine="pc2",
        chat_id=-1001234567890,
        allowed_user_ids=[42, 7],
        bot_username="pc2_bot",
        proxy="",
        hook_port=9001,
        active_window_h=12,
        content_exclude=[r"C:\secret"],
        claude_remote_bypass_allowed=True,
    )
    cfg.warnings.append("should not be saved")
    cfg.save(path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert list(data) == sorted(data)
    assert "warnings" not in data
    assert data["chat_id"] == -1001234567890

    loaded = Config.load(path)
    assert loaded == cfg
    assert loaded.warnings == []
    assert loaded.bound


def test_config_save_defaults_to_config_path(monkeypatch: pytest.MonkeyPatch) -> None:
    Config(machine="pc1", chat_id=-100, allowed_user_ids=[1]).save()
    assert Config.load(config.config_path()).chat_id == -100


def test_wrong_types_fall_back_to_defaults_with_warnings(tmp_path: Path) -> None:
    path = write_json(
        tmp_path / "config.json",
        {
            "chat_id": "-100123",  # string, not an int
            "allowed_user_ids": [1, "2"],
            "hook_port": True,  # bool is not an int here
            "recent_n": 0,  # out of range
            "summary_chars": 99999,
            "presence_idle_s": -5,
            "waiting_alert_s": "600",
            "codex_enabled": "yes",
            "roots": "C:\\projects",
            "proxy": "ftp://127.0.0.1:21",
            "machine": 5,
            "bot_username": "ok_bot",
            "write_max_age_s": 300,  # int accepted for a float field
        },
    )
    cfg = Config.load(path)
    defaults = Config()
    for name in ("chat_id", "allowed_user_ids", "hook_port", "recent_n", "summary_chars", "presence_idle_s",
                 "waiting_alert_s", "codex_enabled", "roots", "proxy"):
        assert getattr(cfg, name) == getattr(defaults, name), name
        assert any(f" {name} " in warning for warning in cfg.warnings), name
    assert cfg.machine == config.default_machine()
    assert cfg.bot_username == "ok_bot"
    assert cfg.write_max_age_s == 300.0 and isinstance(cfg.write_max_age_s, float)
    assert len(cfg.warnings) == 11


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    path = write_json(tmp_path / "config.json", {"chatid": 1, "_comment": "notes", "recent_n": 5})
    cfg = Config.load(path)
    assert cfg.recent_n == 5
    assert not hasattr(cfg, "chatid")
    assert len(cfg.warnings) == 1 and "chatid" in cfg.warnings[0]


@pytest.mark.parametrize("proxy", [None, "", "http://127.0.0.1:7897", "https://proxy.lan"])
def test_valid_proxy_values_are_kept(proxy: str | None, tmp_path: Path) -> None:
    cfg = Config.load(write_json(tmp_path / "config.json", {"proxy": proxy}))
    assert cfg.proxy == proxy and cfg.warnings == []


@pytest.mark.parametrize("proxy", ["127.0.0.1:7897", "http://", "http://h:99999", 7897, "socks5://127.0.0.1:1080"])
def test_invalid_proxy_values_are_rejected(proxy: object, tmp_path: Path) -> None:
    cfg = Config.load(write_json(tmp_path / "config.json", {"proxy": proxy}))
    assert cfg.proxy is None and len(cfg.warnings) == 1


def test_float_field_beyond_the_float_range_is_a_warning(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"active_window_h": 1' + "0" * 400 + ', "recent_n": 5}', encoding="utf-8")
    cfg = Config.load(path)  # must not raise OverflowError
    assert cfg.active_window_h == 24.0 and cfg.recent_n == 5
    assert len(cfg.warnings) == 1 and "active_window_h" in cfg.warnings[0]


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"[1, 2]", b"", b"\xff\xfe", b"[" * 100_000],
    ids=["syntax", "array", "empty", "not-utf8", "too-deep"],
)
def test_damaged_config_gives_unbound_defaults_with_a_warning(content: bytes, tmp_path: Path) -> None:
    # cli.py calls Config.load() without a try (under pythonw for `run`): it must not raise.
    path = tmp_path / "config.json"
    path.write_bytes(content)
    cfg = Config.load(path)
    assert cfg == Config(machine=config.default_machine())
    assert not cfg.bound
    assert len(cfg.warnings) == 1 and "config.json.corrupt" in cfg.warnings[0]
    assert path.read_bytes() == content  # load never touches the file


def test_saving_over_a_damaged_config_keeps_a_copy(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(b'{"chat_id": -100, "allowed_user_ids": [1],')  # truncated
    cfg = Config.load(path)
    cfg.chat_id, cfg.allowed_user_ids = -200, [2]
    cfg.save(path)
    assert path.with_name("config.json.corrupt").read_bytes() == b'{"chat_id": -100, "allowed_user_ids": [1],'
    assert Config.load(path).chat_id == -200

    Config.load(path).save(str(path))  # a healthy file is not copied again; str paths work
    assert path.with_name("config.json.corrupt").read_bytes().startswith(b'{"chat_id": -100')
    assert Config.load(str(path)).allowed_user_ids == [2]


def test_config_read_retries_a_sharing_violation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_json(tmp_path / "config.json", {"recent_n": 7})
    real_read = Path.read_bytes
    failures = {"left": 2}

    def flaky_read(self: Path) -> bytes:
        if self == path and failures["left"]:
            failures["left"] -= 1
            raise PermissionError(32, "sharing violation")
        return real_read(self)

    monkeypatch.setattr(config.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(Path, "read_bytes", flaky_read)
    assert Config.load(path).recent_n == 7
    failures["left"] = 99
    with pytest.raises(PermissionError):
        Config.load(path)


def test_config_with_bom_is_accepted(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"chat_id": -5, "allowed_user_ids": [9]}).encode())
    cfg = Config.load(path)
    assert cfg.chat_id == -5 and cfg.bound


def test_bound_needs_chat_and_users() -> None:
    assert not Config(chat_id=-1).bound
    assert not Config(allowed_user_ids=[1]).bound
    assert Config(chat_id=-1, allowed_user_ids=[1]).bound
    assert Config(chat_id=0, allowed_user_ids=[1]).bound  # chat id 0 is still "set"


def test_excluded_matches_whole_path_components() -> None:
    cfg = Config(content_exclude=[r"C:\projects\private", "D:/Secret/", ""])
    assert cfg.excluded(r"C:\projects\private")
    assert cfg.excluded(r"c:\projects\Private\sub\dir")
    assert cfg.excluded("C:/projects/private/x")
    assert cfg.excluded(r"\\?\C:\projects\private\x")
    assert cfg.excluded(r"d:\secret\a")
    assert not cfg.excluded(r"C:\projects\private2")
    assert not cfg.excluded(r"C:\projects")
    assert not cfg.excluded("")
    assert not Config().excluded(r"C:\projects\private")


def test_excluded_drive_root() -> None:
    cfg = Config(content_exclude=["E:\\"])
    assert cfg.excluded(r"E:\anything\below")
    assert not cfg.excluded(r"C:\projects")


@pytest.mark.skipif(os.name != "nt", reason="Windows path semantics")
def test_excluded_accepts_paths_and_unc() -> None:
    cfg = Config(content_exclude=[r"\\?\UNC\server\share\private", r"C:\projects\private"])
    assert cfg.excluded(Path(r"C:\projects\Private\x"))
    assert cfg.excluded(r"\\server\share\private\docs")
    assert not cfg.excluded(r"\\server\share\public")
    assert not cfg.excluded(None)  # type: ignore[arg-type]


# --- proxy -------------------------------------------------------------------------------


def test_proxy_listening_on_a_local_listener() -> None:
    server = socket.socket()
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert config.proxy_listening(f"http://127.0.0.1:{port}")
    finally:
        server.close()


def test_proxy_listening_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[tuple[str, int], float]] = []

    def refuse(address: tuple[str, int], timeout: float) -> None:
        calls.append((address, timeout))
        raise ConnectionRefusedError

    monkeypatch.setattr(config.socket, "create_connection", refuse)
    assert not config.proxy_listening("http://127.0.0.1:7897")
    assert not config.proxy_listening("https://proxy.lan", timeout=0.2)
    assert calls == [(("127.0.0.1", 7897), 0.5), (("proxy.lan", 443), 0.2)]
    assert not config.proxy_listening("not a url")
    assert not config.proxy_listening("http://h:99999")
    assert len(calls) == 2


def test_resolve_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    probes: list[str] = []
    listening = {"value": True}

    def fake_listening(url: str, timeout: float = 0.5) -> bool:
        probes.append(url)
        return listening["value"]

    monkeypatch.setattr(config, "proxy_listening", fake_listening)
    monkeypatch.setattr(config, "_env_proxies", lambda: [])
    monkeypatch.setattr(config, "_system_proxy", lambda: None)
    assert config.resolve_proxy(Config()) == config.DEFAULT_PROXY
    listening["value"] = False
    assert config.resolve_proxy(Config()) is None
    assert probes == [config.DEFAULT_PROXY, config.DEFAULT_PROXY]
    assert config.resolve_proxy(Config(proxy="")) is None
    assert config.resolve_proxy(Config(proxy="  ")) is None
    assert config.resolve_proxy(Config(proxy=" http://10.0.0.2:3128 ")) == "http://10.0.0.2:3128"
    assert len(probes) == 2  # explicit settings never probe


# --- tokens ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [VALID_TOKEN, "123456:" + "a" * 30, "123456789012:" + "Z" * 64],
)
def test_token_format_accepts(token: str) -> None:
    assert config.TOKEN_RE.fullmatch(token)


@pytest.mark.parametrize(
    "token",
    ["12345:" + "a" * 35, "1234567890123:" + "a" * 35, "123456:" + "a" * 29, "123456:" + "a" * 65,
     "123456:" + "a" * 34 + "!", "bot123456:" + "a" * 35, "123456" + "a" * 35, ""],
)
def test_token_format_rejects(token: str) -> None:
    assert not config.TOKEN_RE.fullmatch(token)


def test_bad_bot_token_is_rejected_without_echo() -> None:
    bad = "12345678:short-but-secret-value"
    with pytest.raises(ValueError) as caught:
        config.save_bot_token(bad)
    assert bad not in str(caught.value) and "short-but-secret-value" not in str(caught.value)
    assert not (config.tg_dir() / config.BOT_TOKEN_FILE).exists()


def test_load_bot_token_missing_or_damaged(fake_dpapi: None) -> None:
    assert config.load_bot_token() is None
    path = config.tg_dir() / config.BOT_TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"garbage")
    assert config.load_bot_token() is None
    path.write_bytes(config.protect(b"not-a-token"))
    assert config.load_bot_token() is None


def test_bot_token_round_trip_with_fake_dpapi(fake_dpapi: None) -> None:
    config.save_bot_token(f"  {VALID_TOKEN}\n")
    raw = (config.tg_dir() / config.BOT_TOKEN_FILE).read_bytes()
    assert VALID_TOKEN.encode() not in raw
    assert config.load_bot_token() == VALID_TOKEN


@windows_only
def test_dpapi_round_trip() -> None:
    blob = config.protect(b"secret \x00 bytes")
    assert b"secret" not in blob
    assert config.unprotect(blob) == b"secret \x00 bytes"
    with pytest.raises(OSError):
        config.unprotect(b"not a dpapi blob")


@windows_only
def test_dpapi_blob_needs_our_entropy() -> None:
    import win32crypt

    foreign = win32crypt.CryptProtectData(b"x" * 20, None, None, None, None, 0)
    with pytest.raises(OSError):
        config.unprotect(foreign)


@windows_only
def test_bot_token_round_trip_with_dpapi() -> None:
    config.save_bot_token(VALID_TOKEN)
    raw = (config.tg_dir() / config.BOT_TOKEN_FILE).read_bytes()
    assert VALID_TOKEN.encode() not in raw
    assert config.load_bot_token() == VALID_TOKEN


def test_dpapi_unavailable_raises_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "win32crypt", None)  # import now raises ImportError
    with pytest.raises(OSError):
        config.protect(b"x")
    with pytest.raises(OSError):
        config.unprotect(b"x")


def test_hook_token_is_created_once_and_stable() -> None:
    assert config.hook_token(create=False) is None
    token = config.hook_token()
    assert token and len(token) >= 40
    path = config.tg_dir() / config.HOOK_TOKEN_FILE
    assert path.read_text(encoding="utf-8") == token  # no newline: it goes into an HTTP header
    assert config.hook_token() == token
    assert config.hook_token(create=False) == token
    assert not [item for item in path.parent.iterdir() if item.name.endswith(".tmp")]


def test_hook_token_replaces_an_invalid_file() -> None:
    path = config.tg_dir() / config.HOOK_TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("short", encoding="utf-8")
    assert config.hook_token(create=False) is None
    token = config.hook_token()
    assert token and token != "short"
    assert path.read_text(encoding="utf-8") == token


def test_hook_token_tolerates_a_trailing_newline() -> None:
    path = config.tg_dir() / config.HOOK_TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    value = "A" * 43
    path.write_text(value + "\r\n", encoding="utf-8")
    assert config.hook_token() == value


def test_hook_token_returns_what_is_on_disk_after_a_replace_race(monkeypatch: pytest.MonkeyPatch) -> None:
    path = config.tg_dir() / config.HOOK_TOKEN_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe broken")
    other = "B" * 43
    real_replace = os.replace

    def replace_then_lose(source: object, target: object) -> None:
        real_replace(source, target)
        Path(target).write_text(other, encoding="utf-8")  # another creator replaced it right after us

    monkeypatch.setattr(config.os, "replace", replace_then_lose)
    assert config.hook_token() == other
    assert path.read_text(encoding="utf-8") == other


def test_hook_token_never_replaces_an_unreadable_file() -> None:
    path = config.tg_dir() / config.HOOK_TOKEN_FILE
    path.mkdir(parents=True)  # exists but cannot be read as a file
    assert config.hook_token() is None
    assert path.is_dir()
    assert not [item for item in path.parent.iterdir() if item.name.endswith(".tmp")]


def test_concurrent_hook_token_creation_agrees() -> None:
    results: list[str | None] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        barrier.wait()
        results.append(config.hook_token())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 8 and len(set(results)) == 1 and results[0]
    assert (config.tg_dir() / config.HOOK_TOKEN_FILE).read_text(encoding="utf-8") == results[0]


# --- SecretStore -------------------------------------------------------------------------


def test_secret_store_basic_operations(fake_dpapi: None) -> None:
    store = SecretStore()
    assert store.path == config.tg_dir() / config.INBOX_TOKENS_FILE
    assert store.keys() == [] and store.get("claude:a") is None
    assert not store.path.exists()

    store.put("claude:a", {"socket": r"\\.\pipe\LOCAL\cc-msg-0123456789abcdef", "token": "tok-a"})
    store.put("claude:b", {"socket": "s", "token": "tok-b"})
    assert sorted(store.keys()) == ["claude:a", "claude:b"]
    value = store.get("claude:a")
    assert value == {"socket": r"\\.\pipe\LOCAL\cc-msg-0123456789abcdef", "token": "tok-a"}
    value["token"] = "changed"
    assert store.get("claude:a")["token"] == "tok-a"  # callers get copies

    store.drop("claude:b")
    store.drop("claude:missing")
    assert store.keys() == ["claude:a"]

    reopened = SecretStore()
    assert reopened.keys() == ["claude:a"]
    assert reopened.get("claude:a")["token"] == "tok-a"
    assert b"tok-a" not in store.path.read_bytes()


def test_secret_store_writes_only_on_change(fake_dpapi: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    writes: list[Path] = []
    real_write = config.atomic_write_bytes

    def counting_write(path: Path, data: bytes) -> Path:
        writes.append(path)
        return real_write(path, data)

    monkeypatch.setattr(config, "atomic_write_bytes", counting_write)
    store = SecretStore(tmp_path / "store.dpapi")
    store.put("k", {"token": "t"})
    store.put("k", {"token": "t"})
    store.drop("absent")
    assert len(writes) == 1
    store.put("k", {"token": "u"})
    store.drop("k")
    assert len(writes) == 3


def test_secret_store_failed_write_changes_nothing(fake_dpapi: None, monkeypatch: pytest.MonkeyPatch,
                                                  tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "store.dpapi")
    store.put("keep", {"token": "k"})
    working_protect = config.protect

    def failing_protect(data: bytes) -> bytes:
        raise OSError("DPAPI 加密失败：fake")

    monkeypatch.setattr(config, "protect", failing_protect)
    with pytest.raises(OSError):
        store.put("new", {"token": "n"})
    with pytest.raises(OSError):
        store.drop("keep")
    assert store.keys() == ["keep"] and store.get("new") is None

    monkeypatch.setattr(config, "protect", working_protect)
    store.put("new", {"token": "n"})  # the retry is not skipped as "unchanged"
    assert sorted(SecretStore(store.path).keys()) == ["keep", "new"]


def test_secret_store_rejects_bad_input(fake_dpapi: None, tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "store.dpapi")
    with pytest.raises(ValueError):
        store.put("", {"token": "t"})
    with pytest.raises(TypeError):
        store.put("k", "not a dict")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        store.put("k", {"value": object()})
    assert store.keys() == []


def test_secret_store_damaged_file_starts_empty(fake_dpapi: None, tmp_path: Path) -> None:
    path = tmp_path / "store.dpapi"
    path.write_bytes(b"this is not a blob")
    store = SecretStore(path)
    assert store.keys() == []
    assert path.with_name("store.dpapi.corrupt").read_bytes() == b"this is not a blob"
    store.put("k", {"token": "t"})
    assert SecretStore(path).get("k") == {"token": "t"}


def test_secret_store_non_object_payload_is_damaged(fake_dpapi: None, tmp_path: Path) -> None:
    path = tmp_path / "store.dpapi"
    path.write_bytes(config.protect(b"[1, 2, 3]"))
    assert SecretStore(path).keys() == []
    assert path.with_name("store.dpapi.corrupt").exists()


def test_secret_store_is_thread_safe(fake_dpapi: None, tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "store.dpapi")
    barrier = threading.Barrier(8)

    def worker(index: int) -> None:
        barrier.wait()
        for step in range(10):
            store.put(f"k{index}-{step}", {"token": f"{index}-{step}"})

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(store.keys()) == 80
    assert len(SecretStore(tmp_path / "store.dpapi").keys()) == 80


@windows_only
def test_secret_store_with_real_dpapi(tmp_path: Path) -> None:
    store = SecretStore(tmp_path / "inbox" / "tokens.dpapi")
    store.put("claude:s1", {"socket": "pipe", "token": "super-secret-inbox-token"})
    assert b"super-secret-inbox-token" not in store.path.read_bytes()
    assert SecretStore(store.path).get("claude:s1") == {"socket": "pipe", "token": "super-secret-inbox-token"}


# --- directory ACL -----------------------------------------------------------------------


def _dacl_sids(path: Path) -> tuple[bool, set[str]]:
    import win32security

    descriptor = win32security.GetNamedSecurityInfo(
        str(path), win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION
    )
    control, _revision = descriptor.GetSecurityDescriptorControl()
    dacl = descriptor.GetSecurityDescriptorDacl()
    sids = {win32security.ConvertSidToStringSid(dacl.GetAce(index)[2]) for index in range(dacl.GetAceCount())}
    return bool(control & win32security.SE_DACL_PROTECTED), sids


def _current_user_sid() -> str:
    import win32api
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32security.TOKEN_QUERY)
    return win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])


@windows_only
def test_harden_dir_sets_a_protected_private_dacl(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b"
    assert config.harden_dir(target)
    assert target.is_dir()
    protected, sids = _dacl_sids(target)
    assert protected
    assert sids == {_current_user_sid(), "S-1-5-18"}

    child = target / "file.txt"
    child.write_text("x", encoding="utf-8")
    assert _dacl_sids(child)[1] == {_current_user_sid(), "S-1-5-18"}
    assert config.harden_dir(target)  # idempotent


@windows_only
def test_harden_dir_reports_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import win32security

    def fail(*args: object) -> None:
        raise win32security.error(5, "SetNamedSecurityInfo", "Access is denied.")

    monkeypatch.setattr(win32security, "SetNamedSecurityInfo", fail)
    assert config.harden_dir(tmp_path / "x") is False
    assert (tmp_path / "x").is_dir()


def test_harden_dir_fails_for_a_file(tmp_path: Path) -> None:
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    assert config.harden_dir(target) is False


def test_ensure_dirs_creates_both(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hardened: list[Path] = []
    monkeypatch.setenv("TGBRIDGE_LOG_DIR", str(tmp_path / "logs" / "agent-telegram"))
    monkeypatch.setattr(config, "harden_dir", lambda path: hardened.append(path) or True)
    config.ensure_dirs()
    assert hardened == [config.tg_dir(), tmp_path / "logs" / "agent-telegram"]


@windows_only
def test_ensure_dirs_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TGBRIDGE_LOG_DIR", str(tmp_path / "logs"))
    config.ensure_dirs()
    assert config.tg_dir().is_dir() and (tmp_path / "logs").is_dir()
    assert _dacl_sids(config.tg_dir())[0] and _dacl_sids(tmp_path / "logs")[0]


# --- BridgeState -------------------------------------------------------------------------


def test_fresh_state_defaults() -> None:
    bridge_state = BridgeState()
    assert bridge_state.path == config.tg_dir() / "state.json"
    assert bridge_state.update_offset == 0 and bridge_state.update_offset_at == 0.0
    assert not bridge_state.seen_update(1)
    assert not bridge_state.locked
    assert bridge_state.topics() == {}
    assert bridge_state.key_for_message(1) is None
    assert not bridge_state.muted("codex:t")
    assert bridge_state.get("x", 5) == 5
    assert not bridge_state.path.exists()


def test_mark_update_advances_offset_and_saves_at_once(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    clock = FakeClock(50.0)
    bridge_state = BridgeState(path, clock=clock)
    bridge_state.mark_update(10)
    assert bridge_state.seen_update(10) and not bridge_state.seen_update(9)
    assert bridge_state.update_offset == 11 and bridge_state.update_offset_at == 50.0
    bridge_state.mark_update(5)  # an old id never moves the offset back
    assert bridge_state.update_offset == 11 and bridge_state.seen_update(5)

    reopened = BridgeState(path, clock=clock)
    assert reopened.update_offset == 11
    assert reopened.seen_update(10) and reopened.seen_update(5)
    assert reopened.update_offset_at == 50.0


def test_an_id_below_the_offset_means_the_ids_restarted(tmp_path: Path) -> None:
    # Telegram returns ids below our offset only after restarting them lower; it then ignores
    # the higher offset (confirms nothing), so the offset must follow the new ids at once.
    path = tmp_path / "state.json"
    clock = FakeClock(50.0)
    bridge_state = BridgeState(path, clock=clock)
    bridge_state.mark_update(951_800_136)
    bridge_state.mark_update(313_217_739)
    assert bridge_state.update_offset == 313_217_740
    bridge_state.mark_update(313_217_740)
    assert bridge_state.update_offset == 313_217_741
    assert BridgeState(path, clock=clock).update_offset == 313_217_741


def test_confirming_a_handled_update_moves_the_offset_without_handling_it(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    clock = FakeClock(50.0)
    bridge_state = BridgeState(path, clock=clock)
    bridge_state.mark_update(951_800_136)
    bridge_state.mark_update(313_218_922)  # handled while the offset stayed high (old behaviour)
    bridge_state._offset = 951_800_137
    bridge_state.save()
    bridge_state.confirm_update(313_218_922)
    assert bridge_state.update_offset == 313_218_923
    assert BridgeState(path, clock=clock).update_offset == 313_218_923
    clock.now += 5
    bridge_state.confirm_update(313_218_922)  # nothing moves: no save, offset kept
    assert bridge_state.update_offset == 313_218_923


def test_stale_offset_is_forgotten(tmp_path: Path) -> None:
    # Telegram restarts update ids at a random value after a week without updates; an old,
    # higher offset would then confirm (drop) every new update.
    path = tmp_path / "state.json"
    clock = FakeClock(1_000.0)
    bridge_state = BridgeState(path, clock=clock)
    bridge_state.mark_update(500_000)
    clock.now += state.OFFSET_STALE_S - 1
    assert bridge_state.update_offset == 500_001
    assert BridgeState(path, clock=clock).update_offset == 500_001

    clock.now += 2
    assert bridge_state.update_offset == 0  # poll without an offset
    assert BridgeState(path, clock=clock).update_offset == 0
    assert bridge_state.seen_update(500_000)  # replays are still recognised
    bridge_state.mark_update(1_234)  # new ids restarted lower
    assert bridge_state.update_offset == 1_235
    bridge_state.mark_update(1_235)
    assert bridge_state.update_offset == 1_236
    assert BridgeState(path, clock=clock).update_offset == 1_236


def test_offset_without_a_timestamp_is_kept(tmp_path: Path) -> None:
    path = write_json(tmp_path / "state.json", {"update_offset": 42})
    assert BridgeState(path, clock=FakeClock(1e12)).update_offset == 42


def test_seen_updates_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.json"
    bridge_state = BridgeState(path)
    with monkeypatch.context() as patch:
        patch.setattr(BridgeState, "save", lambda self: None)
        for update_id in range(MAX_SEEN_UPDATES + 100):
            bridge_state.mark_update(update_id)
    bridge_state.save()
    assert not bridge_state.seen_update(99)
    assert bridge_state.seen_update(100) and bridge_state.seen_update(MAX_SEEN_UPDATES + 99)
    reopened = BridgeState(path)
    assert not reopened.seen_update(99) and reopened.seen_update(100)
    assert len(json.loads(path.read_text(encoding="utf-8"))["seen_updates"]) == MAX_SEEN_UPDATES


def test_lock_state_persists(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    clock = FakeClock(100.0)
    bridge_state = BridgeState(path, clock=clock)
    bridge_state.set_locked(True, "陌生成员入群")
    assert bridge_state.locked and bridge_state.locked_reason == "陌生成员入群" and bridge_state.locked_at == 100.0
    reopened = BridgeState(path)
    assert reopened.locked and reopened.locked_reason == "陌生成员入群" and reopened.locked_at == 100.0
    clock.now = 200.0
    bridge_state.set_locked(False)
    reopened = BridgeState(path)
    assert not reopened.locked and reopened.locked_at == 200.0 and reopened.locked_reason == ""


def test_topics(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    clock = FakeClock(1000.0)
    bridge_state = BridgeState(path, clock=clock)
    bridge_state.set_topic("codex:a", 11, "pc1·Codex·任务A")
    assert bridge_state.topic_for("codex:a") == {
        "thread_id": 11, "name": "pc1·Codex·任务A", "closed": False, "created_at": 1000.0, "active_at": 1000.0,
    }
    assert bridge_state.topic_for("codex:missing") is None

    bridge_state.touch_topic("codex:a", 1500.0)
    bridge_state.touch_topic("codex:a", 1200.0)  # never moves back
    bridge_state.touch_topic("codex:a", float("nan"))
    bridge_state.touch_topic("codex:a", float("inf"))
    bridge_state.touch_topic("codex:missing", 1500.0)
    assert bridge_state.topic_for("codex:a")["active_at"] == 1500.0

    bridge_state.set_topic_closed("codex:a", True)
    bridge_state.set_topic_closed("codex:missing", True)
    clock.now = 2000.0
    bridge_state.set_topic("codex:a", 11, "pc1·Codex·改名")  # rename keeps the rest
    record = bridge_state.topic_for("codex:a")
    assert record == {"thread_id": 11, "name": "pc1·Codex·改名", "closed": True, "created_at": 1000.0, "active_at": 1500.0}

    bridge_state.set_topic("codex:a", 12, "new topic")  # a new topic replaces the record
    assert bridge_state.topic_for("codex:a") == {
        "thread_id": 12, "name": "new topic", "closed": False, "created_at": 2000.0, "active_at": 2000.0,
    }
    assert bridge_state.key_for_thread(12) == "codex:a"
    assert bridge_state.key_for_thread(11) is None

    bridge_state.set_topic("claude:b", 12, "reused")  # a thread id belongs to one key only
    assert bridge_state.topic_for("codex:a") is None
    assert bridge_state.key_for_thread(12) == "claude:b"

    topics = bridge_state.topics()
    topics["claude:b"]["name"] = "changed"
    assert bridge_state.topic_for("claude:b")["name"] == "reused"  # copies

    reopened = BridgeState(path)
    assert reopened.topics() == bridge_state.topics()


def test_messages_are_bounded_and_routed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.json"
    bridge_state = BridgeState(path)
    bridge_state.remember_message(1, "codex:a")
    bridge_state.remember_message(2, "claude:b")
    assert bridge_state.key_for_message(1) == "codex:a"
    assert bridge_state.key_for_message(2) == "claude:b"
    assert BridgeState(path).key_for_message(2) == "claude:b"

    with monkeypatch.context() as patch:
        patch.setattr(BridgeState, "save", lambda self: None)
        bridge_state.remember_message(1, "codex:a")  # refreshed: now the newest
        for message_id in range(100, 100 + MAX_MESSAGES - 1):
            bridge_state.remember_message(message_id, f"codex:{message_id}")
    bridge_state.save()
    assert bridge_state.key_for_message(2) is None  # oldest dropped
    assert bridge_state.key_for_message(1) == "codex:a"
    reopened = BridgeState(path)
    assert reopened.key_for_message(1) == "codex:a"
    assert reopened.key_for_message(100 + MAX_MESSAGES - 2) == f"codex:{100 + MAX_MESSAGES - 2}"
    assert len(json.loads(path.read_text(encoding="utf-8"))["messages"]) == MAX_MESSAGES


def test_mute(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    bridge_state = BridgeState(path)
    bridge_state.set_muted("codex:a", True)
    bridge_state.set_muted("codex:a", True)
    assert bridge_state.muted("codex:a") and not bridge_state.muted("codex:b")
    assert BridgeState(path).muted("codex:a")
    bridge_state.set_muted("codex:a", False)
    assert not BridgeState(path).muted("codex:a")


def test_extra_values(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    bridge_state = BridgeState(path)
    bridge_state.put("pending", {"nonce": "ab12", "ids": [1, 2]})
    bridge_state.put("count", 3)
    bridge_state.put("nothing", None)
    value = bridge_state.get("pending")
    value["ids"].append(3)
    assert bridge_state.get("pending") == {"nonce": "ab12", "ids": [1, 2]}
    assert bridge_state.get("nothing", "default") is None
    with pytest.raises(TypeError):
        bridge_state.put("bad", object())
    with pytest.raises(ValueError):
        bridge_state.put("bad", float("nan"))
    reopened = BridgeState(path)
    assert reopened.get("pending") == {"nonce": "ab12", "ids": [1, 2]} and reopened.get("count") == 3
    bridge_state.drop("count")
    bridge_state.drop("absent")
    assert BridgeState(path).get("count", "gone") == "gone"


def test_unchanged_values_do_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writes: list[Path] = []
    monkeypatch.setattr(state, "atomic_write_json", lambda path, value: writes.append(path))
    bridge_state = BridgeState(tmp_path / "state.json")
    bridge_state.put("x", [1])
    bridge_state.put("x", [1])
    bridge_state.set_muted("k", False)
    bridge_state.touch_topic("missing", 1.0)
    bridge_state.set_topic_closed("missing", True)
    bridge_state.drop("absent")
    assert len(writes) == 1
    bridge_state.set_topic("codex:a", 5, "name")
    bridge_state.set_topic("codex:a", 5, "name")
    bridge_state.set_topic_closed("codex:a", False)
    assert len(writes) == 2


@pytest.mark.parametrize(
    "content",
    [b"{broken", b"[1, 2]", b"\xff\xfe\x00", b"[" * 100_000],
    ids=["syntax", "array", "not-utf8", "too-deep"],
)
def test_corrupt_state_starts_empty_and_is_kept(content: bytes, tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_bytes(content)
    bridge_state = BridgeState(path)
    assert bridge_state.update_offset == 0 and bridge_state.topics() == {}
    assert path.with_name("state.json.corrupt").read_bytes() == content
    bridge_state.mark_update(3)
    assert BridgeState(path).update_offset == 4


def test_state_with_wrong_field_types_is_sanitized(tmp_path: Path) -> None:
    path = write_json(
        tmp_path / "state.json",
        {
            "update_offset": "12",
            "seen_updates": [1, "2", True, 3],
            "locked": "yes",
            "locked_reason": 5,
            "topics": {"codex:a": {"thread_id": 7, "name": 3, "closed": 1}, "codex:b": {"thread_id": "8"}, "codex:c": []},
            "messages": {"10": "codex:a", "x": "codex:b", "11": 5},
            "muted": ["codex:a", 3],
            "extra": {"n": 1},
            "unknown": True,
        },
    )
    bridge_state = BridgeState(path)
    assert bridge_state.update_offset == 0
    assert bridge_state.seen_update(1) and bridge_state.seen_update(3) and not bridge_state.seen_update(2)
    assert not bridge_state.locked and bridge_state.locked_reason == ""
    assert bridge_state.topics() == {
        "codex:a": {"thread_id": 7, "name": "", "closed": False, "created_at": 0.0, "active_at": 0.0}
    }
    assert bridge_state.key_for_message(10) == "codex:a"
    assert bridge_state.key_for_message(11) is None
    assert bridge_state.muted("codex:a")
    assert bridge_state.get("n") == 1
    assert not path.with_name("state.json.corrupt").exists()


def test_duplicate_thread_ids_in_the_file_keep_the_latest_topic(tmp_path: Path) -> None:
    path = write_json(
        tmp_path / "state.json",
        {"topics": {
            "codex:old": {"thread_id": 9, "name": "old", "active_at": 10.0},
            "codex:new": {"thread_id": 9, "name": "new", "active_at": 20.0},
            "codex:stale": {"thread_id": 9, "name": "stale", "active_at": 5.0},
        }},
    )
    bridge_state = BridgeState(path)
    assert list(bridge_state.topics()) == ["codex:new"]
    assert bridge_state.key_for_thread(9) == "codex:new"


def test_lock_reason_is_stored_as_text(tmp_path: Path) -> None:
    bridge_state = BridgeState(tmp_path / "state.json")
    bridge_state.set_locked(True, None)  # type: ignore[arg-type]
    assert BridgeState(tmp_path / "state.json").locked_reason == ""


def test_state_is_thread_safe(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    bridge_state = BridgeState(path)
    barrier = threading.Barrier(6)

    def worker(index: int) -> None:
        barrier.wait()
        for step in range(15):
            update_id = index * 100 + step
            bridge_state.mark_update(update_id)
            bridge_state.remember_message(update_id, f"codex:{index}")

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    reopened = BridgeState(path)
    assert reopened.update_offset == 515
    assert all(reopened.seen_update(index * 100 + step) for index in range(6) for step in range(15))
    assert reopened.key_for_message(314) == "codex:3"
    assert not [item for item in tmp_path.iterdir() if item.name.endswith(".tmp")]


def test_resolve_proxy_falls_back_to_env_then_system_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    listening: set[str] = set()
    monkeypatch.setattr(config, "proxy_listening", lambda url, timeout=0.5: url in listening)
    monkeypatch.setattr(config, "_env_proxies", lambda: ["socks5://127.0.0.1:1080", "http://127.0.0.1:10809"])
    monkeypatch.setattr(config, "_system_proxy", lambda: "http://127.0.0.1:7890")
    listening.update({"http://127.0.0.1:7890", "http://127.0.0.1:10809", "socks5://127.0.0.1:1080"})
    assert config.resolve_proxy(Config()) == "http://127.0.0.1:10809"  # env first, SOCKS skipped
    listening.discard("http://127.0.0.1:10809")
    assert config.resolve_proxy(Config()) == "http://127.0.0.1:7890"
    listening.clear()
    assert config.resolve_proxy(Config()) is None


def test_system_proxy_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    import winreg

    values = {"ProxyEnable": 1, "ProxyServer": "http=127.0.0.1:7890;https=127.0.0.1:7891"}

    class Key:
        def __enter__(self) -> "Key":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(winreg, "OpenKey", lambda *args: Key())
    monkeypatch.setattr(winreg, "QueryValueEx", lambda key, name: (values[name], 1))
    assert config._system_proxy() == "http://127.0.0.1:7891"
    values["ProxyServer"] = "127.0.0.1:7890"
    assert config._system_proxy() == "http://127.0.0.1:7890"
    values["ProxyEnable"] = 0
    assert config._system_proxy() is None


def test_expected_bots_is_validated(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"expected_bots": 3, "recent_n": 50}', encoding="utf-8")
    cfg = Config.load(path)
    assert cfg.expected_bots == 3 and cfg.recent_n == Config().recent_n and cfg.warnings

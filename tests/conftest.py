from __future__ import annotations

import sqlite3
import os
import socket
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from agent_telegram import distribution, i18n

# Legacy tests import dynamic message constants during collection.
i18n.set_language('zh')


@pytest.fixture(autouse=True)
def source_language(monkeypatch, request):
    if request.path.name in {
        'test_distribution_defaults.py', 'test_i18n.py', 'test_tgbridge_lang_config.py',
    }:
        yield
        return
    monkeypatch.setenv('AGENT_TG_LANGUAGE', 'zh')
    # CLI entry points resolve the distribution default explicitly.
    monkeypatch.setattr(distribution, 'DEFAULT_LANGUAGE', 'zh')
    with i18n.use('zh'):
        yield

from agent_telegram import jobs
from agent_telegram.worker import JobContext


@pytest.fixture(autouse=True)
def isolated_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Path:
    # Children may inherit the old production var roots. No test, including the
    # web suite, may use them or a real agent home.
    for name in tuple(os.environ):
        if name.startswith(("AGENT_TG_LEGACY_", "WEB_BRIDGE_")):
            monkeypatch.delenv(name, raising=False)
    var = tmp_path / "var"
    monkeypatch.setenv("AGENT_TG_VAR_DIR", str(var))
    # The complete authoritative validator is a pinned test-owned snapshot.
    # Tests must never discover this dependency through the user's skill home.
    validator = Path(__file__).parent / "fixtures" / "paper_review" / "validate_review_artifacts.py"
    monkeypatch.setenv("AGENT_TG_PAPER_REVIEW_VALIDATOR", str(validator))
    monkeypatch.setenv("AGENT_TG_ANTIGRAVITY_HOME", str(tmp_path / "gemini-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("AGENT_CONTEXT_CLAUDE_HOME", str(tmp_path / "claude-home"))
    monkeypatch.setenv("AGENT_CONTEXT_CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("AGENT_CONTEXT_GEMINI_HOME", str(tmp_path / "gemini-home"))
    monkeypatch.setenv("AGENT_CONTEXT_PI_HOME", str(tmp_path / "pi-home"))
    monkeypatch.setenv("AGENT_CONTEXT_DSH_HOME", str(tmp_path / "dsh-home"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-home"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
    for name, directory in (("USERPROFILE", "home"), ("HOME", "home"),
                            ("APPDATA", "appdata"), ("LOCALAPPDATA", "localappdata")):
        monkeypatch.setenv(name, str(tmp_path / directory))
    # Log and Claude config directories of the other tools stay in tmp as well.
    monkeypatch.setenv("TGBRIDGE_LOG_DIR", str(tmp_path / "tgbridge-logs"))
    monkeypatch.setenv("CODEX_WATCH_LOG_DIR", str(tmp_path / "codex-watch-logs"))
    monkeypatch.delenv("AGENT_MAILBOX_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setenv("AGENT_CONTEXT_CALLER", "claude")
    # Running pytest inside Claude Code or Codex inherits the live session ID, which
    # source_provenance would treat as the verified current session.
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("PI_SESSION_ID", raising=False)
    monkeypatch.delenv("DSH_SESSION_ID", raising=False)
    # A real migration state on this machine must not make guarded entry points exit.
    monkeypatch.setenv("AGENT_TG_SUPERSEDED_STATE", str(tmp_path / "no-migration-state.json"))
    # Production log roots intentionally live outside var. Patch every imported
    # helper alias as well as the paths module so tests cannot write repo logs.
    from agent_telegram import paths

    log_root = tmp_path / "logs"
    original_logs_dir = getattr(paths, "logs_dir", None)
    if original_logs_dir is not None:
        for module in tuple(sys.modules.values()):
            if getattr(module, "__name__", "").startswith("agent_telegram") and getattr(module, "logs_dir", None) is original_logs_dir:
                monkeypatch.setattr(module, "logs_dir", lambda: log_root)
    return var


@pytest.fixture
def make_ctx() -> Callable[[str, dict[str, Any]], JobContext]:
    def factory(kind: str, params: dict[str, Any]) -> JobContext:
        return JobContext.load(jobs.create_job(kind, params))

    return factory


def event(kind: str, turn_id: str, **payload: Any) -> dict[str, Any]:
    return {"timestamp": "2026-09-28T00:00:00Z", "type": "event_msg", "payload": {"type": kind, "turn_id": turn_id, **payload}}


def started(turn_id: str) -> dict[str, Any]:
    return event("task_started", turn_id, started_at=1790529000)


def user_message(turn_id: str, text: str) -> dict[str, Any]:
    return event("item_completed", turn_id, item={"type": "UserMessage", "content": [{"type": "text", "text": text}]})


def delegated(turn_id: str, text: str) -> dict[str, Any]:
    """A message delivered through app-tools send_message_to_thread, as Codex records it."""

    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    output = (
        "<codex_delegation>\n  <source_thread_id>01a07078-0000-7000-8000-0000000000f1</source_thread_id>\n"
        f"  <input>{text}</input>\n</codex_delegation>"
    )
    item = {"type": "FunctionCallOutput", "id": "fco_1", "name": "send_message_to_thread", "namespace": "codex_app",
            "output": output}
    return event("item_completed", turn_id, item=item)


def relay_call(turn_id: str, tool: str, **arguments: Any) -> dict[str, Any]:
    item = {
        "type": "McpToolCall",
        "server": "gpt_relay",
        "tool": tool,
        "arguments": arguments,
        "status": "completed",
        "result": {"content": [{"type": "text", "text": f"{tool} ok"}], "isError": False},
    }
    return event("item_completed", turn_id, item=item)


def completed(turn_id: str, message: str | None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    return event("task_complete", turn_id, last_agent_message=message, error=error, completed_at=1790529100)


class CodexHome:
    """A fake CODEX_HOME with a state_5.sqlite threads table and rollout files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "sessions").mkdir(parents=True, exist_ok=True)
        self.db = root / "state_5.sqlite"
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT, created_at INTEGER,"
                " updated_at INTEGER, source TEXT, cwd TEXT, title TEXT, archived INTEGER,"
                " first_user_message TEXT, updated_at_ms INTEGER, name TEXT, is_pinned INTEGER, thread_source TEXT)"
            )

    def rollout_for(self, thread_id: str, suffix: str = "") -> Path:
        return self.root / "sessions" / f"rollout-{thread_id}{suffix}.jsonl"

    def add(
        self,
        thread_id: str,
        name: str,
        records: list[dict[str, Any]] = (),  # type: ignore[assignment]
        *,
        title: str = "",
        archived: bool = False,
        source: str = "vscode",
        updated: int = 1_790_000_000,
        thread_source: str = "user",
    ) -> Path:
        rollout = self.rollout_for(thread_id)
        self.write(rollout, records)
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    thread_id,
                    "\\\\?\\" + str(rollout),
                    updated,
                    updated,
                    source,
                    "\\\\?\\C:\\projects\\demo",
                    title or f"{name} 的首条消息",
                    int(archived),
                    f"{name} 的首条消息",
                    updated * 1000,
                    name,
                    0,
                    thread_source,
                ),
            )
        return rollout

    def set_rollout(self, thread_id: str, rollout: Path) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE threads SET rollout_path = ? WHERE id = ?", ("\\\\?\\" + str(rollout), thread_id))

    @staticmethod
    def write(path: Path, records: list[dict[str, Any]]) -> None:
        import json

        with path.open("a", encoding="utf-8") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CodexHome:
    home = CodexHome(tmp_path / "codex-home")
    monkeypatch.setenv("CODEX_HOME", str(home.root))
    return home


def _pb_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _pb_field(number: int, payload: bytes) -> bytes:
    return _pb_varint(number << 3 | 2) + _pb_varint(len(payload)) + payload


def thinking_signature(kind: str) -> str:
    """A Claude thinking signature shaped like the API's: field 2 > field 1 > field 8 = block kind."""

    import base64

    inner = _pb_varint(1 << 3) + _pb_varint(7) + _pb_field(8, kind.encode())
    return base64.b64encode(_pb_varint(1 << 3) + _pb_varint(4) + _pb_field(2, _pb_field(1, inner) + b"\x18\x02")).decode()

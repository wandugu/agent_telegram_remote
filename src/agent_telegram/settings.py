"""Where agent-telegram keeps its state and logs, and where Codex keeps its own."""

from __future__ import annotations

import os
from pathlib import Path

# src/agent_telegram/settings.py -> the unpacked folder (holds pyproject.toml and .venv).
PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def _local_appdata() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) if local else Path.home() / "AppData" / "Local"


def var_dir() -> Path:
    """Token, config, state and logs: %LOCALAPPDATA%\agent-telegram unless AGENT_TG_VAR_DIR is set."""
    override = os.environ.get("AGENT_TG_VAR_DIR")
    return Path(override) if override else _local_appdata() / "agent-telegram"


def jobs_dir() -> Path:
    return var_dir() / "jobs"


def codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    return Path(override) if override else Path.home() / ".codex"


def codex_watch_log_dir() -> Path:
    """Logs of the separately shared codex-watch tool, read to label failures it will retry."""
    return _local_appdata() / "codex-watch" / "logs"

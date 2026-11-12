"""Standalone runtime paths (standard library only)."""

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]

def var_dir() -> Path:
    override = os.environ.get("AGENT_TG_VAR_DIR")
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return Path(override) if override else base / "agent-telegram"

def logs_dir() -> Path:
    return var_dir() / "logs"

def gate_path() -> Path:
    return var_dir() / "migration" / "GATE"

def is_legacy_stored(path: str | os.PathLike[str]) -> bool:
    return False

def relocate_stored(path: str | os.PathLike[str]) -> Path:
    return Path(path)

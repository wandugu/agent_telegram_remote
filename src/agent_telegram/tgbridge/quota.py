"""Plan limits shown under every turn push.

Codex writes its account's limits into its rollouts: every event_msg ``token_count`` carries
``rate_limits`` with a ``primary`` and an optional ``secondary`` window (``used_percent``,
``window_minutes``, ``resets_at``); a Pro plan has only the weekly one. Claude Code keeps its
limits in memory, and where it runs inside Claude Desktop nothing outside can ask for them,
so the bridge reads Claude Desktop's own record, ``plan-usage-history.json``: samples
``{"t": ms, "u": {"fh": 5-hour % used, "sd": 7-day % used}}`` written only when the Desktop
refreshes its usage meter, so the numbers can be hours old and their time is shown with them.
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from agent_telegram.i18n import tr
from agent_telegram.tgbridge.model import ANTIGRAVITY, CLAUDE, CODEX, DSH, PI, Quota, QuotaWindow

FIVE_HOURS_S = 5 * 3600.0
WEEK_S = 7 * 86400.0
DESKTOP_HISTORY = "plan-usage-history.json"
DESKTOP_MAX_BYTES = 4 * 1024 * 1024
_AGENT_NAMES = {CODEX: "Codex", CLAUDE: "Claude", ANTIGRAVITY: "Antigravity", PI: "Pi", DSH: "DSH"}
# Claude Desktop's sample keys: (key, label, window length).
_DESKTOP_WINDOWS = (("fh", "5h", FIVE_HOURS_S), ("sd", 'This week', WEEK_S))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def window_label(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes == 300:
        return "5h"
    if minutes == 10080:
        return tr('This week')
    if minutes % 1440 == 0:
        return tr('{v0} days', v0=minutes // 1440)
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return tr('{v0} minutes', v0=minutes)


def codex_quota(rate_limits: Any, observed_at: float) -> Quota | None:
    """The limits a Codex token_count event carries; None when it has no usable window."""

    if not isinstance(rate_limits, dict):
        return None
    windows: list[QuotaWindow] = []
    for key in ("primary", "secondary"):
        window = rate_limits.get(key)
        if not isinstance(window, dict):
            continue
        used = _number(window.get("used_percent"))
        minutes = _number(window.get("window_minutes"))
        if used is None or minutes is None or minutes <= 0:
            continue
        span = minutes * 60.0
        windows.append(QuotaWindow(window_label(span), used, _number(window.get("resets_at")), span))
    windows.sort(key=lambda window: window.span_s or 0.0)  # the 5-hour window before the weekly one
    return Quota(CODEX, windows, observed_at) if windows else None


def desktop_history_paths() -> list[Path]:
    """Where Claude Desktop may keep its usage record.

    The Store (MSIX) app's AppData writes land in its package's LocalCache, which is what a
    process outside the package (the bridge's scheduled task) has to read; other installs
    write %APPDATA%\\Claude.
    """

    paths: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        try:
            paths += sorted(Path(local, "Packages").glob(f"Claude_*/LocalCache/Roaming/Claude/{DESKTOP_HISTORY}"))
        except OSError:
            pass
    roaming = os.environ.get("APPDATA")
    if roaming:
        paths.append(Path(roaming, "Claude", DESKTOP_HISTORY))
    return paths


def _latest_sample(path: Path) -> tuple[float, dict[str, Any]] | None:
    """(epoch seconds, usage) of the newest sample in one record file; None when unreadable."""

    try:
        with open(path, "rb") as stream:
            raw = stream.read(DESKTOP_MAX_BYTES + 1)
    except OSError:
        return None
    if len(raw) > DESKTOP_MAX_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    samples = data.get("samples") if isinstance(data, dict) else None
    best: tuple[float, dict[str, Any]] | None = None
    for sample in samples if isinstance(samples, list) else []:
        if not isinstance(sample, dict) or not isinstance(sample.get("u"), dict):
            continue
        at = _number(sample.get("t"))
        if at is not None and (best is None or at / 1000.0 > best[0]):
            best = (at / 1000.0, sample["u"])
    return best


def claude_desktop_quota(paths: list[Path] | None = None) -> Quota | None:
    """The newest sample of Claude Desktop's usage record (of any of ``paths``); None without one."""

    best: tuple[float, dict[str, Any]] | None = None
    for path in desktop_history_paths() if paths is None else paths:
        sample = _latest_sample(path)
        if sample is not None and (best is None or sample[0] > best[0]):
            best = sample
    if best is None:
        return None
    observed, usage = best
    windows = [QuotaWindow(window_label(span), used, None, span) for key, label, span in _DESKTOP_WINDOWS
               if (used := _number(usage.get(key))) is not None]
    return Quota(CLAUDE, windows, observed, source="desktop") if windows else None


def _when(epoch: float, now: float) -> str:
    moment = datetime.fromtimestamp(epoch)
    return f"{moment:%H:%M}" if moment.date() == datetime.fromtimestamp(now).date() else f"{moment:%m-%d %H:%M}"


def quota_line(quota: Quota | None, now: float) -> str:
    """What is left of each window, as one line under a push; "" when no number is current.

    A window that reset since the numbers were taken is left out (what is left is unknown),
    and so is one whose numbers are older than the window itself (Claude Desktop's record
    has no reset times).
    """

    if quota is None:
        return ""
    shown: list[str] = []
    expired: list[str] = []
    for window in quota.windows:
        if window.resets_at is not None and now >= window.resets_at:
            continue
        if window.resets_at is None and window.span_s is not None and now - quota.observed_at >= window.span_s:
            expired.append(window.label)
            continue
        left = max(0, min(100, round(100 - window.used_percent)))
        gap = " " if window.label[-1:].isascii() else ""
        text = tr('{v0}{v1}{v2}% left', v0=window.label, v1=gap, v2=left)
        if window.resets_at is not None:
            text += tr(' (resets {v0})', v0=_when(window.resets_at, now))
        shown.append(text)
    if not shown:
        return ""
    line = tr('📊 {v0} quota: ', v0=_AGENT_NAMES.get(quota.agent, quota.agent)) + " · ".join(shown)
    if quota.source == "desktop":
        note = tr('Claude Desktop record from {v0}', v0=_when(quota.observed_at, now))
        if expired:
            note += tr('; {v0} expired', v0=tr(', ').join(expired))
        line += tr(' ({note})', note=note)
    return line

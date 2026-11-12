from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from agent_telegram.tgbridge import quota
from agent_telegram.tgbridge.model import CLAUDE, CODEX, Quota, QuotaWindow

NOW = 1_791_000_000.0
# What a Codex Pro rollout carries: the weekly window only.
PRO_LIMITS = {"limit_id": "codex", "primary": {"used_percent": 5.0, "window_minutes": 10080, "resets_at": NOW + 3 * 86400},
              "secondary": None, "credits": {"has_credits": True, "balance": "100"}, "plan_type": "pro"}


def _hhmm(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M")


def _desktop_file(path: Path, *samples: tuple[float, dict[str, object]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "samples": [{"t": int(at * 1000), "org": "org-1", "u": usage}
                                                         for at, usage in samples]}), encoding="utf-8")
    return path


def test_codex_limits_come_from_the_rollout_shape() -> None:
    found = quota.codex_quota(PRO_LIMITS, NOW - 60)
    assert found == Quota(CODEX, [QuotaWindow("本周", 5.0, NOW + 3 * 86400, 7 * 86400.0)], NOW - 60)
    both = quota.codex_quota({"primary": {"used_percent": 70, "window_minutes": 10080, "resets_at": NOW + 9},
                              "secondary": {"used_percent": "12.5", "window_minutes": 300}}, NOW)
    assert [(window.label, window.used_percent) for window in both.windows] == [("5h", 12.5), ("本周", 70.0)]
    for odd in (None, [], {"primary": None}, {"primary": {"used_percent": True, "window_minutes": 300}},
                {"primary": {"used_percent": 5, "window_minutes": 0}}, {"primary": {"used_percent": "x", "window_minutes": 9}}):
        assert quota.codex_quota(odd, NOW) is None
    assert [quota.window_label(minutes * 60) for minutes in (300, 10080, 2880, 120, 45)] == ["5h", "本周", "2 天", "2h",
                                                                                           "45 分钟"]


def test_claude_limits_come_from_claude_desktops_record(tmp_path: Path) -> None:
    older = _desktop_file(tmp_path / "a.json", (NOW - 7200, {"fh": 40, "sd": 70}))
    newer = _desktop_file(tmp_path / "b.json", (NOW - 9000, {"fh": 1, "sd": 2}), (NOW - 600, {"fh": 8, "sd": 39}))
    broken = tmp_path / "c.json"
    broken.write_bytes(b"\xff not json")
    found = quota.claude_desktop_quota([older, broken, tmp_path / "missing.json", newer])
    assert found == Quota(CLAUDE, [QuotaWindow("5h", 8.0, None, 5 * 3600.0), QuotaWindow("本周", 39.0, None, 7 * 86400.0)],
                          NOW - 600, source="desktop")
    assert quota.claude_desktop_quota([broken]) is None and quota.claude_desktop_quota([]) is None
    odd = _desktop_file(tmp_path / "d.json", (NOW, {"zz": 5}))
    assert quota.claude_desktop_quota([odd]) is None


def test_claude_desktops_record_is_found_where_the_store_app_keeps_it(tmp_path: Path,
                                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    local, roaming = tmp_path / "local", tmp_path / "roaming"
    packaged = local / "Packages" / "Claude_abc123" / "LocalCache" / "Roaming" / "Claude" / quota.DESKTOP_HISTORY
    _desktop_file(packaged, (NOW, {"fh": 3, "sd": 4}))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    assert quota.desktop_history_paths() == [packaged, roaming / "Claude" / quota.DESKTOP_HISTORY]
    assert quota.claude_desktop_quota().observed_at == NOW


def test_the_line_under_a_push() -> None:
    codex = quota.codex_quota(PRO_LIMITS, NOW - 60)
    reset = datetime.fromtimestamp(NOW + 3 * 86400).strftime("%m-%d %H:%M")
    assert quota.quota_line(codex, NOW) == f"📊 Codex 额度：本周剩 95%（{reset} 重置）"
    assert quota.quota_line(codex, NOW + 3 * 86400) == ""  # that week has reset: nothing current left
    desktop = Quota(CLAUDE, [QuotaWindow("5h", 8.4, None, 5 * 3600.0), QuotaWindow("本周", 39, None, 7 * 86400.0)],
                    NOW - 600, source="desktop")
    assert quota.quota_line(desktop, NOW) == f"📊 Claude 额度：5h 剩 92% · 本周剩 61%（Claude Desktop {_hhmm(NOW - 600)} 的记录）"
    later = NOW - 600 + 6 * 3600
    stale = quota.quota_line(desktop, later)
    assert stale.startswith("📊 Claude 额度：本周剩 61%（Claude Desktop ") and stale.endswith("的记录；5h 已过期）")
    assert quota.quota_line(desktop, NOW + 8 * 86400) == "" and quota.quota_line(None, NOW) == ""
    full = Quota(CODEX, [QuotaWindow("5h", 130.0, NOW + 60, 18000.0)], NOW)
    assert quota.quota_line(full, NOW) == f"📊 Codex 额度：5h 剩 0%（{_hhmm(NOW + 60)} 重置）"

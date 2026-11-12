from __future__ import annotations

from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent_telegram import i18n
from agent_telegram import gate
from agent_telegram.tgbridge import bridge as bridge_module, cli, config as config_module
from agent_telegram.tgbridge import dsh_source, hook_server, pi_source, redact, telegram_api
from agent_telegram.tgbridge.config import Config


@pytest.mark.parametrize("pi_layout", [None, "sessions", "agent/sessions"])
@pytest.mark.parametrize("dsh_present", [False, True])
def test_run_constructs_enabled_readonly_sources_only_when_session_dirs_exist(
        tmp_path: Path, monkeypatch, pi_layout, dsh_present):
    pi_home, dsh_home = tmp_path / "fake-pi", tmp_path / "fake-dsh"
    if pi_layout:
        (pi_home / pi_layout).mkdir(parents=True)
    if dsh_present:
        (dsh_home / "sessions").mkdir(parents=True)
    cfg = Config(machine="pc1", chat_id=-1001, allowed_user_ids=[42], codex_enabled=False,
                 claude_enabled=False, antigravity_enabled=False, pi_enabled=True, dsh_enabled=True,
                 pi_home=str(pi_home), dsh_home=str(dsh_home))
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: cfg))
    monkeypatch.setattr(config_module, "ensure_dirs", lambda: None)
    monkeypatch.setattr(config_module, "load_bot_token", lambda: "test-token")
    monkeypatch.setattr(config_module, "hook_token", lambda: "test-hook-token")
    monkeypatch.setattr(config_module, "resolve_proxy", lambda cfg: None)
    monkeypatch.setattr(gate, "wait_until_open", lambda: None)
    monkeypatch.setattr(cli.codex_watch, "single_instance_lock", lambda path: object())
    monkeypatch.setattr(cli, "_install_excepthooks", lambda log: None)
    monkeypatch.setattr(cli, "_refresh_outdated_hooks", lambda *args: None)
    monkeypatch.setattr(redact, "load_local_secrets", lambda *args: [])
    monkeypatch.setattr(telegram_api, "BotAPI", Mock())
    monkeypatch.setattr(hook_server, "HookServer", Mock())
    fake_bridge = Mock()
    fake_bridge.run.return_value = 0
    monkeypatch.setattr(bridge_module, "Bridge", Mock(return_value=fake_bridge))
    pi_factory, dsh_factory = Mock(), Mock()
    monkeypatch.setattr(pi_source, "PiSource", pi_factory)
    monkeypatch.setattr(dsh_source, "DshSource", dsh_factory)
    pi_dirs = Mock(wraps=pi_source._sessions_dirs)
    dsh_default = Mock(wraps=dsh_source.default_home)
    monkeypatch.setattr(pi_source, "_sessions_dirs", pi_dirs)
    monkeypatch.setattr(dsh_source, "default_home", dsh_default)

    with i18n.use("zh"):
        assert cli.cmd_run(Namespace()) == 0

    # These assertions cover the actual lazy-import/startup path. With the old
    # sessions_root imports, neither helper nor constructor can be reached,
    # even though the broad optional-source exception lets cmd_run return 0.
    pi_dirs.assert_called_once_with(pi_home)
    dsh_default.assert_called_once_with(cfg)
    if pi_layout:
        pi_factory.assert_called_once_with(cfg, fake_bridge.on_event)
    else:
        pi_factory.assert_not_called()
    if dsh_present:
        dsh_factory.assert_called_once_with(cfg, fake_bridge.on_event)
    else:
        dsh_factory.assert_not_called()
    fake_bridge.attach_sources.assert_called_once_with(
        codex=None, claude=None, antigravity=None,
        pi=pi_factory.return_value if pi_layout else None,
        dsh=dsh_factory.return_value if dsh_present else None,
    )
    fake_bridge.run.assert_called_once_with()

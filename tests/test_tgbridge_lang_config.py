from __future__ import annotations

import json
import re

import pytest

from agent_telegram import distribution
from agent_telegram import codex_send as send
from agent_telegram import i18n
from agent_telegram.tgbridge import cli, config, codex_ipc, quota, redact, render
from agent_telegram.tgbridge.config import Config
from agent_telegram.tgbridge.model import CODEX, SessionView

CJK = re.compile(r'[\u3400-\u9fff]')


@pytest.fixture(autouse=True)
def restore_language():
    original = i18n.current()
    i18n.set_language(distribution.DEFAULT_LANGUAGE)
    yield
    i18n.set_language(original)


def save_raw(data):
    path = config.config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding='utf-8')
    return path


def test_distribution_defaults_when_missing(tmp_path):
    cfg = Config.load(tmp_path / 'missing.json')
    assert cfg.language == ''
    assert cfg.antigravity_enabled is distribution.ANTIGRAVITY_ENABLED
    assert cfg.pi_enabled is distribution.PI_ENABLED
    assert cfg.dsh_enabled is distribution.DSH_ENABLED
    assert not cfg.antigravity_enabled and not cfg.pi_enabled and not cfg.dsh_enabled
    assert distribution.RESTART_COMMAND == r'powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Action Install'


@pytest.mark.parametrize(('value', 'expected'), [
    ('ENGLISH', 'en'), ('cn', 'zh'), ('ZH_cn', 'zh'), ('中文', 'zh'), ('', ''),
])
def test_config_language_normalizes(value, expected):
    assert Config.load(save_raw({'language': value})).language == expected


@pytest.mark.parametrize('value', ['fr', ' ', False, 12, None])
def test_config_language_rejects_unknown_values(value):
    cfg = Config.load(save_raw({'language': value}))
    assert cfg.language == '' and cfg.warnings


def test_lang_shows_effective_default_and_change_command(capsys):
    assert cli.main(['lang']) == 0
    out = capsys.readouterr().out
    assert 'Language: en.' in out and 'lang en|zh' in out
    assert not config.config_path().exists()


def test_lang_saves_and_localizes_next_invocation(capsys):
    assert cli.main(['lang', 'en']) == 0
    out = capsys.readouterr().out
    assert 'Language saved: en.' in out and not CJK.search(out)
    assert distribution.RESTART_COMMAND in out and '/lang' in out
    assert Config.load().language == 'en'
    assert cli.main(['lang']) == 0
    assert 'Language: en.' in capsys.readouterr().out
    assert cli.main(['lang', 'zh']) == 0
    assert '语言已保存：zh。' in capsys.readouterr().out


@pytest.mark.parametrize('language', ['en', 'zh'])
def test_cli_help_uses_saved_language(language, capsys):
    save_raw({'language': language})
    with i18n.use('zh' if language == 'en' else 'en'):
        with pytest.raises(SystemExit) as caught:
            cli.main(['--help'])
        assert caught.value.code == 0
        out = capsys.readouterr().out
        assert 'lang' in out and 'config' in out
        assert bool(CJK.search(out)) is (language == 'zh')


def test_config_show_effective_values_and_defaults_without_secrets(monkeypatch, capsys):
    save_raw({'language': 'en', 'summary_chars': 777, 'machine': 'pc1'})
    monkeypatch.setattr(config, 'load_bot_token', lambda: pytest.fail('config must not read bot tokens'))
    assert cli.main(['config', 'show']) == 0
    lines = capsys.readouterr().out.splitlines()
    assert 'summary_chars = 777' in lines
    assert 'language = "en" (default)' in lines
    assert 'pi_enabled = false (default)' in lines
    assert 'machine = "pc1"' in lines
    assert len(lines) == len(Config().to_dict())
    assert not any('token' in line.lower() for line in lines)
    assert not CJK.search('\n'.join(lines))


@pytest.mark.parametrize(('key', 'value', 'expected'), [
    ('quota_enabled', 'yes', True), ('quota_enabled', 'off', False),
    ('codex_enabled', '1', True), ('codex_enabled', '0', False),
    ('recent_n', '7', 7), ('active_window_h', '1.5', 1.5),
    ('machine', '', ''), ('claude_exe', r'C:\tools\claude.exe', r'C:\tools\claude.exe'),
    ('proxy', 'auto', None), ('proxy', 'null', None),
    ('proxy', 'direct', ''), ('proxy', '', ''),
    ('proxy', 'http://localhost:7897', 'http://localhost:7897'),
    ('roots', 'C:\\proj,C:\\other', ['C:\\proj', 'C:\\other']),
    ('content_exclude', '["C:\\\\private", "C:\\\\hidden"]', ['C:\\private', 'C:\\hidden']),
    ('codex_remote_hosts', '', []), ('language', 'english', 'en'),
])
def test_config_set_parses_validates_and_saves(key, value, expected, capsys):
    save_raw({'language': 'en'})
    assert cli.main(['config', 'set', key, value]) == 0
    raw = json.loads(config.config_path().read_text(encoding='utf-8'))
    assert raw[key] == expected
    assert not Config.load().warnings
    out = capsys.readouterr().out
    assert distribution.RESTART_COMMAND in out and not CJK.search(out)


@pytest.mark.parametrize(('key', 'value'), [
    ('recent_n', '0'), ('recent_n', '31'), ('recent_n', '1.5'),
    ('active_window_h', '0'), ('active_window_h', 'nan'), ('active_window_h', 'inf'),
    ('active_window_h', 'x'), ('quota_enabled', 'maybe'),
    ('proxy', 'socks5://localhost:1080'), ('proxy', 'http://'),
    ('roots', '[1,2]'), ('roots', '["broken"'), ('language', 'fr'),
    ('chat_id', '-1001'), ('allowed_user_ids', '[42]'),
    ('bot_username', 'testbot'), ('warnings', '[]'), ('unknown', 'x'),
])
def test_config_set_refuses_invalid_or_protected_without_saving(key, value, capsys):
    path = save_raw({'language': 'en', 'recent_n': 8})
    original = path.read_bytes()
    assert cli.main(['config', 'set', key, value]) == 2
    assert path.read_bytes() == original
    out = capsys.readouterr().out
    assert out.strip() and not CJK.search(out)


def test_config_unset_restores_defaults_and_preserves_binding(capsys):
    save_raw({'language': 'en', 'summary_chars': 777, 'chat_id': -1001, 'allowed_user_ids': [42]})
    assert cli.main(['config', 'unset', 'summary_chars']) == 0
    cfg = Config.load()
    assert cfg.summary_chars == Config().summary_chars
    assert cfg.chat_id == -1001 and cfg.allowed_user_ids == [42]
    assert distribution.RESTART_COMMAND in capsys.readouterr().out
    assert cli.main(['config', 'unset', 'chat_id']) == 2
    capsys.readouterr()
    assert Config.load().chat_id == -1001
    assert cli.main(['config', 'unset', 'language']) == 0
    assert Config.load().language == ''


def test_chinese_config_errors_remain_identical():
    with i18n.use('zh'):
        cfg = Config.load(save_raw({'recent_n': 0, 'quota_enabled': 'yes'}))
    assert cfg.warnings == [
        'config.json 的 recent_n 取值无效，已使用默认值',
        'config.json 的 quota_enabled 应为true/false，已使用默认值',
    ]
    with i18n.use('en'):
        assert not CJK.search('\n'.join(Config.load().warnings))


def test_render_and_errors_switch_immediately_without_reimporting():
    view = SessionView(agent=CODEX, id='test', title='', cwd='', last_activity=40, state='idle')
    outputs = {}
    for language in ('zh', 'en'):
        with i18n.use(language):
            turn, _ = render.turn_card(machine='pc1', agent=CODEX, title='', status='completed',
                                       duration_s=65, text=None)
            error = send._manifest_warnings({'status': 'invalid'})[0]
            outputs[language] = '\n'.join([
                render.session_line(view, 100), turn,
                render.confirm_card(machine='pc1', agent=CODEX, title='Task', action='send', text='hello'),
                str(codex_ipc.TurnEnded()), str(codex_ipc.OutcomeUnknown()),
                str(error), quota.window_label(7 * 86400), redact.Redactor(['secret-123456'])('secret-123456'),
                redact.redact_patterns('Bearer sk-abcdefghijklmnopqrstuvwxyz123456'),
            ])
    assert not CJK.search(outputs['en'])
    assert '（无标题）' in outputs['zh']
    assert '（没有最终回复）' in outputs['zh'] and '1分' in outputs['zh']
    assert '📨 确认发送到 pc1·Codex·Task？' in outputs['zh']
    assert '本周' in outputs['zh'] and '‹已脱敏›' in outputs['zh']


def test_build_parser_uses_context_language():
    with i18n.use('en'):
        assert not CJK.search(cli.build_parser().format_help())
    with i18n.use('zh'):
        assert '把本机 Codex / Claude Code 对话接到 Telegram 群。' in cli.build_parser().format_help()

"""Exercise the public distribution's defaults in isolated processes and homes."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_telegram import distribution, i18n
from agent_telegram.tgbridge import cli, config
from test_i18n import CJK


@pytest.fixture(autouse=True)
def default_language(monkeypatch):
    monkeypatch.delenv("AGENT_TG_LANGUAGE", raising=False)
    with i18n.use(distribution.DEFAULT_LANGUAGE):
        yield


def run_python(tmp_path, *arguments, language=None):
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    env.pop("AGENT_TG_LANGUAGE", None)
    if language is not None:
        env["AGENT_TG_LANGUAGE"] = language
    return subprocess.run(
        [sys.executable, "-B", "-X", "utf8", *arguments], cwd=tmp_path, env=env,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
        timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_missing_config_uses_english_and_disables_optional_readers(tmp_path, capsys):
    assert not config.config_path().exists()
    cfg = config.Config.load(tmp_path / "missing.json")
    assert (cfg.language or distribution.DEFAULT_LANGUAGE) == "en"
    assert distribution.DEFAULT_LANGUAGE == "en"
    assert not cfg.antigravity_enabled and not cfg.pi_enabled and not cfg.dsh_enabled
    assert not distribution.ANTIGRAVITY_ENABLED and not distribution.PI_ENABLED and not distribution.DSH_ENABLED
    assert distribution.RESTART_COMMAND == (
        r"powershell -ExecutionPolicy Bypass -File .\install_task.ps1 -Action Install"
    )
    assert cli.main(["lang"]) == 0
    assert "Language: en." in capsys.readouterr().out
    assert i18n.current() == "en"
    assert not config.config_path().exists()


def test_module_help_defaults_to_english(tmp_path):
    result = run_python(tmp_path, "-m", "agent_telegram", "--help")
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout and "lang" in result.stdout
    assert not CJK.search(result.stdout + result.stderr)
    assert not config.config_path().exists()


def test_lang_zh_persists_for_following_process(tmp_path):
    result = run_python(tmp_path, "-m", "agent_telegram", "lang", "zh")
    assert result.returncode == 0, result.stderr
    assert i18n.MESSAGES["Language saved: {language}."].format(language="zh") in result.stdout
    assert distribution.RESTART_COMMAND in result.stdout
    assert config.Config.load().language == "zh"
    result = run_python(tmp_path, "-m", "agent_telegram", "--help")
    assert result.returncode == 0, result.stderr
    assert CJK.search(result.stdout)


@pytest.mark.parametrize(("language", "expected"), [
    (None, "en"), ("zh", "zh"), ("ENGLISH", "en"), ("unknown", "en"),
])
def test_initial_language_override_and_explicit_precedence(tmp_path, language, expected):
    result = run_python(
        tmp_path, "-c",
        "import os; from agent_telegram import i18n; print(i18n.current()); "
        "os.environ['AGENT_TG_LANGUAGE'] = 'zh'; print(i18n.current()); "
        "i18n.set_language('en'); print(i18n.current())",
        language=language,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [expected, expected, "en"]


def test_cli_config_takes_precedence_over_initial_language(tmp_path):
    result = run_python(tmp_path, "-m", "agent_telegram", "--help", language="zh")
    assert result.returncode == 0, result.stderr
    assert not CJK.search(result.stdout + result.stderr)

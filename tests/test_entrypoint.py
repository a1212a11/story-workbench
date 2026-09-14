"""P02：启动入口的最小行为断言。"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from story_workbench import bootstrap
from story_workbench.__main__ import main


def test_help_exits_zero_without_project(capsys):
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "小说项目目录" in out


def test_project_argument_is_not_yet_served(tmp_path, capsys):
    assert main([str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert str(tmp_path) in err


def test_module_is_runnable_via_python_m():
    result = subprocess.run(
        [sys.executable, "-m", "story_workbench", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "story_workbench" in result.stdout


def test_deepseek_configuration_selects_own_key_and_compatible_client(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"provider": "deepseek"}))
    settings = bootstrap.load_settings(path)
    assert settings.model == "deepseek-flash"
    assert settings.api_key_env == "DEEPSEEK_API_KEY"
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "other-provider-secret")
    assert bootstrap.build_clients(settings) is None
    captured = {}

    def make_clients(key, **kwargs):
        captured.update(key=key, **kwargs)
        return ("stream", "complete")

    monkeypatch.setattr(bootstrap, "make_anthropic_clients", make_clients)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    assert bootstrap.build_clients(settings).stream == "stream"
    assert captured == {
        "key": "deepseek-secret",
        "base_url": "https://api.deepseek.com/anthropic",
        "disable_thinking": True,
    }
    assert "secret" not in repr(settings)
    assert bootstrap.build_generation_config(settings).provider == "deepseek"
    path.write_text(json.dumps({"provider": "deepseek", "model": "deepseek-v4-pro"}))
    assert bootstrap.load_settings(path).model == "deepseek-v4-pro"
    assert bootstrap.Settings().model == "claude-sonnet-5"


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"provider": "unknown"},
        {"provider": "deepseek", "DEEPSEEK_API_KEY": "secret"},
        {"api_key": "secret"},
        {"model": ""},
        {"max_output_tokens": -1},
        {"timeout_seconds": "slow"},
        {"context_tokens": True},
        {"max_output_token": 5},
    ],
)
def test_invalid_settings_are_rejected_without_echoing_values(tmp_path, raw):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError) as exc:
        bootstrap.load_settings(path)
    assert "secret" not in str(exc.value)


def test_explicit_missing_config_does_not_fall_back_to_another_provider(tmp_path, monkeypatch):
    missing = tmp_path / "missing.json"
    monkeypatch.setenv(bootstrap.CONFIG_ENV, str(missing))
    with pytest.raises(ValueError, match="不存在"):
        bootstrap.load_settings()
    monkeypatch.delenv(bootstrap.CONFIG_ENV)
    monkeypatch.setattr(bootstrap, "DEFAULT_CONFIG_PATH", missing)
    assert bootstrap.load_settings().provider == "anthropic"

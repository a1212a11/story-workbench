"""配置：三家供应商的默认模型与密钥变量；配置文件不能含密钥；没有密钥不建客户端。"""

from __future__ import annotations

import json

import pytest

from story_agent import config


def test_provider_defaults_and_key_envs():
    assert config.Settings().model == "claude-sonnet-5"
    assert config.Settings(provider="deepseek").api_key_env == "DEEPSEEK_API_KEY"
    openrouter = config.Settings(provider="openrouter")
    assert openrouter.model == "google/gemini-3.8-flash"
    assert openrouter.api_key_env == "OPENROUTER_API_KEY"
    with pytest.raises(ValueError):
        config.Settings(provider="openai")


def test_config_file_rejects_keys_and_builds_clients_only_with_env(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"provider": "openrouter", "OPENROUTER_API_KEY": "x"}))
    with pytest.raises(ValueError):
        config.load_settings(path)
    path.write_text(json.dumps({"provider": "openrouter", "model": "google/gemini-3.5-flash"}))
    settings = config.load_settings(path)
    assert settings.model == "google/gemini-3.5-flash"
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert config.build_clients(settings) is None
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    clients = config.build_clients(settings)
    assert clients is not None and callable(clients.stream)
    agent_config = config.build_agent_config(settings)
    assert agent_config.provider == "openrouter" and agent_config.budget > 0

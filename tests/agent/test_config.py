"""配置：三家供应商的默认模型与密钥变量；配置文件不能含密钥；没有密钥不建客户端。"""

from __future__ import annotations

import json

import pytest

from story_agent import config


def test_provider_defaults_and_key_envs():
    assert config.Settings().timeout_seconds == 120.0  # 卡住时早点报错，不要干等五分钟
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


def test_prices_come_from_table_or_config_and_cost_is_estimated():
    known = config.Settings(provider="openrouter")
    assert known.prices == (0.75, 3.75)
    unknown = config.Settings(provider="openrouter", model="some/new-model")
    assert unknown.prices is None
    custom = config.Settings(
        provider="openrouter", model="some/new-model", input_price=1, output_price=2
    )
    assert custom.prices == (1.0, 2.0)
    with pytest.raises(ValueError):
        config.Settings(provider="openrouter", input_price=1)
    with pytest.raises(ValueError):
        config.Settings(provider="openrouter", input_price=-1, output_price=2)
    agent_config = config.build_agent_config(custom)
    assert agent_config.cost({"input_tokens": 1_000_000, "output_tokens": 500_000}) == 2.0
    assert config.build_agent_config(unknown).cost({"input_tokens": 5}) is None

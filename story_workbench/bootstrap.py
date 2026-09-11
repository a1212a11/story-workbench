"""运行配置装配：非敏感配置在用户配置目录，密钥只走环境变量。

由 __main__ 调用；不进入 domain、ai 或模型任务。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from story_workbench.ai.context import ContextLimits
from story_workbench.application.generation import Clients, GenerationConfig
from story_workbench.infrastructure.provider import CallOptions, make_anthropic_clients

API_KEY_ENV = "ANTHROPIC_API_KEY"
CONFIG_ENV = "STORY_WORKBENCH_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "story-workbench" / "config.json"


@dataclass(frozen=True, slots=True)
class Settings:
    """默认值来自技术选型 P04 决定；均可在配置文件覆盖。"""

    model: str = "claude-sonnet-5"
    max_output_tokens: int = 8000
    timeout_seconds: float = 300.0
    context_tokens: int = 100_000
    template_reserve: int = 2_000
    provider: str = "anthropic"


def load_settings(path: Path | None = None) -> Settings:
    config_path = path or Path(os.environ.get(CONFIG_ENV, DEFAULT_CONFIG_PATH))
    if not config_path.exists():
        return Settings()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    allowed = {k: raw[k] for k in Settings.__dataclass_fields__ if k in raw}
    if "api_key" in raw or API_KEY_ENV in raw:
        raise ValueError("配置文件不能包含密钥；请改用环境变量 " + API_KEY_ENV)
    return Settings(**allowed)


def build_generation_config(settings: Settings) -> GenerationConfig:
    return GenerationConfig(
        options=CallOptions(
            model=settings.model,
            max_output_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds,
        ),
        limits=ContextLimits(
            total_tokens=settings.context_tokens,
            template_reserve=settings.template_reserve,
            output_reserve=settings.max_output_tokens,
        ),
        provider=settings.provider,
    )


def build_clients(settings: Settings) -> Clients | None:
    """没有密钥时返回 None，界面据此提示“模型尚未接入”。"""
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        return None
    stream, complete = make_anthropic_clients(api_key)
    return Clients(stream=stream, complete=complete)

"""运行配置：非敏感配置在用户配置目录，密钥只走环境变量。

沿用旧包的配置文件位置与字段，作者已有的配置不用改。
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

from story_agent.provider import CallOptions, CompleteCall, StreamCall, make_anthropic_clients


@dataclass(frozen=True, slots=True)
class Provider:
    key_env: str
    default_model: str
    base_url: str | None = None
    disable_thinking: bool = False


# 三家都走 Anthropic 消息协议：DeepSeek 与 OpenRouter 提供兼容端点。
PROVIDERS = {
    "anthropic": Provider("ANTHROPIC_API_KEY", "claude-sonnet-5"),
    "deepseek": Provider(
        "DEEPSEEK_API_KEY", "deepseek-flash", "https://api.deepseek.com/anthropic", True
    ),
    "openrouter": Provider(
        "OPENROUTER_API_KEY", "google/gemini-3.8-flash", "https://openrouter.ai/api"
    ),
}
API_KEY_ENV = "ANTHROPIC_API_KEY"
CONFIG_ENV = "STORY_WORKBENCH_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "story-workbench" / "config.json"


@dataclass(frozen=True, slots=True)
class Settings:
    model: str | None = None
    max_output_tokens: int = 8000
    timeout_seconds: float = 300.0
    context_tokens: int = 100_000
    template_reserve: int = 2_000
    provider: str = "anthropic"

    def __post_init__(self) -> None:
        if self.provider not in PROVIDERS:
            raise ValueError("模型供应商请选择 anthropic、deepseek 或 openrouter。")
        if self.model is None:
            object.__setattr__(self, "model", PROVIDERS[self.provider].default_model)
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("模型名称不能为空。")
        for name in ("max_output_tokens", "context_tokens", "template_reserve"):
            value = getattr(self, name)
            if type(value) is not int or value < (0 if name == "template_reserve" else 1):
                raise ValueError("上下文与输出限额须为有效整数。")
        if (
            type(self.timeout_seconds) not in (int, float)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("超时秒数须为有限正数。")
        if self.context_tokens <= self.template_reserve + self.max_output_tokens:
            raise ValueError("上下文限额必须大于模板预留与输出限额之和。")

    @property
    def api_key_env(self) -> str:
        return PROVIDERS[self.provider].key_env


def load_settings(path: Path | None = None) -> Settings:
    configured = path or os.environ.get(CONFIG_ENV)
    config_path = Path(configured) if configured else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        if configured:
            raise ValueError("指定的模型配置文件不存在，请检查配置路径。")
        return Settings()
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("无法读取模型配置，请检查文件权限与 JSON 格式。") from None
    if not isinstance(raw, dict):
        raise ValueError("模型配置必须是 JSON 对象。")
    if any(k in raw for k in ("api_key", *(p.key_env for p in PROVIDERS.values()))):
        raise ValueError("配置文件不能包含密钥；请使用对应供应商的环境变量。")
    if raw.keys() - Settings.__dataclass_fields__.keys():
        raise ValueError("模型配置含未知字段，请核对配置说明。")
    return Settings(**raw)


@dataclass(frozen=True, slots=True)
class Clients:
    stream: StreamCall
    complete: CompleteCall


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """代理需要的全部模型参数。"""

    options: CallOptions
    context_tokens: int
    template_reserve: int
    provider: str

    @property
    def budget(self) -> int:
        """留给系统提示与对话的估算 token 数。"""
        return self.context_tokens - self.template_reserve - self.options.max_output_tokens


def estimate_tokens(text: str) -> int:
    """保守估算：中文按字符数 × 1.2 向上取整。"""
    return math.ceil(len(text) * 1.2)


def build_agent_config(settings: Settings) -> AgentConfig:
    return AgentConfig(
        options=CallOptions(
            model=settings.model,
            max_output_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds,
        ),
        context_tokens=settings.context_tokens,
        template_reserve=settings.template_reserve,
        provider=settings.provider,
    )


def build_clients(settings: Settings) -> Clients | None:
    """没有密钥时返回 None：界面可打开，代理不发请求。"""
    api_key = os.environ.get(settings.api_key_env, "").strip()
    if not api_key:
        return None
    provider = PROVIDERS[settings.provider]
    stream, complete = make_anthropic_clients(
        api_key, base_url=provider.base_url, disable_thinking=provider.disable_thinking
    )
    return Clients(stream, complete)

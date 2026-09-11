"""模型客户端合同。首期只定义事件与调用函数签名；真实供应商在 G05 接入。

客户端只处理协议、认证、超时、流与网络错误；不理解采用与 Canon，
不执行模型提出的动作，不自动重试付费请求。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class EventKind(StrEnum):
    TEXT = "text"
    USAGE = "usage"
    COMPLETED = "completed"
    TRUNCATED = "truncated"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """供应商事件归一化后的形态。截断和异常必须区别于 completed。"""

    kind: EventKind
    text: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: str  # system / user / assistant
    content: str


@dataclass(frozen=True, slots=True)
class CallOptions:
    model: str
    max_output_tokens: int
    timeout_seconds: float
    temperature: float | None = None


@dataclass(frozen=True, slots=True)
class CompleteResult:
    text: str
    usage: dict[str, int]
    truncated: bool


CancelSignal = threading.Event


class StreamCall(Protocol):
    def __call__(
        self, messages: list[ChatMessage], options: CallOptions, cancel: CancelSignal
    ) -> Iterator[StreamEvent]: ...


class CompleteCall(Protocol):
    def __call__(
        self, messages: list[ChatMessage], options: CallOptions, cancel: CancelSignal
    ) -> CompleteResult: ...


# ---------------------------------------------------------------- Anthropic 客户端（G05）


class ProviderError(Exception):
    """协议或网络层错误；由用例转记为 MODEL_FAILED。不含密钥或请求头。"""


def _split_messages(messages: list[ChatMessage]) -> tuple[str, list[dict[str, str]]]:
    system_parts = [m.content for m in messages if m.role == "system"]
    chat = [
        {"role": m.role, "content": m.content} for m in messages if m.role in ("user", "assistant")
    ]
    return "\n\n".join(system_parts), chat


def _usage(message) -> dict[str, int]:
    usage = getattr(message, "usage", None)
    if usage is None:
        return {}
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
    }


def make_anthropic_clients(api_key: str) -> tuple[StreamCall, CompleteCall]:
    """返回一对绑定密钥的客户端函数。max_retries=0：付费请求不自动重试。

    密钥只保存在闭包里，不出现在返回的事件、异常或日志中。
    """
    from anthropic import Anthropic, APIError

    def _client(options: CallOptions) -> Anthropic:
        return Anthropic(api_key=api_key, max_retries=0, timeout=options.timeout_seconds)

    def stream(
        messages: list[ChatMessage], options: CallOptions, cancel: CancelSignal
    ) -> Iterator[StreamEvent]:
        system, chat = _split_messages(messages)
        kwargs: dict = {
            "model": options.model,
            "max_tokens": options.max_output_tokens,
            "messages": chat,
        }
        if system:
            kwargs["system"] = system
        if options.temperature is not None:
            kwargs["temperature"] = options.temperature
        try:
            with _client(options).messages.stream(**kwargs) as response:
                for text in response.text_stream:
                    if cancel.is_set():
                        response.close()
                        yield StreamEvent(EventKind.ERROR, error="cancelled")
                        return
                    if text:
                        yield StreamEvent(EventKind.TEXT, text=text)
                final = response.get_final_message()
        except APIError as exc:
            yield StreamEvent(EventKind.ERROR, error=type(exc).__name__)
            return
        except Exception as exc:  # 超时、网络中断等
            yield StreamEvent(EventKind.ERROR, error=type(exc).__name__)
            return
        yield StreamEvent(EventKind.USAGE, usage=_usage(final))
        if getattr(final, "stop_reason", None) == "max_tokens":
            yield StreamEvent(EventKind.TRUNCATED)
        else:
            yield StreamEvent(EventKind.COMPLETED)

    def complete(
        messages: list[ChatMessage], options: CallOptions, cancel: CancelSignal
    ) -> CompleteResult:
        if cancel.is_set():
            raise ProviderError("cancelled")
        system, chat = _split_messages(messages)
        kwargs: dict = {
            "model": options.model,
            "max_tokens": options.max_output_tokens,
            "messages": chat,
        }
        if system:
            kwargs["system"] = system
        if options.temperature is not None:
            kwargs["temperature"] = options.temperature
        try:
            message = _client(options).messages.create(**kwargs)
        except APIError as exc:
            raise ProviderError(type(exc).__name__) from None
        except Exception as exc:
            raise ProviderError(type(exc).__name__) from None
        text = "".join(
            getattr(block, "text", "")
            for block in message.content
            if getattr(block, "type", "") == "text"
        )
        return CompleteResult(
            text=text,
            usage=_usage(message),
            truncated=getattr(message, "stop_reason", None) == "max_tokens",
        )

    return stream, complete

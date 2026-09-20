"""模型客户端合同：事件、消息与调用函数签名，以及 Anthropic 协议客户端（自旧包原样搬入）。

客户端只处理协议、认证、超时、流与网络错误；不理解采用与 Canon，
不执行模型提出的动作，不自动重试付费请求。工具调用只是把模型提出的
tool_use 块原样归一化交给调用方，由调用方决定是否执行。
"""

from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Awaitable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class EventKind(StrEnum):
    TEXT = "text"
    THINKING = "thinking"  # 推理型号回答前的思考增量；不是回复正文
    TOOL_USE = "tool_use"
    USAGE = "usage"
    COMPLETED = "completed"
    TRUNCATED = "truncated"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """提供给模型的工具定义；input_schema 为 JSON Schema 对象。"""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    """模型提出的一次工具调用；是否执行由调用方决定。"""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """供应商事件归一化后的形态。截断和异常必须区别于 completed。

    tool_use 事件在文本之后、usage 之前产出；模型以工具调用结束发言也算 completed。
    """

    kind: EventKind
    text: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None
    tool_use: ToolUseBlock | None = None


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """纯文本消息 content 为 str；带工具调用或工具结果的消息 content 为内容块序列。"""

    role: str  # system / user / assistant
    content: str | tuple[ContentBlock, ...]


@dataclass(frozen=True, slots=True)
class CallOptions:
    model: str
    max_output_tokens: int
    timeout_seconds: float
    temperature: float | None = None
    tools: tuple[ToolSpec, ...] = ()


@dataclass(frozen=True, slots=True)
class CompleteResult:
    text: str
    usage: dict[str, int]
    truncated: bool
    tool_uses: tuple[ToolUseBlock, ...] = ()


CancelSignal = threading.Event


class StreamCall(Protocol):
    def __call__(
        self, messages: list[ChatMessage], options: CallOptions, cancel: CancelSignal
    ) -> Iterator[StreamEvent]: ...


class CompleteCall(Protocol):
    def __call__(
        self, messages: list[ChatMessage], options: CallOptions, cancel: CancelSignal
    ) -> CompleteResult: ...


class AsyncStreamCall(Protocol):
    def __call__(
        self, messages: list[ChatMessage], options: CallOptions, cancel: CancelSignal
    ) -> AsyncIterator[StreamEvent]: ...


class AsyncCompleteCall(Protocol):
    def __call__(
        self, messages: list[ChatMessage], options: CallOptions, cancel: CancelSignal
    ) -> Awaitable[CompleteResult]: ...


# ---------------------------------------------------------------- Anthropic 客户端（G05）


class ProviderError(Exception):
    """协议或网络层错误；由用例转记为 MODEL_FAILED。不含密钥或请求头。"""


def _delta_of(event) -> StreamEvent | None:
    """流事件里的正文或思考增量；其他事件返回 None。"""
    if getattr(event, "type", "") != "content_block_delta":
        return None
    delta = getattr(event, "delta", None)
    kind = getattr(delta, "type", "")
    if kind == "text_delta":
        text = getattr(delta, "text", "")
        return StreamEvent(EventKind.TEXT, text=text) if text else None
    if kind == "thinking_delta":
        text = getattr(delta, "thinking", "")
        return StreamEvent(EventKind.THINKING, text=text) if text else None
    return None


def describe_api_error(exc: Exception) -> str:
    """错误类型、HTTP 状态码与供应商给的原因，截短；不含请求头与密钥。"""
    parts = [type(exc).__name__]
    status = getattr(exc, "status_code", None)
    if status:
        parts.append(f"HTTP {status}")
    message = str(getattr(exc, "message", "") or "").strip()
    if message:
        message = " ".join(message.split())
        parts.append(message if len(message) <= 160 else message[:160] + "…")
    return " · ".join(parts)


def _block_param(block: ContentBlock) -> dict[str, Any]:
    if isinstance(block, TextBlock):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseBlock):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    param: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": block.tool_use_id,
        "content": block.content,
    }
    if block.is_error:
        param["is_error"] = True
    return param


def _content_param(content: str | tuple[ContentBlock, ...]) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    return [_block_param(block) for block in content]


def _text_of(content: str | tuple[ContentBlock, ...]) -> str:
    if isinstance(content, str):
        return content
    return "\n\n".join(block.text for block in content if isinstance(block, TextBlock))


def _split_messages(messages: list[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
    system_parts = [_text_of(m.content) for m in messages if m.role == "system"]
    chat = [
        {"role": m.role, "content": _content_param(m.content)}
        for m in messages
        if m.role in ("user", "assistant")
    ]
    return "\n\n".join(system_parts), chat


def _tool_uses(message) -> tuple[ToolUseBlock, ...]:
    return tuple(
        ToolUseBlock(
            id=str(getattr(block, "id", "")),
            name=str(getattr(block, "name", "")),
            input=dict(getattr(block, "input", None) or {}),
        )
        for block in getattr(message, "content", None) or ()
        if getattr(block, "type", "") == "tool_use"
    )


_FINISHED_STOP_REASONS = ("end_turn", "stop_sequence", "tool_use")


def _usage(message) -> dict[str, int]:
    usage = getattr(message, "usage", None)
    if usage is None:
        return {}
    return {
        "input_tokens": int(getattr(usage, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(usage, "output_tokens", 0) or 0),
    }


def _request_kwargs(messages, options, disable_thinking):
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
    if options.tools:
        kwargs["tools"] = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in options.tools
        ]
    if disable_thinking:
        kwargs["thinking"] = {"type": "disabled"}
    return kwargs


def make_anthropic_clients(
    api_key: str, *, base_url: str | None = None, disable_thinking: bool = False
) -> tuple[StreamCall, CompleteCall]:
    """返回一对绑定密钥的客户端函数。max_retries=0：付费请求不自动重试。

    密钥只保存在闭包里，不出现在返回的事件、异常或日志中。
    """
    from anthropic import Anthropic, APIError

    def _client(options: CallOptions) -> Anthropic:
        return Anthropic(
            api_key=api_key, base_url=base_url, max_retries=0, timeout=options.timeout_seconds
        )

    def stream(
        messages: list[ChatMessage], options: CallOptions, cancel: CancelSignal
    ) -> Iterator[StreamEvent]:
        if cancel.is_set():
            yield StreamEvent(EventKind.ERROR, error="cancelled")
            return
        kwargs = _request_kwargs(messages, options, disable_thinking)
        try:
            with _client(options).messages.stream(**kwargs) as response:
                for event in response:
                    if cancel.is_set():
                        response.close()
                        yield StreamEvent(EventKind.ERROR, error="cancelled")
                        return
                    piece = _delta_of(event)
                    if piece is not None:
                        yield piece
                final = response.get_final_message()
        except APIError as exc:
            yield StreamEvent(EventKind.ERROR, error=describe_api_error(exc))
            return
        except Exception as exc:  # 超时、网络中断等
            yield StreamEvent(EventKind.ERROR, error=type(exc).__name__)
            return
        if cancel.is_set():
            yield StreamEvent(EventKind.ERROR, error="cancelled")
            return
        for tool_use in _tool_uses(final):
            yield StreamEvent(EventKind.TOOL_USE, tool_use=tool_use)
        yield StreamEvent(EventKind.USAGE, usage=_usage(final))
        if getattr(final, "stop_reason", None) == "max_tokens":
            yield StreamEvent(EventKind.TRUNCATED)
        elif getattr(final, "stop_reason", None) in _FINISHED_STOP_REASONS:
            yield StreamEvent(EventKind.COMPLETED)
        else:
            yield StreamEvent(EventKind.ERROR, error="unexpected_stop_reason")

    def complete(
        messages: list[ChatMessage], options: CallOptions, cancel: CancelSignal
    ) -> CompleteResult:
        if cancel.is_set():
            raise ProviderError("cancelled")
        kwargs = _request_kwargs(messages, options, disable_thinking)
        try:
            message = _client(options).messages.create(**kwargs)
        except APIError as exc:
            raise ProviderError(describe_api_error(exc)) from None
        except Exception as exc:
            raise ProviderError(type(exc).__name__) from None
        if cancel.is_set():
            raise ProviderError("cancelled")
        if getattr(message, "stop_reason", None) not in (*_FINISHED_STOP_REASONS, "max_tokens"):
            raise ProviderError("unexpected_stop_reason")
        text = "".join(
            getattr(block, "text", "")
            for block in message.content
            if getattr(block, "type", "") == "text"
        )
        return CompleteResult(
            text=text,
            usage=_usage(message),
            truncated=getattr(message, "stop_reason", None) == "max_tokens",
            tool_uses=_tool_uses(message),
        )

    return stream, complete


def make_async_anthropic_clients(
    api_key: str, *, base_url: str | None = None, disable_thinking: bool = False
) -> tuple[AsyncStreamCall, AsyncCompleteCall]:
    """返回一对绑定密钥的客户端函数。max_retries=0：付费请求不自动重试。

    密钥只保存在闭包里，不出现在返回的事件、异常或日志中。
    """
    from anthropic import APIError, AsyncAnthropic

    def _client(options: CallOptions) -> AsyncAnthropic:
        return AsyncAnthropic(
            api_key=api_key, base_url=base_url, max_retries=0, timeout=options.timeout_seconds
        )

    async def stream(
        messages: list[ChatMessage], options: CallOptions, cancel: CancelSignal
    ) -> AsyncIterator[StreamEvent]:
        if cancel.is_set():
            yield StreamEvent(EventKind.ERROR, error="cancelled")
            return
        kwargs = _request_kwargs(messages, options, disable_thinking)
        try:
            async with _client(options) as client, client.messages.stream(**kwargs) as response:
                async for event in response:
                    if cancel.is_set():
                        await response.close()
                        yield StreamEvent(EventKind.ERROR, error="cancelled")
                        return
                    piece = _delta_of(event)
                    if piece is not None:
                        yield piece
                final = await response.get_final_message()
        except APIError as exc:
            yield StreamEvent(EventKind.ERROR, error=describe_api_error(exc))
            return
        except Exception as exc:  # 超时、网络中断等
            yield StreamEvent(EventKind.ERROR, error=type(exc).__name__)
            return
        if cancel.is_set():
            yield StreamEvent(EventKind.ERROR, error="cancelled")
            return
        for tool_use in _tool_uses(final):
            yield StreamEvent(EventKind.TOOL_USE, tool_use=tool_use)
        yield StreamEvent(EventKind.USAGE, usage=_usage(final))
        if getattr(final, "stop_reason", None) == "max_tokens":
            yield StreamEvent(EventKind.TRUNCATED)
        elif getattr(final, "stop_reason", None) in _FINISHED_STOP_REASONS:
            yield StreamEvent(EventKind.COMPLETED)
        else:
            yield StreamEvent(EventKind.ERROR, error="unexpected_stop_reason")

    async def complete(
        messages: list[ChatMessage], options: CallOptions, cancel: CancelSignal
    ) -> CompleteResult:
        if cancel.is_set():
            raise ProviderError("cancelled")
        kwargs = _request_kwargs(messages, options, disable_thinking)
        try:
            async with _client(options) as client:
                message = await client.messages.create(**kwargs)
        except APIError as exc:
            raise ProviderError(describe_api_error(exc)) from None
        except Exception as exc:
            raise ProviderError(type(exc).__name__) from None
        if cancel.is_set():
            raise ProviderError("cancelled")
        if getattr(message, "stop_reason", None) not in (*_FINISHED_STOP_REASONS, "max_tokens"):
            raise ProviderError("unexpected_stop_reason")
        text = "".join(
            getattr(block, "text", "")
            for block in message.content
            if getattr(block, "type", "") == "text"
        )
        return CompleteResult(
            text=text,
            usage=_usage(message),
            truncated=getattr(message, "stop_reason", None) == "max_tokens",
            tool_uses=_tool_uses(message),
        )

    return stream, complete

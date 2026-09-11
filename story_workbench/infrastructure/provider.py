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

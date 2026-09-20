"""新包测试夹具：真实临时目录与 git，假端口预置审批答案，脚本客户端预置模型回复。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from story_agent.approval import Approval, ApprovalCard, parse_answer
from story_agent.config import AgentConfig
from story_agent.provider import (
    CallOptions,
    CancelSignal,
    ChatMessage,
    CompleteResult,
    EventKind,
    ProviderError,
    StreamEvent,
    ToolUseBlock,
)
from story_agent.workspace import Workspace

CONFIG = AgentConfig(
    options=CallOptions(model="fixed", max_output_tokens=2000, timeout_seconds=5),
    context_tokens=60_000,
    template_reserve=1_000,
    provider="fixed",
)


class FakePort:
    """预置审批答案（字符串按作者输入解析）；编辑器返回预置文本。"""

    def __init__(self, answers: list[str] | None = None, edits: list[str | None] | None = None):
        self.answers = list(answers or [])
        self.edits = list(edits or [])
        self.lines: list[str] = []
        self.chunks: list[str] = []
        self.cards: list[ApprovalCard] = []

    def say(self, text: str) -> None:
        self.lines.append(text)

    def emit(self, chunk: str) -> None:
        self.chunks.append(chunk)

    def approve(self, card: ApprovalCard) -> Approval:
        self.cards.append(card)
        answer = self.answers.pop(0) if self.answers else "n"
        return parse_answer(answer)

    def edit(self, initial: str, title: str) -> str | None:
        if self.edits:
            return self.edits.pop(0)
        return None

    @property
    def output(self) -> str:
        return "\n".join(self.lines)


@dataclass
class Reply:
    text: str = ""
    uses: tuple[ToolUseBlock, ...] = ()
    truncated: bool = False
    error: str | None = None
    hook: object = None  # 首个片段后调用 hook(cancel)


def use(name: str, uid: str = "t1", **args) -> ToolUseBlock:
    return ToolUseBlock(uid, name, args)


def scripted_clients(script: list[Reply], complete_text: str | Exception = "{}"):
    """按 stream 调用顺序消费脚本；complete 用于审读，返回固定文本或抛出异常。"""
    replies = list(script)
    calls: list[list[ChatMessage]] = []
    completes: list[list[ChatMessage]] = []

    def stream(messages, options, cancel) -> Iterator[StreamEvent]:
        calls.append(list(messages))
        reply = replies.pop(0) if replies else Reply()
        if reply.error:
            yield StreamEvent(EventKind.ERROR, error=reply.error)
            return
        half = len(reply.text) // 2
        pieces = [p for p in (reply.text[:half], reply.text[half:]) if p]
        for index, piece in enumerate(pieces):
            if cancel.is_set():
                yield StreamEvent(EventKind.ERROR, error="cancelled")
                return
            yield StreamEvent(EventKind.TEXT, text=piece)
            if index == 0 and reply.hook is not None:
                reply.hook(cancel)
        for block in reply.uses:
            yield StreamEvent(EventKind.TOOL_USE, tool_use=block)
        yield StreamEvent(EventKind.USAGE, usage={"input_tokens": 3, "output_tokens": 4})
        yield StreamEvent(EventKind.TRUNCATED if reply.truncated else EventKind.COMPLETED)

    def complete(messages, options, cancel) -> CompleteResult:
        completes.append(list(messages))
        if isinstance(complete_text, Exception):
            raise complete_text
        return CompleteResult(
            text=complete_text, usage={"input_tokens": 5, "output_tokens": 6}, truncated=False
        )

    return ScriptedClients(stream, complete, calls, completes)


class ScriptedClients:
    """与 Clients 同形，另带记录：calls 是每次 stream 收到的消息，completes 是每次审读的。"""

    def __init__(self, stream, complete, calls, completes) -> None:
        self.stream = stream
        self.complete = complete
        self.calls = calls
        self.completes = completes


def provider_failure(name: str = "APIConnectionError") -> Exception:
    return ProviderError(name)


@pytest.fixture
def workspace(tmp_path: Path) -> Workspace:
    return Workspace.create(tmp_path / "井下回声", "井下回声")


CHAPTER = "# 第一章\n\n沈砚把灯搁在井沿。井底有一具尸体，他怀疑是师父。\n\n他没有下去。\n"


@pytest.fixture
def chapter_workspace(workspace: Workspace) -> Workspace:
    workspace.write_text("章节/第01章.md", CHAPTER)
    workspace.commit("第一章初稿", "章节/第01章.md")
    return workspace


__all__ = ["CONFIG", "CHAPTER", "CancelSignal", "FakePort", "Reply", "scripted_clients", "use"]

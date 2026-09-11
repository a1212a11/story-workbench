"""G05 真实调用验证：只有设置 ANTHROPIC_API_KEY 且 STORY_WORKBENCH_LIVE=1 时执行，会产生费用。"""

from __future__ import annotations

import os
import time

import pytest

from story_workbench.bootstrap import build_clients, build_generation_config, load_settings
from story_workbench.infrastructure.provider import CancelSignal, ChatMessage, EventKind

LIVE = os.environ.get("STORY_WORKBENCH_LIVE") == "1" and bool(os.environ.get("ANTHROPIC_API_KEY"))
pytestmark = pytest.mark.skipif(not LIVE, reason="需要 STORY_WORKBENCH_LIVE=1 与 ANTHROPIC_API_KEY")


def test_live_stream_complete_and_cancel():
    settings = load_settings()
    config = build_generation_config(settings)
    clients = build_clients(settings)
    assert clients is not None
    options = config.options
    messages = [
        ChatMessage("system", "你是一位中文小说助手。只用中文回答。"),
        ChatMessage("user", "用两句话描写一口古井，不超过 60 字。"),
    ]
    # 流式
    started = time.monotonic()
    events = list(clients.stream(messages, options, CancelSignal()))
    elapsed = time.monotonic() - started
    kinds = [e.kind for e in events]
    assert kinds[-1] is EventKind.COMPLETED
    assert EventKind.USAGE in kinds
    text = "".join(e.text for e in events if e.kind is EventKind.TEXT)
    assert text.strip()
    usage = next(e.usage for e in events if e.kind is EventKind.USAGE)
    print(f"\n[live stream] {elapsed:.1f}s usage={usage} text={text!r}")
    # 非流式
    result = clients.complete(messages, options, CancelSignal())
    assert result.text.strip() and not result.truncated
    print(f"[live complete] usage={result.usage}")
    # 取消：第一块后置位
    cancel = CancelSignal()
    seen = []
    for e in clients.stream(messages, options, cancel):
        seen.append(e.kind)
        if e.kind is EventKind.TEXT:
            cancel.set()
    assert seen[-1] is EventKind.ERROR

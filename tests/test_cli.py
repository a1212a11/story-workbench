"""作者交互入口：编辑器能力验证（P03）与 CLI 流程（C01/C02）。

编辑器与 CLI 都通过 prompt_toolkit 的管道输入和空输出驱动，不需要真实终端。
输入法（IME）体验无法在此自动化，留待人工 T05 验证。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import PipeInput, create_pipe_input
from prompt_toolkit.output import DummyOutput

from story_workbench.cli_editor import StreamPane, build_editor, edit_text

ENTER = "\r"
CTRL_S = "\x13"
CTRL_Q = "\x11"
CTRL_C = "\x03"
LEFT = "\x1b[D"
HOME = "\x1b[H"
END = "\x1b[F"


def bracketed_paste(text: str) -> str:
    """终端粘贴时发送的括号粘贴序列；prompt_toolkit 会整体插入。"""
    return "\x1b[200~" + text + "\x1b[201~"


@pytest.fixture
def pipe() -> Iterator[PipeInput]:
    """把管道输入和空输出绑定为当前会话；测试内所有 prompt_toolkit 应用共用。"""
    with create_pipe_input() as pipe_input:
        with create_app_session(input=pipe_input, output=DummyOutput()):
            yield pipe_input


# ---------------------------------------------------------------- P03 编辑器能力


def test_editor_accepts_multiline_chinese_and_saves(pipe):
    pipe.send_text("沈砚把灯搁在井沿，顺着绳子往下。" + ENTER + "井壁下蜷着一个人。" + CTRL_S)
    assert edit_text("", "第一章") == "沈砚把灯搁在井沿，顺着绳子往下。\n井壁下蜷着一个人。"


def test_editor_accepts_pasted_multiline_chinese_at_once(pipe):
    pasted = "门板贴着耳侧，凉得像井壁。\r\n他直起身。\r那声音太短。"
    pipe.send_text(bracketed_paste(pasted) + CTRL_S)
    assert edit_text("", "第三章") == "门板贴着耳侧，凉得像井壁。\n他直起身。\n那声音太短。"


def test_editor_cursor_moves_then_inserts_in_the_middle(pipe):
    # 光标初始在正文开头；End 到行尾，左移两格，再插入。
    pipe.send_text(END + LEFT + LEFT + "慢慢" + CTRL_S)
    assert edit_text("沈砚下井", "第一章") == "沈砚慢慢下井"
    pipe.send_text(HOME + "夜里，" + CTRL_S)
    assert edit_text("沈砚下井", "第一章") == "夜里，沈砚下井"


def test_editor_save_returns_modified_text_keeping_untouched_lines(pipe):
    original = "第一段。\n第二段。"
    pipe.send_text(END + "他停了一会儿。" + CTRL_S)
    assert edit_text(original, "第二章") == "第一段。他停了一会儿。\n第二段。"


def test_editor_cancel_returns_none_and_leaves_original_untouched(pipe):
    original = "他把灯搁在脚边。"
    pipe.send_text(END + "侧耳贴近门板。" + CTRL_Q)
    assert edit_text(original, "第二章") is None
    assert original == "他把灯搁在脚边。"


def test_editor_hints_are_text_not_only_color():
    app = build_editor("", "第一章")
    texts = []
    for control in app.layout.find_all_controls():
        value = getattr(control, "text", None)
        if value is not None:
            texts.append(value() if callable(value) else value)
    joined = "".join(str(t) for t in texts)
    assert "Ctrl-S" in joined and "保存并返回" in joined
    assert "Ctrl-Q" in joined and "取消编辑" in joined
    assert "编辑：第一章" in joined


# ---------------------------------------------------------------- P03 流式输出与输入共存


def test_stream_output_keeps_editor_input_and_cancel_responsive(pipe):
    """异步流每 10ms 追加一段中文到显示区时，按键仍被处理，取消信号能停止流。"""

    async def scenario() -> tuple[str | None, list[str], asyncio.Event, StreamPane]:
        cancelled = asyncio.Event()
        pane = StreamPane("AI 输出", on_cancel=cancelled.set)
        app = build_editor("", "第一章", stream=pane)
        produced: list[str] = []

        async def producer() -> None:
            for i in range(500):
                if cancelled.is_set():
                    return
                chunk = f"第{i}段。"
                produced.append(chunk)
                pane.append(chunk)
                await asyncio.sleep(0.01)

        async def author() -> None:
            await asyncio.sleep(0.05)
            pipe.send_text("沈砚")
            await asyncio.sleep(0.05)
            pipe.send_text(CTRL_C)
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            await asyncio.sleep(0.05)
            pipe.send_text("下井" + CTRL_S)

        running = asyncio.create_task(app.run_async())
        stream = asyncio.create_task(producer())
        await author()
        result = await asyncio.wait_for(running, timeout=2)
        await asyncio.wait_for(stream, timeout=2)
        return result, produced, cancelled, pane

    result, produced, cancelled, pane = asyncio.run(scenario())
    assert result == "沈砚下井"
    assert cancelled.is_set()
    assert 3 <= len(produced) < 500
    assert pane.text == "".join(produced)
    assert pane.text.startswith("第0段。第1段。")

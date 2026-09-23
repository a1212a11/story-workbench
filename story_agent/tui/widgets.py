"""界面控件：输入框、全屏编辑器、对话里的工具块与思考块。

输入框只按属性和应用打交道（palette_open 等），不 import 应用本身。
"""

from __future__ import annotations

import time

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static, TextArea

from story_agent.tui.text import colorize_diff

COMMANDS: tuple[tuple[str, str], ...] = (
    ("/help", "按键与命令说明"),
    ("/dev", "开发者模式开关"),
    ("/exit", "退出"),
)


class PromptArea(TextArea):
    """多行输入：Enter 发送，Ctrl-J 换行；输入 / 时上下键、Tab、Enter 操作命令列表。"""

    BINDINGS = [
        Binding("enter", "submit", "发送", priority=True),
        Binding("ctrl+j", "newline", "换行", priority=True, show=False),
        Binding("shift+enter", "newline", "换行", priority=True, show=False),
        Binding("up", "palette_move(-1)", "上一个命令", priority=True, show=False),
        Binding("down", "palette_move(1)", "下一个命令", priority=True, show=False),
        Binding("tab", "palette_complete", "补全命令", priority=True, show=False),
    ]

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def action_submit(self) -> None:
        app = self.app
        if getattr(app, "palette_open", False):
            app.palette_execute()
            return
        text = self.text
        self.clear()
        self.post_message(self.Submitted(text))

    def action_newline(self) -> None:
        self.insert("\n")

    def action_palette_move(self, delta: int) -> None:
        app = self.app
        if getattr(app, "palette_open", False):
            app.palette_move(delta)
        elif delta < 0:
            self.action_cursor_up()
        else:
            self.action_cursor_down()

    def on_key(self, event) -> None:
        """选项面板开着且输入框是空的时候，按数字直接选那一项（和 Claude Code 一样）。"""
        app = self.app
        if getattr(app, "palette_mode", None) != "choice":
            return
        if self.text or not (event.character or "").isdigit():
            return
        index = int(event.character) - 1
        if 0 <= index < len(app.palette_items):
            event.prevent_default()
            event.stop()
            app.palette_index = index
            app.palette_execute()

    def action_palette_complete(self) -> None:
        app = self.app
        if getattr(app, "palette_open", False):
            app.palette_complete()


class EditorScreen(ModalScreen[str | None]):
    """全屏编辑器：Ctrl-S 保存返回文本，Ctrl-Q 取消返回 None。"""

    BINDINGS = [
        Binding("ctrl+s", "save", "保存并返回", priority=True),
        Binding("ctrl+q", "cancel", "取消编辑", priority=True),
    ]

    def __init__(self, initial: str, title: str) -> None:
        super().__init__()
        self.initial = initial
        self.title_text = title

    def compose(self) -> ComposeResult:
        yield Static(
            f"编辑：{self.title_text}    [Ctrl-S] 保存并返回    [Ctrl-Q] 取消编辑",
            id="editor-title",
            markup=False,
        )
        yield TextArea(self.initial, id="editor", soft_wrap=True)

    def on_mount(self) -> None:
        self.query_one("#editor", TextArea).focus()

    def action_save(self) -> None:
        self.dismiss(self.query_one("#editor", TextArea).text)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------- 对话记录中的条目


class ToolBlock(Vertical):
    """一次工具调用：标题、缩进的输出、结束行；可折叠。"""

    def __init__(
        self, label: str, name: str = "", preface: str | None = None, display: str = "normal"
    ) -> None:
        super().__init__(classes="tool")
        self.label = label
        self.tool_name = name
        # 工具表定的显示方式：quiet 是代理自己看的输出，默认折叠成一行
        self.tool_display = display
        self.preface = preface  # 开发者模式下的参数行，挂载完成后才能加
        self.lines = 0
        self.collapsed = False
        self.tool_state = "…"
        self.stream: Static | None = None
        self.stream_text = ""

    def compose(self) -> ComposeResult:
        yield Static(f"⏺ {self.label}", classes="tool-header running", markup=False)
        yield Vertical(classes="tool-body")
        yield Static("", classes="tool-footer", markup=False)

    def on_mount(self) -> None:
        if self.preface:
            self.add_line(self.preface)

    def body(self) -> Vertical:
        return self.query_one(".tool-body", Vertical)

    def add_line(self, text: str) -> None:
        self.flush_stream()
        self.body().mount(Static(colorize_diff(text), classes="tool-line"))
        self.lines += 1 + text.count("\n")

    def add_chunk(self, chunk: str) -> None:
        if self.stream is None:
            self.stream = Static("", classes="tool-line", markup=False)
            self.stream_text = ""
            self.body().mount(self.stream)
        self.stream_text += chunk
        self.stream.update(self.stream_text)

    def flush_stream(self) -> None:
        if self.stream is not None:
            if self.stream_text.strip():
                self.lines += 1 + self.stream_text.rstrip("\n").count("\n")
            else:
                self.stream.remove()
            self.stream = None
            self.stream_text = ""

    def finish(self, is_error: bool) -> None:
        self.flush_stream()
        header = self.query_one(".tool-header", Static)
        header.remove_class("running")
        header.add_class("error" if is_error else "ok")
        self.tool_state = "出错" if is_error else "完成"
        self._refresh_footer()

    def _refresh_footer(self) -> None:
        hint = " · Ctrl-O 或点击展开" if self.collapsed else ""
        self.query_one(".tool-footer", Static).update(
            f"  ⎿  {self.tool_state} · {self.lines} 行{hint}"
        )

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.body().display = not collapsed
        self._refresh_footer()

    def on_click(self) -> None:
        if self.tool_state != "…":
            self.set_collapsed(not self.collapsed)


class ThinkingBlock(Vertical):
    """模型回答前的思考：实时暗色显示，回答开始后收起成一行，点击或 Ctrl-O 展开。"""

    def __init__(self) -> None:
        super().__init__(classes="thinking")
        self.text = ""
        self.collapsed = False
        self.started = time.monotonic()
        self.done_seconds: int | None = None

    @property
    def seconds(self) -> int:
        return max(int(time.monotonic() - self.started), 1)

    def compose(self) -> ComposeResult:
        yield Static("✻ 思考中……", classes="thinking-header", markup=False)
        yield Static("", classes="thinking-body", markup=False)

    def on_mount(self) -> None:
        self._repaint()  # 挂载之前收到的片段在这里补上

    def _repaint(self) -> None:  # 不能叫 _render，那是界面库的内部方法
        try:
            header = self.query_one(".thinking-header", Static)
            body = self.query_one(".thinking-body", Static)
        except NoMatches:  # 还没 compose，等 on_mount
            return
        done = self.done_seconds
        header.display = done is not None  # 思考期间标题让位给下方的转圈，避免两处都写「思考中」
        if done is not None:
            header.update(f"✻ 思考了 {done} 秒")
        body.update(self.text)
        body.display = not self.collapsed

    def add_chunk(self, chunk: str) -> None:
        self.text += chunk
        self._repaint()

    def finish(self, collapsed: bool) -> None:
        self.done_seconds = self.seconds
        self.collapsed = collapsed
        self._repaint()

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self._repaint()

    def on_click(self) -> None:
        self.set_collapsed(not self.collapsed)

"""终端正文编辑器：普通多行文字编辑，保存或取消。

基于 prompt_toolkit 的全屏 TextArea。不显示 Markdown 标记，不做语法高亮；
正文区有边框和标题，操作提示常驻在底部，靠文字与布局区分，不只靠颜色。
可选的流式输出区用于验证生成进行中仍可输入与取消（P03）。
"""

from __future__ import annotations

from collections.abc import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.widgets import Frame, TextArea

SAVE_HINT = "[Ctrl-S] 保存并返回"
CANCEL_HINT = "[Ctrl-Q] 取消编辑"
STOP_STREAM_HINT = "[Ctrl-C] 停止输出"


class StreamPane:
    """生成进行中的流式输出显示区。

    append 只追加文字并请求重绘，可以由 asyncio 任务在编辑器运行时调用；
    作者按停止键时调用 on_cancel，由调用方决定如何停止流。
    """

    def __init__(self, title: str, on_cancel: Callable[[], None]) -> None:
        self.title = title
        self.on_cancel = on_cancel
        self.area = TextArea(text="", read_only=True, wrap_lines=True, scrollbar=True, height=6)
        self._app: Application[str | None] | None = None

    @property
    def text(self) -> str:
        return self.area.text

    def append(self, chunk: str) -> None:
        text = self.area.text + chunk
        self.area.buffer.set_document(Document(text, len(text)), bypass_readonly=True)
        if self._app is not None:
            self._app.invalidate()

    def attach(self, app: Application[str | None]) -> None:
        self._app = app


def build_editor(
    initial: str, title: str, *, stream: StreamPane | None = None
) -> Application[str | None]:
    """组装编辑器应用；返回值由 run()/run_async() 给出：新文本或 None。"""
    body = TextArea(
        text=initial,
        multiline=True,
        wrap_lines=True,
        scrollbar=True,
        focus_on_click=True,
    )
    body.buffer.cursor_position = 0

    bindings = KeyBindings()

    @bindings.add("c-s")
    def _save(event: KeyPressEvent) -> None:
        event.app.exit(result=body.text)

    @bindings.add("c-q")
    def _cancel(event: KeyPressEvent) -> None:
        event.app.exit(result=None)

    hints = [SAVE_HINT, CANCEL_HINT]
    children = [
        Window(FormattedTextControl(f"编辑：{title}"), height=1, style="bold"),
    ]
    if stream is not None:

        @bindings.add("c-c")
        def _stop_stream(event: KeyPressEvent) -> None:
            stream.on_cancel()

        hints.append(STOP_STREAM_HINT)
        children.append(Frame(stream.area, title=stream.title))
    children.append(Frame(body, title="正文"))
    children.append(Window(FormattedTextControl(" · ".join(hints)), height=1, style="reverse"))

    app: Application[str | None] = Application(
        layout=Layout(HSplit(children), focused_element=body),
        key_bindings=bindings,
        full_screen=True,
        mouse_support=False,
    )
    if stream is not None:
        stream.attach(app)
    return app


def edit_text(initial: str, title: str) -> str | None:
    """打开全屏编辑区；Ctrl-S 返回新文本，Ctrl-Q 返回 None 表示取消。"""
    return build_editor(initial, title).run()

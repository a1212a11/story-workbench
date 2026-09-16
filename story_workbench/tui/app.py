"""Textual 对话式工作台，外观与交互仿照 Claude Code：

对话像终端一样向下滚动；作者的话是灰色的「> 你的话」，助手回复带「⏺」并按 Markdown 渲染；
工具调用显示为「⏺ 写作(第二章 · 正式)」，其下缩进显示流程输出，结束后一行「⎿ 完成 · N 行」，
ctrl+o 折叠或展开；工作时输入框上方有旋转指示与已用时间，esc 中断；底部是圆角输入框，
下面一行左侧快捷键、右侧当前章、模型与本次用量；需要作者确认时显示带边框的问题卡。

界面在主线程跑事件循环；引擎在专用线程里拥有 Project 与 SQLite 连接，所有用例与数据库
操作只在该线程执行。作者输入经队列送给引擎，引擎经 call_from_thread 把输出投递到界面。
取消先落库，再通知客户端。
"""

from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import replace
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static, TextArea

from story_workbench.application import generation
from story_workbench.application.projects import Project, create_project, open_project
from story_workbench.console.engine import Engine
from story_workbench.console.workbench import Workbench, chapter_label
from story_workbench.domain.records import ErrorCode, RunStatus, WorkbenchError
from story_workbench.infrastructure import database
from story_workbench.infrastructure.provider import CancelSignal

HELP = (
    "直接用一句话说你要做什么，例如「写第二章」「这稿采用」「第一章读起来太赶，帮我看看」。\n"
    "固定操作：/chapters 章节列表 · /go 2 去第二章 · /new 新建章节 · /edit 编辑正文 ·"
    " /status 历史状态 · /branch 分支 · /help · /quit\n"
    "按键：Enter 发送 · Ctrl-J 换行 · Esc 中断模型任务 · Ctrl-O 折叠/展开工具输出 · Ctrl-D 退出"
)
SPINNER = "·✢✳✶✻✽"
IDLE_HINT = "? /help 查看操作 · Enter 发送 · Ctrl-J 换行"
BUSY_HINT = "esc 中断 · 已保存的内容会保留"


# ---------------------------------------------------------------- 输入与编辑器


class PromptArea(TextArea):
    """多行输入：Enter 发送，Ctrl-J 换行。"""

    BINDINGS = [
        Binding("enter", "submit", "发送", priority=True),
        Binding("ctrl+j", "newline", "换行", priority=True, show=False),
        Binding("shift+enter", "newline", "换行", priority=True, show=False),
    ]

    class Submitted(Message):
        def __init__(self, text: str) -> None:
            super().__init__()
            self.text = text

    def action_submit(self) -> None:
        text = self.text
        self.clear()
        self.post_message(self.Submitted(text))

    def action_newline(self) -> None:
        self.insert("\n")


class EditorScreen(ModalScreen[str | None]):
    """全屏正文编辑器：Ctrl-S 保存返回文本，Ctrl-Q 取消返回 None。"""

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
    """一次工具调用：标题、缩进的流程输出、结束行；可折叠。"""

    def __init__(self, label: str) -> None:
        super().__init__(classes="tool")
        self.label = label
        self.lines = 0
        self.stream: Static | None = None
        self.stream_text = ""

    def compose(self) -> ComposeResult:
        yield Static(f"⏺ {self.label}", classes="tool-header running", markup=False)
        yield Vertical(classes="tool-body")
        yield Static("", classes="tool-footer", markup=False)

    def body(self) -> Vertical:
        return self.query_one(".tool-body", Vertical)

    def add_line(self, text: str) -> None:
        self.flush_stream()
        self.body().mount(Static(text, classes="tool-line", markup=False))
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
        state = "出错" if is_error else "完成"
        self.query_one(".tool-footer", Static).update(f"  ⎿  {state} · {self.lines} 行")

    def set_collapsed(self, collapsed: bool) -> None:
        self.body().display = not collapsed


# ---------------------------------------------------------------- 引擎线程端口


class ThreadPort:
    """引擎线程使用的作者端口：输出投递到界面，提问与编辑阻塞等待作者作答。"""

    def __init__(self, app: WorkbenchApp) -> None:
        self.app = app
        self.answers: queue.Queue[str] = queue.Queue()
        self.edits: queue.Queue[str | None] = queue.Queue()
        self.cancel_requested = threading.Event()

    def _ui(self, callback, *args) -> None:
        if self.app.stopping.is_set():
            return
        try:
            self.app.call_from_thread(callback, *args)
        except RuntimeError:  # 界面已关闭
            pass

    # 作者端口
    def say(self, text: str) -> None:
        self._ui(self.app.show_line, text)

    def emit(self, chunk: str) -> None:
        self._ui(self.app.show_chunk, chunk)

    def ask(self, prompt: str) -> str:
        self._ui(self.app.begin_question, prompt)
        while True:
            try:
                return self.answers.get(timeout=0.2)
            except queue.Empty:
                if self.app.stopping.is_set():
                    return ""

    def edit(self, initial: str, title: str) -> str | None:
        self._ui(self.app.begin_edit, initial, title)
        while True:
            try:
                return self.edits.get(timeout=0.2)
            except queue.Empty:
                if self.app.stopping.is_set():
                    return None

    def run_model(self, project, clients, config, operation, *args, **kwargs):
        """在引擎线程同步执行；Esc 置位后，在下一个事件边界先落库取消，再通知客户端。"""
        cancel = CancelSignal()
        self.cancel_requested.clear()

        def guard() -> None:
            if self.cancel_requested.is_set() and not cancel.is_set():
                run_id = database.any_run_in_flight(project.conn, project.novel_id)
                if run_id is not None and generation.cancel_run(project, run_id, cancel):
                    self.say("取消已记录，正在结束模型请求……")

        def stream(messages, options, signal):
            events = clients.stream(messages, options, signal)
            try:
                for event in events:
                    guard()
                    yield event
            finally:
                close = getattr(events, "close", None)
                if close is not None:
                    close()

        def complete(messages, options, signal):
            guard()
            return clients.complete(messages, options, signal)

        adapted = replace(clients, stream=stream, complete=complete)
        self._ui(self.app.set_busy, True)
        try:
            return operation(
                project, *args, clients=adapted, config=config, cancel=cancel, **kwargs
            )
        finally:
            self._ui(self.app.set_busy, False)

    # 引擎观察者
    def reply_started(self) -> None:
        self._ui(self.app.begin_reply)

    def reply_finished(self, text: str) -> None:
        self._ui(self.app.end_reply, text)

    def tool_started(self, name: str, label: str) -> None:
        self._ui(self.app.begin_tool, label)

    def tool_finished(self, name: str, text: str, is_error: bool) -> None:
        self._ui(self.app.end_tool, is_error)


# ---------------------------------------------------------------- 应用


class WorkbenchApp(App[int]):
    CSS = """
    Screen { layout: vertical; background: $background; }
    #transcript {
        height: 1fr; padding: 0 2;
        scrollbar-size-vertical: 1; scrollbar-color: $surface; scrollbar-background: $background;
    }
    #transcript > * { margin-bottom: 1; }
    .welcome { border: round $accent; padding: 0 1; width: auto; }
    .author { color: $text-muted; }
    .system { color: $text-muted; }
    .reply { height: auto; }
    .reply > .dot { width: 2; color: $accent; }
    .reply > Static, .reply > Markdown { width: 1fr; }
    .reply > Markdown { margin: 0; padding: 0; }
    .tool { height: auto; margin-bottom: 1; }
    .tool-header { color: $accent; }
    .tool-header.ok { color: $success; }
    .tool-header.error { color: $error; }
    .tool-body { height: auto; padding-left: 2; }
    .tool-line { color: $text; }
    .tool-footer { color: $text-muted; }
    #spinner { height: 1; padding: 0 2; color: $accent; }
    #question { border: round $warning; padding: 0 1; margin: 0 2; height: auto; }
    #inputbox { border: round $accent; height: auto; max-height: 8; margin: 0 1; }
    #inputbox > .prompt { width: 2; padding-left: 1; color: $accent; }
    #input { height: auto; max-height: 6; border: none; padding: 0; background: transparent; }
    #input:focus { border: none; }
    #hint { height: 1; margin: 0 2; color: $text-muted; }
    #hint-left { width: 1fr; }
    #hint-right { width: auto; text-align: right; }
    #editor-title { height: 1; background: $accent; color: $text; padding: 0 1; }
    #editor { height: 1fr; }
    """

    BINDINGS = [
        Binding("escape", "cancel_model", "中断模型任务", priority=True, show=False),
        Binding("ctrl+c", "interrupt", "中断或退出", priority=True, show=False),
        Binding("ctrl+d", "quit_app", "退出", priority=True, show=False),
        # Textual 自带的 ctrl+q 退出优先于屏幕绑定；编辑器打开时 ctrl+q 应是取消编辑。
        Binding("ctrl+q", "editor_cancel_or_quit", "退出", priority=True, show=False),
        Binding("ctrl+o", "toggle_tools", "折叠/展开工具输出", priority=True, show=False),
    ]

    def __init__(
        self,
        path: Path,
        *,
        clients: generation.Clients | None = None,
        config: generation.GenerationConfig | None = None,
    ) -> None:
        super().__init__()
        self.path = Path(path)
        self.clients = clients
        self.config = config
        self.port = ThreadPort(self)
        self.inbox: queue.Queue[str | None] = queue.Queue()
        self.stopping = threading.Event()
        self.mode = "chat"
        self.busy = False
        self.busy_since = 0.0
        self.busy_verb = "思考中"
        self.tools_collapsed = False
        self._current_tool: ToolBlock | None = None
        self._reply_row: Horizontal | None = None
        self._reply_stream: Static | None = None
        self._reply_text = ""
        self._log: list[str] = []
        self.session_started = database.now_utc()
        self.status_text = ""

    # ------------------------------------------------------------ 布局

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript")
        yield Static("", id="spinner", markup=False)
        yield Static("", id="question", markup=False)
        with Horizontal(id="inputbox"):
            yield Static(">", classes="prompt", markup=False)
            yield PromptArea(id="input", soft_wrap=True)
        with Horizontal(id="hint"):
            yield Static(IDLE_HINT, id="hint-left", markup=False)
            yield Static("", id="hint-right", markup=False)

    def on_mount(self) -> None:
        self.query_one("#question", Static).display = False
        self.query_one("#spinner", Static).display = False
        self.query_one("#input", PromptArea).focus()
        self.set_interval(0.1, self._tick)
        self.run_worker(self.engine_loop, thread=True, exclusive=True, exit_on_error=False)

    # ------------------------------------------------------------ 引擎线程

    def engine_loop(self) -> None:
        port = self.port
        try:
            project = self._open_or_create()
        except WorkbenchError as exc:
            port.say(f"无法打开作品：{exc.message}")
            return
        if project is None:
            return
        workbench = Workbench(project, port, clients=self.clients, config=self.config)
        engine = Engine(workbench, observer=port)
        try:
            model = (
                f"{self.config.provider} / {self.config.options.model}"
                if self.config and self.clients
                else "未接入（没有密钥）"
            )
            port._ui(
                self.show_welcome,
                f"✻ 故事工作台 · 《{project.novel.title}》\n\n"
                f"  分支：{project.branch.name}\n"
                f"  模型：{model}\n"
                f"  目录：{self.path}\n\n"
                "  直接说你要做什么；/help 查看固定操作。",
            )
            workbench.show_chapters()
            if self.clients is None:
                port.say(
                    "未检测到模型密钥：可以记录想法、编辑正文；写作与检查需要设置密钥后重新打开。"
                )
            self._refresh_status(workbench)
            while not self.stopping.is_set():
                try:
                    item = self.inbox.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is None:
                    break
                try:
                    self._dispatch(engine, workbench, item)
                except WorkbenchError as exc:
                    port.say(f"没有完成：{exc.message}")
                except Exception as exc:  # 界面不因单次异常退出；已保存的内容都在
                    port.say(f"这一步出错：{type(exc).__name__}。已保存的内容都在。")
                self._refresh_status(workbench)
                port._ui(self.end_turn)
        finally:
            project.close()

    def _open_or_create(self) -> Project | None:
        try:
            project, notice = open_project(self.path)
        except WorkbenchError as exc:
            if exc.code is not ErrorCode.NOT_FOUND:
                raise
            self.port.say(f"这里还没有作品：{self.path}")
            while not self.stopping.is_set():
                title = self.port.ask("作品标题：").strip()
                if title:
                    return create_project(self.path, title)
                self.port.say("标题不能为空。")
            return None
        for note in notice.notes:
            self.port.say(note)
        return project

    def _dispatch(self, engine: Engine, wb: Workbench, text: str) -> None:
        stripped = text.strip()
        if not stripped:
            return
        if stripped.startswith("/"):
            self._slash(wb, stripped)
            return
        engine.handle(stripped)

    def _slash(self, wb: Workbench, command: str) -> None:
        name, _, argument = command[1:].partition(" ")
        argument = argument.strip()
        if name in ("help", "帮助"):
            wb.say(HELP)
        elif name in ("chapters", "章节"):
            wb.show_chapters()
        elif name in ("go", "去"):
            if not argument.isdigit():
                wb.say("用法：/go 章号")
                return
            chapter = wb.select_chapter(int(argument))
            if chapter is not None:
                wb.say(f"现在在{chapter_label(chapter)}。")
        elif name in ("new", "新建"):
            wb.new_chapter()
        elif name in ("edit", "编辑"):
            wb.edit_body()
        elif name in ("status", "历史"):
            wb.history_status()
        elif name in ("branch", "分支"):
            wb.branches()
        elif name in ("quit", "exit", "退出"):
            self.stopping.set()
            self.call_from_thread(self.exit, 0)
        else:
            wb.say(f"不认识「/{name}」。输入 /help 查看固定操作。")

    def _session_usage(self, wb: Workbench) -> tuple[int, int]:
        conn, novel_id = wb.project.conn, wb.project.novel_id
        total_in = total_out = 0
        for status in (RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED):
            for run_id in database.list_runs_with_status(conn, novel_id, status):
                run = database.get_run(conn, novel_id, run_id)
                if run.created_at < self.session_started or not run.usage_json:
                    continue
                usage = json.loads(run.usage_json)
                total_in += int(usage.get("input_tokens", 0))
                total_out += int(usage.get("output_tokens", 0))
        return total_in, total_out

    def _refresh_status(self, wb: Workbench) -> None:
        chapter = wb.refresh()
        where = chapter_label(chapter) if chapter else "未选章"
        model = wb.config.options.model if (wb.config and wb.clients) else "模型未接入"
        used_in, used_out = self._session_usage(wb)
        text = f"{where} · {model} · ↑{_short(used_in)} ↓{_short(used_out)} tokens"
        self.port._ui(self.set_status, text)

    # ------------------------------------------------------------ 界面更新（主线程）

    def _transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    def _mount(self, widget) -> None:
        transcript = self._transcript()
        transcript.mount(widget)
        transcript.scroll_end(animate=False)

    def _scroll(self) -> None:
        self._transcript().scroll_end(animate=False)

    def _flush_reply_stream(self) -> None:
        if self._reply_stream is not None and not self._reply_text.strip():
            self._reply_stream.remove()
        self._reply_stream = None

    def show_welcome(self, text: str) -> None:
        self._log.append(text)
        self._mount(Static(text, classes="welcome", markup=False))

    def _log_tool_stream(self) -> None:
        block = self._current_tool
        if block is not None and block.stream_text.strip():
            self._log.append(block.stream_text)

    def show_line(self, text: str) -> None:
        if self._current_tool is not None:
            self._log_tool_stream()
            self._log.append(text)
            self._current_tool.add_line(text)
        else:
            self._log.append(text)
            self._flush_reply_stream()
            self._mount(Static(text, classes="system", markup=False))
        self._scroll()

    def show_chunk(self, chunk: str) -> None:
        if self._current_tool is not None:
            self._current_tool.add_chunk(chunk)
        else:
            if self._reply_stream is None:
                self.begin_reply()
            self._reply_text += chunk
            assert self._reply_stream is not None
            self._reply_stream.update(self._reply_text)
        self._scroll()

    def show_author(self, text: str) -> None:
        self._log.append(f"> {text}")
        self._flush_reply_stream()
        self._mount(Static(f"> {text}", classes="author", markup=False))

    def begin_reply(self) -> None:
        self._flush_reply_stream()
        self._reply_text = ""
        self._reply_stream = Static("", markup=False)
        self._reply_row = Horizontal(
            Static("⏺", classes="dot", markup=False), self._reply_stream, classes="reply"
        )
        self.busy_verb = "思考中"
        self._mount(self._reply_row)

    def end_reply(self, text: str) -> None:
        row, stream = self._reply_row, self._reply_stream
        self._reply_row = self._reply_stream = None
        self._reply_text = ""
        if row is None:
            return
        if not text.strip():
            row.remove()
            return
        self._log.append(text)
        if stream is not None:
            stream.remove()
        row.mount(Markdown(text))
        self._scroll()

    def begin_tool(self, label: str) -> None:
        self._flush_reply_stream()
        block = ToolBlock(label)
        self._current_tool = block
        self.busy_verb = label.split("(", 1)[0] + "中"
        self._log.append(f"⏺ {label}")
        self._mount(block)

    def end_tool(self, is_error: bool) -> None:
        self._log_tool_stream()
        block = self._current_tool
        self._current_tool = None
        if block is not None:
            block.finish(is_error)
            block.set_collapsed(self.tools_collapsed)
            self._log.append(f"  ⎿  {'出错' if is_error else '完成'}")
        self._scroll()

    def end_turn(self) -> None:
        self._flush_reply_stream()
        self._current_tool = None

    def begin_question(self, prompt: str) -> None:
        self._log.append(prompt)
        question = self.query_one("#question", Static)
        question.update(f"需要你确认\n\n{prompt}\n\n在下方输入后按 Enter；直接 Enter 表示回车。")
        question.display = True
        self.mode = "answer"
        self.query_one("#input", PromptArea).focus()

    def begin_edit(self, initial: str, title: str) -> None:
        self.push_screen(EditorScreen(initial, title), callback=self.port.edits.put)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        if busy:
            self.busy_since = time.monotonic()
        spinner = self.query_one("#spinner", Static)
        spinner.display = busy
        self.query_one("#hint-left", Static).update(BUSY_HINT if busy else IDLE_HINT)

    def _tick(self) -> None:
        if not self.busy:
            return
        elapsed = int(time.monotonic() - self.busy_since)
        frame = SPINNER[(elapsed * 10 + int(time.monotonic() * 10)) % len(SPINNER)]
        self.query_one("#spinner", Static).update(
            f"{frame} {self.busy_verb}… ({elapsed}s · esc 中断)"
        )

    def set_status(self, text: str) -> None:
        self.status_text = text
        self.query_one("#hint-right", Static).update(text)

    def transcript_text(self) -> str:
        """已记录的对话文本，含正在流式显示的部分；供测试与调试。"""
        live = [self._reply_text]
        if self._current_tool is not None:
            live.append(self._current_tool.stream_text)
        return "\n".join([*self._log, *(part for part in live if part)])

    # ------------------------------------------------------------ 作者输入与按键

    def on_prompt_area_submitted(self, message: PromptArea.Submitted) -> None:
        text = message.text.strip()
        if self.mode == "answer":
            question = self.query_one("#question", Static)
            question.display = False
            self.mode = "chat"
            self._log.append(f"> {text or '（回车）'}")
            self._mount(Static(f"> {text or '（回车）'}", classes="author", markup=False))
            self.port.answers.put(text)
            return
        if not text:
            return
        self.show_author(text)
        self.inbox.put(text)

    def action_cancel_model(self) -> None:
        if self.busy:
            self.port.cancel_requested.set()
            self.query_one("#hint-left", Static).update("正在中断……")

    def action_interrupt(self) -> None:
        """ctrl+c：模型工作中则中断；输入框有字则清空；否则退出。"""
        if self.busy:
            self.action_cancel_model()
            return
        area = self.query_one("#input", PromptArea)
        if area.text:
            area.clear()
            return
        self.action_quit_app()

    def action_toggle_tools(self) -> None:
        self.tools_collapsed = not self.tools_collapsed
        for block in self._transcript().query(ToolBlock):
            if block is not self._current_tool:
                block.set_collapsed(self.tools_collapsed)
        self._scroll()

    def action_editor_cancel_or_quit(self) -> None:
        if isinstance(self.screen, EditorScreen):
            self.screen.action_cancel()
        else:
            self.action_quit_app()

    def action_quit_app(self) -> None:
        self.stopping.set()
        self.inbox.put(None)
        self.exit(0)

    def on_unmount(self) -> None:
        # 无论如何退出（含测试中途失败），都让引擎线程结束阻塞等待并关闭作品。
        self.stopping.set()
        self.inbox.put(None)


def _short(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)


def run(
    path: Path,
    *,
    clients: generation.Clients | None = None,
    config: generation.GenerationConfig | None = None,
) -> int:
    result = WorkbenchApp(path, clients=clients, config=config).run()
    return 0 if result is None else int(result)

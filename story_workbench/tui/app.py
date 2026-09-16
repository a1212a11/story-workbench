"""Textual 全屏工作台：对话记录、状态栏、多行输入、问题卡与正文编辑器。

界面在主线程跑事件循环；引擎在专用线程里拥有 Project 与 SQLite 连接，所有用例与数据库
操作只在该线程执行。作者输入经队列送给引擎，引擎经 call_from_thread 把 say/emit/ask/edit
投递到界面。取消标志是线程安全的事件；取消先落库，再通知客户端。
"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import replace
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Static, TextArea

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
    "按键：Enter 发送 · Ctrl-J 换行 · Esc 取消进行中的模型任务 · Ctrl-D 退出"
)


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


class WorkbenchApp(App[int]):
    CSS = """
    Screen { layout: vertical; }
    #status { height: 1; background: $primary; color: $text; padding: 0 1; }
    #transcript { height: 1fr; padding: 0 1; }
    #transcript > Static { margin-bottom: 1; }
    .author { color: $accent; text-style: bold; }
    .assistant { color: $text; }
    .stream { color: $text; }
    .system { color: $text-muted; }
    #question { height: auto; background: $warning 20%; padding: 0 1; }
    #input { height: 4; }
    #hint { height: 1; color: $text-muted; padding: 0 1; }
    #editor-title { height: 1; background: $primary; color: $text; padding: 0 1; }
    #editor { height: 1fr; }
    """

    BINDINGS = [
        Binding("escape", "cancel_model", "取消模型任务", priority=True, show=False),
        Binding("ctrl+d", "quit_app", "退出", priority=True, show=False),
        # Textual 自带的 ctrl+q 退出优先于屏幕绑定；编辑器打开时 ctrl+q 应是取消编辑。
        Binding("ctrl+q", "editor_cancel_or_quit", "退出", priority=True, show=False),
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
        self._stream_widget: Static | None = None
        self._stream_text = ""
        self.session_started = database.now_utc()
        self.status_text = "正在打开作品……"

    # ------------------------------------------------------------ 布局

    def compose(self) -> ComposeResult:
        yield Static(self.status_text, id="status", markup=False)
        yield VerticalScroll(id="transcript")
        yield Static("", id="question", markup=False)
        yield PromptArea(id="input", soft_wrap=True)
        yield Static("Enter 发送 · Ctrl-J 换行 · Esc 取消 · /help 帮助", id="hint", markup=False)

    def on_mount(self) -> None:
        self.query_one("#question", Static).display = False
        self.query_one("#input", PromptArea).focus()
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
        engine = Engine(workbench)
        try:
            port.say(f"已打开《{project.novel.title}》。")
            workbench.show_chapters()
            if self.clients is None:
                port.say(
                    "未检测到模型密钥：可以记录想法、编辑正文；写作与检查需要设置密钥后重新打开。"
                )
            port.say(HELP)
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
        model = f"{wb.config.provider} / {wb.config.options.model}" if wb.config else "模型未接入"
        used_in, used_out = self._session_usage(wb)
        text = (
            f"《{wb.project.novel.title}》 · {wb.project.branch.name} · {where} · {model}"
            f" · 本次用量 {used_in}/{used_out} token"
        )
        self.port._ui(self.set_status, text)

    # ------------------------------------------------------------ 界面更新（主线程）

    def _transcript(self) -> VerticalScroll:
        return self.query_one("#transcript", VerticalScroll)

    def _mount(self, widget: Static) -> None:
        transcript = self._transcript()
        transcript.mount(widget)
        transcript.scroll_end(animate=False)

    def _flush_stream(self) -> None:
        if self._stream_widget is not None:
            if not self._stream_text.strip():
                self._stream_widget.remove()
            self._stream_widget = None
            self._stream_text = ""

    def show_line(self, text: str) -> None:
        self._flush_stream()
        self._mount(Static(text, classes="system", markup=False))

    def show_chunk(self, chunk: str) -> None:
        if self._stream_widget is None:
            self._stream_text = ""
            self._stream_widget = Static("", classes="stream", markup=False)
            self._mount(self._stream_widget)
        self._stream_text += chunk
        self._stream_widget.update(self._stream_text)
        self._transcript().scroll_end(animate=False)

    def show_author(self, text: str) -> None:
        self._flush_stream()
        self._mount(Static(f"你：{text}", classes="author", markup=False))

    def begin_question(self, prompt: str) -> None:
        self._flush_stream()
        question = self.query_one("#question", Static)
        question.update(prompt)
        question.display = True
        self.mode = "answer"
        self.query_one("#input", PromptArea).focus()

    def begin_edit(self, initial: str, title: str) -> None:
        self._flush_stream()
        self.push_screen(EditorScreen(initial, title), callback=self.port.edits.put)

    def set_busy(self, busy: bool) -> None:
        hint = self.query_one("#hint", Static)
        hint.update(
            "模型任务进行中 · Esc 取消 · 已保存的内容会保留"
            if busy
            else "Enter 发送 · Ctrl-J 换行 · Esc 取消 · /help 帮助"
        )

    def set_status(self, text: str) -> None:
        self.status_text = text
        self.query_one("#status", Static).update(text)

    def transcript_text(self) -> str:
        return "\n".join(str(widget.content) for widget in self._transcript().query(Static))

    # ------------------------------------------------------------ 作者输入

    def on_prompt_area_submitted(self, message: PromptArea.Submitted) -> None:
        text = message.text.strip()
        if self.mode == "answer":
            question = self.query_one("#question", Static)
            question.display = False
            self.mode = "chat"
            self._mount(Static(f"你：{text or '（回车）'}", classes="author", markup=False))
            self.port.answers.put(text)
            return
        if not text:
            return
        self.show_author(text)
        self.inbox.put(text)

    def action_cancel_model(self) -> None:
        self.port.cancel_requested.set()
        self.query_one("#hint", Static).update("正在取消……")

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


def run(
    path: Path,
    *,
    clients: generation.Clients | None = None,
    config: generation.GenerationConfig | None = None,
) -> int:
    result = WorkbenchApp(path, clients=clients, config=config).run()
    return 0 if result is None else int(result)

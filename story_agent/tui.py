"""Textual 全屏对话界面，外观仿照 Claude Code：

对话向下滚动；作者的话是灰色的「> 你的话」，助手回复带「⏺」并按 Markdown 渲染；
工具调用显示为「⏺ 修改(章节/第02章.md · 改了守夜人的段落)」，其下缩进显示输出，结束后一行
「⎿ 完成 · N 行」，Ctrl-O 折叠；需要作者批准时输入框上方出现审批卡，显示着色的 diff，
Enter 批准、n 不批准、打字是不批准并带意见、Esc 也是不批准。没有斜杠命令。

界面在主线程跑事件循环；代理在专用线程里拥有作品目录，所有文件与 git 操作只在该线程执行。
作者输入经队列送给代理，代理经 call_from_thread 把输出投递到界面。
"""

from __future__ import annotations

import queue
import re
import threading
import time
from pathlib import Path

import textual._wrap as textual_wrap
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static, TextArea

from story_agent.agent import Agent
from story_agent.approval import Approval, ApprovalCard, parse_answer
from story_agent.config import AgentConfig, Clients
from story_agent.provider import CancelSignal
from story_agent.workspace import Workspace, WorkspaceError

# 界面库按空格切词换行：一整段没有空格的中文会被当成一个词整体挪行，出现「1.」或「⏺」
# 单独占一行。把每个中日韩字符（连同紧跟的收尾标点）当成一个可断的词，就能在字间换行。
_CJK = "\u2e80-\u9fff\uf900-\ufaff\uff00-\uffef"
_CLOSING = "，。、；：！？）」』》〉】…—"
CJK_CHUNK = re.compile(rf"[{_CJK}][{_CLOSING}]*\s*|\S+?(?=[{_CJK}])\s*|\S+\s*|\s+")


def install_cjk_wrapping() -> None:
    textual_wrap.re_chunk = CJK_CHUNK


install_cjk_wrapping()

SPINNER = "·✢✳✶✻✽"
# 这些工具的输出是代理自己看的，默认折叠成一行；修改、审读、编辑器的输出作者必须看，默认展开。
QUIET_TOOLS = frozenset({"read", "search", "verify", "history"})
HISTORY_ON_SCREEN = 12
IDLE_HINT = "Enter 发送 · Ctrl-J 换行 · Ctrl-O 展开/折叠工具输出 · Ctrl-D 退出"
BUSY_HINT = "Esc 中断 · 已保存的改动都在"
CARD_HINT = "Enter 批准 · n 不批准 · 或直接输入你的意见"


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

    def __init__(self, label: str, name: str = "") -> None:
        super().__init__(classes="tool")
        self.label = label
        self.name = name
        self.lines = 0
        self.collapsed = False
        self.state = "…"
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
        self.state = "出错" if is_error else "完成"
        self._refresh_footer()

    def _refresh_footer(self) -> None:
        hint = " · Ctrl-O 或点击展开" if self.collapsed else ""
        self.query_one(".tool-footer", Static).update(f"  ⎿  {self.state} · {self.lines} 行{hint}")

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.body().display = not collapsed
        self._refresh_footer()

    def on_click(self) -> None:
        if self.state != "…":
            self.set_collapsed(not self.collapsed)


def colorize_diff(text: str) -> Text:
    """把统一 diff 着色：删除行红、新增行绿、位置行暗；其他文字原样。"""
    result = Text()
    for index, line in enumerate(text.split("\n")):
        if index:
            result.append("\n")
        if line.startswith("+++") or line.startswith("---"):
            result.append(line, style="bold")
        elif line.startswith("+"):
            result.append(line, style="green")
        elif line.startswith("-"):
            result.append(line, style="red")
        elif line.startswith("@@"):
            result.append(line, style="dim")
        else:
            result.append(line)
    return result


# ---------------------------------------------------------------- 代理线程端口


class ThreadPort:
    """代理线程使用的作者端口：输出投递到界面，审批与编辑阻塞等待作者作答。"""

    def __init__(self, app: WorkbenchApp) -> None:
        self.app = app
        self.answers: queue.Queue[str] = queue.Queue()
        self.edits: queue.Queue[str | None] = queue.Queue()
        self.cancel = CancelSignal()

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

    def approve(self, card: ApprovalCard) -> Approval:
        while not self.answers.empty():  # 丢掉过期的作答
            self.answers.get_nowait()
        self._ui(self.app.begin_approval, card)
        while True:
            try:
                answer = self.answers.get(timeout=0.2)
            except queue.Empty:
                if self.app.stopping.is_set() or self.cancel.is_set():
                    self._ui(self.app.end_approval, None)
                    return Approval(False)
                continue
            return parse_answer(answer)

    def edit(self, initial: str, title: str) -> str | None:
        self._ui(self.app.begin_edit, initial, title)
        while True:
            try:
                return self.edits.get(timeout=0.2)
            except queue.Empty:
                if self.app.stopping.is_set():
                    return None

    # 代理观察者
    def reply_started(self) -> None:
        self._ui(self.app.begin_reply)

    def reply_finished(self, text: str) -> None:
        self._ui(self.app.end_reply, text)

    def tool_started(self, name: str, label: str) -> None:
        self._ui(self.app.begin_tool, label, name)

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
    .welcome { color: $text-muted; margin-bottom: 1; }
    .divider { color: $text-muted; margin: 1 0; }
    .author { color: $text-muted; margin-top: 1; }
    .system { color: $text-muted; }
    .history { opacity: 70%; }
    .reply { height: auto; margin-top: 1; }
    .reply > .dot { width: 2; color: $accent; }
    .reply > Static, .reply > Markdown { width: 1fr; }
    .reply > Markdown { margin: 0; padding: 0; background: transparent; }
    .tool { height: auto; margin-top: 1; }
    .tool-header { color: $accent; }
    .tool-header.ok { color: $success; }
    .tool-header.error { color: $error; }
    .tool-body { height: auto; padding-left: 2; }
    .tool-line { color: $text; }
    .tool-footer { color: $text-muted; }
    #spinner { height: 1; padding: 0 2; color: $accent; }
    #card { border: round $warning; padding: 0 1; margin: 0 2; height: auto; max-height: 24; }
    #card-title { color: $warning; text-style: bold; }
    #card-diff { height: auto; max-height: 18; scrollbar-size-vertical: 1; }
    #card-hint { color: $text-muted; }
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
        Binding("escape", "cancel_turn", "中断或不批准", priority=True, show=False),
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
        clients: Clients | None = None,
        config: AgentConfig | None = None,
    ) -> None:
        super().__init__()
        self.path = Path(path)
        self.clients = clients
        self.config = config
        self.port = ThreadPort(self)
        self.inbox: queue.Queue[str | None] = queue.Queue()
        self.stopping = threading.Event()
        self.mode = "chat"  # chat / approve
        self.busy = False
        self.busy_since = 0.0
        self.busy_verb = "思考中"
        self.verbose = False  # Ctrl-O：展开全部工具输出
        self._current_tool: ToolBlock | None = None
        self._reply_row: Horizontal | None = None
        self._reply_stream: Static | None = None
        self._reply_text = ""
        self._log: list[str] = []
        self.status_text = ""
        self.current_card: ApprovalCard | None = None

    # ------------------------------------------------------------ 布局

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="transcript", can_focus=False)
        yield Static("", id="spinner", markup=False)
        with Vertical(id="card"):
            yield Static("", id="card-title", markup=False)
            with VerticalScroll(id="card-diff", can_focus=False):
                yield Static("", id="card-body")
            yield Static(CARD_HINT, id="card-hint", markup=False)
        with Horizontal(id="inputbox"):
            yield Static(">", classes="prompt", markup=False)
            yield PromptArea(id="input", soft_wrap=True)
        with Horizontal(id="hint"):
            yield Static(IDLE_HINT, id="hint-left", markup=False)
            yield Static("", id="hint-right", markup=False)

    def on_mount(self) -> None:
        self.query_one("#card", Vertical).display = False
        self.query_one("#spinner", Static).display = False
        self.query_one("#input", PromptArea).focus()
        self.set_interval(0.1, self._tick)
        self.run_worker(self.agent_loop, thread=True, exclusive=True, exit_on_error=False)

    # ------------------------------------------------------------ 代理线程

    def agent_loop(self) -> None:
        port = self.port
        try:
            workspace = self._open_or_create()
        except WorkspaceError as exc:
            port.say(f"无法打开作品：{exc}")
            return
        agent = Agent(workspace, port, clients=self.clients, config=self.config, observer=port)
        model = (
            f"{self.config.provider} / {self.config.options.model}"
            if self.config and self.clients
            else "未接入（没有密钥）"
        )
        port._ui(
            self.show_welcome,
            f"✻ 《{workspace.title}》 · {model}\n"
            f"  {_short_path(workspace.root)} · 直接说你要做什么；改正式文件前我会先给你看改动。",
        )
        if self.clients is None:
            port.say("未检测到模型密钥：可以记录想法；写作与审读需要设置密钥后重新打开。")
        recent = workspace.recent_transcript(HISTORY_ON_SCREEN)
        if recent:
            port._ui(self.show_history, recent)
        self._refresh_status(agent)
        try:
            self._run_turn(agent, None)
            while not self.stopping.is_set():
                try:
                    item = self.inbox.get(timeout=0.2)
                except queue.Empty:
                    continue
                if item is None:
                    break
                self._run_turn(agent, item)
        finally:
            port._ui(self.set_busy, False)

    def _open_or_create(self) -> Workspace:
        workspace = Workspace(self.path)
        if workspace.exists:
            return Workspace.open(self.path)
        title = self.path.name.strip() or "未命名"
        created = Workspace.create(self.path, title)
        self.port.say(f"这里还没有作品，已按目录名新建《{title}》；书名可以在对话里改。")
        return created

    def _run_turn(self, agent: Agent, text: str | None) -> None:
        port = self.port
        port.cancel = CancelSignal()
        port._ui(self.set_busy, True)
        try:
            if text is None:
                agent.opening(port.cancel)
            else:
                agent.handle(text, port.cancel)
        except WorkspaceError as exc:
            port.say(f"没有完成：{exc}")
        except Exception as exc:  # 界面不因单次异常退出；已保存的改动都在
            port.say(f"这一步出错：{type(exc).__name__}。已保存的改动都在。")
        finally:
            port._ui(self.set_busy, False)
            self._refresh_status(agent)
            port._ui(self.end_turn)

    def _refresh_status(self, agent: Agent) -> None:
        model = agent.config.options.model if agent.model_ready and agent.config else "模型未接入"
        used = agent.session_usage
        text = (
            f"《{agent.workspace.title}》 · {model}"
            f" · ↑{_short(used['input_tokens'])} ↓{_short(used['output_tokens'])} tokens"
        )
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

    def show_history(self, rows: list[tuple[str, str]]) -> None:
        """重新打开时回放最近的对话，淡色显示；代理的上下文里也有这些。"""
        self._log.append("── 上次的对话 ──")
        self._mount(Static("── 上次的对话 ──", classes="divider", markup=False))
        for role, text in rows:
            if role == "author":
                self._log.append(f"> {text}")
                self._mount(Static(f"> {text}", classes="author history", markup=False))
            else:
                self._log.append(f"⏺ {text}")
                self._mount(
                    Horizontal(
                        Static("⏺", classes="dot", markup=False),
                        Markdown(text),
                        classes="reply history",
                    )
                )
        self._log.append("── 现在 ──")
        self._mount(Static("── 现在 ──", classes="divider", markup=False))

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

    def begin_tool(self, label: str, name: str = "") -> None:
        self._flush_reply_stream()
        block = ToolBlock(label, name)
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
            block.set_collapsed(self._default_collapsed(block))
            self._log.append(f"  ⎿  {'出错' if is_error else '完成'}")
        self._scroll()

    def end_turn(self) -> None:
        self._flush_reply_stream()
        self._current_tool = None

    def begin_approval(self, card: ApprovalCard) -> None:
        self.current_card = card
        self._log.append(f"审批：{card.title}（{card.summary}）\n{card.diff}")
        self.query_one("#card-title", Static).update(f"{card.title} · {card.summary}")
        self.query_one("#card-body", Static).update(colorize_diff(card.diff.rstrip("\n")))
        self.query_one("#card-diff", VerticalScroll).scroll_home(animate=False)
        self.query_one("#card", Vertical).display = True
        self.query_one("#hint-left", Static).update(CARD_HINT)
        self.mode = "approve"
        self.query_one("#input", PromptArea).focus()

    def end_approval(self, answer: str | None) -> None:
        self.query_one("#card", Vertical).display = False
        self.current_card = None
        self.mode = "chat"
        self.query_one("#hint-left", Static).update(BUSY_HINT if self.busy else IDLE_HINT)
        if answer is not None:
            shown = "⏎ 批准" if parse_answer(answer).approved else (answer or "n")
            self._log.append(f"> {shown}")
            self._mount(Static(f"> {shown}", classes="author", markup=False))

    def begin_edit(self, initial: str, title: str) -> None:
        self.push_screen(EditorScreen(initial, title), callback=self.port.edits.put)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        if busy:
            self.busy_since = time.monotonic()
        spinner = self.query_one("#spinner", Static)
        spinner.display = busy
        if self.mode != "approve":
            self.query_one("#hint-left", Static).update(BUSY_HINT if busy else IDLE_HINT)

    def _tick(self) -> None:
        if not self.busy or self.mode == "approve":
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

    def on_key(self, event) -> None:
        """保险：鼠标点到对话区之类的地方后，按键时把焦点还给输入框。"""
        if isinstance(self.screen, EditorScreen):
            return
        if not isinstance(self.focused, PromptArea):
            self.query_one("#input", PromptArea).focus()

    def on_prompt_area_submitted(self, message: PromptArea.Submitted) -> None:
        text = message.text.strip()
        if self.mode == "approve":
            self.end_approval(text)
            self.port.answers.put(text)
            return
        if not text:
            return
        self.show_author(text)
        self.inbox.put(text)

    def action_cancel_turn(self) -> None:
        if self.mode == "approve":
            self.end_approval("n")
            self.port.answers.put("n")
            return
        if self.busy:
            self.port.cancel.set()
            self.query_one("#hint-left", Static).update("正在中断……")

    def action_interrupt(self) -> None:
        """ctrl+c：工作中则中断；输入框有字则清空；否则退出。"""
        if self.busy or self.mode == "approve":
            self.action_cancel_turn()
            return
        area = self.query_one("#input", PromptArea)
        if area.text:
            area.clear()
            return
        self.action_quit_app()

    def _default_collapsed(self, block: ToolBlock) -> bool:
        return not self.verbose and block.name in QUIET_TOOLS

    def action_toggle_tools(self) -> None:
        self.verbose = not self.verbose
        for block in self._transcript().query(ToolBlock):
            if block is not self._current_tool:
                block.set_collapsed(self._default_collapsed(block))
        self._scroll()

    def action_editor_cancel_or_quit(self) -> None:
        if isinstance(self.screen, EditorScreen):
            self.screen.action_cancel()
        else:
            self.action_quit_app()

    def action_quit_app(self) -> None:
        self.stopping.set()
        self.port.cancel.set()
        self.inbox.put(None)
        self.exit(0)

    def on_unmount(self) -> None:
        # 无论如何退出（含测试中途失败），都让代理线程结束阻塞等待。
        self.stopping.set()
        self.port.cancel.set()
        self.inbox.put(None)


def _short_path(path: Path) -> str:
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def _short(count: int) -> str:
    return f"{count / 1000:.1f}k" if count >= 1000 else str(count)


def run(path: Path, *, clients: Clients | None = None, config: AgentConfig | None = None) -> int:
    result = WorkbenchApp(path, clients=clients, config=config).run()
    return 0 if result is None else int(result)

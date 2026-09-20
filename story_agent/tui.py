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
QUIT_CONFIRM_SECONDS = 2.0
REWIND_CONFIRM_SECONDS = 1.5  # 空闲时两次 Esc 的间隔上限
REWIND_CHOICES = 10
# 这些工具的输出是代理自己看的，默认折叠成一行；修改、审读、编辑器的输出作者必须看，默认展开。
QUIET_TOOLS = frozenset({"read", "search", "verify", "history"})
HISTORY_ON_SCREEN = 12
IDLE_HINT = "/help 查看按键与命令 · Enter 发送 · Ctrl-J 换行"
GAUGE_CELLS = 10
BUSY_HINT = "Esc 中断 · 已保存的改动都在"
HELP_TEXT = (
    "直接说你要做什么；改正式文件前会先给你看改动，Enter 批准、n 不批准、打字是把意见交给它。\n"
    "按键：Enter 发送 · Ctrl-J 换行 · Esc 中断或不批准 · PgUp/PgDn 翻页 · Ctrl-O 展开/折叠工具输出"
    " · Ctrl-C 连按两次或 Ctrl-D 退出\n"
    "命令：/help 这段说明 · /exit 退出 · /dev 开发者模式开关\n"
    "撤回：空闲时连按两次 Esc，选一句话，对话（可选连同文件）退回到它之前"
)
CARD_HINT = "Enter 批准 · n 不批准 · 或直接输入你的意见 · PgUp/PgDn 翻看改动"


# ---------------------------------------------------------------- 输入与编辑器


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
        if isinstance(app, WorkbenchApp) and app.palette_open:
            app.palette_execute()
            return
        text = self.text
        self.clear()
        self.post_message(self.Submitted(text))

    def action_newline(self) -> None:
        self.insert("\n")

    def action_palette_move(self, delta: int) -> None:
        app = self.app
        if isinstance(app, WorkbenchApp) and app.palette_open:
            app.palette_move(delta)
        elif delta < 0:
            self.action_cursor_up()
        else:
            self.action_cursor_down()

    def action_palette_complete(self) -> None:
        app = self.app
        if isinstance(app, WorkbenchApp) and app.palette_open:
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

    def __init__(self, label: str, name: str = "", preface: str | None = None) -> None:
        super().__init__(classes="tool")
        self.label = label
        self.tool_name = name
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

    def tool_started(self, name: str, label: str, args: dict | None = None) -> None:
        self._ui(self.app.begin_tool, label, name, args or {})

    def call_finished(self, usage: dict, seconds: float, ok: bool) -> None:
        self._ui(self.app.show_call_stats, usage, seconds, ok)

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
    #palette { margin: 0 2; height: auto; border: round $surface; padding: 0 1; }
    .palette-title { color: $warning; text-wrap: nowrap; text-overflow: ellipsis; }
    .palette-item { color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis; height: 1; }
    .palette-item.selected { color: $text; background: $accent 30%; }
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
        Binding("pageup", "page('up')", "上翻", priority=True, show=False),
        Binding("pagedown", "page('down')", "下翻", priority=True, show=False),
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
        self.inbox: queue.Queue[str | tuple | None] = queue.Queue()
        self.agent: Agent | None = None
        self.stopping = threading.Event()
        self.mode = "chat"  # chat / approve
        self.busy = False
        self.busy_since = 0.0
        self.busy_verb = "思考中"
        self.verbose = False  # Ctrl-O：展开全部工具输出
        self.dev = False  # /dev：工具参数、每次调用的用量与耗时
        self.palette_items: list[tuple[str, str]] = []
        self.palette_index = 0
        self.palette_mode = "commands"  # commands / rewind / rewind-scope
        self.palette_payload: list = []
        self._rewind_choice: dict | None = None
        self._esc_armed_at = 0.0
        self._quit_armed_at = 0.0  # 空闲时第一次 Ctrl-C 的时刻；两秒内再按一次才退出
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
        yield Vertical(id="palette")
        with Horizontal(id="inputbox"):
            yield Static(">", classes="prompt", markup=False)
            yield PromptArea(id="input", soft_wrap=True)
        with Horizontal(id="hint"):
            yield Static(IDLE_HINT, id="hint-left", markup=False)
            yield Static("", id="hint-right", markup=False)

    def on_mount(self) -> None:
        self.query_one("#card", Vertical).display = False
        self.query_one("#palette", Vertical).display = False
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
        self.agent = agent
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
                if isinstance(item, tuple):
                    self._do_rewind(agent, item)
                    continue
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

    def _do_rewind(self, agent: Agent, item: tuple) -> None:
        """在代理线程里执行撤回：截断对话记录，按需把文件退回，然后重载代理与界面。"""
        _, choice, with_files = item
        ws = self.workspace_of(agent)
        try:
            removed = ws.rewind_transcript(choice["index"])
            note = f"已撤回 {removed} 条记录，回到「{choice['text'][:30]}」之前。"
            if with_files:
                head = choice.get("head")
                ref = (
                    ws.git.restore_tree(head, f"撤回到「{choice['text'][:20]}」之前的文件状态")
                    if head
                    else None
                )
                if head is None:
                    note += " 这句话没有记录当时的文件状态，文件没有动。"
                elif ref is None:
                    note += " 文件和那时一样，没有变化。"
                else:
                    note += f" 文件已退回到那时，记为一次改动（{ref}）。"
            agent.reload()
            ws.append_log(note)
        except WorkspaceError as exc:
            note = f"没有撤回：{exc}"
        self.port._ui(self.reset_transcript_view, ws.recent_transcript(HISTORY_ON_SCREEN), note)
        self._refresh_status(agent)

    @staticmethod
    def workspace_of(agent: Agent) -> Workspace:
        return agent.workspace

    def reset_transcript_view(self, rows: list[tuple[str, str]], note: str) -> None:
        self._transcript().remove_children()
        self._log.append(note)
        self._current_tool = None
        self._reply_row = self._reply_stream = None
        if rows:
            self.show_history(rows)
        self._mount(Static(note, classes="system", markup=False))
        self._scroll()

    def _refresh_status(self, agent: Agent) -> None:
        model = agent.config.options.model if agent.model_ready and agent.config else "模型未接入"
        used = agent.session_usage
        text = (
            f"《{agent.workspace.title}》 · {model}"
            f" · ↑{_short(used['input_tokens'])} ↓{_short(used['output_tokens'])} tokens"
        )
        cost = agent.config.cost(used) if agent.config else None
        if cost is not None:
            text += f" · ≈${cost:.2f}"
        context_used, context_limit = agent.context_usage()
        self.port._ui(self.set_status, text, context_used, context_limit)

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
        row = self._reply_row
        self._reply_row = self._reply_stream = None
        self._reply_text = ""
        if row is None:
            return
        if not text.strip():
            row.remove()
            return
        self._log.append(text)
        for widget in row.query(Static):
            if not widget.has_class("dot"):
                widget.remove()
        row.mount(Markdown(text))
        self._scroll()

    def begin_tool(self, label: str, name: str = "", args: dict | None = None) -> None:
        self._flush_reply_stream()
        preface = None
        if self.dev and args:
            import json

            preface = "参数 " + json.dumps(args, ensure_ascii=False)[:800]
            self._log.append(preface)
        block = ToolBlock(label, name, preface)
        self._current_tool = block
        self.busy_verb = label.split("(", 1)[0] + "中"
        self._log.append(f"⏺ {label}")
        self._mount(block)

    def show_call_stats(self, usage: dict, seconds: float, ok: bool) -> None:
        if not self.dev:
            return
        line = (
            f"  ⎿  模型{'完成' if ok else '失败'} · ↑{_short(int(usage.get('input_tokens', 0)))}"
            f" ↓{_short(int(usage.get('output_tokens', 0)))} · {seconds:.1f}s"
        )
        self._log.append(line)
        self._flush_reply_stream()
        self._mount(Static(line, classes="system", markup=False))

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
        if (
            self._quit_armed_at
            and time.monotonic() - self._quit_armed_at >= QUIT_CONFIRM_SECONDS
            and not self.busy
            and self.mode != "approve"
        ):
            self._quit_armed_at = 0.0
            self.query_one("#hint-left", Static).update(IDLE_HINT)
        if not self.busy or self.mode == "approve":
            return
        elapsed = int(time.monotonic() - self.busy_since)
        frame = SPINNER[(elapsed * 10 + int(time.monotonic() * 10)) % len(SPINNER)]
        self.query_one("#spinner", Static).update(
            f"{frame} {self.busy_verb}… ({elapsed}s · esc 中断)"
        )

    def set_status(self, text: str, context_used: int = 0, context_limit: int = 0) -> None:
        gauge, ratio = context_gauge(context_used, context_limit)
        self.status_text = f"{text} · {gauge}" if gauge else text
        rendered = Text(text + " · ")
        if gauge:
            style = "green" if ratio < 0.7 else ("yellow" if ratio < 0.85 else "bold red")
            rendered.append(gauge, style=style)
        self.query_one("#hint-right", Static).update(rendered)

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
        if self._meta_command(text):
            return
        self.show_author(text)
        self.inbox.put(text)

    # ------------------------------------------------------------ 命令列表

    @property
    def palette_open(self) -> bool:
        return bool(self.palette_items) and self.query_one("#palette", Vertical).display

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id == "input":
            self._update_palette(event.text_area.text)

    def _update_palette(self, text: str) -> None:
        if self.palette_mode != "commands":
            return
        panel = self.query_one("#palette", Vertical)
        head = text.split("\n", 1)[0].strip().lower()
        items: list[tuple[str, str]] = []
        if text.startswith("/") and "\n" not in text and self.mode != "approve":
            items = [c for c in COMMANDS if c[0].startswith(head)]
        if items != self.palette_items:
            self.palette_items = items
            self.palette_index = 0
        panel.display = bool(items)
        if items:
            self._render_palette()

    def _render_palette(self) -> None:
        panel = self.query_one("#palette", Vertical)
        panel.remove_children()
        if self.palette_mode == "rewind":
            panel.mount(
                Static("撤回到哪一句之前？上下选，Enter 确认，Esc 取消", classes="palette-title")
            )
        elif self.palette_mode == "rewind-scope":
            panel.mount(Static("撤回什么？", classes="palette-title"))
        for index, (name, description) in enumerate(self.palette_items):
            classes = "palette-item selected" if index == self.palette_index else "palette-item"
            marker = "❯" if index == self.palette_index else " "
            line = f"{marker} {name:<7} {description}".replace("\n", " ")
            panel.mount(Static(line, classes=classes, markup=False))

    def palette_move(self, delta: int) -> None:
        if not self.palette_items:
            return
        self.palette_index = (self.palette_index + delta) % len(self.palette_items)
        self._render_palette()

    def palette_complete(self) -> None:
        if not self.palette_items or self.palette_mode != "commands":
            return
        area = self.query_one("#input", PromptArea)
        area.clear()
        area.insert(self.palette_items[self.palette_index][0])

    def palette_execute(self) -> None:
        if not self.palette_items:
            return
        if self.palette_mode == "rewind":
            self._rewind_choice = self.palette_payload[self.palette_index]
            self._open_palette(
                "rewind-scope",
                [("对话", "只撤回对话，文件不动"), ("全部", "对话和文件都退回到那时")],
                [False, True],
            )
            return
        if self.palette_mode == "rewind-scope":
            choice, with_files = self._rewind_choice, self.palette_payload[self.palette_index]
            self.palette_close()
            if choice is not None:
                self.inbox.put(("rewind", choice, with_files))
            return
        command = self.palette_items[self.palette_index][0]
        self.palette_close()
        self.query_one("#input", PromptArea).clear()
        self._meta_command(command)

    def _open_palette(self, mode: str, items: list[tuple[str, str]], payload: list) -> None:
        self.palette_mode = mode
        self.palette_items = items
        self.palette_payload = payload
        self.palette_index = 0
        self.query_one("#palette", Vertical).display = True
        self._render_palette()
        self.query_one("#input", PromptArea).focus()

    def open_rewind(self) -> None:
        """两次 Esc：列出最近说过的话，选一句撤回到它之前。"""
        if self.agent is None or self.busy or self.mode != "chat":
            return
        messages = self.agent.workspace.author_messages()[-REWIND_CHOICES:]
        if not messages:
            self.show_line("还没有可以撤回的话。")
            return
        messages.reverse()
        items = [(m["time"][11:16] or "--:--", m["text"].split("\n", 1)[0][:60]) for m in messages]
        self._open_palette("rewind", items, messages)

    def palette_close(self) -> None:
        self.palette_items = []
        self.palette_payload = []
        self.palette_mode = "commands"
        self._rewind_choice = None
        self.query_one("#palette", Vertical).display = False

    def _meta_command(self, text: str) -> bool:
        """只处理几个元命令；别的一律交给代理。"""
        command = text.lower()
        if command in ("/exit", "/quit", "/退出"):
            self.action_quit_app()
            return True
        if command in ("/help", "/?", "/帮助"):
            self.show_author(text)
            self.show_line(HELP_TEXT)
            return True
        if command == "/dev":
            self.show_author(text)
            self.dev = not self.dev
            self.verbose = self.dev
            for block in self._transcript().query(ToolBlock):
                if block is not self._current_tool:
                    block.set_collapsed(self._default_collapsed(block))
            self.show_line(
                "开发者模式已打开：工具输出全部展开，显示工具参数、每次模型调用的用量与耗时。"
                "日志一直在 .story/日志.txt。"
                if self.dev
                else "开发者模式已关闭。"
            )
            return True
        return False

    def action_cancel_turn(self) -> None:
        if self.palette_open:
            self.palette_close()
            return
        if self.mode == "approve":
            self.end_approval("n")
            self.port.answers.put("n")
            return
        if self.busy:
            self.port.cancel.set()
            self.query_one("#hint-left", Static).update("正在中断……")
            return
        now = time.monotonic()
        if now - self._esc_armed_at < REWIND_CONFIRM_SECONDS:
            self._esc_armed_at = 0.0
            self.open_rewind()
            return
        self._esc_armed_at = now

    def action_interrupt(self) -> None:
        """ctrl+c：工作中则中断；输入框有字则清空；否则退出。"""
        if self.busy or self.mode == "approve":
            self.action_cancel_turn()
            return
        area = self.query_one("#input", PromptArea)
        if area.text:
            area.clear()
            return
        now = time.monotonic()
        if now - self._quit_armed_at < QUIT_CONFIRM_SECONDS:
            self.action_quit_app()
            return
        self._quit_armed_at = now
        self.query_one("#hint-left", Static).update("再按一次 Ctrl-C 退出（Ctrl-D 直接退出）")

    def _default_collapsed(self, block: ToolBlock) -> bool:
        return not self.verbose and block.tool_name in QUIET_TOOLS

    def action_page(self, direction: str) -> None:
        """PageUp/PageDown 翻看对话；审批卡打开时翻卡片里的 diff。"""
        if isinstance(self.screen, EditorScreen):
            return
        target = self._transcript()
        if self.mode == "approve":
            card = self.query_one("#card-diff", VerticalScroll)
            if card.max_scroll_y > 0:  # 新建文件的卡片没有可翻的内容，翻的是上方的全文
                target = card
        if direction == "up":
            target.scroll_page_up(animate=False)
        else:
            target.scroll_page_down(animate=False)

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


def context_gauge(used: int, limit: int) -> tuple[str, float]:
    """上下文占用的十格小条与比例，例如「▮▮▮▮▯▯▯▯▯▯ 42%」。"""
    if limit <= 0:
        return "", 0.0
    ratio = min(used / limit, 1.0)
    filled = round(ratio * GAUGE_CELLS)
    bar = "▮" * filled + "▯" * (GAUGE_CELLS - filled)
    return f"{bar} {ratio * 100:.0f}%", ratio


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

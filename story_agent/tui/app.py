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
import threading
import time
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Static, TextArea

from story_agent.agent import Agent
from story_agent.approval import ApprovalCard, parse_answer
from story_agent.config import AgentConfig, Clients
from story_agent.provider import CancelSignal
from story_agent.tui.palette import CommandPalette
from story_agent.tui.port import ThreadPort
from story_agent.tui.text import (
    _short,
    _short_path,
    colorize_diff,
    context_gauge,
    copy_text,
    model_label,
)
from story_agent.tui.theme import PALETTE, STORY_THEME
from story_agent.tui.transcript import TranscriptView
from story_agent.tui.widgets import COMMANDS, EditorScreen, PromptArea
from story_agent.workspace import Workspace, WorkspaceError

SPINNER = "·✢✳✶✻✽"
QUIT_CONFIRM_SECONDS = 2.0
REWIND_CONFIRM_SECONDS = 1.5  # 空闲时两次 Esc 的间隔上限
REWIND_COOLDOWN_SECONDS = 3.0  # 刚中断过的这几秒里，Esc 只当中断，不进入撤回
REWIND_CHOICES = 10
HISTORY_ON_SCREEN = 12
IDLE_HINT = "/help 查看按键与命令 · Enter 发送 · Ctrl-J 换行"
BUSY_HINT = "Esc 中断 · 已保存的改动都在"
HELP_TEXT = (
    "直接说你要做什么；改正式文件前会先给你看改动，Enter 批准、n 不批准、打字是把意见交给它。\n"
    "按键：Enter 发送 · Ctrl-J 换行 · Esc 中断或不批准 · PgUp/PgDn 翻页 · Ctrl-O 展开/折叠工具输出"
    " · Ctrl-C 连按两次或 Ctrl-D 退出\n"
    "命令：/help 这段说明 · /exit 退出 · /dev 开发者模式开关\n"
    "它问你选哪个时：按数字或上下键选、Enter 确认，也可以直接打字说别的，Esc 跳过\n"
    "复制：鼠标划选要的文字，按 Ctrl-C 复制；也可以按住 Option 划选，用终端自己的复制\n"
    "撤回：空闲时连按两次 Esc，选一句话，对话（可选连同文件）退回到它之前"
)
CHOICE_HINT = "按数字或上下键选 · Enter 确认 · 或直接打字说别的 · Esc 跳过"
CARD_HINT = "Enter 批准 · n 不批准 · 或直接输入你的意见 · PgUp/PgDn 翻看改动"


class WorkbenchApp(App[int]):
    CSS = """
    Screen { layout: vertical; }
    #transcript {
        height: 1fr; padding: 0 0;
        scrollbar-size: 0 0;
    }
    .welcome { color: $text-muted; margin-bottom: 1; padding: 0 1; }
    .divider { color: $text-muted; margin: 1 0; padding: 0 1; }
    .author {
        color: ansi_default; background: #252533;
        padding: 0 1; margin: 1 0 0 0; width: 1fr;
    }
    .system { color: ansi_bright_black; padding: 0 1; }
    .history { opacity: 70%; }
    .reply { height: auto; margin-top: 1; padding: 0 1; }
    .reply > .dot { width: 2; color: $accent; }
    .reply > Static, .reply > Markdown { width: 1fr; }
    .reply > Markdown { margin: 0; padding: 0; background: ansi_default; }
    .reply > Markdown MarkdownBlock { background: ansi_default; margin: 0; padding: 0; }
    .reply > Markdown > MarkdownBlock { margin: 0 0 1 0; }
    .reply > Markdown > MarkdownBlock:last-child { margin: 0; }
    .reply > Markdown MarkdownHorizontalRule { display: none; }
    .reply > Markdown MarkdownHeader {
        color: ansi_default; text-style: bold; text-align: left; border: none;
    }
    .reply > Markdown > MarkdownHeader { margin: 0; }
    .reply > Markdown MarkdownListItem { margin: 0; margin-right: 1; }
    .reply > Markdown MarkdownListItem MarkdownBlock { margin: 0; }
    .reply > Markdown MarkdownBullet { color: $accent; }
    .thinking { height: auto; margin-top: 1; padding: 0 1; }
    .thinking-header { color: $text-muted; }
    .thinking-body { color: $text-muted; text-style: italic; padding-left: 2; }
    .tool { height: auto; margin-top: 1; padding: 0 1; }
    .tool-header { color: $accent; }
    .tool-header.ok { color: $success; }
    .tool-header.error { color: $error; }
    .tool-body { height: auto; padding-left: 2; }
    .tool-line { color: $text; }
    .tool-footer { color: $text-muted; }
    #spinner { height: 1; padding: 0 1; color: $accent; }
    #card { border: round $warning; padding: 0 1; margin: 0; height: auto; max-height: 24; }
    #card-title { color: $warning; text-style: bold; }
    #card-diff { height: auto; max-height: 18; scrollbar-size-vertical: 1; }
    #card-hint { color: $text-muted; }
    #palette { margin: 0; height: auto; border: round #45475A; padding: 0 1; }
    .palette-title { color: $warning; text-wrap: nowrap; text-overflow: ellipsis; }
    .palette-item { color: $text-muted; text-wrap: nowrap; text-overflow: ellipsis; height: 1; }
    .palette-item.selected { color: $accent; background: #313244; text-style: bold; }
    #inputbox { border: round $accent; height: auto; max-height: 8; margin: 0; }
    #inputbox > .prompt { width: 2; padding-left: 1; color: $accent; }
    #input { height: auto; max-height: 6; border: none; padding: 0; background: ansi_default; }
    #input:focus { border: none; }
    #hint { height: 1; margin: 0 1; color: $text-muted; }
    #hint-left { width: 1fr; }
    #hint-right { width: auto; text-align: right; }
    #editor-title { height: 1; background: $accent; color: ansi_black; padding: 0 1; }
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
        self.register_theme(STORY_THEME)
        self.theme = "story"
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
        self.transcript_view = TranscriptView()
        self._hint_until = 0.0  # 提示行临时文字的到期时刻
        self.palette = CommandPalette()
        self._rewind_choice: dict | None = None  # 撤回列表里选中的那句话，等作者选撤回范围
        self._esc_armed_at = 0.0
        self._interrupted_at = 0.0
        self._quit_armed_at = 0.0  # 空闲时第一次 Ctrl-C 的时刻；两秒内再按一次才退出
        self.status_text = ""
        self.current_card: ApprovalCard | None = None

    # ------------------------------------------------------------ 布局

    def compose(self) -> ComposeResult:
        yield self.transcript_view
        yield Static("", id="spinner", markup=False)
        with Vertical(id="card"):
            yield Static("", id="card-title", markup=False)
            with VerticalScroll(id="card-diff", can_focus=False):
                yield Static("", id="card-body")
            yield Static(CARD_HINT, id="card-hint", markup=False)
        yield self.palette
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
        view = self.transcript_view
        try:
            workspace = self._open_or_create()
        except WorkspaceError as exc:
            port.say(f"无法打开作品：{exc}")
            return
        agent = Agent(workspace, port, clients=self.clients, config=self.config, observer=port)
        self.agent = agent
        model = (
            f"{self.config.provider} / {model_label(self.config)}"
            if self.config and self.clients
            else "未接入（没有密钥）"
        )
        port._ui(
            view.show_welcome,
            f"✻ 《{workspace.title}》 · {model}\n"
            f"  {_short_path(workspace.root)} · 直接说你要做什么；改正式文件前我会先给你看改动。",
        )
        if self.clients is None:
            port.say("未检测到模型密钥：可以记录想法；写作与审读需要设置密钥后重新打开。")
        recent = workspace.recent_transcript(HISTORY_ON_SCREEN)
        if recent:
            port._ui(view.show_history, recent)
        if agent.needs_compacting:  # 压缩旧对话要调用模型，先把界面显示出来再做
            port._ui(view.begin_notice, "正在整理之前的对话……", "整理旧对话")
            port._ui(self.set_busy, True)
            try:
                done = agent.compact_pending()
            finally:
                port._ui(self.set_busy, False)
            port._ui(
                view.end_notice,
                "旧对话已整理成摘要。" if done else "这次没整理成，之前的对话照原样带着。",
            )
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
        started = time.monotonic()
        result = None
        try:
            if text is None:
                result = agent.opening(port.cancel)
            else:
                result = agent.handle(text, port.cancel)
        except WorkspaceError as exc:
            port.say(f"没有完成：{exc}")
        except Exception as exc:  # 界面不因单次异常退出；已保存的改动都在
            port.say(f"这一步出错：{type(exc).__name__}。已保存的改动都在。")
        finally:
            port._ui(self.set_busy, False)
            self._refresh_status(agent)
            if text and result is not None and result.failed:
                port._ui(self.restore_input, text)  # 没回应成，把你的话放回输入框
            seconds = time.monotonic() - started
            calls = result.calls if result is not None else 0
            cancelled = bool(result is not None and result.cancelled)
            port._ui(self.transcript_view.end_turn, seconds, calls, cancelled)

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
        self.port._ui(
            self.reset_transcript_view,
            ws.recent_transcript(HISTORY_ON_SCREEN),
            note,
            choice["text"],
        )
        self._refresh_status(agent)

    @staticmethod
    def workspace_of(agent: Agent) -> Workspace:
        return agent.workspace

    def reset_transcript_view(
        self, rows: list[tuple[str, str]], note: str, restore_text: str = ""
    ) -> None:
        self.transcript_view.reset(rows, note)
        if restore_text:  # 撤回的那句话放回输入框，作者改一改再发
            area = self.query_one("#input", PromptArea)
            area.clear()
            area.insert(restore_text)
            area.focus()

    def _refresh_status(self, agent: Agent) -> None:
        model = model_label(agent.config) if agent.model_ready and agent.config else "模型未接入"
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

    def on_mouse_scroll_up(self, event) -> None:
        self.transcript_view.follow = False  # 作者往上翻了；滚回底部或发一句话时恢复跟随

    def on_mouse_scroll_down(self, event) -> None:
        self.transcript_view.following()  # 滚回底部就恢复跟随

    def action_page(self, direction: str) -> None:
        """PageUp/PageDown 翻看对话；审批卡打开时翻卡片里的 diff。"""
        if isinstance(self.screen, EditorScreen):
            return
        view = self.transcript_view
        target: VerticalScroll = view
        if self.mode == "approve":
            card = self.query_one("#card-diff", VerticalScroll)
            if card.max_scroll_y > 0:  # 新建文件的卡片没有可翻的内容，翻的是上方的全文
                target = card
        if direction == "up":
            if target is view:
                view.follow = False
            target.scroll_page_up(animate=False)
        else:
            target.scroll_page_down(animate=False)
            if target is view and target.is_vertical_scroll_end:
                view.follow = True

    def restore_input(self, text: str) -> None:
        """一轮没成（比如网络断了）：把作者这句话放回输入框，按 Enter 就能重发。"""
        area = self.query_one("#input", PromptArea)
        if area.text.strip():
            return  # 作者已经在打新的了，别覆盖
        area.clear()
        area.insert(text)
        area.focus()
        self._flash_hint("这句话放回输入框了，按 Enter 重发")

    def begin_approval(self, card: ApprovalCard) -> None:
        self.current_card = card
        self.transcript_view.record(f"审批：{card.title}（{card.summary}）\n{card.diff}")
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
            self.transcript_view.echo_author(shown)

    def begin_edit(self, initial: str, title: str) -> None:
        self.push_screen(EditorScreen(initial, title), callback=self.port.edits.put)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        if busy:
            self.busy_since = time.monotonic()
        spinner = self.query_one("#spinner", Static)
        spinner.display = busy and self.mode == "chat"
        if self.mode != "approve":
            self.query_one("#hint-left", Static).update(BUSY_HINT if busy else IDLE_HINT)

    def _flash_hint(self, text: str, seconds: float = 2.0) -> None:
        """提示行临时显示一句话，到点自动恢复。"""
        self.query_one("#hint-left", Static).update(text)
        self._hint_until = time.monotonic() + seconds

    def _tick(self) -> None:
        if self._hint_until and time.monotonic() >= self._hint_until:
            self._hint_until = 0.0
            self.query_one("#hint-left", Static).update(BUSY_HINT if self.busy else IDLE_HINT)
        if self.mode != "chat":  # 等作者批准或选择时，别再转圈说自己在忙
            self.query_one("#spinner", Static).display = False
        if (
            self._quit_armed_at
            and time.monotonic() - self._quit_armed_at >= QUIT_CONFIRM_SECONDS
            and not self.busy
            and self.mode != "approve"
        ):
            self._quit_armed_at = 0.0
            self.query_one("#hint-left", Static).update(IDLE_HINT)
        if not self.busy or self.mode != "chat":
            return
        self.query_one("#spinner", Static).display = True
        elapsed = int(time.monotonic() - self.busy_since)
        frame = SPINNER[(elapsed * 10 + int(time.monotonic() * 10)) % len(SPINNER)]
        self.query_one("#spinner", Static).update(
            f"{frame} {self.transcript_view.busy_verb}… ({elapsed}s · esc 中断)"
        )

    def set_status(self, text: str, context_used: int = 0, context_limit: int = 0) -> None:
        gauge, ratio = context_gauge(context_used, context_limit)
        self.status_text = f"{text} · {gauge}" if gauge else text
        rendered = Text(text + " · ")
        if gauge:
            style = (
                PALETTE["green"]
                if ratio < 0.7
                else (PALETTE["yellow"] if ratio < 0.85 else f"bold {PALETTE['red']}")
            )
            rendered.append(gauge, style=style)
        self.query_one("#hint-right", Static).update(rendered)

    def transcript_text(self) -> str:
        """已记录的对话文本，含正在流式显示的部分；供测试与调试。"""
        return self.transcript_view.transcript_text()

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
        if self.mode == "choose":
            if not text and self.palette_open:  # 空 Enter = 选中高亮那一项
                self.palette_execute()
                return
            self.end_choice(text)
            self.port.answers.put(text)
            return
        if not text:
            return
        if self._meta_command(text):
            return
        self.transcript_view.show_author(text)
        self.inbox.put(text)

    # ------------------------------------------------------------ 命令列表

    @property
    def palette_open(self) -> bool:
        return self.palette.is_open

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id != "input":
            return
        text = event.text_area.text
        if self.palette.mode != "commands":
            if text.strip():  # 撤回列表开着时打字，说明作者要正常发言，收起列表
                self.palette_close()
            return
        self.palette.filter_commands(text, COMMANDS if self.mode != "approve" else ())

    def palette_execute(self) -> None:
        """面板上选定了一项：按面板的用途去做。"""
        palette = self.palette
        if not palette.items:
            return
        if palette.mode == "choice":
            answer = palette.selected_payload
            self.end_choice(answer)
            self.port.answers.put(answer)
            return
        if palette.mode == "rewind":
            self._rewind_choice = palette.selected_payload
            self._open_palette(
                "rewind-scope",
                [("对话", "只撤回对话，文件不动"), ("全部", "对话和文件都退回到那时")],
                [False, True],
                "撤回什么？",
            )
            return
        if palette.mode == "rewind-scope":
            choice, with_files = self._rewind_choice, palette.selected_payload
            self.palette_close()
            if choice is not None:
                self.inbox.put(("rewind", choice, with_files))
            return
        command = palette.selected_name
        self.palette_close()
        self.query_one("#input", PromptArea).clear()
        self._meta_command(command)

    def _open_palette(
        self, mode: str, items: list[tuple[str, str]], payload: list, title: str
    ) -> None:
        self.palette.show(mode, items, payload, title)
        self.query_one("#input", PromptArea).focus()

    def begin_choice(self, question: str, options: tuple[str, ...]) -> None:
        """代理问作者一个带选项的问题：上下选、Enter 确认，也可以直接打字说别的。"""
        self.mode = "choose"
        self.transcript_view.record(f"？{question}")
        self._open_palette(
            "choice",
            [(str(i), option) for i, option in enumerate(options, start=1)],
            list(options),
            question,
        )
        self.query_one("#hint-left", Static).update(CHOICE_HINT)

    def end_choice(self, shown: str | None) -> None:
        self.mode = "chat"
        self.palette_close()
        self.query_one("#hint-left", Static).update(BUSY_HINT if self.busy else IDLE_HINT)
        if shown is not None:
            self.transcript_view.echo_author(shown or "（跳过，我自己说）")

    def open_rewind(self) -> None:
        """两次 Esc：列出最近说过的话，选一句撤回到它之前。"""
        if self.agent is None or self.busy or self.mode != "chat":
            return
        messages = self.agent.workspace.author_messages()[-REWIND_CHOICES:]
        if not messages:
            self.transcript_view.show_line("还没有可以撤回的话。")
            return
        messages.reverse()
        items = [(m["time"][11:16] or "--:--", m["text"].split("\n", 1)[0][:60]) for m in messages]
        self._open_palette("rewind", items, messages, "撤回到哪一句之前")

    def palette_close(self) -> None:
        self._rewind_choice = None
        self.palette.close()

    def _meta_command(self, text: str) -> bool:
        """只处理几个元命令；别的一律交给代理。"""
        command = text.lower()
        if command in ("/exit", "/quit", "/退出"):
            self.action_quit_app()
            return True
        view = self.transcript_view
        if command in ("/help", "/?", "/帮助"):
            view.show_author(text)
            view.show_line(HELP_TEXT)
            return True
        if command == "/dev":
            view.show_author(text)
            view.dev = not view.dev
            view.set_verbose(view.dev, thinking=False)
            view.show_line(
                "开发者模式已打开：工具输出全部展开，显示工具参数、每次模型调用的用量与耗时。"
                "日志一直在 .story/日志.txt。"
                if view.dev
                else "开发者模式已关闭。"
            )
            return True
        return False

    def action_cancel_turn(self) -> None:
        if self.mode == "choose":
            self.end_choice("")
            self.port.answers.put("")
            return
        if self.palette_open:
            self.palette_close()
            return
        if self.mode == "approve":
            self.end_approval("n")
            self.port.answers.put("n")
            return
        now = time.monotonic()
        if self.busy:
            self.port.cancel.set()
            self._esc_armed_at = 0.0
            self._interrupted_at = now  # 连着按 Esc 想停下来，不该变成撤回
            self.query_one("#hint-left", Static).update("正在中断……")
            return
        if now - self._interrupted_at < REWIND_COOLDOWN_SECONDS:
            return
        if now - self._esc_armed_at < REWIND_CONFIRM_SECONDS:
            self._esc_armed_at = 0.0
            self.open_rewind()
            return
        self._esc_armed_at = now
        self._flash_hint("再按一次 Esc 撤回到之前说过的某句话", REWIND_CONFIRM_SECONDS)

    def _copy_selection(self) -> bool:
        """有划选的文字就复制到剪贴板；返回是否复制了。"""
        screen = self.screen
        if isinstance(screen, EditorScreen):
            text = screen.query_one("#editor", TextArea).selected_text or None
        else:
            text = screen.get_selected_text()
        if not text:
            return False
        copy_text(self, text)
        if not isinstance(screen, EditorScreen):
            screen.clear_selection()
            self._flash_hint(f"已复制 {len(text)} 个字")
        return True

    def action_interrupt(self) -> None:
        """ctrl+c：划选了文字就复制；编辑器里不退出；工作中则中断；输入框有字则清空；否则退出。"""
        if self._copy_selection():
            return
        if isinstance(self.screen, EditorScreen):
            return
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

    def action_toggle_tools(self) -> None:
        view = self.transcript_view
        view.set_verbose(not view.verbose)

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


def run(path: Path, *, clients: Clients | None = None, config: AgentConfig | None = None) -> int:
    result = WorkbenchApp(path, clients=clients, config=config).run()
    return 0 if result is None else int(result)

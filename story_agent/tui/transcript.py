"""对话区：作者的话、助手的回复、思考块、工具块、系统行，以及「跟着新内容滚到底」。

界面上每多一种显示，改的都是这里；应用本体只管线程、输入、审批卡与面板。
代理线程经端口把输出投递到这里的方法（在主线程执行）。
"""

from __future__ import annotations

import json
import time

from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Markdown, Static

from story_agent.tui.text import _duration, _short, author_line, clean_markdown
from story_agent.tui.widgets import ThinkingBlock, ToolBlock


class TranscriptView(VerticalScroll):
    def __init__(self) -> None:
        super().__init__(id="transcript", can_focus=False)
        self.follow = True  # 是否跟着新内容滚到底
        self.verbose = False  # Ctrl-O：展开全部工具输出
        self.dev = False  # /dev：工具参数、每次调用的用量与耗时
        self.busy_verb = "思考中"  # 转圈那一行说自己在干什么；应用每一跳来读
        self._current_tool: ToolBlock | None = None
        self._reply_row: Horizontal | None = None
        self._reply_stream: Static | None = None
        self._reply_text = ""
        self._notice: Static | None = None  # 正在做某事的那一行，做完就地改成结果
        self._thinking: ThinkingBlock | None = None
        self._log: list[str] = []

    # ------------------------------------------------------------ 滚动

    def following(self) -> bool:
        """作者停在底部就跟着新内容滚；他往上翻了就不打扰，翻回底部自动恢复。"""
        if self.is_vertical_scroll_end:
            self.follow = True
        return self.follow

    def add(self, widget) -> None:
        following = self.following()
        self.mount(widget)
        if following:
            self.scroll_end(animate=False)

    def scroll_if_following(self) -> None:
        if self.following():
            self.scroll_end(animate=False)

    # ------------------------------------------------------------ 记录

    def record(self, text: str) -> None:
        """只记进文字记录，不显示（审批卡、选项面板这类另有界面的内容）。"""
        self._log.append(text)

    def transcript_text(self) -> str:
        """已记录的对话文本，含正在流式显示的部分；供测试与调试。"""
        live = [self._reply_text]
        if self._current_tool is not None:
            live.append(self._current_tool.stream_text)
        return "\n".join([*self._log, *(part for part in live if part)])

    def reset(self, rows: list[tuple[str, str]], note: str) -> None:
        """撤回之后：清空，回放剩下的最近对话，再说一句撤回了什么。"""
        self.remove_children()
        self._log.append(note)
        self._current_tool = None
        self._reply_row = self._reply_stream = None
        if rows:
            self.show_history(rows)
        self.add(Static(note, classes="system", markup=False))
        self.scroll_if_following()

    # ------------------------------------------------------------ 系统行与作者的话

    def show_history(self, rows: list[tuple[str, str]]) -> None:
        """重新打开时回放最近的对话，淡色显示；代理的上下文里也有这些。"""
        self._log.append("── 上次的对话 ──")
        self.add(Static("── 上次的对话 ──", classes="divider", markup=False))
        for role, text in rows:
            if role == "author":
                self._log.append(f"> {text}")
                self.add(Static(author_line(text), classes="author history"))
            else:
                self._log.append(f"⏺ {text}")
                self.add(
                    Horizontal(
                        Static("⏺", classes="dot", markup=False),
                        Markdown(clean_markdown(text)),
                        classes="reply history",
                    )
                )

    def show_welcome(self, text: str) -> None:
        self._log.append(text)
        self.add(Static(text, classes="welcome", markup=False))

    def show_line(self, text: str) -> None:
        if self._current_tool is not None:
            self._log_tool_stream()
            self._log.append(text)
            self._current_tool.add_line(text)
        else:
            self._log.append(text)
            self._flush_reply_stream()
            self.add(Static(text, classes="system", markup=False))
        self.scroll_if_following()

    def begin_notice(self, text: str, verb: str = "") -> None:
        """显示一行「正在……」，完成时用 end_notice 就地换成结果。"""
        if verb:
            self.busy_verb = verb
        self._notice = Static(text, classes="system", markup=False)
        self.add(self._notice)

    def end_notice(self, text: str | None) -> None:
        notice, self._notice = self._notice, None
        if notice is None:
            return
        if text:
            self._log.append(text)
            notice.update(text)
        else:
            notice.remove()
        self.scroll_if_following()

    def show_author(self, text: str) -> None:
        self._log.append(f"> {text}")
        self._flush_reply_stream()
        self.follow = True  # 作者说了话，回到底部
        self.add(Static(author_line(text), classes="author"))

    def echo_author(self, text: str) -> None:
        """作者在审批卡或选项面板里的作答，回显成一句作者的话。"""
        self._log.append(f"> {text}")
        self.add(Static(author_line(text), classes="author"))
        self.scroll_if_following()

    def show_dev_note(self, text: str) -> None:
        """只在开发者模式显示：每轮附在作者发言后面、不落库的提醒。"""
        if not self.dev:
            return
        line = f"（每轮提醒）{text}"
        self._log.append(line)
        self.add(Static(line, classes="system", markup=False))
        self.scroll_if_following()

    # ------------------------------------------------------------ 助手的回复与思考

    def _flush_reply_stream(self) -> None:
        if self._reply_stream is not None and not self._reply_text.strip():
            self._reply_stream.remove()
        self._reply_stream = None

    def show_chunk(self, chunk: str) -> None:
        if self._current_tool is not None:
            self._current_tool.add_chunk(chunk)
        else:
            if self._reply_stream is None:
                self.begin_reply()
            self._reply_text += chunk
            assert self._reply_stream is not None
            self._reply_stream.update(self._reply_text)
        self.scroll_if_following()

    def prepare_reply(self) -> None:
        """一次模型调用开始。先不挂回复行，等真有文字再挂，好让思考过程排在它前面。"""
        self._flush_reply_stream()
        self._reply_row = None
        self._reply_text = ""
        self.busy_verb = "思考中"

    def show_thinking(self, chunk: str) -> None:
        if self._current_tool is not None:
            return
        if self._thinking is None:
            self._flush_reply_stream()
            self._thinking = ThinkingBlock()
            self.busy_verb = "思考中"
            self.add(self._thinking)
        self._thinking.add_chunk(chunk)
        self.scroll_if_following()

    def keep_thinking_open(self) -> None:
        """这一轮没有正文时，把最后那块思考展开，作者才知道自己看到的是什么。"""
        blocks = list(self.query(ThinkingBlock))
        if blocks:
            blocks[-1].set_collapsed(False)
            self.scroll_if_following()

    def _finish_thinking(self) -> None:
        block, self._thinking = self._thinking, None
        if block is not None:
            self._log.append(f"✻ 思考了 {block.seconds} 秒")
            block.finish(collapsed=not self.verbose)

    def begin_reply(self) -> None:
        self._finish_thinking()
        self._flush_reply_stream()
        self._reply_text = ""
        self._reply_stream = Static("", markup=False)
        self._reply_row = Horizontal(
            Static("⏺", classes="dot", markup=False), self._reply_stream, classes="reply"
        )
        self.busy_verb = "思考中"
        self.add(self._reply_row)

    def end_reply(self, text: str) -> None:
        self._finish_thinking()
        row = self._reply_row
        self._reply_row = self._reply_stream = None
        self._reply_text = ""
        if not text.strip():
            if row is not None:
                row.remove()
            return
        if row is None:  # 没有流式片段就直接给了整段（脚本客户端、非流式回退）
            row = Horizontal(Static("⏺", classes="dot", markup=False), classes="reply")
            self.add(row)
        self._log.append(text)
        for widget in row.query(Static):
            if not widget.has_class("dot"):
                widget.remove()
        row.mount(Markdown(clean_markdown(text)))
        self.scroll_if_following()

    # ------------------------------------------------------------ 工具

    def _log_tool_stream(self) -> None:
        block = self._current_tool
        if block is not None and block.stream_text.strip():
            self._log.append(block.stream_text)

    def _default_collapsed(self, block: ToolBlock) -> bool:
        return not self.verbose and block.tool_display == "quiet"

    def begin_tool(
        self, label: str, name: str = "", args: dict | None = None, display: str = "normal"
    ) -> None:
        self._finish_thinking()
        self._flush_reply_stream()
        # silent：界面另有其人（比如选项面板），对话里不用再占一行
        if display == "silent" and not self.dev:
            return
        preface = None
        if self.dev and args:
            preface = "参数 " + json.dumps(args, ensure_ascii=False)[:800]
            self._log.append(preface)
        block = ToolBlock(label, name, preface, display)
        self._current_tool = block
        self.busy_verb = label.split("(", 1)[0] + "中"
        self._log.append(f"⏺ {label}")
        self.add(block)

    def show_call_stats(self, usage: dict, seconds: float, ok: bool) -> None:
        if not self.dev:
            return
        line = (
            f"  ⎿  模型{'完成' if ok else '失败'} · ↑{_short(int(usage.get('input_tokens', 0)))}"
            f" ↓{_short(int(usage.get('output_tokens', 0)))} · {seconds:.1f}s"
        )
        self._log.append(line)
        self._flush_reply_stream()
        self.add(Static(line, classes="system", markup=False))

    def end_tool(self, is_error: bool) -> None:
        self._log_tool_stream()
        block = self._current_tool
        self._current_tool = None
        if block is not None:
            block.finish(is_error)
            block.set_collapsed(self._default_collapsed(block))
            self._log.append(f"  ⎿  {'出错' if is_error else '完成'}")
        self.scroll_if_following()

    def set_verbose(self, verbose: bool, *, thinking: bool = True) -> None:
        """展开或折叠已完成的工具输出（Ctrl-O 连思考块一起，/dev 只管工具）。"""
        self.verbose = verbose
        for block in self.query(ToolBlock):
            if block is not self._current_tool:
                block.set_collapsed(self._default_collapsed(block))
        if not thinking:
            return
        for block in self.query(ThinkingBlock):
            if block is not self._thinking:
                block.set_collapsed(not verbose)
        self.scroll_if_following()

    # ------------------------------------------------------------ 一轮结束

    def end_turn(self, seconds: float = 0.0, calls: int = 0, cancelled: bool = False) -> None:
        self._finish_thinking()
        self._flush_reply_stream()
        self._current_tool = None
        if calls:
            # 像 Claude Code 的「✻ Brewed for 43s · done 3:00 PM」：用了多久，几点完成
            finished = time.strftime("%H:%M")
            state = "已中断" if cancelled else "完成"
            line = f"✻ 用时 {_duration(seconds)} · {state}于 {finished}"
            self._log.append(line)
            self.add(Static(line, classes="system", markup=False))
            self.scroll_if_following()

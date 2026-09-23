"""代理线程使用的作者端口：输出经 call_from_thread 投递到界面，审批与编辑阻塞等待作者作答。"""

from __future__ import annotations

import queue
from typing import TYPE_CHECKING

from story_agent.approval import Approval, ApprovalCard, parse_answer
from story_agent.provider import CancelSignal

if TYPE_CHECKING:
    from story_agent.tui.app import WorkbenchApp


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
        self._ui(self.app.transcript_view.show_line, text)

    def emit(self, chunk: str) -> None:
        self._ui(self.app.transcript_view.show_chunk, chunk)

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

    def choose(self, question: str, options: tuple[str, ...]) -> str:
        while not self.answers.empty():  # 丢掉过期的作答
            self.answers.get_nowait()
        self._ui(self.app.begin_choice, question, options)
        while True:
            try:
                return self.answers.get(timeout=0.2)
            except queue.Empty:
                if self.app.stopping.is_set() or self.cancel.is_set():
                    self._ui(self.app.end_choice, None)
                    return ""

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
        self._ui(self.app.transcript_view.prepare_reply)

    def turn_reminder(self, text: str) -> None:
        self._ui(self.app.transcript_view.show_dev_note, text)

    def reply_empty(self) -> None:
        self._ui(self.app.transcript_view.keep_thinking_open)

    def thinking(self, chunk: str) -> None:
        self._ui(self.app.transcript_view.show_thinking, chunk)

    def reply_finished(self, text: str) -> None:
        self._ui(self.app.transcript_view.end_reply, text)

    def tool_started(
        self, name: str, label: str, args: dict | None = None, display: str = "normal"
    ) -> None:
        self._ui(self.app.transcript_view.begin_tool, label, name, args or {}, display)

    def call_finished(self, usage: dict, seconds: float, ok: bool) -> None:
        self._ui(self.app.transcript_view.show_call_stats, usage, seconds, ok)

    def tool_finished(self, name: str, text: str, is_error: bool) -> None:
        self._ui(self.app.transcript_view.end_tool, is_error)

"""代理引擎：一次作者发言 = 一轮；装配消息、带工具调用模型、执行工具、回传结果，直到模型结束发言。

引擎只经 AuthorPort 与作者交互，只经 application 用例读写数据；模型的话与工具调用都不是授权。
"""

from __future__ import annotations

from dataclasses import dataclass

from story_workbench.ai import assistant as prompts
from story_workbench.application import assistant, authoring
from story_workbench.console.tools import ToolSession, describe_call, execute
from story_workbench.console.workbench import Workbench, chapter_label
from story_workbench.domain.records import WorkbenchError
from story_workbench.infrastructure.provider import (
    ChatMessage,
    TextBlock,
    ToolResultBlock,
)

MAX_CALLS_PER_TURN = 8
HISTORY_MESSAGES = 30


@dataclass(frozen=True, slots=True)
class TurnResult:
    reply: str
    calls: int
    tools: tuple[str, ...]
    stopped_early: bool = False


class Engine:
    """observer 可选：界面据此区分助手自己的回复与工具里的流式输出，并折叠工具结果。"""

    def __init__(
        self, workbench: Workbench, *, max_calls: int = MAX_CALLS_PER_TURN, observer=None
    ) -> None:
        self.workbench = workbench
        self.max_calls = max_calls
        self.observer = observer
        self.transcript: list[ChatMessage] = prompts.history_from_messages(
            [
                prompts.HistoryEntry(m.role.value, m.body)
                for m in assistant.recent_messages(workbench.project, HISTORY_MESSAGES)
            ]
        )

    # ------------------------------------------------------------ 消息装配

    def system_prompt(self) -> str:
        wb = self.workbench
        chapters = authoring.list_chapters(wb.project)
        lines = []
        for chapter in chapters:
            marks = ["有正文" if chapter.current_draft_id else "无正文"]
            if chapter.current_plan_id:
                marks.append("有计划")
            if chapter.adopted_id:
                marks.append("待复核" if chapter.needs_review else "已采用")
            lines.append(f"{chapter_label(chapter)}（{'、'.join(marks)}）")
        current = chapter_label(wb.chapter) if wb.chapter else "未选章"
        return prompts.build_system(
            title=wb.project.novel.title,
            branch=wb.project.branch.name,
            chapter_lines=lines,
            current=current,
            model_ready=wb.clients is not None and wb.config is not None,
        )

    def _budget(self) -> int:
        config = self.workbench.config
        if config is None:
            return 20_000
        limits = config.limits
        return max(limits.available - config.estimate_tokens(self.system_prompt()), 1_000)

    def _messages(self) -> list[ChatMessage]:
        estimate = self.workbench.config.estimate_tokens if self.workbench.config else len
        self.transcript = prompts.fit_transcript(self.transcript, self._budget(), estimate)
        return [ChatMessage("system", self.system_prompt()), *self.transcript]

    def _notify(self, event: str, *args) -> None:
        hook = getattr(self.observer, event, None)
        if hook is not None:
            hook(*args)

    # ------------------------------------------------------------ 一轮

    def handle(self, author_text: str) -> TurnResult:
        wb = self.workbench
        text = author_text.strip()
        chapter_id = wb.chapter.id if wb.chapter else None
        source_id = assistant.record_author_message(wb.project, text, chapter_id)
        if wb.clients is None or wb.config is None:
            wb.say("模型尚未接入，你的话已记录。可以用 /edit 直接编辑正文，或设置密钥后重新打开。")
            return TurnResult("", 0, ())
        self.transcript.append(ChatMessage("user", text))
        session = ToolSession(wb)
        calls = 0
        reply_parts: list[str] = []
        stopped_early = False
        while True:
            if calls >= self.max_calls:
                wb.say("这一轮已经调用了很多次模型，先停下来；你可以接着说下一步要做什么。")
                stopped_early = True
                break
            calls += 1
            self._notify("reply_started")
            try:
                reply = wb.run_model(
                    assistant.call_model,
                    self._messages(),
                    prompts.TOOL_SPECS,
                    source_message_id=source_id,
                    chapter_id=wb.chapter.id if wb.chapter else None,
                    template_version=prompts.TEMPLATE_VERSION,
                    on_text=wb.emit,
                )
            except WorkbenchError as exc:
                self._notify("reply_finished", "")
                wb.say(f"助手没有完成回复：{exc.message}")
                stopped_early = True
                break
            self._notify("reply_finished", reply.text)
            if reply.text:
                reply_parts.append(reply.text)
            blocks = ((TextBlock(reply.text),) if reply.text else ()) + reply.tool_uses
            self.transcript.append(ChatMessage("assistant", blocks))
            if not reply.tool_uses:
                break
            results = []
            for use in reply.tool_uses:
                self._notify("tool_started", use.name, describe_call(use.name, use.input))
                outcome = execute(session, use.name, use.input)
                self._notify("tool_finished", use.name, outcome.text, outcome.is_error)
                results.append(ToolResultBlock(use.id, outcome.text, outcome.is_error))
            self.transcript.append(ChatMessage("user", tuple(results)))
        final = "\n\n".join(part for part in reply_parts if part.strip())
        if final:
            assistant.record_reply(wb.project, final, wb.chapter.id if wb.chapter else None)
        return TurnResult(final, calls, tuple(session.executed), stopped_early)

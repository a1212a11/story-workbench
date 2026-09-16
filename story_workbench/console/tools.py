"""工具执行：模型提出的工具调用映射到 Workbench 流程。

工具不构造 AuthorAction，不写 SQL；需要作者确认的动作一律走 Workbench 里的门槛流程。
执行期间作者看到的每一行同时收集为工具结果回给模型，避免两套说法。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from story_workbench.ai.assistant import PAID_TOOLS, TOOL_NAMES
from story_workbench.application import authoring
from story_workbench.console.workbench import Workbench, chapter_label
from story_workbench.domain.records import ChapterRecord, ErrorCode, WorkbenchError, WriteMode


class RecordingPort:
    """转发到真实端口，同时收集 say 的整行与 emit 的片段。"""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.lines: list[str] = []
        self._chunks: list[str] = []

    def _flush(self) -> None:
        if self._chunks:
            self.lines.append("".join(self._chunks).rstrip("\n"))
            self._chunks = []

    def say(self, text: str) -> None:
        self._flush()
        self.lines.append(text)
        self.inner.say(text)

    def emit(self, chunk: str) -> None:
        self._chunks.append(chunk)
        self.inner.emit(chunk)

    def ask(self, prompt: str) -> str:
        self._flush()
        answer = self.inner.ask(prompt)
        self.lines.append(f"{prompt}{answer or '（回车）'}")
        return answer

    def edit(self, initial: str, title: str) -> str | None:
        self._flush()
        return self.inner.edit(initial, title)

    def run_model(self, *args, **kwargs):
        return self.inner.run_model(*args, **kwargs)

    def transcript(self) -> str:
        self._flush()
        return "\n".join(self.lines).strip()


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    text: str
    is_error: bool = False
    paid_failure: bool = False


@dataclass
class ToolSession:
    """一轮作者发言内的工具执行状态。"""

    workbench: Workbench
    paid_failed: bool = False
    executed: list[str] = field(default_factory=list)


def _int(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | str) or not str(value).isdigit():
        raise WorkbenchError(ErrorCode.INVALID_INPUT, f"参数 {key} 必须是正整数。")
    return int(value)


def _text(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkbenchError(ErrorCode.INVALID_INPUT, f"参数 {key} 必须是文字。")
    return value.strip()


def _chapter(wb: Workbench, args: dict[str, Any]) -> ChapterRecord:
    number = _int(args, "chapter")
    if number is None:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "需要给出章号。")
    chapter = wb.select_chapter(number)
    if chapter is None:
        raise WorkbenchError(ErrorCode.NOT_FOUND, f"没有第 {number} 章。")
    return chapter


def _bind_version(wb: Workbench, chapter: ChapterRecord, args: dict[str, Any]) -> bool:
    """指定了稿件序号时展示该稿并明确绑定；未指定时保持现有绑定（现在的编辑稿）。"""
    index = _int(args, "version")
    if index is None:
        return True
    version = wb.version_by_index(chapter, index)
    if version is None:
        return False
    wb.display_draft(chapter, version, explicit=True)
    return True


# ---------------------------------------------------------------- 各工具


def list_chapters(wb: Workbench, args: dict[str, Any]) -> None:
    wb.show_chapters()


def get_chapter(wb: Workbench, args: dict[str, Any]) -> None:
    wb.describe_chapter(_chapter(wb, args))


def read_draft(wb: Workbench, args: dict[str, Any]) -> None:
    chapter = _chapter(wb, args)
    index = _int(args, "version")
    version = wb.version_by_index(chapter, index) if index else wb.current_draft(chapter)
    if version is None:
        if index is None:
            wb.say(f"{chapter_label(chapter)}还没有正文。")
        return
    wb.display_draft(chapter, version, explicit=index is not None)


def get_check(wb: Workbench, args: dict[str, Any]) -> None:
    chapter = _chapter(wb, args)
    if _bind_version(wb, chapter, args):
        wb.show_latest_check()


def list_story_items(wb: Workbench, args: dict[str, Any]) -> None:
    items = authoring.list_story_items(wb.project)
    if not items:
        wb.say("还没有候选想法或决定。")
        return
    from story_workbench.console.workbench import _LIFECYCLE_LABEL

    for index, item in enumerate(items, start=1):
        wb.say(f"  {index:>3}. {_LIFECYCLE_LABEL[item.lifecycle]} · {item.body}")


def history_status(wb: Workbench, args: dict[str, Any]) -> None:
    wb.history_status()


def create_chapter(wb: Workbench, args: dict[str, Any]) -> None:
    wb.new_chapter()


def select_chapter(wb: Workbench, args: dict[str, Any]) -> None:
    chapter = _chapter(wb, args)
    wb.say(f"现在在{chapter_label(chapter)}。")


def set_plan(wb: Workbench, args: dict[str, Any]) -> None:
    chapter = _chapter(wb, args)
    plan = authoring.get_current_plan(wb.project, chapter.id)
    plot = _text(args, "plot")
    ending = _text(args, "ending")
    if plot is None:
        plot = (plan.plot if plan else None) or ""
    if ending is None:
        ending = (plan.ending if plan else None) or ""
    wb.say(f"将为{chapter_label(chapter)}记录：")
    wb.say(f"本章剧情：{plot or '（暂不确定）'}")
    wb.say(f"本章结尾状态：{ending or '（暂不确定）'}")
    answer = wb.ask("回车确认记录；输入任何文字表示先不记录：")
    if answer:
        wb.say("没有记录计划。")
        return
    wb.set_plan(plot, ending)


def record_candidate(wb: Workbench, args: dict[str, Any]) -> None:
    text = _text(args, "text") or ""
    if not text:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "候选想法不能为空。")
    answer = wb.ask(f"记为候选想法：「{text}」 回车确认；输入任何文字表示不记录：")
    if answer:
        wb.say("没有记录候选想法。")
        return
    wb.record_candidate(text)


def decide(wb: Workbench, args: dict[str, Any]) -> None:
    text = _text(args, "text") or ""
    if not text:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "决定内容不能为空。")
    answer = wb.ask(f"记录作者决定：「{text}」 回车确认；输入任何文字表示不记录：")
    if answer:
        wb.say("没有记录决定。")
        return
    wb.decide(text)


def manage_decisions(wb: Workbench, args: dict[str, Any]) -> None:
    wb.decide()


def open_editor(wb: Workbench, args: dict[str, Any]) -> None:
    _chapter(wb, args)
    wb.edit_body()


def reject_draft(wb: Workbench, args: dict[str, Any]) -> None:
    chapter = _chapter(wb, args)
    if _bind_version(wb, chapter, args):
        wb.reject(_text(args, "reason"))


def restore_version(wb: Workbench, args: dict[str, Any]) -> None:
    _chapter(wb, args)
    index = _int(args, "version")
    if index is None:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "需要给出稿件序号。")
    wb.restore(index)


def write_chapter(wb: Workbench, args: dict[str, Any]) -> None:
    _chapter(wb, args)
    mode_text = _text(args, "mode") or "formal"
    if mode_text not in ("formal", "exploratory"):
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "mode 只能是 formal 或 exploratory。")
    mode = WriteMode.FORMAL if mode_text == "formal" else WriteMode.EXPLORATORY
    wb.generate(
        mode, request=_text(args, "requirements") or "", assumption=_text(args, "assumption") or ""
    )


def check_draft(wb: Workbench, args: dict[str, Any]) -> None:
    chapter = _chapter(wb, args)
    if _bind_version(wb, chapter, args):
        wb.check()


def diagnose_draft(wb: Workbench, args: dict[str, Any]) -> None:
    _chapter(wb, args)
    feedback = _text(args, "feedback") or ""
    if not feedback:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "需要作者的阅读反馈。")
    wb.diagnose(feedback)


def analyze_impact(wb: Workbench, args: dict[str, Any]) -> None:
    _chapter(wb, args)
    wb.analyze_impact()


def retry_failed(wb: Workbench, args: dict[str, Any]) -> None:
    _chapter(wb, args)
    wb.retry()


def consult_story(wb: Workbench, args: dict[str, Any]) -> None:
    _chapter(wb, args)
    question = _text(args, "question") or ""
    if not question:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "需要给出问题。")
    wb.discuss(question)


def adopt_draft(wb: Workbench, args: dict[str, Any]) -> None:
    chapter = _chapter(wb, args)
    if _bind_version(wb, chapter, args):
        wb.adopt()


def revalidate_chapter(wb: Workbench, args: dict[str, Any]) -> None:
    _chapter(wb, args)
    wb.revalidate()


def mark_published(wb: Workbench, args: dict[str, Any]) -> None:
    _chapter(wb, args)
    wb.publish()


def remediation_discussion(wb: Workbench, args: dict[str, Any]) -> None:
    wb.remediate()


def record_foreshadow(wb: Workbench, args: dict[str, Any]) -> None:
    _chapter(wb, args)
    wb.foreshadow()


def manage_branches(wb: Workbench, args: dict[str, Any]) -> None:
    wb.branches()


HANDLERS: dict[str, Callable[[Workbench, dict[str, Any]], None]] = {
    name: obj for name, obj in globals().items() if name in TOOL_NAMES and callable(obj)
}
assert set(HANDLERS) == set(TOOL_NAMES), set(TOOL_NAMES) - set(HANDLERS)


def execute(session: ToolSession, name: str, args: dict[str, Any]) -> ToolOutcome:
    """执行一个工具调用，返回给模型的文字结果。作者看到的内容与结果一致。"""
    wb = session.workbench
    handler = HANDLERS.get(name)
    if handler is None:
        return ToolOutcome(f"没有名为 {name} 的工具。", is_error=True)
    if name in PAID_TOOLS and session.paid_failed:
        return ToolOutcome(
            "本轮已有一次模型请求失败，不再自动发起新的付费请求；请把情况告诉作者，由作者决定是否重试。",
            is_error=True,
        )
    if name in PAID_TOOLS and (wb.clients is None or wb.config is None):
        return ToolOutcome("模型尚未接入（没有密钥），不能执行这个工具。", is_error=True)
    recorder = RecordingPort(wb.port)
    wb.port = recorder
    wb.last_failure = None
    session.executed.append(name)
    try:
        handler(wb, dict(args or {}))
    except WorkbenchError as exc:
        paid = name in PAID_TOOLS
        session.paid_failed = session.paid_failed or paid
        detail = recorder.transcript()
        text = f"没有完成：{exc.message}" + (f"\n{detail}" if detail else "")
        return ToolOutcome(text, is_error=True, paid_failure=paid)
    finally:
        wb.port = recorder.inner
    if name in PAID_TOOLS and wb.last_failure is not None:
        session.paid_failed = True
        return ToolOutcome(
            recorder.transcript() or "模型任务没有完成。", is_error=True, paid_failure=True
        )
    return ToolOutcome(recorder.transcript() or "已完成。")

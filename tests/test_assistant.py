"""对话代理引擎：自然语言 → 模型调度工具 → 门槛流程；作者权限规则不变。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from story_workbench.ai import assistant as prompts
from story_workbench.application import authoring, generation
from story_workbench.console.engine import Engine
from story_workbench.console.workbench import Workbench
from story_workbench.domain.records import MessageRole, RunStatus
from story_workbench.infrastructure import database
from story_workbench.infrastructure.provider import (
    CallOptions,
    CancelSignal,
    ChatMessage,
    EventKind,
    StreamEvent,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from tests.conftest import fixed_complete
from tests.test_workflow import CONFIG, inspection_json, plan_chapter


class FakePort:
    """预置作答的作者端口；模型任务直接同步执行。"""

    def __init__(self, answers: list[str] | None = None) -> None:
        self.answers = list(answers or [])
        self.lines: list[str] = []
        self.chunks: list[str] = []
        self.asked: list[str] = []

    def say(self, text: str) -> None:
        self.lines.append(text)

    def emit(self, chunk: str) -> None:
        self.chunks.append(chunk)

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        return self.answers.pop(0) if self.answers else ""

    def edit(self, initial: str, title: str) -> str | None:
        return initial + "（作者补了一句。）"

    def run_model(self, project, clients, config, operation, *args, **kwargs):
        return operation(
            project, *args, clients=clients, config=config, cancel=CancelSignal(), **kwargs
        )

    @property
    def output(self) -> str:
        return "\n".join(self.lines)


@dataclass
class Reply:
    text: str = ""
    uses: tuple[ToolUseBlock, ...] = ()
    truncated: bool = False
    hook: object = None  # 首个片段后调用 hook(cancel)


def scripted_clients(script: list[Reply], complete_text: str = "{}"):
    """按 stream 调用顺序消费脚本；助手调度与正文生成共用同一 stream。"""
    replies = list(script)
    calls: list[list[ChatMessage]] = []

    def stream(messages, options, cancel) -> Iterator[StreamEvent]:
        calls.append(list(messages))
        reply = replies.pop(0) if replies else Reply()
        pieces = [reply.text[: len(reply.text) // 2], reply.text[len(reply.text) // 2 :]]
        for index, piece in enumerate(p for p in pieces if p):
            if cancel.is_set():
                yield StreamEvent(EventKind.ERROR, error="cancelled")
                return
            yield StreamEvent(EventKind.TEXT, text=piece)
            if index == 0 and reply.hook is not None:
                reply.hook(cancel)
        for use in reply.uses:
            yield StreamEvent(EventKind.TOOL_USE, tool_use=use)
        yield StreamEvent(EventKind.USAGE, usage={"input_tokens": 3, "output_tokens": 4})
        yield StreamEvent(EventKind.TRUNCATED if reply.truncated else EventKind.COMPLETED)

    return generation.Clients(stream=stream, complete=fixed_complete(complete_text)), calls


def use(name: str, uid: str = "t1", **args) -> ToolUseBlock:
    return ToolUseBlock(uid, name, args)


def runs_by_task(project) -> dict[str, list[RunStatus]]:
    rows = project.conn.execute(
        "SELECT task, status FROM run WHERE novel_id = ? ORDER BY created_at, rowid",
        (project.novel_id,),
    ).fetchall()
    out: dict[str, list[RunStatus]] = {}
    for row in rows:
        out.setdefault(row["task"], []).append(RunStatus(row["status"]))
    return out


BODY = "井底有一具尸体。沈砚把灯搁在井沿。"


def make_engine(project, script, answers=None, *, complete_text=None, clients=True):
    port = FakePort(answers)
    if clients:
        clients_obj, calls = scripted_clients(
            script, complete_text or inspection_json(BODY, plot="调查旧井", ending="看到尸体")
        )
    else:
        clients_obj, calls = None, []
    wb = Workbench(project, port, clients=clients_obj, config=CONFIG if clients else None)
    return Engine(wb), port, calls


# ---------------------------------------------------------------- 一轮：读取、写作、回复


def test_turn_orchestrates_tools_and_records_every_model_call(project):
    plan_chapter(project, 1, "调查旧井", "看到尸体")
    script = [
        Reply("先看第一章。", (use("get_chapter", chapter=1),)),
        Reply("开始写。", (use("write_chapter", "t2", chapter=1, mode="formal"),)),
        Reply(BODY),  # 正文生成流
        Reply("写完了，检查没有发现问题。"),
    ]
    engine, port, calls = make_engine(project, script)

    result = engine.handle("写第一章")

    assert result.tools == ("get_chapter", "write_chapter") and result.calls == 3
    assert not result.stopped_early
    assert result.reply == "先看第一章。\n\n开始写。\n\n写完了，检查没有发现问题。"
    assert [v.body for v in authoring.list_versions(project, engine.workbench.chapter.id)] == [BODY]
    assert runs_by_task(project) == {
        "assist": [RunStatus.SUCCEEDED] * 3,
        "generate": [RunStatus.SUCCEEDED],
        "inspect": [RunStatus.SUCCEEDED],
    }
    assist_runs = [
        database.get_run(project.conn, project.novel_id, row["id"])
        for row in project.conn.execute("SELECT id FROM run WHERE task = 'assist'").fetchall()
    ]
    assert all(r.usage_json and r.finished_at and r.provider == "fixed" for r in assist_runs)
    # 作者看到的与回给模型的一致：第一章概况进入了第二次调度的消息
    second_call = calls[1]
    tool_results = [
        block
        for message in second_call
        if not isinstance(message.content, str)
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert tool_results and "本章剧情：调查旧井" in tool_results[0].content
    assert "本章剧情：调查旧井" in port.output
    assert "生成结束。以下是完整正文：" in port.output and BODY in port.output
    assert "".join(port.chunks).count(BODY) == 1  # 正文流式回显一次
    # 系统提示带当前状态；对话持久化为作者/模型消息
    assert second_call[0].role == "system" and "第一章（" in second_call[0].content
    messages = database.list_messages(project.conn, project.novel_id)
    roles_bodies = [(m.role, m.body) for m in messages]
    assert (MessageRole.AUTHOR, "写第一章") in roles_bodies
    assert roles_bodies[-1] == (MessageRole.MODEL, result.reply)


# ---------------------------------------------------------------- 采用仍需作者已读声明


def _written_chapter(project):
    plan_chapter(project, 1, "调查旧井", "看到尸体")
    engine, port, _ = make_engine(
        project,
        [
            Reply("", (use("write_chapter", chapter=1, mode="formal"),)),
            Reply(BODY),
            Reply("好了。"),
        ],
    )
    engine.handle("写第一章")
    return engine.workbench.chapter


def test_adopt_tool_requires_read_declaration_and_writes_nothing_otherwise(project):
    chapter = _written_chapter(project)
    engine, port, _ = make_engine(
        project, [Reply("", (use("adopt_draft", chapter=1),)), Reply("没有采用。")], answers=[""]
    )
    engine.handle("采用")
    assert authoring.adopted_version_id(project, chapter.id) is None
    assert "没有采用。采用需要明确声明已读全文并采用。" in port.output
    assert any("输入「已读，采用」" in prompt for prompt in port.asked)

    engine, port, calls = make_engine(
        project,
        [Reply("", (use("adopt_draft", chapter=1),)), Reply("已采用第一章。")],
        answers=["已读，采用"],
    )
    result = engine.handle("我读完了，采用")
    assert authoring.adopted_version_id(project, chapter.id) is not None
    assert "已采用第一章这一稿，故事状态更新完成。" in port.output
    # 模型拿到的是作者实际看到的流程记录
    final_call = calls[-1]
    tool_result = next(
        block
        for message in final_call
        if not isinstance(message.content, str)
        for block in message.content
        if isinstance(block, ToolResultBlock)
    )
    assert "已采用第一章这一稿" in tool_result.content and not tool_result.is_error
    assert result.tools == ("adopt_draft",)


def test_model_cannot_adopt_by_claiming_it(project):
    """模型只说“已采用”而不调用工具，不产生任何正式记录。"""
    chapter = _written_chapter(project)
    engine, port, _ = make_engine(project, [Reply("我已经替你采用了第一章。")])
    engine.handle("采用")
    assert authoring.adopted_version_id(project, chapter.id) is None


# ---------------------------------------------------------------- 付费失败不自动重试


def test_paid_tool_failure_blocks_further_paid_tools_in_the_same_turn(project):
    plan_chapter(project, 1, "调查旧井", "看到尸体")
    script = [
        Reply("", (use("write_chapter", chapter=1, mode="formal"),)),
        Reply("半截", truncated=True),  # 正文生成截断
        Reply("再试一次。", (use("write_chapter", "t2", chapter=1, mode="formal"),)),
        Reply("这次先停下，等作者决定。"),
    ]
    engine, port, calls = make_engine(project, script)
    result = engine.handle("写第一章")
    assert result.tools == ("write_chapter",)  # 第二次被拒绝，没有执行
    assert runs_by_task(project)["generate"] == [RunStatus.FAILED]  # 只有一次付费生成
    results = [
        block
        for message in calls[-1]
        if not isinstance(message.content, str)
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert results[-1].is_error and "不再自动发起新的付费请求" in results[-1].content
    assert results[-2].is_error and "写作没有完成" in results[-2].content


def test_paid_tools_refused_without_model_and_turn_recorded(project):
    plan_chapter(project, 1, "调查旧井", "看到尸体")
    engine, port, _ = make_engine(project, [], clients=False)
    result = engine.handle("写第一章")
    assert result.calls == 0 and "模型尚未接入" in port.output
    messages = database.list_messages(project.conn, project.novel_id)
    assert messages[-1].role is MessageRole.AUTHOR and messages[-1].body == "写第一章"
    assert runs_by_task(project) == {}


# ---------------------------------------------------------------- 单轮上限与取消


def test_turn_stops_after_call_limit(project):
    plan_chapter(project, 1, None, None)
    script = [Reply("看看。", (use("list_chapters", f"t{i}"),)) for i in range(9)]
    engine, port, _ = make_engine(project, script)
    engine.max_calls = 3
    result = engine.handle("反复看章节")
    assert result.calls == 3 and result.stopped_early
    assert runs_by_task(project) == {"assist": [RunStatus.SUCCEEDED] * 3}
    assert "先停下来" in port.output


def test_cancel_is_recorded_before_late_text_and_no_reply_is_saved(project):
    plan_chapter(project, 1, None, None)

    def cancel_now(cancel):
        run_id = database.any_run_in_flight(project.conn, project.novel_id)
        assert generation.cancel_run(project, run_id, cancel)

    engine, port, _ = make_engine(project, [Reply("这一句会被打断。", hook=cancel_now)])
    result = engine.handle("随便聊聊")
    assert result.stopped_early and "助手没有完成回复" in port.output
    assert runs_by_task(project) == {"assist": [RunStatus.CANCELLED]}
    run = database.get_run(
        project.conn,
        project.novel_id,
        project.conn.execute("SELECT id FROM run").fetchone()["id"],
    )
    assert run.partial_text == "这一句会" and run.finished_at is not None  # 迟到片段被丢弃
    assert [m.role for m in database.list_messages(project.conn, project.novel_id)][-1] is (
        MessageRole.AUTHOR
    )


# ---------------------------------------------------------------- 未知工具、参数与编辑器


def test_unknown_tool_and_bad_arguments_are_reported_not_executed(project):
    plan_chapter(project, 1, None, None)
    script = [
        Reply("", (use("adopt_everything"), use("get_chapter", "t2", chapter="二"))),
        Reply("明白了。"),
    ]
    engine, port, calls = make_engine(project, script)
    engine.handle("全部采用")
    results = [
        block
        for message in calls[-1]
        if not isinstance(message.content, str)
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]
    assert [r.is_error for r in results] == [True, True]
    assert "没有名为 adopt_everything 的工具" in results[0].content
    assert "必须是正整数" in results[1].content
    assert runs_by_task(project) == {"assist": [RunStatus.SUCCEEDED] * 2}


def test_open_editor_lets_author_save_and_plan_needs_confirmation(project):
    plan_chapter(project, 1, None, None)
    script = [
        Reply("", (use("open_editor", chapter=1),)),
        Reply("", (use("set_plan", "t2", chapter=1, plot="调查旧井", ending="看到尸体"),)),
        Reply("记好了。"),
    ]
    engine, port, _ = make_engine(project, script, answers=[""])
    engine.handle("我来改正文，然后把计划记下来")
    chapter = engine.workbench.chapter
    assert [v.body for v in authoring.list_versions(project, chapter.id)] == ["（作者补了一句。）"]
    plan = authoring.get_current_plan(project, chapter.id)
    assert (plan.plot, plan.ending) == ("调查旧井", "看到尸体")
    assert any("回车确认记录" in prompt for prompt in port.asked)


# ---------------------------------------------------------------- 纯函数：对话整理


def test_history_and_transcript_fitting():
    history = prompts.history_from_messages(
        [
            prompts.HistoryEntry("model", "开场白不算"),
            prompts.HistoryEntry("author", "第一句"),
            prompts.HistoryEntry("author", "第二句"),
            prompts.HistoryEntry("model", "回复"),
        ]
    )
    assert history == [ChatMessage("user", "第一句\n\n第二句"), ChatMessage("assistant", "回复")]
    transcript = [
        ChatMessage("user", "很久以前的话" * 20),
        ChatMessage("assistant", (TextBlock("看看"), ToolUseBlock("t", "list_chapters", {}))),
        ChatMessage("user", (ToolResultBlock("t", "章节……" * 20),)),
        ChatMessage("assistant", "好的"),
        ChatMessage("user", "现在这句"),
    ]
    fitted = prompts.fit_transcript(transcript, budget=20, estimate=len)
    assert fitted == [ChatMessage("user", "现在这句")]
    assert prompts.fit_transcript(transcript, budget=10_000, estimate=len) == transcript


def test_tool_specs_cover_paid_tools_and_are_valid_schemas():
    names = {spec.name for spec in prompts.TOOL_SPECS}
    assert prompts.PAID_TOOLS <= names
    for spec in prompts.TOOL_SPECS:
        assert spec.description and spec.input_schema["type"] == "object"
        assert set(spec.input_schema["required"]) <= set(spec.input_schema["properties"])
    options = CallOptions(
        model="m", max_output_tokens=1, timeout_seconds=1, tools=prompts.TOOL_SPECS
    )
    assert len(options.tools) == len(prompts.TOOL_SPECS)


@pytest.mark.parametrize("name", sorted(prompts.PAID_TOOLS))
def test_every_paid_tool_is_refused_after_a_paid_failure(project, name):
    from story_workbench.console.tools import ToolSession, execute

    plan_chapter(project, 1, "调查旧井", "看到尸体")
    engine, port, _ = make_engine(project, [])
    session = ToolSession(engine.workbench, paid_failed=True)
    outcome = execute(
        session, name, {"chapter": 1, "mode": "formal", "feedback": "x", "question": "y"}
    )
    assert outcome.is_error and "不再自动发起新的付费请求" in outcome.text
    assert runs_by_task(project) == {}

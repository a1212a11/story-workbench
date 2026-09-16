"""对话助手的模型调用用例：一次带工具定义的调度请求记一条 run（task=assist）。

工具是否执行、如何执行由 console 层决定；本模块只负责发起请求、落库流片段、记录用量与终态，
并沿用生成任务的规则：同一作品最多一个在途请求，取消先落库，付费失败不自动重试。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace

from story_workbench.application.generation import (
    Clients,
    GenerationConfig,
    OnText,
    _ensure_idle,
    _fail_run,
)
from story_workbench.application.projects import Project
from story_workbench.domain.records import (
    ErrorCode,
    MessageRecord,
    MessageRole,
    RunStatus,
    TaskKind,
    WorkbenchError,
)
from story_workbench.infrastructure import database
from story_workbench.infrastructure.provider import (
    CancelSignal,
    ChatMessage,
    EventKind,
    ToolSpec,
    ToolUseBlock,
)


@dataclass(frozen=True, slots=True)
class AssistReply:
    run_id: str
    text: str
    tool_uses: tuple[ToolUseBlock, ...]
    usage: dict[str, int]


def record_author_message(project: Project, text: str, chapter_id: str | None) -> str:
    return database.commit_message(
        project.conn, project.novel_id, MessageRole.AUTHOR.value, text, chapter_id
    )


def record_reply(project: Project, text: str, chapter_id: str | None) -> str:
    return database.commit_message(
        project.conn, project.novel_id, MessageRole.MODEL.value, text, chapter_id
    )


def recent_messages(project: Project, limit: int) -> tuple[MessageRecord, ...]:
    """最近的作者与模型消息（按时间先后），供重启后恢复对话上下文。"""
    messages = [
        m
        for m in database.list_messages(project.conn, project.novel_id)
        if m.role in (MessageRole.AUTHOR, MessageRole.MODEL)
    ]
    return tuple(messages[-limit:])


def call_model(
    project: Project,
    messages: list[ChatMessage],
    tools: tuple[ToolSpec, ...],
    *,
    source_message_id: str,
    chapter_id: str | None,
    template_version: str,
    clients: Clients,
    config: GenerationConfig,
    cancel: CancelSignal | None = None,
    on_text: OnText | None = None,
) -> AssistReply:
    """发起一次调度请求。文本片段落库后回显；工具调用块原样返回，不在此执行。"""
    _ensure_idle(project)
    run_id = database.commit_run(
        project.conn,
        project.novel_id,
        chapter_id=chapter_id,
        task=TaskKind.ASSIST,
        source_message_id=source_message_id,
        template_version=template_version,
        status=RunStatus.RUNNING,
        provider=config.provider,
        model=config.options.model,
        request_json=json.dumps({"tools": [t.name for t in tools]}, ensure_ascii=False),
    )
    parts: list[str] = []
    tool_uses: list[ToolUseBlock] = []
    usage: dict[str, int] = {}
    final = EventKind.ERROR
    try:
        events = clients.stream(
            messages, replace(config.options, tools=tools), cancel or CancelSignal()
        )
        try:
            for event in events:
                if event.kind is EventKind.TEXT:
                    if not database.commit_run_chunk(
                        project.conn, project.novel_id, run_id, event.text
                    ):
                        break  # 已取消，丢弃迟到块
                    parts.append(event.text)
                    if on_text is not None:
                        on_text(event.text)
                elif event.kind is EventKind.TOOL_USE and event.tool_use is not None:
                    tool_uses.append(event.tool_use)
                elif event.kind is EventKind.USAGE:
                    usage = dict(event.usage)
                else:
                    final = event.kind
                    break
        finally:
            close = getattr(events, "close", None)
            if close is not None:
                close()
    except Exception as exc:
        _fail_run(project, run_id, "MODEL_FAILED")
        if isinstance(exc, WorkbenchError):
            raise
        raise WorkbenchError(ErrorCode.MODEL_FAILED, "助手请求失败。") from None
    usage_json = json.dumps(usage) if usage else None
    run = database.get_run(project.conn, project.novel_id, run_id)
    if final is not EventKind.COMPLETED or run.status is not RunStatus.RUNNING:
        if run.status is RunStatus.RUNNING:
            database.commit_run_status(
                project.conn,
                project.novel_id,
                run_id,
                RunStatus.FAILED,
                error_code=final.value,
                usage_json=usage_json,
            )
        raise WorkbenchError(
            ErrorCode.MODEL_FAILED,
            "助手回复没有完整结束。",
            {
                "run_id": run_id,
                "status": run.status.value if run.status is not RunStatus.RUNNING else final.value,
            },
        )
    database.commit_run_status(
        project.conn, project.novel_id, run_id, RunStatus.SUCCEEDED, usage_json=usage_json
    )
    return AssistReply(run_id, "".join(parts), tuple(tool_uses), usage)

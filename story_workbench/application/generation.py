"""上下文读取、任务阶段、流保存、检查与取消的用例。

模型请求在数据库事务外进行；只有已落库的流块才算可恢复；取消先落库再通知客户端；
迟到结果按 run 状态拒绝；同项目最多一个模型请求在途。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from story_workbench.ai import context as ctx
from story_workbench.ai import prompts, tasks
from story_workbench.domain.records import (
    CandidateFactInput,
    CheckInput,
    DelegationScope,
    DraftResult,
    ErrorCode,
    EvidenceInput,
    IssueInput,
    MessageRole,
    RunStatus,
    Target,
    TaskKind,
    TaskRequest,
    WorkbenchError,
    WriteMode,
)
from story_workbench.infrastructure import database
from story_workbench.infrastructure.provider import (
    CallOptions,
    CancelSignal,
    CompleteCall,
    EventKind,
    StreamCall,
)

from . import projects
from .projects import Project


@dataclass(frozen=True, slots=True)
class Clients:
    """启动入口注入的客户端函数；测试传固定函数。"""

    stream: StreamCall
    complete: CompleteCall


@dataclass(frozen=True, slots=True)
class GenerationConfig:
    options: CallOptions
    limits: ctx.ContextLimits
    estimate_tokens: ctx.EstimateTokens = ctx.default_estimate_tokens
    provider: str = "fixed"


OnText = Callable[[str], None]


# ---------------------------------------------------------------- 来源装配（只读）


def load_sources(project: Project, chapter_id: str) -> ctx.ContextSources:
    """把上下文构建需要的候选来源整理成记录；筛选与容量由 ai/context 决定。"""
    conn, novel_id = project.conn, project.novel_id
    chapters = database.list_chapters(conn, novel_id)
    number_of = {c.id: c.number for c in chapters}
    target = database.get_chapter(conn, novel_id, chapter_id)
    previous = next((c for c in chapters if c.number == target.number - 1), None)

    story_items = tuple(
        ctx.StoryItemSource(
            story_item_id=item.id,
            body=item.body,
            authority=item.authority,
            lifecycle=item.lifecycle,
            chapter_number=None if item.chapter_id is None else number_of.get(item.chapter_id),
        )
        for item in database.list_story_items_current(conn, novel_id)
    )
    plans = []
    for c in chapters:
        if c.current_plan_id is not None:
            plan = database.get_plan(conn, novel_id, c.current_plan_id)
            plans.append(ctx.PlanSource(plan.id, c.number, plan.plot, plan.ending))

    facts: list[ctx.FactSource] = []
    previous_snapshot_id = None
    previous_status = None
    if previous is not None:
        if previous.current_snapshot_id is not None:
            snap = database.get_snapshot(conn, novel_id, previous.current_snapshot_id)
            previous_snapshot_id, previous_status = snap.id, snap.status
            for d in database.list_snapshot_fact_details(conn, novel_id, snap.id):
                facts.append(
                    ctx.FactSource(
                        snapshot_id=snap.id,
                        chapter_number=number_of[d.chapter_id],
                        kind=d.kind,
                        proposition=d.proposition,
                        fact_revision_id=d.fact_revision_id,
                        candidate_id=d.candidate_id,
                        subject=d.subject,
                        story_time=d.story_time,
                    )
                )
        else:
            previous_status = database.latest_snapshot_status_for_chapter(
                conn, novel_id, previous.id
            )

    rejected = database.list_rejected_version_ids(conn, novel_id)
    texts: list[ctx.TextSource] = []
    for c in chapters:
        adopted_version = database.get_adopted_version_id(conn, novel_id, c)
        for v in database.list_versions(conn, novel_id, c.id):
            if v.id == adopted_version:
                status = ctx.TextStatus.ADOPTED
            elif v.id in rejected:
                status = ctx.TextStatus.REJECTED
            else:
                status = ctx.TextStatus.DRAFT
            texts.append(ctx.TextSource(v.id, c.number, v.body, status))

    issues: list[ctx.IssueSource] = []
    for c in chapters:
        if c.adopted_id is None or c.number >= target.number:
            continue
        adopted = database.get_adoption(conn, novel_id, c.adopted_id)
        check = database.get_check(
            conn, novel_id, database.get_review_check_id(conn, novel_id, adopted.review_id)
        )
        resolved = database.list_resolved_issue_ids(conn, novel_id, adopted.review_id)
        for issue in check.issues:
            issues.append(
                ctx.IssueSource(
                    issue.id, c.number, issue.kind, issue.explanation, issue.id in resolved
                )
            )

    messages = tuple(
        ctx.MessageSource(
            m.id,
            m.role,
            m.body,
            None if m.chapter_id is None else number_of.get(m.chapter_id),
        )
        for m in database.list_messages(conn, novel_id)
    )
    return ctx.ContextSources(
        novel_id=novel_id,
        chapter_id=chapter_id,
        design_revision=_design_revision(project),
        story_items=story_items,
        plans=tuple(plans),
        facts=tuple(facts),
        texts=tuple(texts),
        issues=tuple(issues),
        messages=messages,
        previous_snapshot_id=previous_snapshot_id,
        previous_snapshot_status=previous_status,
    )


def _design_revision(project: Project) -> int:
    novel = database.get_novel(project.conn)
    return project.novel.design_revision if novel is None else novel.design_revision


def build_context(
    project: Project, chapter_id: str, mode: WriteMode, config: GenerationConfig
) -> ctx.ContextDraft:
    chapter = database.get_chapter(project.conn, project.novel_id, chapter_id)
    sources = load_sources(project, chapter_id)
    return ctx.build_context(sources, chapter.number, mode, config.limits, config.estimate_tokens)


def save_basis(project: Project, draft: ctx.ContextDraft, purpose: str) -> str:
    """把 ContextDraft 的引用保存为 basis/basis_source；事务内比较设计修订号。"""
    sources: list[tuple[str, dict[str, object]]] = []
    for c in draft.citations:
        ref: dict[str, object] = {
            "quote": c.quote,
            "excerpt_start": c.excerpt_start,
            "excerpt_end": c.excerpt_end,
        }
        for key in ("version_id", "story_item_id", "snapshot_id", "fact_revision_id", "message_id"):
            value = getattr(c, key)
            if value is not None:
                ref[key] = value
        sources.append((c.role.value, ref))
    omitted = json.dumps(
        [
            {
                "source_kind": o.source_kind,
                "source_id": o.source_id,
                "reason": o.reason.value,
                "chapter_number": o.chapter_number,
            }
            for o in draft.omitted
        ],
        ensure_ascii=False,
    )
    return database.commit_basis(
        project.conn,
        draft.novel_id,
        draft.chapter_id,
        draft.plan_id,
        draft.previous_snapshot_id,
        draft.design_revision,
        purpose,
        sources,
        omitted,
    )


# ---------------------------------------------------------------- 任务执行


def _ensure_idle(project: Project) -> None:
    busy = database.any_run_in_flight(project.conn, project.novel_id)
    if busy is not None:
        raise WorkbenchError(
            ErrorCode.PROJECT_BUSY, "有一个模型任务正在进行，请先等待或取消。", {"run_id": busy}
        )


def _request(
    task: TaskKind,
    basis_id: str,
    source_message_id: str,
    *,
    input_version_id: str | None = None,
    delegation: DelegationScope | None = None,
    missing: tuple[str, ...] = (),
    assumptions: tuple[str, ...] = (),
    output_limit: int = 0,
) -> TaskRequest:
    return TaskRequest(
        task=task,
        basis_id=basis_id,
        source_message_id=source_message_id,
        template_version=prompts.TEMPLATE_VERSION,
        input_version_id=input_version_id,
        delegation_scope=delegation,
        missing_inputs=missing,
        assumptions=assumptions,
        output_limit=output_limit,
    )


def _request_json(request: TaskRequest, extra: dict[str, object] | None = None) -> str:
    payload: dict[str, object] = {
        "missing_inputs": list(request.missing_inputs),
        "assumptions": list(request.assumptions),
        "output_limit": request.output_limit,
        "delegation_scope": None
        if request.delegation_scope is None
        else {
            "chapter_id": request.delegation_scope.chapter_id,
            "request_message_id": request.delegation_scope.request_message_id,
            "may_choose_plot": request.delegation_scope.may_choose_plot,
        },
    }
    payload.update(extra or {})
    return json.dumps(payload, ensure_ascii=False)


def _run_stream(
    project: Project,
    run_id: str,
    events,
    on_text: OnText | None,
) -> tuple[str, str | None, EventKind]:
    """消费事件流：每个 TEXT 块落库后才回显；返回 (正文, usage_json, 终止事件)。"""
    parts: list[str] = []
    usage_json: str | None = None
    final = EventKind.ERROR
    for event in events:
        if event.kind is EventKind.TEXT:
            if not database.commit_run_chunk(project.conn, project.novel_id, run_id, event.text):
                final = EventKind.ERROR  # 已取消，丢弃迟到块
                break
            parts.append(event.text)
            if on_text is not None:
                on_text(event.text)
        elif event.kind is EventKind.USAGE:
            usage_json = json.dumps(event.usage)
        else:
            final = event.kind
            break
    return "".join(parts), usage_json, final


def write_chapter(
    project: Project,
    chapter_id: str,
    mode: WriteMode,
    source_message_id: str,
    clients: Clients,
    config: GenerationConfig,
    *,
    assumptions: tuple[str, ...] = (),
    delegation: DelegationScope | None = None,
    cancel: CancelSignal | None = None,
    on_text: OnText | None = None,
    retry_of: str | None = None,
) -> DraftResult:
    """正式写作或试写：对账、构建上下文、保存依据与 run、流式生成、保存结果、独立检查。

    不采用。缺口与假设在生成前已由 build_context 计算并写入 run.request_json。
    检查失败保留正文与失败原因，返回 check_id=None。
    """
    projects.reconcile_workcopy(project, chapter_id)
    _ensure_idle(project)
    draft = build_context(project, chapter_id, mode, config)
    purpose = "exploratory" if mode is WriteMode.EXPLORATORY else "formal"
    basis_id = save_basis(project, draft, purpose)
    chapter = database.get_chapter(project.conn, project.novel_id, chapter_id)
    request = _request(
        TaskKind.GENERATE,
        basis_id,
        source_message_id,
        input_version_id=chapter.current_draft_id,
        delegation=delegation,
        missing=draft.missing_inputs,
        assumptions=assumptions,
        output_limit=config.options.max_output_tokens,
    )
    run_id = database.commit_run(
        project.conn,
        project.novel_id,
        chapter_id=chapter_id,
        task=TaskKind.GENERATE,
        source_message_id=source_message_id,
        template_version=prompts.TEMPLATE_VERSION,
        status=RunStatus.RUNNING,
        basis_id=basis_id,
        input_version_id=chapter.current_draft_id,
        retry_of_id=retry_of,
        request_json=_request_json(request, {"mode": mode.value}),
    )
    cancel = cancel or CancelSignal()
    try:
        events = tasks.generate(
            request, draft, clients.stream, options=config.options, cancel=cancel
        )
        body, usage_json, final = _run_stream(project, run_id, events, on_text)
    except WorkbenchError as exc:
        database.commit_run_status(
            project.conn, project.novel_id, run_id, RunStatus.FAILED, error_code=exc.code.value
        )
        raise
    except Exception as exc:
        database.commit_run_status(
            project.conn, project.novel_id, run_id, RunStatus.FAILED, error_code="MODEL_FAILED"
        )
        raise WorkbenchError(
            ErrorCode.MODEL_FAILED, "模型请求失败。", {"run_id": run_id, "error": repr(exc)}
        ) from exc
    if final is not EventKind.COMPLETED:
        run = database.get_run(project.conn, project.novel_id, run_id)
        if run.status is RunStatus.CANCELLED:
            raise WorkbenchError(
                ErrorCode.MODEL_FAILED,
                "本次生成已取消；已保存的部分输出保留在任务记录中。",
                {"run_id": run_id, "status": "cancelled"},
            )
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
            "生成没有完整结束，已保存的部分输出保留。",
            {"run_id": run_id, "status": final.value},
        )
    result = database.commit_run_result(
        project.conn, project.novel_id, run_id, body, chapter.current_draft_id, usage_json
    )
    if result is None:
        raise WorkbenchError(
            ErrorCode.MODEL_FAILED, "本次生成已取消，结果未保存为草稿。", {"run_id": run_id}
        )
    target = Target(project.novel_id, chapter_id, result.version_id)
    check_id: str | None
    try:
        check_id = check_draft(
            project, target, source_message_id, clients, config, basis_id=basis_id, context=draft
        )
    except WorkbenchError:
        check_id = None
    return DraftResult(
        version_id=result.version_id,
        run_id=run_id,
        based_on_old_inputs=not result.became_current,
        check_id=check_id,
    )


def check_draft(
    project: Project,
    target: Target,
    source_message_id: str,
    clients: Clients,
    config: GenerationConfig,
    *,
    basis_id: str | None = None,
    context: ctx.ContextDraft | None = None,
    cancel: CancelSignal | None = None,
    retry_of: str | None = None,
) -> str:
    """独立检查 run：一次检查与候选提取；结构或引用校验失败不能作为检查完成。"""
    if target.novel_id != project.novel_id:
        raise WorkbenchError(ErrorCode.WRONG_SCOPE, "稿件不属于当前作品。")
    version = database.get_version(project.conn, project.novel_id, target.version_id)
    if version.chapter_id != target.chapter_id:
        raise WorkbenchError(ErrorCode.WRONG_SCOPE, "稿件不属于该章。")
    _ensure_idle(project)
    if context is None or basis_id is None:
        mode = WriteMode.EXPLORATORY
        context = build_context(project, target.chapter_id, mode, config)
        basis_id = save_basis(project, context, "inspect")
    request = _request(
        TaskKind.INSPECT,
        basis_id,
        source_message_id,
        input_version_id=target.version_id,
        output_limit=config.options.max_output_tokens,
    )
    run_id = database.commit_run(
        project.conn,
        project.novel_id,
        chapter_id=target.chapter_id,
        task=TaskKind.INSPECT,
        source_message_id=source_message_id,
        template_version=prompts.TEMPLATE_VERSION,
        status=RunStatus.RUNNING,
        basis_id=basis_id,
        input_version_id=target.version_id,
        retry_of_id=retry_of,
        request_json=_request_json(request),
    )
    try:
        payload = tasks.inspect(
            request,
            context,
            version.body,
            clients.complete,
            options=config.options,
            cancel=cancel or CancelSignal(),
        )
        check_input = CheckInput(
            coverage_json=json.dumps(
                {
                    "checked": list(payload.coverage.checked),
                    "unchecked": list(payload.coverage.unchecked),
                },
                ensure_ascii=False,
            ),
            actual_plot=payload.actual_plot,
            actual_ending=payload.actual_ending,
            issues=tuple(
                IssueInput(
                    kind=i.kind,
                    explanation=i.explanation,
                    proposed_change=i.proposed_change or None,
                    evidence=tuple(
                        EvidenceInput(
                            quote=e.quote, version_id=target.version_id, start=e.start, end=e.end
                        )
                        for e in i.evidence
                    ),
                )
                for i in payload.issues
            ),
            candidate_facts=tuple(
                CandidateFactInput(
                    kind=f.kind,
                    proposition=f.proposition,
                    evidence_start=f.start,
                    evidence_end=f.end,
                    quote=f.quote,
                    subject=f.subject,
                    story_time=f.story_time,
                )
                for f in payload.candidate_facts
            ),
        )
        run = database.get_run(project.conn, project.novel_id, run_id)
        if run.status is not RunStatus.RUNNING:
            raise WorkbenchError(
                ErrorCode.MODEL_FAILED, "检查已取消，结果不予保存。", {"run_id": run_id}
            )
        check_id = database.commit_check_result(
            project.conn,
            project.novel_id,
            target.chapter_id,
            target.version_id,
            basis_id,
            run_id,
            check_input,
        )
    except WorkbenchError as exc:
        database.commit_run_status(
            project.conn, project.novel_id, run_id, RunStatus.FAILED, error_code=exc.code.value
        )
        raise
    database.commit_run_status(
        project.conn,
        project.novel_id,
        run_id,
        RunStatus.SUCCEEDED,
        usage_json=json.dumps(payload.usage),
    )
    return check_id


def discuss(
    project: Project,
    chapter_id: str,
    author_text: str,
    clients: Clients,
    config: GenerationConfig,
    *,
    cancel: CancelSignal | None = None,
    on_text: OnText | None = None,
) -> tuple[str, str]:
    """讨论：作者消息落库，模型回复以 message 保存；不抽取结构化想法。

    返回 (作者消息 ID, 模型消息 ID)。
    """
    author_message_id = database.commit_message(
        project.conn, project.novel_id, MessageRole.AUTHOR.value, author_text, chapter_id
    )
    _ensure_idle(project)
    draft = build_context(project, chapter_id, WriteMode.EXPLORATORY, config)
    basis_id = save_basis(project, draft, "discuss")
    request = _request(TaskKind.DISCUSS, basis_id, author_message_id)
    run_id = database.commit_run(
        project.conn,
        project.novel_id,
        chapter_id=chapter_id,
        task=TaskKind.DISCUSS,
        source_message_id=author_message_id,
        template_version=prompts.TEMPLATE_VERSION,
        status=RunStatus.RUNNING,
        basis_id=basis_id,
        request_json=_request_json(request),
    )
    events = tasks.discuss(
        request,
        draft,
        author_text,
        clients.stream,
        options=config.options,
        cancel=cancel or CancelSignal(),
    )
    body, usage_json, final = _run_stream(project, run_id, events, on_text)
    if final is not EventKind.COMPLETED:
        run = database.get_run(project.conn, project.novel_id, run_id)
        if run.status is RunStatus.RUNNING:
            database.commit_run_status(
                project.conn, project.novel_id, run_id, RunStatus.FAILED, error_code=final.value
            )
        raise WorkbenchError(
            ErrorCode.MODEL_FAILED,
            "讨论回复没有完整结束。",
            {"run_id": run_id, "status": final.value},
        )
    model_message_id = database.commit_message(
        project.conn, project.novel_id, MessageRole.MODEL.value, body, chapter_id
    )
    database.commit_run_status(
        project.conn, project.novel_id, run_id, RunStatus.SUCCEEDED, usage_json=usage_json
    )
    return author_message_id, model_message_id


def cancel_run(project: Project, run_id: str, cancel: CancelSignal) -> bool:
    """取消先落库，成功后才置位取消信号通知客户端尽力终止。"""
    recorded = database.commit_run_cancel(project.conn, project.novel_id, run_id)
    if recorded:
        cancel.set()
    return recorded


def retry_run(
    project: Project,
    run_id: str,
    source_message_id: str,
    clients: Clients,
    config: GenerationConfig,
    *,
    cancel: CancelSignal | None = None,
    on_text: OnText | None = None,
) -> DraftResult | str:
    """失败阶段的显式重试：检查失败只重试检查；生成失败是新任务；重算走 adoption。"""
    run = database.get_run(project.conn, project.novel_id, run_id)
    if run.status not in (RunStatus.FAILED, RunStatus.INTERRUPTED, RunStatus.CANCELLED):
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "只有失败、中断或取消的任务可以重试。")
    if run.task is TaskKind.INSPECT:
        assert run.chapter_id is not None and run.input_version_id is not None
        target = Target(project.novel_id, run.chapter_id, run.input_version_id)
        return check_draft(
            project, target, source_message_id, clients, config, cancel=cancel, retry_of=run_id
        )
    if run.task is TaskKind.GENERATE:
        assert run.chapter_id is not None
        payload = json.loads(run.request_json or "{}")
        mode = WriteMode(payload.get("mode", WriteMode.FORMAL.value))
        return write_chapter(
            project,
            run.chapter_id,
            mode,
            source_message_id,
            clients,
            config,
            assumptions=tuple(payload.get("assumptions", ())),
            cancel=cancel,
            on_text=on_text,
            retry_of=run_id,
        )
    raise WorkbenchError(
        ErrorCode.INVALID_INPUT, "该任务类型不支持在此重试。", {"task": run.task.value}
    )

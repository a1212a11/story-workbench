"""上下文读取、任务阶段、流保存、检查与取消的用例。

模型请求在数据库事务外进行；只有已落库的流块才算可恢复；取消先落库再通知客户端；
迟到结果按 run 状态拒绝；同项目最多一个模型请求在途。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace

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
    AsyncCompleteCall,
    AsyncStreamCall,
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
    async_stream: AsyncStreamCall | None = None
    async_complete: AsyncCompleteCall | None = None


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
    foreshadow = tuple(
        ctx.ForeshadowSource(
            event.id,
            event.logical_id,
            event.stage,
            event.body,
            number_of[event.chapter_id],
        )
        for event in database.list_foreshadow_events(conn, novel_id)
        if event.chapter_id in number_of
    )
    reader_history: list[ctx.ReaderHistorySource] = []
    publications = database.list_publications(conn, novel_id)
    publications_by_id = {publication.id: publication for publication in publications}
    for publication in publications:
        if publication.chapter_id not in number_of:
            continue
        number = number_of[publication.chapter_id]
        version = database.get_version(conn, novel_id, publication.version_id)
        reader_history.append(
            ctx.ReaderHistorySource(
                history_id=publication.id,
                kind="publication_text",
                body=f"[publication] 第{number}章已发布正文：\n{version.body}",
                chapter_number=number,
                version_id=version.id,
            )
        )
        snapshot = database.get_snapshot(conn, novel_id, publication.ready_snapshot_id)
        snapshot_facts = database.list_snapshot_fact_details(conn, novel_id, snapshot.id)
        fact_lines = []
        for fact in snapshot_facts:
            line = f"[{fact.kind.value}] {fact.proposition}"
            if fact.subject:
                line = f"{line}（主体：{fact.subject}）"
            if fact.story_time:
                line = f"{line}（故事时间：{fact.story_time}）"
            fact_lines.append(line)
        if fact_lines:
            reader_history.append(
                ctx.ReaderHistorySource(
                    history_id=f"{publication.id}:snapshot",
                    kind="publication_snapshot",
                    body=f"[publication_snapshot] 第{number}章发布时故事状态：\n"
                    + "\n".join(fact_lines),
                    chapter_number=number,
                    snapshot_id=snapshot.id,
                )
            )
    for event in database.list_foreshadow_events(conn, novel_id):
        if event.chapter_id not in number_of:
            continue
        number = number_of[event.chapter_id]
        quote = f"；原文：{event.quote}" if event.quote else ""
        reader_history.append(
            ctx.ReaderHistorySource(
                history_id=event.id,
                kind="foreshadow",
                body=f"[foreshadow:{event.stage.value}] 第{number}章：{event.body}{quote}",
                chapter_number=number,
            )
        )
    for publication in publications:
        if publication.chapter_id not in number_of:
            continue
        publication_number = number_of[publication.chapter_id]
        if publication_number >= target.number:
            continue
        for plan in database.list_remediation_plans(conn, novel_id, publication.id):
            plan_publication = publications_by_id.get(plan.publication_id)
            if (
                plan_publication is None
                or plan.chapter_id != plan_publication.chapter_id
                or plan.chapter_id not in number_of
            ):
                continue
            number = number_of[plan.chapter_id]
            if number >= target.number:
                continue
            reader_history.append(
                ctx.ReaderHistorySource(
                    history_id=plan.id,
                    kind="remediation",
                    body=f"[remediation:{plan.status}] 第{number}章补救计划，尚未成为正文事实："
                    f"{plan.body}",
                    chapter_number=number,
                )
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
        foreshadow=foreshadow,
        reader_history=tuple(reader_history),
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


def _reserve_required_input(config: GenerationConfig, *texts: str | None) -> GenerationConfig:
    required = sum(config.estimate_tokens(text) for text in texts if text)
    if required == 0:
        return config
    remaining = config.limits.available - required
    if remaining <= 0:
        raise WorkbenchError(
            ErrorCode.CONTEXT_TOO_LARGE,
            "本次必需输入已超出上下文容量，不能发起模型请求。",
            {"required_input_tokens": required, "available_tokens": config.limits.available},
        )
    limits = replace(config.limits, total_tokens=config.limits.total_tokens - required)
    return replace(config, limits=limits)


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


def _fail_run(project: Project, run_id: str, code: str) -> None:
    if database.get_run(project.conn, project.novel_id, run_id).status is RunStatus.RUNNING:
        database.commit_run_status(
            project.conn, project.novel_id, run_id, RunStatus.FAILED, error_code=code
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
    try:
        try:
            for event in events:
                if event.kind is EventKind.TEXT:
                    if not database.commit_run_chunk(
                        project.conn, project.novel_id, run_id, event.text
                    ):
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
        finally:
            close = getattr(events, "close", None)
            if close is not None:
                close()
    except Exception as exc:
        _fail_run(project, run_id, "MODEL_FAILED")
        if isinstance(exc, WorkbenchError):
            raise
        raise WorkbenchError(ErrorCode.MODEL_FAILED, "模型请求失败。") from None
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
    as_candidate: bool = False,
    input_target: Target | None = None,
) -> DraftResult:
    """正式写作或试写：对账、构建上下文、保存依据与 run、流式生成、保存结果、独立检查。

    不采用。缺口与假设在生成前已由 build_context 计算并写入 run.request_json。
    检查失败保留正文与失败原因，返回 check_id=None。
    as_candidate=True 时结果只作候选稿，不替换当前编辑稿（用于诊断后的试改）。
    """
    projects.reconcile_workcopy(project, chapter_id)
    _ensure_idle(project)
    chapter = database.get_chapter(project.conn, project.novel_id, chapter_id)
    input_version_id = chapter.current_draft_id
    input_body: str | None = None
    if input_target is not None:
        if input_target.novel_id != project.novel_id or input_target.chapter_id != chapter_id:
            raise WorkbenchError(ErrorCode.WRONG_SCOPE, "稿件不属于该章。")
        input_version = database.get_version(
            project.conn, project.novel_id, input_target.version_id
        )
        if input_version.chapter_id != chapter_id:
            raise WorkbenchError(ErrorCode.WRONG_SCOPE, "稿件不属于该章。")
        input_version_id = input_version.id
        input_body = input_version.body
    config = _reserve_required_input(config, input_body, *assumptions)
    draft = build_context(project, chapter_id, mode, config)
    purpose = "exploratory" if mode is WriteMode.EXPLORATORY else "formal"
    basis_id = save_basis(project, draft, purpose)
    request = _request(
        TaskKind.GENERATE,
        basis_id,
        source_message_id,
        input_version_id=input_version_id,
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
        input_version_id=input_version_id,
        retry_of_id=retry_of,
        request_json=_request_json(
            request,
            {
                "mode": mode.value,
                "as_candidate": as_candidate,
                "revision": input_target is not None,
            },
        ),
    )
    cancel = cancel or CancelSignal()
    try:
        events = tasks.generate(
            request,
            draft,
            clients.stream,
            input_body=input_body,
            options=config.options,
            cancel=cancel,
        )
        body, usage_json, final = _run_stream(project, run_id, events, on_text)
    except WorkbenchError as exc:
        _fail_run(project, run_id, exc.code.value)
        raise
    except Exception as exc:
        _fail_run(project, run_id, "MODEL_FAILED")
        raise WorkbenchError(
            ErrorCode.MODEL_FAILED,
            "模型请求失败。",
            {"run_id": run_id, "error_type": type(exc).__name__},
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
        project.conn,
        project.novel_id,
        run_id,
        body,
        chapter.current_draft_id,
        usage_json,
        move_pointer=not as_candidate,
    )
    if result is None:
        raise WorkbenchError(
            ErrorCode.MODEL_FAILED, "本次生成已取消，结果未保存为草稿。", {"run_id": run_id}
        )
    target = Target(project.novel_id, chapter_id, result.version_id)
    check_id: str | None
    try:
        check_id = check_draft(
            project,
            target,
            source_message_id,
            clients,
            config,
            basis_id=basis_id,
            context=draft,
            cancel=cancel,
        )
    except WorkbenchError:
        check_id = None
    return DraftResult(
        version_id=result.version_id,
        run_id=run_id,
        based_on_old_inputs=not result.became_current and not as_candidate,
        check_id=check_id,
    )


def diagnose_draft(
    project: Project,
    target: Target,
    author_feedback: str,
    clients: Clients,
    config: GenerationConfig,
    *,
    cancel: CancelSignal | None = None,
) -> tuple[str, tasks.DiagnosisPayload]:
    """按作者请求做阅读体验诊断：返回问题证据、修改选项与需授权的新设定；不生成改稿。

    作者反馈只作为本次稿件的消息保存，不升级为整书规则。返回 (作者消息 ID, 诊断)。
    """
    if target.novel_id != project.novel_id:
        raise WorkbenchError(ErrorCode.WRONG_SCOPE, "稿件不属于当前作品。")
    version = database.get_version(project.conn, project.novel_id, target.version_id)
    if version.chapter_id != target.chapter_id:
        raise WorkbenchError(ErrorCode.WRONG_SCOPE, "稿件不属于该章。")
    author_message_id = database.commit_message(
        project.conn, project.novel_id, MessageRole.AUTHOR.value, author_feedback, target.chapter_id
    )
    _ensure_idle(project)
    config = _reserve_required_input(config, version.body, author_feedback)
    context = build_context(project, target.chapter_id, WriteMode.EXPLORATORY, config)
    basis_id = save_basis(project, context, "diagnose")
    request = _request(
        TaskKind.DIAGNOSE,
        basis_id,
        author_message_id,
        input_version_id=target.version_id,
        output_limit=config.options.max_output_tokens,
    )
    run_id = database.commit_run(
        project.conn,
        project.novel_id,
        chapter_id=target.chapter_id,
        task=TaskKind.DIAGNOSE,
        source_message_id=author_message_id,
        template_version=prompts.TEMPLATE_VERSION,
        status=RunStatus.RUNNING,
        basis_id=basis_id,
        input_version_id=target.version_id,
        request_json=_request_json(request, {"feedback": author_feedback}),
    )
    try:
        payload = tasks.diagnose(
            request,
            context,
            version.body,
            clients.complete,
            author_feedback=author_feedback,
            options=config.options,
            cancel=cancel or CancelSignal(),
        )
    except WorkbenchError as exc:
        _fail_run(project, run_id, exc.code.value)
        raise
    except Exception as exc:
        _fail_run(project, run_id, "MODEL_FAILED")
        raise WorkbenchError(
            ErrorCode.MODEL_FAILED,
            "模型请求失败。",
            {"run_id": run_id, "error_type": type(exc).__name__},
        ) from exc
    run = database.get_run(project.conn, project.novel_id, run_id)
    if run.status is not RunStatus.RUNNING:
        raise WorkbenchError(ErrorCode.MODEL_FAILED, "诊断已取消。", {"run_id": run_id})
    summary = _diagnosis_text(payload)
    database.commit_message(
        project.conn, project.novel_id, MessageRole.MODEL.value, summary, target.chapter_id
    )
    database.commit_run_status(
        project.conn,
        project.novel_id,
        run_id,
        RunStatus.SUCCEEDED,
        usage_json=json.dumps(payload.usage),
    )
    return author_message_id, payload


def analyze_impact(
    project: Project,
    source_adoption_id: str,
    source_message_id: str,
    clients: Clients,
    config: GenerationConfig,
    *,
    cancel: CancelSignal | None = None,
) -> tuple[str, tasks.ImpactReport]:
    """分析一次前章重新采用对后续已采用正文的影响；结果保存为 pending impact。"""
    _ensure_idle(project)
    adoption = database.get_adoption(project.conn, project.novel_id, source_adoption_id)
    source_chapter = database.get_chapter(project.conn, project.novel_id, adoption.chapter_id)
    later_versions: dict[int, tuple[str, str]] = {}
    chapter_ids: dict[int, str] = {}
    for chapter in database.list_chapters(project.conn, project.novel_id):
        if chapter.number <= source_chapter.number:
            continue
        version_id = database.get_adopted_version_id(project.conn, project.novel_id, chapter)
        if version_id is None:
            continue
        version = database.get_version(project.conn, project.novel_id, version_id)
        later_versions[chapter.number] = (version.id, version.body)
        chapter_ids[chapter.number] = chapter.id
    if not later_versions:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "没有后续已采用正文可分析。")
    config = _reserve_required_input(config, *(body for _vid, body in later_versions.values()))
    context = build_context(project, source_chapter.id, WriteMode.EXPLORATORY, config)
    basis_id = save_basis(project, context, "impact")
    request = _request(
        TaskKind.IMPACT,
        basis_id,
        source_message_id,
        output_limit=config.options.max_output_tokens,
    )
    run_id = database.commit_run(
        project.conn,
        project.novel_id,
        chapter_id=source_chapter.id,
        task=TaskKind.IMPACT,
        source_message_id=source_message_id,
        template_version=prompts.TEMPLATE_VERSION,
        status=RunStatus.RUNNING,
        basis_id=basis_id,
        request_json=_request_json(request, {"source_adoption_id": source_adoption_id}),
    )
    try:
        report, usage = tasks.impact(
            request,
            context,
            source_chapter.number,
            later_versions,
            clients.complete,
            options=config.options,
            cancel=cancel or CancelSignal(),
        )
        run = database.get_run(project.conn, project.novel_id, run_id)
        if run.status is not RunStatus.RUNNING:
            raise WorkbenchError(ErrorCode.MODEL_FAILED, "影响分析已取消。", {"run_id": run_id})
        for chapter in report.chapters:
            evidence = tuple(
                (item.version_id, item.start, item.end, item.quote) for item in chapter.evidence
            )
            database.commit_impact_analysis(
                project.conn,
                project.novel_id,
                target_chapter_id=chapter_ids[chapter.chapter_number],
                classification=chapter.status,
                summary=chapter.reason,
                source_adoption_id=source_adoption_id,
                run_id=run_id,
                evidence=evidence,
                coverage_checked=report.coverage_checked,
                coverage_unchecked=report.coverage_unchecked,
            )
        findings = tuple(
            (chapter_ids[f.chapter_number], f.reason, f.evidence) for f in report.findings
        )
        if findings:
            database.commit_impact_findings(
                project.conn, project.novel_id, source_adoption_id, findings
            )
    except WorkbenchError as exc:
        _fail_run(project, run_id, exc.code.value)
        raise
    except Exception as exc:
        _fail_run(project, run_id, "MODEL_FAILED")
        raise WorkbenchError(
            ErrorCode.MODEL_FAILED,
            "影响分析请求失败。",
            {"run_id": run_id, "error_type": type(exc).__name__},
        ) from exc
    database.commit_run_status(
        project.conn,
        project.novel_id,
        run_id,
        RunStatus.SUCCEEDED,
        usage_json=json.dumps(usage),
    )
    return run_id, report


def propose_remediation(
    project: Project,
    chapter_id: str,
    prompt: str,
    clients: Clients,
    config: GenerationConfig,
    *,
    cancel: CancelSignal | None = None,
    on_text: OnText | None = None,
) -> tuple[str, str]:
    """为已发布或待复核冲突讨论补救方案；只是讨论消息，不写正式事实。"""
    text = (
        "请提出后续补救方案。不要删除读者已看到的已发布正文；方案被作者采用前只是建议。\n" + prompt
    )
    return discuss(project, chapter_id, text, clients, config, cancel=cancel, on_text=on_text)


def _diagnosis_text(payload: tasks.DiagnosisPayload) -> str:
    lines = [f"诊断：{payload.verdict}"]
    for problem in payload.problems:
        quotes = "；".join(e.quote for e in problem.evidence)
        lines.append(f"问题：{problem.explanation}（原文：{quotes}）")
    for option in payload.options:
        flag = "（需新增设定授权）" if option.requires_new_setting else ""
        lines.append(f"可选修改：{option.description}{flag}")
    for setting in payload.new_settings_requiring_authorization:
        lines.append(f"需授权的新设定：{setting}")
    return "\n".join(lines)


def revise_draft(
    project: Project,
    target: Target,
    chosen_option: str,
    source_message_id: str,
    clients: Clients,
    config: GenerationConfig,
    *,
    cancel: CancelSignal | None = None,
    on_text: OnText | None = None,
) -> DraftResult:
    """作者选定修改方向后生成候选稿：试写模式、结果不替换当前编辑稿、不采用。"""
    if target.novel_id != project.novel_id:
        raise WorkbenchError(ErrorCode.WRONG_SCOPE, "稿件不属于当前作品。")
    version = database.get_version(project.conn, project.novel_id, target.version_id)
    if version.chapter_id != target.chapter_id:
        raise WorkbenchError(ErrorCode.WRONG_SCOPE, "稿件不属于该章。")
    return write_chapter(
        project,
        target.chapter_id,
        WriteMode.EXPLORATORY,
        source_message_id,
        clients,
        config,
        assumptions=(f"按作者选定的修改方向重写：{chosen_option}",),
        cancel=cancel,
        on_text=on_text,
        as_candidate=True,
        input_target=target,
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
        _fail_run(project, run_id, exc.code.value)
        raise
    except Exception as exc:
        _fail_run(project, run_id, "MODEL_FAILED")
        raise WorkbenchError(
            ErrorCode.MODEL_FAILED, "模型检查请求失败。", {"error_type": type(exc).__name__}
        ) from None
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
    try:
        events = tasks.discuss(
            request,
            draft,
            author_text,
            clients.stream,
            options=config.options,
            cancel=cancel or CancelSignal(),
        )
        body, usage_json, final = _run_stream(project, run_id, events, on_text)
    except Exception as exc:
        _fail_run(project, run_id, "MODEL_FAILED")
        if isinstance(exc, WorkbenchError):
            raise
        raise WorkbenchError(ErrorCode.MODEL_FAILED, "讨论请求失败。") from None
    run = database.get_run(project.conn, project.novel_id, run_id)
    if final is not EventKind.COMPLETED or run.status is not RunStatus.RUNNING:
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
            as_candidate=payload.get("as_candidate", False),
            input_target=Target(project.novel_id, run.chapter_id, run.input_version_id)
            if payload.get("revision") and run.input_version_id
            else None,
        )
    raise WorkbenchError(
        ErrorCode.INVALID_INPUT, "该任务类型不支持在此重试。", {"task": run.task.value}
    )

"""四类 AI 任务：discuss、generate、inspect、diagnose。

只构建消息、通过传入的客户端函数调用模型、解析并校验输出；不保存、不采用、不控制 run。
Pydantic 只用于校验模型返回的 JSON 载荷（strict、拒绝未声明字段）；对外返回的仍是 dataclass。
证据区间用 domain/rules.validate_evidence_span 复核；任何校验失败都记 MODEL_FAILED，
details 只带定位与诊断（字段路径、区间、长度），不带正文或引用全文。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from story_workbench.ai import prompts
from story_workbench.ai.context import ContextDraft
from story_workbench.domain.records import (
    ErrorCode,
    FactKind,
    IssueKind,
    TaskKind,
    TaskRequest,
    WorkbenchError,
    WriteMode,
)
from story_workbench.domain.rules import validate_evidence_span
from story_workbench.infrastructure.provider import (
    CallOptions,
    CancelSignal,
    ChatMessage,
    CompleteCall,
    CompleteResult,
    StreamCall,
    StreamEvent,
)

# ---------------------------------------------------------------- 对外记录（dataclass）


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    start: int
    end: int
    quote: str


@dataclass(frozen=True, slots=True)
class IssueFinding:
    kind: IssueKind
    explanation: str
    proposed_change: str
    evidence: tuple[EvidenceSpan, ...]


@dataclass(frozen=True, slots=True)
class CandidateFact:
    kind: FactKind
    proposition: str
    start: int
    end: int
    quote: str
    subject: str | None = None
    story_time: str | None = None


@dataclass(frozen=True, slots=True)
class Coverage:
    checked: tuple[str, ...]
    unchecked: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InspectionPayload:
    """通过结构与证据校验的检查结果；generation 负责落库为 check_result/issue/candidate_fact。"""

    issues: tuple[IssueFinding, ...]
    candidate_facts: tuple[CandidateFact, ...]
    actual_plot: str
    actual_ending: str
    coverage: Coverage
    template_version: str
    usage: dict[str, int]


@dataclass(frozen=True, slots=True)
class ReadingProblem:
    explanation: str
    evidence: tuple[EvidenceSpan, ...]


@dataclass(frozen=True, slots=True)
class RevisionOption:
    description: str
    requires_new_setting: bool


@dataclass(frozen=True, slots=True)
class DiagnosisPayload:
    """阅读体验诊断：问题证据、修改选项、需授权的新设定；不含改稿。"""

    verdict: str
    problems: tuple[ReadingProblem, ...]
    options: tuple[RevisionOption, ...]
    new_settings_requiring_authorization: tuple[str, ...]
    template_version: str
    usage: dict[str, int]


# ---------------------------------------------------------------- 载荷校验模型（Pydantic）


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class _EvidenceModel(_Strict):
    # 兼容旧模型响应的下标字段；定位仅以唯一原文引用为准。
    start: int = 0
    end: int = 0
    quote: str


class _IssueModel(_Strict):
    kind: IssueKind
    explanation: str
    proposed_change: str
    evidence: list[_EvidenceModel] = Field(min_length=1)


class _CandidateFactModel(_Strict):
    kind: FactKind
    subject: str | None
    proposition: str
    story_time: str | None
    start: int = 0
    end: int = 0
    quote: str


class _CoverageModel(_Strict):
    checked: list[str]
    unchecked: list[str]


class _InspectionModel(_Strict):
    issues: list[_IssueModel]
    candidate_facts: list[_CandidateFactModel]
    actual_plot: str
    actual_ending: str
    coverage: _CoverageModel


class _ProblemModel(_Strict):
    explanation: str
    evidence: list[_EvidenceModel] = Field(min_length=1)


class _OptionModel(_Strict):
    description: str
    requires_new_setting: bool


class _DiagnosisModel(_Strict):
    verdict: str
    problems: list[_ProblemModel]
    options: list[_OptionModel]
    new_settings_requiring_authorization: list[str]


# ---------------------------------------------------------------- 公共检查


def _require_task(request: TaskRequest, expected: TaskKind) -> None:
    if request.task is not expected:
        raise WorkbenchError(
            ErrorCode.INVALID_INPUT,
            "任务类型与请求不一致。",
            {"expected": expected.value, "actual": request.task.value},
        )
    if request.template_version != prompts.TEMPLATE_VERSION:
        raise WorkbenchError(
            ErrorCode.INVALID_INPUT,
            "请求记录的模板版本与当前模板不一致。",
            {"expected": prompts.TEMPLATE_VERSION, "actual": request.template_version},
        )


def _require_scope(request: TaskRequest, context: ContextDraft) -> None:
    """委托范围本期只允许本次请求本章；不接受跨章或从上次任务继承。"""
    scope = request.delegation_scope
    if scope is None:
        return
    if scope.chapter_id != context.chapter_id:
        raise WorkbenchError(
            ErrorCode.WRONG_SCOPE,
            "委托范围只能是本章。",
            {"scope_chapter_id": scope.chapter_id, "context_chapter_id": context.chapter_id},
        )
    if scope.request_message_id != request.source_message_id:
        raise WorkbenchError(
            ErrorCode.WRONG_SCOPE,
            "委托范围只对本次请求有效，不能沿用上次任务的授权。",
            {
                "scope_message_id": scope.request_message_id,
                "request_message_id": request.source_message_id,
            },
        )


def _options_for(request: TaskRequest, options: CallOptions) -> CallOptions:
    if request.output_limit > 0 and request.output_limit != options.max_output_tokens:
        return CallOptions(
            model=options.model,
            max_output_tokens=request.output_limit,
            timeout_seconds=options.timeout_seconds,
            temperature=options.temperature,
        )
    return options


def render_context(context: ContextDraft) -> str:
    """把 ContextDraft 的非空分区按固定标题拼成提示正文。"""
    blocks = [
        f"## {prompts.SECTION_TITLES[name]}\n{text}" for name, text in context.sections().items()
    ]
    return "\n\n".join(blocks)


# ---------------------------------------------------------------- 消息构建（纯函数，便于测试）


def build_generate_messages(
    request: TaskRequest, context: ContextDraft, input_body: str | None = None
) -> list[ChatMessage]:
    _require_task(request, TaskKind.GENERATE)
    _require_scope(request, context)
    if context.mode is WriteMode.FORMAL:
        if request.missing_inputs or context.missing_inputs:
            raise WorkbenchError(
                ErrorCode.INVALID_INPUT,
                "正式写作不允许缺口；请补齐计划或改为试写。",
                {"missing": sorted(set(request.missing_inputs) | set(context.missing_inputs))},
            )
        system = prompts.GENERATE_SYSTEM_FORMAL
        mode_line = "正式写作，按计划落到指定结尾。"
        gap_block = ""
    else:
        system = prompts.GENERATE_SYSTEM_EXPLORATORY
        mode_line = "试写，结果需作者采用时一并接受实际剧情与结尾。"
        missing = tuple(dict.fromkeys((*context.missing_inputs, *request.missing_inputs)))
        lines = ["### 尚未由作者确定（不得替作者确认）"]
        lines.extend(f"- {item}" for item in missing or ("无",))
        lines.append("### 本次试写采用的假设（只对本次本章有效）")
        lines.extend(f"- {item}" for item in request.assumptions or ("无",))
        gap_block = "\n".join(lines) + "\n\n"
    if input_body is not None:
        system += prompts.REVISION_SYSTEM
    scope = request.delegation_scope
    if scope and scope.may_choose_plot:
        delegation_line = (
            "作者允许你在本次、仅限本章内选择具体情节走向；不得改变作者决定或新增设定。"
        )
    else:
        delegation_line = "没有情节选择的委托；不确定的地方保持克制，不替作者决定。"
    user = prompts.GENERATE_USER.format(
        context=render_context(context),
        input_body_block=""
        if input_body is None
        else f"\n## 待改正文（第{context.chapter_number}章）\n{input_body}\n",
        chapter_number=context.chapter_number,
        mode_line=mode_line,
        delegation_line=delegation_line,
        gap_block=gap_block,
    )
    return [ChatMessage("system", system), ChatMessage("user", user)]


def build_discuss_messages(
    request: TaskRequest, context: ContextDraft, author_text: str
) -> list[ChatMessage]:
    _require_task(request, TaskKind.DISCUSS)
    _require_scope(request, context)
    if not author_text.strip():
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "讨论内容不能为空。")
    user = prompts.DISCUSS_USER.format(context=render_context(context), author_text=author_text)
    return [ChatMessage("system", prompts.DISCUSS_SYSTEM), ChatMessage("user", user)]


def build_inspect_messages(
    request: TaskRequest, context: ContextDraft, body: str
) -> list[ChatMessage]:
    _require_task(request, TaskKind.INSPECT)
    _require_scope(request, context)
    if not body:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "待检查正文为空。")
    user = prompts.INSPECT_USER.format(
        context=render_context(context), chapter_number=context.chapter_number, body=body
    )
    return [ChatMessage("system", prompts.INSPECT_SYSTEM), ChatMessage("user", user)]


def build_diagnose_messages(
    request: TaskRequest, context: ContextDraft, body: str, author_feedback: str = ""
) -> list[ChatMessage]:
    _require_task(request, TaskKind.DIAGNOSE)
    _require_scope(request, context)
    if not body:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "待诊断正文为空。")
    user = prompts.DIAGNOSE_USER.format(
        context=render_context(context),
        author_feedback_block=""
        if not author_feedback
        else f"\n## 作者本次反馈\n{author_feedback}\n",
        chapter_number=context.chapter_number,
        body=body,
    )
    return [ChatMessage("system", prompts.DIAGNOSE_SYSTEM), ChatMessage("user", user)]


# ---------------------------------------------------------------- 流式任务


def generate(
    request: TaskRequest,
    context: ContextDraft,
    stream_call: StreamCall,
    *,
    input_body: str | None = None,
    options: CallOptions,
    cancel: CancelSignal | None = None,
) -> Iterator[StreamEvent]:
    """正式写作或试写：返回标准文本事件流。不保存、不采用；generation 持久化流块。"""
    messages = build_generate_messages(request, context, input_body)
    return stream_call(messages, _options_for(request, options), cancel or CancelSignal())


def discuss(
    request: TaskRequest,
    context: ContextDraft,
    author_text: str,
    stream_call: StreamCall,
    *,
    options: CallOptions,
    cancel: CancelSignal | None = None,
) -> Iterator[StreamEvent]:
    """讨论：只返回文本流，不抽取结构化想法（首期决定）。"""
    messages = build_discuss_messages(request, context, author_text)
    return stream_call(messages, _options_for(request, options), cancel or CancelSignal())


# ---------------------------------------------------------------- 结构化任务


_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*)\n\s*```\s*$", re.DOTALL)


def _model_failed(message: str, details: dict[str, Any]) -> WorkbenchError:
    return WorkbenchError(ErrorCode.MODEL_FAILED, message, details)


def _safe_validation_details(exc: ValidationError) -> list[dict[str, Any]]:
    """只保留字段路径与错误类型；丢弃可能含正文的 input 与 msg 细节。"""
    return [
        {"loc": [str(part) for part in err["loc"]], "type": err["type"]}
        for err in exc.errors(include_url=False, include_input=False)
    ]


def _parse_payload(result: CompleteResult, model: type[BaseModel], task: TaskKind) -> BaseModel:
    if result.truncated:
        raise _model_failed(
            "模型输出被截断，检查结果不完整。",
            {"task": task.value, "reason": "truncated", "output_length": len(result.text)},
        )
    text = result.text.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        raise _model_failed(
            "模型没有返回合法 JSON。",
            {"task": task.value, "reason": "invalid_json", "position": exc.pos},
        ) from exc
    try:
        return model.model_validate_json(text, strict=True)
    except ValidationError as exc:
        raise _model_failed(
            "模型返回的结构不符合检查合同。",
            {
                "task": task.value,
                "reason": "schema_violation",
                "errors": _safe_validation_details(exc),
            },
        ) from exc


def _resolve_span(body: str, span: _EvidenceModel | _CandidateFactModel, loc: str) -> EvidenceSpan:
    """只定位非空、唯一、逐字匹配的引用；包括重叠重复，不使用模型猜测的下标。"""
    start = body.find(span.quote)
    cause = None
    if not span.quote:
        cause = "empty_quote"
    elif start < 0:
        cause = "quote_not_found"
    elif body.find(span.quote, start + 1) >= 0:
        cause = "ambiguous_quote"
    if cause:
        raise _model_failed(
            "模型引用无法唯一对应正文，请提供逐字相同且能唯一定位的原文。",
            {"reason": "evidence_mismatch", "cause": cause, "loc": loc},
        )
    end = start + len(span.quote)
    validate_evidence_span(body, start, end, span.quote)
    return EvidenceSpan(start, end, span.quote)


def inspect(
    request: TaskRequest,
    context: ContextDraft,
    body: str,
    complete_call: CompleteCall,
    *,
    options: CallOptions,
    cancel: CancelSignal | None = None,
) -> InspectionPayload:
    """检查与候选事实提取。失败不返回有效检查（MODEL_FAILED）；generation 负责落库。"""
    messages = build_inspect_messages(request, context, body)
    result = complete_call(messages, _options_for(request, options), cancel or CancelSignal())
    parsed = _parse_payload(result, _InspectionModel, TaskKind.INSPECT)
    assert isinstance(parsed, _InspectionModel)

    issues: list[IssueFinding] = []
    for i, issue in enumerate(parsed.issues):
        evidence: list[EvidenceSpan] = []
        for j, span in enumerate(issue.evidence):
            evidence.append(_resolve_span(body, span, f"issues[{i}].evidence[{j}]"))
        issues.append(
            IssueFinding(issue.kind, issue.explanation, issue.proposed_change, tuple(evidence))
        )
    facts: list[CandidateFact] = []
    for i, fact in enumerate(parsed.candidate_facts):
        span = _resolve_span(body, fact, f"candidate_facts[{i}]")
        facts.append(
            CandidateFact(
                kind=fact.kind,
                proposition=fact.proposition,
                start=span.start,
                end=span.end,
                quote=fact.quote,
                subject=fact.subject,
                story_time=fact.story_time,
            )
        )
    return InspectionPayload(
        issues=tuple(issues),
        candidate_facts=tuple(facts),
        actual_plot=parsed.actual_plot,
        actual_ending=parsed.actual_ending,
        coverage=Coverage(tuple(parsed.coverage.checked), tuple(parsed.coverage.unchecked)),
        template_version=request.template_version,
        usage=dict(result.usage),
    )


def diagnose(
    request: TaskRequest,
    context: ContextDraft,
    body: str,
    complete_call: CompleteCall,
    *,
    author_feedback: str = "",
    options: CallOptions,
    cancel: CancelSignal | None = None,
) -> DiagnosisPayload:
    """阅读体验诊断：问题证据、修改选项、需授权的新设定；不生成改稿。"""
    messages = build_diagnose_messages(request, context, body, author_feedback)
    result = complete_call(messages, _options_for(request, options), cancel or CancelSignal())
    parsed = _parse_payload(result, _DiagnosisModel, TaskKind.DIAGNOSE)
    assert isinstance(parsed, _DiagnosisModel)

    problems: list[ReadingProblem] = []
    for i, problem in enumerate(parsed.problems):
        evidence: list[EvidenceSpan] = []
        for j, span in enumerate(problem.evidence):
            evidence.append(_resolve_span(body, span, f"problems[{i}].evidence[{j}]"))
        problems.append(ReadingProblem(problem.explanation, tuple(evidence)))
    return DiagnosisPayload(
        verdict=parsed.verdict,
        problems=tuple(problems),
        options=tuple(
            RevisionOption(o.description, o.requires_new_setting) for o in parsed.options
        ),
        new_settings_requiring_authorization=tuple(parsed.new_settings_requiring_authorization),
        template_version=request.template_version,
        usage=dict(result.usage),
    )

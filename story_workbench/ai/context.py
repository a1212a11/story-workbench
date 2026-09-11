"""纯上下文构建：章节过滤、来源选择、摘录、容量分配与遗漏清单。

输入是 generation 已查询好的候选来源记录（本文件定义的输入记录，不进 domain），
输出是尚无 basis_id 的 ContextDraft。本模块不查询数据库、不调用模型、不读时钟。

隔离规则（架构第 8 节、T08）：
- 拒绝稿和未采用稿不进入；只有已采用正文可作历史。
- 章号 >= 目标章的正文与事实一律排除（第三章知识不进第二章）。
- 只有上一章的 ready 快照可作前章状态；其他快照的事实排除。
- 只有作者决定（authority=author_decision 且 lifecycle=decided）成为规则；
  候选、草稿、AI 建议、已拒绝、已归档不进入。
- 指向目标章之后的作者决定只进作者意图区，不进规则或事实区。
- 容量不足先裁讨论，再裁可选正文与问题；规则、计划、上一章快照仍超限时报错。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from story_workbench.domain.records import (
    Authority,
    ErrorCode,
    FactKind,
    IssueKind,
    Lifecycle,
    MessageRole,
    SnapshotStatus,
    WorkbenchError,
    WriteMode,
)
from story_workbench.domain.rules import validate_plan_inputs

# ---------------------------------------------------------------- 输入记录


class SourceRole(StrEnum):
    """basis_source.role：区分规则、事实、作者意图、讨论与正文。"""

    RULE = "rule"
    FACT = "fact"
    INTENT = "intent"
    DISCUSSION = "discussion"
    TEXT = "text"


class TextStatus(StrEnum):
    ADOPTED = "adopted"
    DRAFT = "draft"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class StoryItemSource:
    """作者规则、决定或候选想法；chapter_number 为 None 表示全书适用。"""

    story_item_id: str
    body: str
    authority: Authority
    lifecycle: Lifecycle
    chapter_number: int | None = None


@dataclass(frozen=True, slots=True)
class PlanSource:
    plan_id: str
    chapter_number: int
    plot: str | None
    ending: str | None


@dataclass(frozen=True, slots=True)
class FactSource:
    """快照中的一条事实；fact_revision_id 为作者纠正后的有效修订，否则引用候选。"""

    snapshot_id: str
    chapter_number: int
    kind: FactKind
    proposition: str
    fact_revision_id: str | None = None
    candidate_id: str | None = None
    subject: str | None = None
    story_time: str | None = None


@dataclass(frozen=True, slots=True)
class TextSource:
    version_id: str
    chapter_number: int
    body: str
    status: TextStatus


@dataclass(frozen=True, slots=True)
class IssueSource:
    issue_id: str
    chapter_number: int
    kind: IssueKind
    explanation: str
    resolved: bool = False


@dataclass(frozen=True, slots=True)
class MessageSource:
    message_id: str
    role: MessageRole
    body: str
    chapter_number: int | None = None


@dataclass(frozen=True, slots=True)
class ContextSources:
    """generation 查询到的候选来源全集；过滤由 build_context 负责。"""

    novel_id: str
    chapter_id: str
    design_revision: int
    story_items: tuple[StoryItemSource, ...] = ()
    plans: tuple[PlanSource, ...] = ()
    facts: tuple[FactSource, ...] = ()
    texts: tuple[TextSource, ...] = ()
    issues: tuple[IssueSource, ...] = ()
    messages: tuple[MessageSource, ...] = ()
    previous_snapshot_id: str | None = None
    previous_snapshot_status: SnapshotStatus | None = None


@dataclass(frozen=True, slots=True)
class ContextLimits:
    """容量预算，单位为估算 token；可用量 = 总上限 − 模板预留 − 输出预留。"""

    total_tokens: int
    template_reserve: int
    output_reserve: int

    @property
    def available(self) -> int:
        return self.total_tokens - self.template_reserve - self.output_reserve


# ---------------------------------------------------------------- 输出记录


class OmitReason(StrEnum):
    REJECTED_DRAFT = "rejected_draft"
    UNADOPTED_DRAFT = "unadopted_draft"
    FUTURE_CHAPTER = "future_chapter"
    NOT_PREVIOUS_SNAPSHOT = "not_previous_snapshot"
    NOT_AUTHOR_DECISION = "not_author_decision"
    LIFECYCLE_REJECTED = "lifecycle_rejected"
    LIFECYCLE_ARCHIVED = "lifecycle_archived"
    RESOLVED_ISSUE = "resolved_issue"
    SYSTEM_MESSAGE = "system_message"
    CAPACITY = "capacity"


@dataclass(frozen=True, slots=True)
class SourceCitation:
    """对应 basis_source 一行：恰有一个来源引用，附实际发送的摘录区间与文本。"""

    role: SourceRole
    section: str
    excerpt_start: int
    excerpt_end: int
    quote: str
    version_id: str | None = None
    story_item_id: str | None = None
    snapshot_id: str | None = None
    fact_revision_id: str | None = None
    message_id: str | None = None

    @property
    def source_id(self) -> str:
        for value in (
            self.version_id,
            self.story_item_id,
            self.fact_revision_id,
            self.snapshot_id,
            self.message_id,
        ):
            if value is not None:
                return value
        raise AssertionError("citation without source")  # pragma: no cover


@dataclass(frozen=True, slots=True)
class OmittedSource:
    """对应 basis.omitted_sources_json 的一项。"""

    source_kind: str
    source_id: str
    reason: OmitReason
    chapter_number: int | None = None


@dataclass(frozen=True, slots=True)
class ContextDraft:
    """尚无 basis_id 的上下文草案；generation 保存 basis 后组装 ContextPackage。"""

    novel_id: str
    chapter_id: str
    chapter_number: int
    mode: WriteMode
    design_revision: int
    plan_id: str | None
    previous_snapshot_id: str | None
    missing_inputs: tuple[str, ...]
    rules: str
    plan: str
    previous_state: str
    adopted_text: str
    open_issues: str
    discussion: str
    author_intent: str
    citations: tuple[SourceCitation, ...]
    issue_ids: tuple[str, ...]
    omitted: tuple[OmittedSource, ...]
    estimated_tokens: int
    limits: ContextLimits
    section_tokens: dict[str, int] = field(default_factory=dict)

    def sections(self) -> dict[str, str]:
        """按固定顺序返回非空分区，供任务模板拼接。"""
        ordered = (
            ("rules", self.rules),
            ("plan", self.plan),
            ("previous_state", self.previous_state),
            ("adopted_text", self.adopted_text),
            ("open_issues", self.open_issues),
            ("author_intent", self.author_intent),
            ("discussion", self.discussion),
        )
        return {name: text for name, text in ordered if text}


# ---------------------------------------------------------------- 估算


def default_estimate_tokens(text: str) -> int:
    """保守估算：中文按字符数 × 1.2 向上取整。供应商接入后可注入官方计数。"""
    return math.ceil(len(text) * 1.2)


EstimateTokens = Callable[[str], int]

# ---------------------------------------------------------------- 内部结构


@dataclass(frozen=True, slots=True)
class _Item:
    section: str
    text: str
    citation: SourceCitation | None
    source_kind: str
    chapter_number: int | None
    tokens: int
    issue_id: str | None = None

    @property
    def source_id(self) -> str:
        if self.citation is not None:
            return self.citation.source_id
        assert self.issue_id is not None
        return self.issue_id


def _fact_line(fact: FactSource) -> str:
    parts = [f"[{fact.kind.value}]"]
    if fact.subject:
        parts.append(f"主体：{fact.subject}；")
    parts.append(fact.proposition)
    if fact.story_time:
        parts.append(f"（故事时间：{fact.story_time}）")
    return "".join(parts)


def _make_item(
    section: str,
    role: SourceRole,
    text: str,
    source_kind: str,
    chapter_number: int | None,
    estimate: EstimateTokens,
    **ref: str | None,
) -> _Item:
    citation = SourceCitation(
        role=role,
        section=section,
        excerpt_start=0,
        excerpt_end=len(text),
        quote=text,
        **ref,
    )
    return _Item(section, text, citation, source_kind, chapter_number, estimate(text))


def _collect(
    sources: ContextSources,
    chapter_number: int,
    estimate: EstimateTokens,
) -> tuple[dict[str, list[_Item]], list[OmittedSource]]:
    """按隔离规则把来源分到各分区，并记录被排除的来源。"""
    buckets: dict[str, list[_Item]] = {
        "rules": [],
        "previous_state": [],
        "adopted_text": [],
        "open_issues": [],
        "author_intent": [],
        "discussion": [],
    }
    omitted: list[OmittedSource] = []

    for item in sources.story_items:
        reason: OmitReason | None = None
        if item.lifecycle is Lifecycle.REJECTED:
            reason = OmitReason.LIFECYCLE_REJECTED
        elif item.lifecycle is Lifecycle.ARCHIVED:
            reason = OmitReason.LIFECYCLE_ARCHIVED
        elif (
            item.lifecycle is not Lifecycle.DECIDED
            or item.authority is not Authority.AUTHOR_DECISION
        ):
            reason = OmitReason.NOT_AUTHOR_DECISION
        if reason is not None:
            omitted.append(
                OmittedSource("story_item", item.story_item_id, reason, item.chapter_number)
            )
            continue
        future_design = item.chapter_number is not None and item.chapter_number > chapter_number
        section = "author_intent" if future_design else "rules"
        text = item.body
        if future_design:
            text = f"（第{item.chapter_number}章的设计，尚未发生，人物不知情）{item.body}"
        buckets[section].append(
            _make_item(
                section,
                SourceRole.INTENT if future_design else SourceRole.RULE,
                text,
                "story_item",
                item.chapter_number,
                estimate,
                story_item_id=item.story_item_id,
            )
        )

    for fact in sources.facts:
        if fact.chapter_number >= chapter_number:
            omitted.append(
                OmittedSource(
                    "snapshot_fact",
                    fact.fact_revision_id or fact.candidate_id or fact.snapshot_id,
                    OmitReason.FUTURE_CHAPTER,
                    fact.chapter_number,
                )
            )
            continue
        if fact.snapshot_id != sources.previous_snapshot_id:
            omitted.append(
                OmittedSource(
                    "snapshot_fact",
                    fact.fact_revision_id or fact.candidate_id or fact.snapshot_id,
                    OmitReason.NOT_PREVIOUS_SNAPSHOT,
                    fact.chapter_number,
                )
            )
            continue
        buckets["previous_state"].append(
            _make_item(
                "previous_state",
                SourceRole.FACT,
                _fact_line(fact),
                "snapshot_fact",
                fact.chapter_number,
                estimate,
                # basis_source 每行恰有一个引用：有作者纠正则引用修订，否则引用快照本身。
                snapshot_id=None if fact.fact_revision_id else fact.snapshot_id,
                fact_revision_id=fact.fact_revision_id,
            )
        )

    for text_source in sources.texts:
        if text_source.status is TextStatus.REJECTED:
            omitted.append(
                OmittedSource(
                    "version",
                    text_source.version_id,
                    OmitReason.REJECTED_DRAFT,
                    text_source.chapter_number,
                )
            )
            continue
        if text_source.status is not TextStatus.ADOPTED:
            omitted.append(
                OmittedSource(
                    "version",
                    text_source.version_id,
                    OmitReason.UNADOPTED_DRAFT,
                    text_source.chapter_number,
                )
            )
            continue
        if text_source.chapter_number >= chapter_number:
            omitted.append(
                OmittedSource(
                    "version",
                    text_source.version_id,
                    OmitReason.FUTURE_CHAPTER,
                    text_source.chapter_number,
                )
            )
            continue
        buckets["adopted_text"].append(
            _make_item(
                "adopted_text",
                SourceRole.TEXT,
                text_source.body,
                "version",
                text_source.chapter_number,
                estimate,
                version_id=text_source.version_id,
            )
        )

    for issue in sources.issues:
        if issue.resolved:
            omitted.append(
                OmittedSource(
                    "issue", issue.issue_id, OmitReason.RESOLVED_ISSUE, issue.chapter_number
                )
            )
            continue
        if issue.chapter_number >= chapter_number:
            omitted.append(
                OmittedSource(
                    "issue", issue.issue_id, OmitReason.FUTURE_CHAPTER, issue.chapter_number
                )
            )
            continue
        # issue 不是 basis_source 的来源类型：不生成引用，纳入的 ID 记在 ContextDraft.issue_ids。
        text = f"[{issue.kind.value}] 第{issue.chapter_number}章：{issue.explanation}"
        buckets["open_issues"].append(
            _Item(
                "open_issues",
                text,
                None,
                "issue",
                issue.chapter_number,
                estimate(text),
                issue.issue_id,
            )
        )

    for message in sources.messages:
        if message.role is MessageRole.SYSTEM:
            omitted.append(
                OmittedSource(
                    "message", message.message_id, OmitReason.SYSTEM_MESSAGE, message.chapter_number
                )
            )
            continue
        if message.chapter_number is not None and message.chapter_number > chapter_number:
            omitted.append(
                OmittedSource(
                    "message", message.message_id, OmitReason.FUTURE_CHAPTER, message.chapter_number
                )
            )
            continue
        speaker = "作者" if message.role is MessageRole.AUTHOR else "助手"
        text = f"{speaker}：{message.body}"
        buckets["discussion"].append(
            _make_item(
                "discussion",
                SourceRole.DISCUSSION,
                text,
                "message",
                message.chapter_number,
                estimate,
                message_id=message.message_id,
            )
        )

    return buckets, omitted


def _select_plan(
    sources: ContextSources, chapter_number: int, mode: WriteMode
) -> tuple[PlanSource | None, tuple[str, ...], list[OmittedSource]]:
    omitted: list[OmittedSource] = []
    chosen: PlanSource | None = None
    for plan in sources.plans:
        if plan.chapter_number != chapter_number:
            omitted.append(
                OmittedSource("plan", plan.plan_id, OmitReason.FUTURE_CHAPTER, plan.chapter_number)
            )
            continue
        if chosen is not None:
            raise WorkbenchError(
                ErrorCode.INVALID_INPUT,
                "目标章只能有一个当前计划。",
                {"plan_ids": [chosen.plan_id, plan.plan_id]},
            )
        chosen = plan
    plot = chosen.plot if chosen else None
    ending = chosen.ending if chosen else None
    missing = validate_plan_inputs(mode, plot, ending)
    return chosen, missing, omitted


def _render_plan(plan: PlanSource | None, missing: tuple[str, ...]) -> str:
    lines: list[str] = []
    if plan and plan.plot and plan.plot.strip():
        lines.append(f"本章剧情：{plan.plot.strip()}")
    else:
        lines.append("本章剧情：作者尚未确定")
    if plan and plan.ending and plan.ending.strip():
        lines.append(f"结尾状态：{plan.ending.strip()}")
    else:
        lines.append("结尾状态：作者尚未确定")
    if missing:
        lines.append("缺口：" + "、".join(missing))
    return "\n".join(lines)


def _join(items: list[_Item]) -> str:
    return "\n\n".join(item.text for item in items)


# ---------------------------------------------------------------- 公开函数


def build_context(
    sources: ContextSources,
    chapter_number: int,
    mode: WriteMode,
    limits: ContextLimits,
    estimate_tokens: EstimateTokens = default_estimate_tokens,
) -> ContextDraft:
    """把候选来源筛选、分区并按容量装配为 ContextDraft。

    失败合同：
    - 目标章 > 1 而上一章快照不是 ready → STALE_BASIS；
    - 正式写作缺剧情或结尾 → INVALID_INPUT（来自 validate_plan_inputs）；
    - 规则、计划、上一章快照合计超出可用容量 → CONTEXT_TOO_LARGE，details 带各分区用量。
    可选来源（讨论、历史正文、未解决问题）放不下时记入 omitted，原因 capacity。
    """
    if chapter_number < 1:
        raise WorkbenchError(
            ErrorCode.INVALID_INPUT, "章号必须大于零。", {"chapter_number": chapter_number}
        )
    if limits.available <= 0:
        raise WorkbenchError(
            ErrorCode.CONTEXT_TOO_LARGE,
            "模板与输出预留已占满容量，无法放入任何依据。",
            {"total": limits.total_tokens, "available": limits.available},
        )
    if chapter_number > 1 and sources.previous_snapshot_status is not SnapshotStatus.READY:
        raise WorkbenchError(
            ErrorCode.STALE_BASIS,
            "上一章的故事状态尚未就绪，暂不能依据它续写。",
            {
                "chapter_number": chapter_number,
                "previous_snapshot_id": sources.previous_snapshot_id,
                "previous_snapshot_status": (
                    sources.previous_snapshot_status.value
                    if sources.previous_snapshot_status
                    else None
                ),
            },
        )

    plan, missing, plan_omitted = _select_plan(sources, chapter_number, mode)
    buckets, omitted = _collect(sources, chapter_number, estimate_tokens)
    omitted = plan_omitted + omitted

    plan_text = _render_plan(plan, missing)
    plan_tokens = estimate_tokens(plan_text)
    rules_tokens = sum(i.tokens for i in buckets["rules"])
    state_tokens = sum(i.tokens for i in buckets["previous_state"])
    required = rules_tokens + plan_tokens + state_tokens
    if required > limits.available:
        raise WorkbenchError(
            ErrorCode.CONTEXT_TOO_LARGE,
            "作者规则、本章计划与上一章状态已超出可用容量，不能发起生成。",
            {
                "required_tokens": required,
                "available_tokens": limits.available,
                "rules_tokens": rules_tokens,
                "plan_tokens": plan_tokens,
                "previous_state_tokens": state_tokens,
            },
        )

    section_tokens: dict[str, int] = {
        "rules": rules_tokens,
        "plan": plan_tokens,
        "previous_state": state_tokens,
    }
    included: dict[str, list[_Item]] = {
        "rules": list(buckets["rules"]),
        "previous_state": list(buckets["previous_state"]),
        "adopted_text": [],
        "open_issues": [],
        "author_intent": [],
        "discussion": [],
    }
    remaining = limits.available - required

    # 可选来源按优先级装入：作者意图 → 未解决问题 → 已采用正文（近章优先）→ 讨论（近期优先）。
    # 放不下的记入 omitted(capacity)；讨论优先级最低，因此最先被裁掉。
    def fill(section: str, ordered: list[_Item]) -> None:
        nonlocal remaining
        used = 0
        for item in ordered:
            if item.tokens <= remaining:
                included[section].append(item)
                remaining -= item.tokens
                used += item.tokens
            else:
                omitted.append(
                    OmittedSource(
                        item.source_kind, item.source_id, OmitReason.CAPACITY, item.chapter_number
                    )
                )
        section_tokens[section] = used

    fill("author_intent", buckets["author_intent"])
    fill("open_issues", buckets["open_issues"])
    texts_recent_first = sorted(
        buckets["adopted_text"], key=lambda i: i.chapter_number or 0, reverse=True
    )
    fill("adopted_text", texts_recent_first)
    fill("discussion", list(reversed(buckets["discussion"])))

    # 正文与讨论恢复自然顺序展示。
    included["adopted_text"].sort(key=lambda i: i.chapter_number or 0)
    included["discussion"].reverse()

    citations: list[SourceCitation] = []
    for section in ("rules", "previous_state", "adopted_text", "author_intent", "discussion"):
        citations.extend(item.citation for item in included[section] if item.citation is not None)
    issue_ids = tuple(item.issue_id for item in included["open_issues"] if item.issue_id)

    adopted_parts = [
        f"第{item.chapter_number}章（已采用）：\n{item.text}" for item in included["adopted_text"]
    ]
    estimated = limits.template_reserve + limits.output_reserve + sum(section_tokens.values())
    return ContextDraft(
        novel_id=sources.novel_id,
        chapter_id=sources.chapter_id,
        chapter_number=chapter_number,
        mode=mode,
        design_revision=sources.design_revision,
        plan_id=plan.plan_id if plan else None,
        previous_snapshot_id=sources.previous_snapshot_id if chapter_number > 1 else None,
        missing_inputs=missing,
        rules=_join(included["rules"]),
        plan=plan_text,
        previous_state=_join(included["previous_state"]),
        adopted_text="\n\n".join(adopted_parts),
        open_issues=_join(included["open_issues"]),
        discussion=_join(included["discussion"]),
        author_intent=_join(included["author_intent"]),
        citations=tuple(citations),
        issue_ids=issue_ids,
        omitted=tuple(omitted),
        estimated_tokens=estimated,
        limits=limits,
        section_tokens=section_tokens,
    )

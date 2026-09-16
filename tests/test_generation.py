"""任务、上下文与检查载荷流程（G01–G03）。全部用固定客户端与纯函数，不碰数据库。"""

from __future__ import annotations

import json

import pytest

from story_workbench.ai import prompts, tasks
from story_workbench.ai.context import (
    ContextLimits,
    ContextSources,
    FactSource,
    IssueSource,
    MessageSource,
    OmitReason,
    PlanSource,
    ReaderHistorySource,
    SourceRole,
    StoryItemSource,
    TextSource,
    TextStatus,
    build_context,
    default_estimate_tokens,
)
from story_workbench.application import authoring
from story_workbench.application import generation as app_generation
from story_workbench.domain.records import (
    Authority,
    DelegationScope,
    ErrorCode,
    Expected,
    FactKind,
    IssueKind,
    Lifecycle,
    MessageRole,
    SnapshotStatus,
    TaskKind,
    TaskRequest,
    WorkbenchError,
    WriteMode,
)
from story_workbench.domain.rules import validate_evidence_span
from story_workbench.infrastructure.provider import CallOptions, EventKind
from tests.conftest import fixed_complete, fixed_stream
from tests.test_workflow import adopt_current, inspection_json

# ---------------------------------------------------------------- 夹具

CH2 = "chapter-2"
OPTIONS = CallOptions(model="fixed", max_output_tokens=800, timeout_seconds=5)
GEN_CONFIG = app_generation.GenerationConfig(
    options=CallOptions(model="fixed", max_output_tokens=1000, timeout_seconds=5),
    limits=ContextLimits(total_tokens=20000, template_reserve=500, output_reserve=1000),
)
BODY = (
    "沈砚爬回井沿，把绳子收好。他还疑心井底那人就是师父，只是不敢认。\n\n"
    "旁边旧屋的门卡在门框里。他用肩膀顶了两次，木门擦过地面，慢慢让出一道缝。\n\n"
    "“师父死了。”他低声说。"
)


def rule(item_id: str, body: str, chapter: int | None = None) -> StoryItemSource:
    return StoryItemSource(item_id, body, Authority.AUTHOR_DECISION, Lifecycle.DECIDED, chapter)


def chapter_two_sources(**overrides) -> ContextSources:
    """写第二章时的候选来源：库中已有第三章事件、第二章拒绝稿与候选想法。"""
    base = dict(
        novel_id="novel",
        chapter_id=CH2,
        design_revision=3,
        story_items=(
            rule("s-wrist", "师父右腕有旧伤"),
            StoryItemSource(
                "s-villain", "师父可能是反派", Authority.AUTHOR_IDEA, Lifecycle.CANDIDATE
            ),
            StoryItemSource("s-old", "旧规则", Authority.AUTHOR_DECISION, Lifecycle.ARCHIVED),
            StoryItemSource("s-no", "被否的规则", Authority.AUTHOR_DECISION, Lifecycle.REJECTED),
            rule("s-future", "第五章揭示师父还活着", 5),
        ),
        plans=(
            PlanSource("p2", 2, "离开井底，去旁边旧屋找线索", "停在一扇紧闭的内门前"),
            PlanSource("p3", 3, "试写门外", None),
        ),
        facts=(
            FactSource(
                "snap1",
                1,
                FactKind.CHARACTER_BELIEF,
                "井底那人是师父",
                candidate_id="cf1",
                subject="沈砚",
            ),
            FactSource("snap1", 1, FactKind.WORLD, "井底有一具身份不明的尸体", candidate_id="cf2"),
            FactSource("snap3", 3, FactKind.WORLD, "门内有轻微声响", candidate_id="cf3"),
        ),
        texts=(
            TextSource("v1", 1, "第一章正文。", TextStatus.ADOPTED),
            TextSource("v2-rejected", 2, "他从袖中抽出细铁丝拨动锁簧。", TextStatus.REJECTED),
            TextSource("v2-draft", 2, "第二章未采用稿。", TextStatus.DRAFT),
            TextSource("v3", 3, "他抬起手，轻轻敲了三下。", TextStatus.ADOPTED),
        ),
        issues=(
            IssueSource("i1", 1, IssueKind.RUMOR_OR_BELIEF, "尸体身份未确认"),
            IssueSource("i3", 3, IssueKind.PLAN_DEVIATION, "第三章偏差"),
            IssueSource("i-done", 1, IssueKind.SUSPECTED_ERROR, "已处理", resolved=True),
        ),
        messages=(
            MessageSource("m1", MessageRole.AUTHOR, "写得克制一点。", 2),
            MessageSource("m2", MessageRole.MODEL, "好的。", 2),
            MessageSource("m3", MessageRole.AUTHOR, "第三章别揭示屋里是谁。", 3),
        ),
        reader_history=(
            ReaderHistorySource(
                "pub1",
                "publication_text",
                "[publication] 第1章已发布正文：\n读者已经看过井边蓝火。",
                1,
                version_id="pub-v1",
            ),
            ReaderHistorySource(
                "pub3",
                "publication_text",
                "[publication] 第3章已发布正文：\n未来发布内容。",
                3,
                version_id="v3",
            ),
        ),
        previous_snapshot_id="snap1",
        previous_snapshot_status=SnapshotStatus.READY,
    )
    base.update(overrides)
    return ContextSources(**base)


def build_chapter_two(mode: WriteMode = WriteMode.FORMAL, total: int = 4000):
    return build_context(
        chapter_two_sources(), 2, mode, ContextLimits(total, template_reserve=50, output_reserve=50)
    )


def request(task: TaskKind, **kw) -> TaskRequest:
    defaults = dict(
        task=task,
        basis_id="basis-1",
        source_message_id="m1",
        template_version=prompts.TEMPLATE_VERSION,
    )
    defaults.update(kw)
    return TaskRequest(**defaults)


def evidence(quote: str) -> dict:
    start = BODY.index(quote)
    return {"start": start, "end": start + len(quote), "quote": quote}


def valid_inspection() -> dict:
    belief = "师父死了。"
    return {
        "issues": [
            {
                "kind": "rumor_or_belief",
                "explanation": "沈砚的话只是他的认知，不能视为师父死亡。",
                "proposed_change": "",
                "evidence": [evidence(belief)],
            }
        ],
        "candidate_facts": [
            {
                "kind": "character_belief",
                "subject": "沈砚",
                "proposition": "沈砚认为师父死了",
                "story_time": None,
                **evidence(belief),
            },
            {
                "kind": "world",
                "subject": None,
                "proposition": "旧屋的门卡在门框里",
                "story_time": None,
                **evidence("旁边旧屋的门卡在门框里。"),
            },
        ],
        "actual_plot": "沈砚离开井底进入旧屋。",
        "actual_ending": "停在门缝前。",
        "coverage": {"checked": ["新增设定", "计划偏差"], "unchecked": ["时间线"]},
    }


# ---------------------------------------------------------------- G01 上下文构建


def test_rejected_and_future_sources_never_enter_chapter_two():
    """T08：库中已有第三章事件时写第二章，第三章知识与拒绝稿都不能进入。"""
    draft = build_chapter_two()

    sent = "\n".join(draft.sections().values())
    assert "细铁丝" not in sent  # 拒绝稿
    assert "第二章未采用稿" not in sent  # 未采用稿
    assert "敲了三下" not in sent  # 第三章正文
    assert "门内有轻微声响" not in sent  # 第三章事实
    assert "第三章偏差" not in sent  # 第三章问题
    assert "别揭示屋里是谁" not in sent  # 第三章讨论
    assert "师父可能是反派" not in sent  # 候选想法
    assert "旧规则" not in sent and "被否的规则" not in sent

    assert draft.rules == "师父右腕有旧伤"
    assert "井底那人是师父" in draft.previous_state and "[character_belief]" in draft.previous_state
    assert "第1章（已采用）" in draft.adopted_text
    assert "读者已经看过井边蓝火" in draft.reader_history
    assert "未来发布内容" not in draft.reader_history
    assert draft.plan_id == "p2" and draft.previous_snapshot_id == "snap1"
    assert draft.design_revision == 3 and draft.missing_inputs == ()

    # 未来设计只进作者意图区，并标明人物不知情。
    assert "第五章揭示师父还活着" in draft.author_intent
    assert "第五章揭示师父还活着" not in draft.rules
    assert "第五章揭示师父还活着" not in draft.previous_state
    intent = [c for c in draft.citations if c.story_item_id == "s-future"]
    assert intent[0].role is SourceRole.INTENT and intent[0].section == "author_intent"


def test_omitted_sources_are_traceable_by_reason():
    draft = build_chapter_two()
    reasons = {(o.source_kind, o.source_id): o.reason for o in draft.omitted}
    assert reasons[("version", "v2-rejected")] is OmitReason.REJECTED_DRAFT
    assert reasons[("version", "v2-draft")] is OmitReason.UNADOPTED_DRAFT
    assert reasons[("version", "v3")] is OmitReason.FUTURE_CHAPTER
    assert reasons[("snapshot_fact", "cf3")] is OmitReason.FUTURE_CHAPTER
    assert reasons[("story_item", "s-villain")] is OmitReason.NOT_AUTHOR_DECISION
    assert reasons[("story_item", "s-old")] is OmitReason.LIFECYCLE_ARCHIVED
    assert reasons[("story_item", "s-no")] is OmitReason.LIFECYCLE_REJECTED
    assert reasons[("issue", "i3")] is OmitReason.FUTURE_CHAPTER
    assert reasons[("issue", "i-done")] is OmitReason.RESOLVED_ISSUE
    assert reasons[("message", "m3")] is OmitReason.FUTURE_CHAPTER
    assert reasons[("plan", "p3")] is OmitReason.FUTURE_CHAPTER
    assert reasons[("reader_history", "pub3")] is OmitReason.FUTURE_CHAPTER
    assert draft.issue_ids == ("i1",)


def test_citations_map_to_basis_source_rows():
    draft = build_chapter_two()
    by_id = {c.source_id: c for c in draft.citations}
    assert set(by_id) == {"s-wrist", "s-future", "snap1", "v1", "pub-v1", "m1", "m2"}
    text = by_id["v1"]
    assert text.role is SourceRole.TEXT and text.version_id == "v1"
    assert text.quote == "第一章正文。" and (text.excerpt_start, text.excerpt_end) == (0, 6)
    # 每条引用恰有一个来源 ID。
    for c in draft.citations:
        refs = [c.version_id, c.story_item_id, c.snapshot_id, c.fact_revision_id, c.message_id]
        assert sum(r is not None for r in refs) == 1


def test_capacity_trims_discussion_first_and_records_omission():
    full = build_chapter_two()
    required = full.section_tokens["rules"] + full.section_tokens["plan"]
    required += full.section_tokens["previous_state"]
    keep = required + full.section_tokens["author_intent"] + full.section_tokens["open_issues"]
    keep += full.section_tokens["reader_history"] + full.section_tokens["adopted_text"]
    limits = ContextLimits(keep + 100, template_reserve=50, output_reserve=50)

    draft = build_context(chapter_two_sources(), 2, WriteMode.FORMAL, limits)
    assert draft.discussion == ""
    assert draft.adopted_text == full.adopted_text and draft.rules == full.rules
    capacity = {o.source_id for o in draft.omitted if o.reason is OmitReason.CAPACITY}
    assert capacity == {"m1", "m2"}
    assert draft.estimated_tokens <= limits.total_tokens


def test_required_basis_over_limit_raises_context_too_large():
    full = build_chapter_two()
    required = full.section_tokens["rules"] + full.section_tokens["plan"]
    required += full.section_tokens["previous_state"]
    limits = ContextLimits(required - 1 + 100, template_reserve=50, output_reserve=50)
    with pytest.raises(WorkbenchError) as exc:
        build_context(chapter_two_sources(), 2, WriteMode.FORMAL, limits)
    assert exc.value.code is ErrorCode.CONTEXT_TOO_LARGE
    assert exc.value.details["required_tokens"] == required
    assert exc.value.details["available_tokens"] == required - 1


def test_stale_previous_snapshot_blocks_dependent_writing():
    sources = chapter_two_sources(previous_snapshot_status=SnapshotStatus.STALE)
    with pytest.raises(WorkbenchError) as exc:
        build_context(sources, 2, WriteMode.FORMAL, ContextLimits(4000, 50, 50))
    assert exc.value.code is ErrorCode.STALE_BASIS


def test_formal_mode_requires_plan_while_exploratory_records_gaps():
    sources = chapter_two_sources(plans=(PlanSource("p2", 2, "去旧屋", None),))
    with pytest.raises(WorkbenchError) as exc:
        build_context(sources, 2, WriteMode.FORMAL, ContextLimits(4000, 50, 50))
    assert exc.value.code is ErrorCode.INVALID_INPUT

    draft = build_context(sources, 2, WriteMode.EXPLORATORY, ContextLimits(4000, 50, 50))
    assert draft.missing_inputs == ("ending",)
    assert "结尾状态：作者尚未确定" in draft.plan


def test_default_estimate_is_conservative_and_injectable():
    assert default_estimate_tokens("沈砚") == 3
    draft = build_context(
        chapter_two_sources(), 2, WriteMode.FORMAL, ContextLimits(10_000, 0, 0), lambda _: 1
    )
    assert draft.section_tokens["rules"] == 1


# ---------------------------------------------------------------- G02 写作、讨论与委托


def collect_text(events) -> str:
    kinds = [e.kind for e in events]
    assert kinds[-1] is EventKind.COMPLETED
    return "".join(e.text for e in events if e.kind is EventKind.TEXT)


def test_generate_formal_streams_body_without_saving():
    draft = build_chapter_two()
    captured: dict = {}

    def stream(messages, options, cancel):
        captured["messages"] = messages
        captured["options"] = options
        return fixed_stream(["沈砚爬回井沿，", "把绳子收好。"])(messages, options, cancel)

    events = list(
        tasks.generate(request(TaskKind.GENERATE, output_limit=600), draft, stream, options=OPTIONS)
    )
    assert collect_text(events) == "沈砚爬回井沿，把绳子收好。"
    system, user = captured["messages"]
    assert system.role == "system" and "正式写作" in system.content
    assert "师父右腕有旧伤" in user.content and "停在一扇紧闭的内门前" in user.content
    assert "细铁丝" not in user.content and "敲了三下" not in user.content
    assert "尚未由作者确定" not in user.content
    assert captured["options"].max_output_tokens == 600


def test_exploratory_prompt_lists_gaps_and_assumptions():
    sources = chapter_two_sources(plans=(PlanSource("p2", 2, "试写门外这段", None),))
    draft = build_context(sources, 2, WriteMode.EXPLORATORY, ContextLimits(4000, 50, 50))
    req = request(
        TaskKind.GENERATE,
        missing_inputs=("ending",),
        assumptions=("门内有轻微声响，沈砚犹豫后敲门，但没有得到回应",),
    )
    system, user = tasks.build_generate_messages(req, draft)
    assert "试写" in system.content and "不得确认作者未定的事项" in system.content
    assert "尚未由作者确定" in user.content and "- ending" in user.content
    assert "本次试写采用的假设" in user.content and "没有得到回应" in user.content


def test_formal_generate_rejects_missing_inputs():
    draft = build_chapter_two()
    with pytest.raises(WorkbenchError) as exc:
        tasks.build_generate_messages(request(TaskKind.GENERATE, missing_inputs=("ending",)), draft)
    assert exc.value.code is ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("task", [TaskKind.INSPECT, TaskKind.DIAGNOSE])
def test_sent_structured_prompt_contains_valid_json_example(task):
    draft = build_chapter_two()
    build = (
        tasks.build_inspect_messages if task is TaskKind.INSPECT else tasks.build_diagnose_messages
    )
    system, _ = build(request(task), draft, BODY)
    example = json.loads(system.content[system.content.index("{") : system.content.rindex("}") + 1])
    assert ("issues" if task is TaskKind.INSPECT else "problems") in example


def test_revision_instructions_only_apply_when_rewriting_existing_body():
    draft = build_chapter_two(WriteMode.EXPLORATORY)
    req = request(TaskKind.GENERATE, assumptions=("删除敲门声；结尾停在内门前，最多两段。",))
    original_system, _ = tasks.build_generate_messages(req, draft)
    system, user = tasks.build_generate_messages(req, draft, BODY)
    assert prompts.REVISION_SYSTEM not in original_system.content
    assert prompts.REVISION_SYSTEM in system.content
    assert prompts.COMMON_SYSTEM in system.content
    assert BODY in user.content
    assert req.assumptions[0] in user.content


def test_delegation_scope_only_this_request_this_chapter():
    draft = build_chapter_two()
    ok = DelegationScope(chapter_id=CH2, request_message_id="m1", may_choose_plot=True)
    system, user = tasks.build_generate_messages(
        request(TaskKind.GENERATE, delegation_scope=ok), draft
    )
    assert "仅限本章" in user.content

    cross_chapter = DelegationScope(chapter_id="chapter-3", request_message_id="m1")
    with pytest.raises(WorkbenchError) as exc:
        tasks.build_generate_messages(
            request(TaskKind.GENERATE, delegation_scope=cross_chapter), draft
        )
    assert exc.value.code is ErrorCode.WRONG_SCOPE

    inherited = DelegationScope(chapter_id=CH2, request_message_id="m-previous-task")
    with pytest.raises(WorkbenchError) as exc:
        tasks.build_generate_messages(request(TaskKind.GENERATE, delegation_scope=inherited), draft)
    assert exc.value.code is ErrorCode.WRONG_SCOPE


def test_task_kind_and_template_version_must_match():
    draft = build_chapter_two()
    with pytest.raises(WorkbenchError) as wrong_task:
        tasks.build_generate_messages(request(TaskKind.INSPECT), draft)
    assert wrong_task.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(WorkbenchError) as stale:
        tasks.build_generate_messages(request(TaskKind.GENERATE, template_version="0"), draft)
    assert stale.value.code is ErrorCode.INVALID_INPUT
    assert stale.value.details["expected"] == prompts.TEMPLATE_VERSION


def test_discuss_returns_plain_text_stream():
    draft = build_chapter_two()
    captured: dict = {}

    def stream(messages, options, cancel):
        captured["messages"] = messages
        return fixed_stream(["可以先从井沿写起。"])(messages, options, cancel)

    events = list(
        tasks.discuss(request(TaskKind.DISCUSS), draft, "第二章从哪写起？", stream, options=OPTIONS)
    )
    assert collect_text(events) == "可以先从井沿写起。"
    assert "第二章从哪写起？" in captured["messages"][1].content
    assert "不要替作者做决定" in captured["messages"][0].content


# ---------------------------------------------------------------- G03 检查载荷校验


def inspect_with(payload, *, truncated: bool = False):
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return tasks.inspect(
        request(TaskKind.INSPECT, input_version_id="v2"),
        build_chapter_two(),
        BODY,
        fixed_complete(text, truncated=truncated),
        options=OPTIONS,
    )


def test_valid_inspection_parses_enums_and_keeps_belief_separate_from_world():
    result = inspect_with(valid_inspection())
    assert [i.kind for i in result.issues] == [IssueKind.RUMOR_OR_BELIEF]
    assert result.issues[0].evidence[0].quote == "师父死了。"
    kinds = {f.proposition: f.kind for f in result.candidate_facts}
    assert kinds["沈砚认为师父死了"] is FactKind.CHARACTER_BELIEF
    assert kinds["旧屋的门卡在门框里"] is FactKind.WORLD
    # “师父死了”只作为人物认知存在，没有对应的 world 死亡事实。
    assert not any(
        f.kind is FactKind.WORLD and "死" in f.proposition for f in result.candidate_facts
    )
    assert result.coverage.checked == ("新增设定", "计划偏差")
    assert result.coverage.unchecked == ("时间线",)
    assert result.template_version == prompts.TEMPLATE_VERSION
    for fact in result.candidate_facts:
        assert BODY[fact.start : fact.end] == fact.quote


def test_inspection_accepts_fenced_json_and_explicit_empty_arrays():
    payload = valid_inspection()
    payload["issues"] = []
    payload["candidate_facts"] = []
    result = inspect_with("```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```")
    assert result.issues == () and result.candidate_facts == ()


def test_inspection_missing_field_is_model_failed():
    payload = valid_inspection()
    del payload["candidate_facts"]
    with pytest.raises(WorkbenchError) as exc:
        inspect_with(payload)
    assert exc.value.code is ErrorCode.MODEL_FAILED
    assert exc.value.details["reason"] == "schema_violation"
    assert {"loc": ["candidate_facts"], "type": "missing"} in exc.value.details["errors"]
    assert BODY not in json.dumps(exc.value.details, ensure_ascii=False)


def test_inspection_extra_field_is_model_failed():
    payload = valid_inspection()
    payload["confidence"] = 0.9
    with pytest.raises(WorkbenchError) as exc:
        inspect_with(payload)
    assert exc.value.code is ErrorCode.MODEL_FAILED
    assert any(e["type"] == "extra_forbidden" for e in exc.value.details["errors"])


def test_inspection_fake_quote_is_model_failed_without_leaking_body():
    payload = valid_inspection()
    payload["candidate_facts"][0]["quote"] = "师父活着。"
    with pytest.raises(WorkbenchError) as exc:
        inspect_with(payload)
    assert exc.value.code is ErrorCode.MODEL_FAILED
    assert exc.value.details["reason"] == "evidence_mismatch"
    assert exc.value.details["loc"] == "candidate_facts[0]"
    dumped = json.dumps(exc.value.details, ensure_ascii=False)
    assert "师父活着" not in dumped and "沈砚爬回井沿" not in dumped

    payload = valid_inspection()
    payload["issues"][0]["evidence"][0]["end"] = len(BODY) + 5
    span = inspect_with(payload).issues[0].evidence[0]
    assert BODY[span.start : span.end] == span.quote


@pytest.mark.parametrize("kind", ["issue", "fact", "diagnosis"])
@pytest.mark.parametrize(
    "body, quote, expected",
    [
        ("前🌧️。\n井沿。", "井沿。", (5, 8)),
        ("甲乙甲乙", "甲乙", None),
        ("哈哈哈", "哈哈", None),
        ("井沿。", "井沿！", None),
        ("井沿。", "", None),
    ],
)
def test_evidence_is_located_only_by_unique_exact_quote(kind, body, quote, expected):
    span = {"quote": quote}
    if expected is None:
        span.update(start=0, end=len(quote))
    if kind == "diagnosis":
        payload = {
            "verdict": "反馈",
            "problems": [{"explanation": "问题", "evidence": [span]}],
            "options": [],
            "new_settings_requiring_authorization": [],
        }
        invoke = tasks.diagnose
        task = TaskKind.DIAGNOSE
    else:
        payload = valid_inspection()
        payload["issues"] = []
        payload["candidate_facts"] = []
        if kind == "issue":
            payload["issues"] = [
                {
                    "kind": "suspected_error",
                    "explanation": "问题",
                    "proposed_change": "",
                    "evidence": [span],
                }
            ]
        else:
            payload["candidate_facts"] = [
                {
                    "kind": "world",
                    "subject": None,
                    "proposition": "事实",
                    "story_time": None,
                    **span,
                }
            ]
        invoke = tasks.inspect
        task = TaskKind.INSPECT

    def run():
        return invoke(
            request(task),
            build_chapter_two(),
            body,
            fixed_complete(json.dumps(payload)),
            options=OPTIONS,
        )

    if expected is None:
        with pytest.raises(WorkbenchError) as exc:
            run()
        assert exc.value.code is ErrorCode.MODEL_FAILED
        assert exc.value.details["reason"] == "evidence_mismatch"
    else:
        result = run()
        located = (
            result.problems[0].evidence[0]
            if kind == "diagnosis"
            else result.issues[0].evidence[0]
            if kind == "issue"
            else result.candidate_facts[0]
        )
        assert (located.start, located.end) == expected
        assert body[located.start : located.end] == quote


def test_inspection_truncated_or_non_json_is_model_failed():
    with pytest.raises(WorkbenchError) as truncated:
        inspect_with(valid_inspection(), truncated=True)
    assert truncated.value.code is ErrorCode.MODEL_FAILED
    assert truncated.value.details["reason"] == "truncated"

    with pytest.raises(WorkbenchError) as garbage:
        inspect_with("检查没有发现问题。")
    assert garbage.value.code is ErrorCode.MODEL_FAILED
    assert garbage.value.details["reason"] == "invalid_json"


def test_inspection_rejects_unknown_enum_and_loose_types():
    payload = valid_inspection()
    payload["issues"][0]["kind"] = "style_problem"
    with pytest.raises(WorkbenchError) as bad_enum:
        inspect_with(payload)
    assert bad_enum.value.code is ErrorCode.MODEL_FAILED

    payload = valid_inspection()
    payload["candidate_facts"][0]["start"] = "0"
    with pytest.raises(WorkbenchError) as loose:
        inspect_with(payload)
    assert loose.value.code is ErrorCode.MODEL_FAILED

    payload = valid_inspection()
    payload["issues"][0]["evidence"] = []
    with pytest.raises(WorkbenchError) as no_evidence:
        inspect_with(payload)
    assert no_evidence.value.code is ErrorCode.MODEL_FAILED


def test_validate_evidence_span_details_are_safe():
    validate_evidence_span(BODY, 0, 2, "沈砚")
    with pytest.raises(WorkbenchError) as exc:
        validate_evidence_span(BODY, 0, 2, "师父")
    assert exc.value.code is ErrorCode.INVALID_INPUT
    assert exc.value.details == {
        "start": 0,
        "end": 2,
        "body_length": len(BODY),
        "quote_length": 2,
        "reason": "quote_mismatch",
    }
    with pytest.raises(WorkbenchError) as empty:
        validate_evidence_span(BODY, 3, 3, "")
    assert empty.value.details["reason"] == "empty_quote"


# ---------------------------------------------------------------- 影响分析


def impact_payload() -> dict:
    return {
        "summary": "第三章受影响，第四章暂未检查。",
        "chapters": [
            {
                "chapter_number": 3,
                "status": "affected",
                "reason": "仍把蓝火当作旧设定。",
                "evidence": [{"quote": "蓝火再次亮起。"}],
            },
            {
                "chapter_number": 4,
                "status": "unchecked",
                "reason": "缺少这一章与旧设定关系的可靠判断。",
                "evidence": [],
            },
        ],
        "coverage": {"checked": ["第3章正文"], "unchecked": ["第4章因证据不足未判断"]},
    }


def test_impact_requires_every_later_chapter_with_traceable_coverage():
    later = {
        3: ("v3", "蓝火再次亮起。沈砚停下脚步。"),
        4: ("v4", "他走进雨里。"),
    }
    report, _usage = tasks.impact(
        request(TaskKind.IMPACT),
        build_chapter_two(),
        2,
        later,
        fixed_complete(json.dumps(impact_payload(), ensure_ascii=False)),
        options=OPTIONS,
    )

    assert [chapter.chapter_number for chapter in report.chapters] == [3, 4]
    assert [chapter.status for chapter in report.chapters] == ["affected", "unchecked"]
    assert report.coverage_checked == ("第3章正文",)
    assert report.coverage_unchecked == ("第4章因证据不足未判断",)
    assert len(report.findings) == 1
    assert report.findings[0].evidence[0].version_id == "v3"


def test_impact_rejects_partial_or_untraceable_output():
    later = {
        3: ("v3", "蓝火再次亮起。沈砚停下脚步。"),
        4: ("v4", "他走进雨里。"),
    }
    payload = impact_payload()
    payload["chapters"] = payload["chapters"][:1]
    with pytest.raises(WorkbenchError) as missing:
        tasks.impact(
            request(TaskKind.IMPACT),
            build_chapter_two(),
            2,
            later,
            fixed_complete(json.dumps(payload, ensure_ascii=False)),
            options=OPTIONS,
        )
    assert missing.value.code is ErrorCode.MODEL_FAILED
    assert missing.value.details["missing_chapters"] == [4]

    payload = impact_payload()
    payload["chapters"][0]["evidence"] = []
    with pytest.raises(WorkbenchError) as no_evidence:
        tasks.impact(
            request(TaskKind.IMPACT),
            build_chapter_two(),
            2,
            later,
            fixed_complete(json.dumps(payload, ensure_ascii=False)),
            options=OPTIONS,
        )
    assert no_evidence.value.code is ErrorCode.MODEL_FAILED


def _adopt_generated(project, number: int, body: str):
    chapter = authoring.create_chapter(project, number)
    message_id = authoring.record_message(project, f"第{number}章计划。", chapter.id)
    authoring.save_plan(
        project,
        chapter.id,
        f"第{number}章剧情",
        f"第{number}章结尾",
        Expected(),
        message_id,
    )
    clients = app_generation.Clients(
        stream=fixed_stream([body]),
        complete=fixed_complete(
            inspection_json(body, plot=f"第{number}章剧情", ending=f"第{number}章结尾")
        ),
    )
    result = app_generation.write_chapter(
        project, chapter.id, WriteMode.FORMAL, message_id, clients, GEN_CONFIG
    )
    adopted = adopt_current(project, chapter.id, result)
    return authoring.get_chapter(project, chapter.id), result, adopted


def test_generation_impact_analysis_persists_evidence_for_unrelated_and_unchecked(project):
    _ch1, _result1, adopted1 = _adopt_generated(project, 1, "井边蓝火已经熄灭。")
    _ch2, result2, _adopted2 = _adopt_generated(project, 2, "蓝火再次亮起。沈砚停下脚步。")
    _ch3, result3, _adopted3 = _adopt_generated(project, 3, "雨声盖住脚步。")
    message_id = authoring.record_message(project, "分析蓝火影响。", _ch1.id)
    payload = {
        "summary": "第二章无关，第三章未能确认。",
        "chapters": [
            {
                "chapter_number": 2,
                "status": "unrelated",
                "reason": "蓝火描写不依赖第一章新采用内容。",
                "evidence": [{"quote": "蓝火再次亮起。"}],
            },
            {
                "chapter_number": 3,
                "status": "unchecked",
                "reason": "雨声场景证据不足。",
                "evidence": [{"quote": "雨声盖住脚步。"}],
            },
        ],
        "coverage": {"checked": ["第2章正文"], "unchecked": ["第3章只保留证据待复核"]},
    }
    clients = app_generation.Clients(
        stream=fixed_stream([]),
        complete=fixed_complete(json.dumps(payload, ensure_ascii=False)),
    )

    run_id, _report = app_generation.analyze_impact(
        project, adopted1.adoption_id, message_id, clients, GEN_CONFIG
    )

    rows = project.conn.execute(
        "SELECT ia.impact_class, ia.id AS analysis_id, ie.analysis_id AS evidence_analysis_id,"
        " ie.version_id, ie.quote FROM impact_analysis ia"
        " JOIN impact_evidence ie ON ie.analysis_id = ia.id"
        " WHERE ia.run_id = ? ORDER BY ia.impact_class",
        (run_id,),
    ).fetchall()
    by_class = {row["impact_class"]: row for row in rows}
    assert set(by_class) == {"unchecked", "unrelated"}
    assert by_class["unrelated"]["analysis_id"] == by_class["unrelated"]["evidence_analysis_id"]
    assert by_class["unrelated"]["version_id"] == result2.version_id
    assert by_class["unrelated"]["quote"] == "蓝火再次亮起。"
    assert by_class["unchecked"]["analysis_id"] == by_class["unchecked"]["evidence_analysis_id"]
    assert by_class["unchecked"]["version_id"] == result3.version_id
    assert by_class["unchecked"]["quote"] == "雨声盖住脚步。"


# ---------------------------------------------------------------- 阅读体验诊断


def test_diagnose_returns_evidence_options_and_authorization_list():
    quote = "他用肩膀顶了两次"
    payload = {
        "verdict": "开头承接生硬，整体不满意。",
        "problems": [{"explanation": "动作描写重复。", "evidence": [evidence(quote)]}],
        "options": [
            {"description": "删去一次顶门。", "requires_new_setting": False},
            {"description": "让门自动打开。", "requires_new_setting": True},
        ],
        "new_settings_requiring_authorization": ["旧屋的门会自行开启"],
    }
    result = tasks.diagnose(
        request(TaskKind.DIAGNOSE),
        build_chapter_two(),
        BODY,
        fixed_complete(json.dumps(payload, ensure_ascii=False)),
        options=OPTIONS,
    )
    assert result.verdict.endswith("不满意。")
    assert result.problems[0].evidence[0].quote == quote
    assert [o.requires_new_setting for o in result.options] == [False, True]
    assert result.new_settings_requiring_authorization == ("旧屋的门会自行开启",)

    payload["problems"][0]["evidence"][0]["quote"] = "他用肩膀顶了三次"
    with pytest.raises(WorkbenchError) as exc:
        tasks.diagnose(
            request(TaskKind.DIAGNOSE),
            build_chapter_two(),
            BODY,
            fixed_complete(json.dumps(payload, ensure_ascii=False)),
            options=OPTIONS,
        )
    assert exc.value.code is ErrorCode.MODEL_FAILED

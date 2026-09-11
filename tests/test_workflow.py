"""G04/G06：用固定客户端跑三章闭环、拒绝稿隔离、检查失败重试、取消与迟到结果保护。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest

from story_workbench.ai.context import ContextLimits
from story_workbench.application import adoption, authoring, generation
from story_workbench.application.projects import Project
from story_workbench.domain.records import (
    AuthorAction,
    Authority,
    ErrorCode,
    Expected,
    Lifecycle,
    RunStatus,
    SnapshotStatus,
    Target,
    WorkbenchError,
    WriteMode,
)
from story_workbench.infrastructure import database
from story_workbench.infrastructure.provider import (
    CallOptions,
    CancelSignal,
    EventKind,
    StreamEvent,
)
from tests.conftest import fixed_complete, fixed_stream

OPTIONS = CallOptions(model="fixed", max_output_tokens=2000, timeout_seconds=5)
LIMITS = ContextLimits(total_tokens=20000, template_reserve=500, output_reserve=2000)
CONFIG = generation.GenerationConfig(options=OPTIONS, limits=LIMITS)


def evidence(body: str, quote: str) -> dict:
    start = body.index(quote)
    return {"start": start, "end": start + len(quote), "quote": quote}


def inspection_json(body: str, *, issues=(), facts=(), plot="剧情", ending="结尾") -> str:
    return json.dumps(
        {
            "issues": list(issues),
            "candidate_facts": list(facts),
            "actual_plot": plot,
            "actual_ending": ending,
            "coverage": {"checked": ["全文"], "unchecked": []},
        },
        ensure_ascii=False,
    )


def belief_fact(body: str, quote: str, subject: str, proposition: str) -> dict:
    return {
        "kind": "character_belief",
        "subject": subject,
        "proposition": proposition,
        "story_time": None,
        **evidence(body, quote),
    }


def setting_issue(body: str, quote: str, explanation: str) -> dict:
    return {
        "kind": "new_persistent_setting",
        "explanation": explanation,
        "proposed_change": "删除该段。",
        "evidence": [evidence(body, quote)],
    }


def clients_for(body: str, check_json: str) -> generation.Clients:
    return generation.Clients(
        stream=fixed_stream([body[:5], body[5:]]), complete=fixed_complete(check_json)
    )


def counting_clients(body: str, check_json: str, counter: dict) -> generation.Clients:
    inner_stream = fixed_stream([body])
    inner_complete = fixed_complete(check_json)

    def stream(messages, options, cancel):
        counter["generate"] = counter.get("generate", 0) + 1
        yield from inner_stream(messages, options, cancel)

    def complete(messages, options, cancel):
        counter["inspect"] = counter.get("inspect", 0) + 1
        return inner_complete(messages, options, cancel)

    return generation.Clients(stream=stream, complete=complete)


def adopt_current(
    project: Project, chapter_id: str, result, resolutions=(), plot=None, ending=None
):
    ch = authoring.get_chapter(project, chapter_id)
    prev = [c for c in authoring.list_chapters(project) if c.number == ch.number - 1]
    prev_snapshot = prev[0].current_snapshot_id if prev else None
    msg = authoring.record_message(project, "全文和检查都看完了，采用。", chapter_id)
    action = AuthorAction(
        request_id=uuid.uuid4().hex,
        source_message_id=msg,
        target=Target(project.novel_id, chapter_id, result.version_id),
        expected=Expected(
            adopted_id=ch.adopted_id, plan_id=ch.current_plan_id, previous_snapshot_id=prev_snapshot
        ),
        check_id=result.check_id,
        read_declared=True,
        issue_resolutions=tuple(resolutions),
        accepted_plot=plot,
        accepted_ending=ending,
    )
    return adoption.adopt(project, action)


def plan_chapter(project: Project, number: int, plot: str | None, ending: str | None):
    ch = authoring.create_chapter(project, number)
    msg = authoring.record_message(project, f"第{number}章计划", ch.id)
    if plot or ending:
        authoring.save_plan(project, ch.id, plot, ending, Expected(), msg)
    return ch, msg


# ---------------------------------------------------------------- T06 三章闭环


def test_t06_three_chapters_with_rejection_and_exploratory(project):
    counter: dict = {}
    # 第一章：两项输入即可写；一次生成一次检查
    ch1, msg1 = plan_chapter(project, 1, "调查旧井", "看到疑似师父的尸体")
    body1 = "沈砚把灯搁在井沿。井底有一具尸体，他怀疑是师父。"
    check1 = inspection_json(
        body1,
        facts=[belief_fact(body1, "他怀疑是师父", "沈砚", "沈砚怀疑尸体是师父")],
        plot="调查旧井",
        ending="看到疑似师父的尸体",
    )
    r1 = generation.write_chapter(
        project, ch1.id, WriteMode.FORMAL, msg1, counting_clients(body1, check1, counter), CONFIG
    )
    assert counter == {"generate": 1, "inspect": 1}
    assert r1.check_id is not None and not r1.based_on_old_inputs
    assert (
        authoring.get_version(project, Target(project.novel_id, ch1.id, r1.version_id)).body
        == body1
    )
    a1 = adopt_current(project, ch1.id, r1)
    assert a1.state_status is SnapshotStatus.READY

    # 第二章：先来一版越界稿，拒绝后重写
    ch2, msg2 = plan_chapter(project, 2, "进旧屋找线索", "停在内门前")
    bad = "沈砚从袖中抽出细铁丝，凭着早年练熟的手艺拨动锁簧。"
    bad_check = inspection_json(
        bad,
        issues=[setting_issue(bad, "从袖中抽出细铁丝", "新增开锁能力与工具")],
        plot="开锁",
        ending="门外",
    )
    r2a = generation.write_chapter(
        project, ch2.id, WriteMode.FORMAL, msg2, clients_for(bad, bad_check), CONFIG
    )
    check_rec = database.get_check(project.conn, project.novel_id, r2a.check_id)
    assert [i.kind.value for i in check_rec.issues] == ["new_persistent_setting"]
    with pytest.raises(WorkbenchError) as exc:
        adopt_current(project, ch2.id, r2a)
    assert exc.value.code is ErrorCode.AUTHORIZATION_REQUIRED
    reject_msg = authoring.record_message(project, "不要这版，不加开锁能力。", ch2.id)
    authoring.reject_draft(project, Target(project.novel_id, ch2.id, r2a.version_id), reject_msg)
    assert authoring.get_chapter(project, ch2.id).current_draft_id is None

    good = "旁边旧屋的门卡在门框里。他用肩膀顶了两次。他停在一扇紧闭的内门前。"
    good_check = inspection_json(
        good,
        facts=[belief_fact(good, "他停在一扇紧闭的内门前", "沈砚", "沈砚在内门前")],
        plot="进旧屋找线索",
        ending="停在内门前",
    )
    r2b = generation.write_chapter(
        project, ch2.id, WriteMode.FORMAL, msg2, clients_for(good, good_check), CONFIG
    )
    # 拒绝稿不进入本次上下文
    basis_sources = project.conn.execute(
        "SELECT bs.version_id FROM basis_source bs JOIN run r ON r.basis_id = bs.basis_id"
        " WHERE r.id = ? AND bs.version_id IS NOT NULL",
        (r2b.run_id,),
    ).fetchall()
    assert r2a.version_id not in {row[0] for row in basis_sources}
    a2 = adopt_current(project, ch2.id, r2b)
    assert a2.state_status is SnapshotStatus.READY

    # 第三章：缺结尾，试写；采用时需明确接受剧情与结尾
    ch3, msg3 = plan_chapter(project, 3, None, None)
    with pytest.raises(WorkbenchError) as formal:
        generation.write_chapter(
            project, ch3.id, WriteMode.FORMAL, msg3, clients_for("x", "{}"), CONFIG
        )
    assert formal.value.code is ErrorCode.INVALID_INPUT
    body3 = "门板贴着耳侧。他轻轻敲了三下。门内没有回答。"
    check3 = inspection_json(
        body3,
        facts=[belief_fact(body3, "门内没有回答", "沈砚", "敲门后没有回应")],
        plot="敲门",
        ending="门外等待",
    )
    r3 = generation.write_chapter(
        project,
        ch3.id,
        WriteMode.EXPLORATORY,
        msg3,
        clients_for(body3, check3),
        CONFIG,
        assumptions=("门内有轻微声响",),
    )
    run3 = database.get_run(project.conn, project.novel_id, r3.run_id)
    req = json.loads(run3.request_json)
    assert req["mode"] == "exploratory" and req["assumptions"] == ["门内有轻微声响"]
    assert set(req["missing_inputs"]) == {"plot", "ending"}
    with pytest.raises(WorkbenchError) as need:
        adopt_current(project, ch3.id, r3)
    assert need.value.code is ErrorCode.REVIEW_REQUIRED
    a3 = adopt_current(project, ch3.id, r3, plot="听见声响后敲门", ending="门外等待")
    assert a3.state_status is SnapshotStatus.READY
    # 三章事实链：第三章快照继承前两章事实
    snap3 = authoring.get_chapter(project, ch3.id).current_snapshot_id
    details = database.list_snapshot_fact_details(project.conn, project.novel_id, snap3)
    assert [d.proposition for d in details] == [
        "沈砚怀疑尸体是师父",
        "沈砚在内门前",
        "敲门后没有回应",
    ]
    # 第三章上下文里的前章状态只来自已采用稿
    sources = generation.load_sources(project, ch3.id)
    assert all("细铁丝" not in f.proposition for f in sources.facts)


# ---------------------------------------------------------------- T07/T09 检查失败与重试


def test_invalid_check_keeps_draft_and_retry_only_reruns_check(project):
    ch1, msg1 = plan_chapter(project, 1, "调查旧井", "看到尸体")
    body = "沈砚把灯搁在井沿。"
    bad_check = inspection_json(
        body,
        facts=[
            {
                "kind": "world",
                "subject": None,
                "proposition": "x",
                "story_time": None,
                "start": 0,
                "end": 3,
                "quote": "井沿",
            }
        ],
    )
    r = generation.write_chapter(
        project, ch1.id, WriteMode.FORMAL, msg1, clients_for(body, bad_check), CONFIG
    )
    assert r.check_id is None
    assert authoring.get_chapter(project, ch1.id).current_draft_id == r.version_id
    failed = [
        row
        for row in project.conn.execute("SELECT id, task, status, error_code FROM run").fetchall()
        if row["task"] == "inspect"
    ]
    assert (
        len(failed) == 1
        and failed[0]["status"] == "failed"
        and failed[0]["error_code"] == "MODEL_FAILED"
    )
    assert project.conn.execute("SELECT COUNT(*) FROM check_result").fetchone()[0] == 0
    # 没有有效检查不能采用
    with pytest.raises(WorkbenchError) as exc:
        adopt_current(project, ch1.id, r)
    assert exc.value.code is ErrorCode.REVIEW_REQUIRED
    # 重试只重跑检查，不重新生成
    counter: dict = {}
    good_check = inspection_json(body, plot="调查旧井", ending="看到尸体")
    check_id = generation.retry_run(
        project, failed[0]["id"], msg1, counting_clients(body, good_check, counter), CONFIG
    )
    assert counter == {"inspect": 1}
    assert isinstance(check_id, str)
    retry_run = [
        row
        for row in project.conn.execute(
            "SELECT retry_of_id FROM run WHERE task = 'inspect' AND status = 'succeeded'"
        ).fetchall()
    ]
    assert retry_run[0]["retry_of_id"] == failed[0]["id"]
    assert len(authoring.list_versions(project, ch1.id)) == 1


# ---------------------------------------------------------------- T09 取消与迟到结果


def test_cancel_is_recorded_first_and_late_chunks_do_not_update_draft(project):
    ch1, msg1 = plan_chapter(project, 1, "调查旧井", "看到尸体")
    cancel = CancelSignal()
    seen: list[str] = []

    def slow_stream(messages, options, cancel_signal) -> Iterator[StreamEvent]:
        yield StreamEvent(EventKind.TEXT, text="第一块")
        # 作者此刻取消：先落库，再置位
        run_id = database.any_run_in_flight(project.conn, project.novel_id)
        assert generation.cancel_run(project, run_id, cancel)
        assert (
            database.get_run(project.conn, project.novel_id, run_id).status is RunStatus.CANCELLED
        )
        yield StreamEvent(EventKind.TEXT, text="迟到块")
        yield StreamEvent(EventKind.COMPLETED)

    clients = generation.Clients(stream=slow_stream, complete=fixed_complete("{}"))
    with pytest.raises(WorkbenchError) as exc:
        generation.write_chapter(
            project,
            ch1.id,
            WriteMode.FORMAL,
            msg1,
            clients,
            CONFIG,
            cancel=cancel,
            on_text=seen.append,
        )
    assert exc.value.code is ErrorCode.MODEL_FAILED and exc.value.details["status"] == "cancelled"
    assert seen == ["第一块"]
    run = database.get_run(project.conn, project.novel_id, exc.value.details["run_id"])
    assert run.partial_text == "第一块"  # 只有已落库的块可恢复
    assert run.status is RunStatus.CANCELLED and run.result_version_id is None
    assert authoring.list_versions(project, ch1.id) == []
    assert authoring.get_chapter(project, ch1.id).current_draft_id is None
    # 取消后没有在途任务，可以再次生成
    assert database.any_run_in_flight(project.conn, project.novel_id) is None


def test_generation_while_run_in_flight_is_rejected(project):
    ch1, msg1 = plan_chapter(project, 1, "调查旧井", "看到尸体")

    def nested_stream(messages, options, cancel_signal) -> Iterator[StreamEvent]:
        with pytest.raises(WorkbenchError) as exc:
            generation.write_chapter(
                project, ch1.id, WriteMode.FORMAL, msg1, clients_for("y", "{}"), CONFIG
            )
        assert exc.value.code is ErrorCode.PROJECT_BUSY
        yield StreamEvent(EventKind.TEXT, text="正文")
        yield StreamEvent(EventKind.COMPLETED)

    body = "正文"
    clients = generation.Clients(
        stream=nested_stream,
        complete=fixed_complete(inspection_json(body, plot="调查旧井", ending="看到尸体")),
    )
    r = generation.write_chapter(project, ch1.id, WriteMode.FORMAL, msg1, clients, CONFIG)
    assert r.check_id is not None


def test_truncated_stream_is_failed_not_completed(project):
    ch1, msg1 = plan_chapter(project, 1, "调查旧井", "看到尸体")
    clients = generation.Clients(
        stream=fixed_stream(["半截"], truncated=True), complete=fixed_complete("{}")
    )
    with pytest.raises(WorkbenchError) as exc:
        generation.write_chapter(project, ch1.id, WriteMode.FORMAL, msg1, clients, CONFIG)
    assert exc.value.details["status"] == "truncated"
    run = database.get_run(project.conn, project.novel_id, exc.value.details["run_id"])
    assert run.status is RunStatus.FAILED and run.partial_text == "半截"
    assert authoring.list_versions(project, ch1.id) == []


def test_edit_during_generation_keeps_result_as_historical_draft(project):
    ch1, msg1 = plan_chapter(project, 1, "调查旧井", "看到尸体")
    base = authoring.save_draft(project, ch1.id, "作者先写的开头", Expected())

    def stream_with_edit(messages, options, cancel_signal) -> Iterator[StreamEvent]:
        # 生成期间作者又保存了一稿
        authoring.save_draft(
            project, ch1.id, "作者中途改的稿", Expected(current_draft_id=base.version_id)
        )
        yield StreamEvent(EventKind.TEXT, text="模型稿")
        yield StreamEvent(EventKind.COMPLETED)

    clients = generation.Clients(
        stream=stream_with_edit,
        complete=fixed_complete(inspection_json("模型稿", plot="调查旧井", ending="看到尸体")),
    )
    r = generation.write_chapter(project, ch1.id, WriteMode.FORMAL, msg1, clients, CONFIG)
    assert r.based_on_old_inputs
    current = authoring.get_chapter(project, ch1.id).current_draft_id
    assert (
        authoring.get_version(project, Target(project.novel_id, ch1.id, current)).body
        == "作者中途改的稿"
    )
    bodies = [v.body for v in authoring.list_versions(project, ch1.id)]
    assert bodies == ["作者先写的开头", "作者中途改的稿", "模型稿"]


def test_decision_change_after_context_makes_basis_stale(project):
    ch1, msg1 = plan_chapter(project, 1, "调查旧井", "看到尸体")
    item = authoring.record_item(
        project,
        kind="character",
        body="师父右腕有旧伤",
        authority=Authority.AUTHOR_DECISION,
        lifecycle=Lifecycle.DECIDED,
        source_message_id=msg1,
    )
    draft = generation.build_context(project, ch1.id, WriteMode.FORMAL, CONFIG)
    assert "师父右腕有旧伤" in draft.rules
    authoring.decide_item(
        project,
        item,
        lifecycle=Lifecycle.DECIDED,
        source_message_id=msg1,
        body="师父左腕有旧伤",
        authorize_change=True,
    )
    with pytest.raises(WorkbenchError) as exc:
        generation.save_basis(project, draft, "formal")
    assert exc.value.code is ErrorCode.STALE_BASIS


def test_discussion_reply_is_saved_as_message_only(project):
    ch1, _ = plan_chapter(project, 1, None, None)
    clients = generation.Clients(
        stream=fixed_stream(["可以让他", "先在井口犹豫。"]), complete=fixed_complete("{}")
    )
    author_id, model_id = generation.discuss(project, ch1.id, "第一章从哪写起？", clients, CONFIG)
    msgs = database.list_messages(project.conn, project.novel_id)
    assert [(m.role.value, m.body) for m in msgs][-2:] == [
        ("author", "第一章从哪写起？"),
        ("model", "可以让他先在井口犹豫。"),
    ]
    assert authoring.list_story_items(project) == []  # 不自动抽取想法

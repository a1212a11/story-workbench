"""G04/G06：用固定客户端跑三章闭环、拒绝稿隔离、检查失败重试、取消与迟到结果保护。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from story_workbench.ai.context import ContextLimits
from story_workbench.application import adoption, authoring, generation
from story_workbench.application.projects import Project
from story_workbench.domain.records import (
    AuthorAction,
    Authority,
    ErrorCode,
    Expected,
    IssueResolution,
    Lifecycle,
    ResolutionAction,
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


@pytest.mark.parametrize("variant", ["original", "missing", "ambiguous"])
def test_deepseek_real_response_locations_are_repaired_before_commit(project, variant):
    sample = json.loads(
        (Path(__file__).parent / "fixtures" / "deepseek-inspection-offsets.json").read_text()
    )
    body, payload = sample["body"], sample["response"]
    if variant == "missing":
        payload["candidate_facts"][1]["quote"] = "正文没有这句话。"
    elif variant == "ambiguous":
        body += body
    chapter, message = plan_chapter(project, 1, "沈砚观察井沿", "仍站在井边")
    saved = authoring.save_draft(project, chapter.id, body, Expected())
    target = Target(project.novel_id, chapter.id, saved.version_id)
    clients = clients_for("", json.dumps(payload, ensure_ascii=False))
    if variant != "original":
        with pytest.raises(WorkbenchError) as exc:
            generation.check_draft(project, target, message, clients, CONFIG)
        assert exc.value.code is ErrorCode.MODEL_FAILED
        assert database.list_runs_with_status(project.conn, project.novel_id, RunStatus.FAILED)
        assert (
            database.latest_check_for_version(project.conn, project.novel_id, saved.version_id)
            is None
        )
        assert project.conn.execute("SELECT COUNT(*) FROM candidate_fact").fetchone()[0] == 0
        return
    check_id = generation.check_draft(project, target, message, clients, CONFIG)
    facts = database.list_candidate_facts(project.conn, project.novel_id, check_id)
    assert [(f.evidence_start, f.evidence_end) for f in facts] == [(0, 49), (14, 32)]
    assert all(body[f.evidence_start : f.evidence_end] == f.quote for f in facts)
    assert authoring.get_chapter(project, chapter.id).adopted_id is None


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
                "quote": "井中有鱼",
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


# ---------------------------------------------------------------- T10 回改第一章，逐章复核


def _write_and_adopt(project, number, plot, ending, body, fact_quote, fact_prop):
    ch, msg = plan_chapter(project, number, plot, ending)
    check = inspection_json(
        body, facts=[belief_fact(body, fact_quote, "沈砚", fact_prop)], plot=plot, ending=ending
    )
    r = generation.write_chapter(
        project, ch.id, WriteMode.FORMAL, msg, clients_for(body, check), CONFIG
    )
    a = adopt_current(project, ch.id, r)
    assert a.state_status is SnapshotStatus.READY
    return ch, msg, r, body


def _revalidate(project, chapter_id, version_id, check_id, resolutions=()):
    ch = authoring.get_chapter(project, chapter_id)
    prev = [c for c in authoring.list_chapters(project) if c.number == ch.number - 1]
    prev_snapshot = prev[0].current_snapshot_id if prev else None
    msg = authoring.record_message(project, "重新检查看完了，确认无需改动。", chapter_id)
    action = AuthorAction(
        request_id=uuid.uuid4().hex,
        source_message_id=msg,
        target=Target(project.novel_id, chapter_id, version_id),
        expected=Expected(
            adopted_id=ch.adopted_id, plan_id=ch.current_plan_id, previous_snapshot_id=prev_snapshot
        ),
        check_id=check_id,
        read_declared=True,
        issue_resolutions=tuple(resolutions),
    )
    return adoption.revalidate_chapter(project, action)


def test_t10_readopt_chapter_one_then_revalidate_later_chapters_without_new_adoption(project):
    ch1, msg1, r1, body1 = _write_and_adopt(
        project,
        1,
        "调查旧井",
        "看到疑似师父的尸体",
        "井底有一具尸体，他怀疑是师父。",
        "他怀疑是师父",
        "沈砚怀疑尸体是师父",
    )
    ch2, msg2, r2, body2 = _write_and_adopt(
        project,
        2,
        "进旧屋",
        "停在内门前",
        "他还疑心井底那人就是师父。他停在内门前。",
        "他停在内门前",
        "沈砚在内门前",
    )
    ch3, msg3, r3, body3 = _write_and_adopt(
        project,
        3,
        "敲门",
        "门外等待",
        "他敲了一下。门内没有回答。",
        "门内没有回答",
        "敲门后没有回应",
    )
    counts_before = {
        t: project.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("adoption", "version")
    }

    # 回改第一章：保存、检查、重新采用
    new_body1 = body1 + "他拨开尸体右腕的袖子，没有师父那道旧伤。"
    saved = authoring.save_draft(
        project, ch1.id, new_body1, Expected(current_draft_id=r1.version_id)
    )
    check1b = inspection_json(
        new_body1,
        facts=[belief_fact(new_body1, "没有师父那道旧伤", "沈砚", "沈砚怀疑尸体不是师父")],
        plot="调查旧井",
        ending="怀疑尸体不是师父",
    )
    cid = generation.check_draft(
        project,
        Target(project.novel_id, ch1.id, saved.version_id),
        msg1,
        clients_for("", check1b),
        CONFIG,
    )

    class R:  # 复用 adopt_current 的结构
        version_id = saved.version_id
        check_id = cid

    a1b = adopt_current(project, ch1.id, R)
    assert a1b.state_status is SnapshotStatus.READY
    for ch in (ch2, ch3):
        c = authoring.get_chapter(project, ch.id)
        assert c.needs_review and c.current_snapshot_id is None
        assert adoption.pending_impacts(project, ch.id)

    # 依赖过时状态的续写被阻断
    ch4, msg4 = plan_chapter(project, 4, "进门", "看见屋内")
    with pytest.raises(WorkbenchError) as blocked:
        generation.write_chapter(
            project, ch4.id, WriteMode.FORMAL, msg4, clients_for("x", "{}"), CONFIG
        )
    assert blocked.value.code is ErrorCode.STALE_BASIS

    # 第二章：旧检查不能复核（依据里的前章快照已变）
    old_check2 = database.latest_check_for_version(project.conn, project.novel_id, r2.version_id)
    with pytest.raises(WorkbenchError) as stale:
        _revalidate(project, ch2.id, r2.version_id, old_check2.id)
    assert stale.value.code is ErrorCode.STALE_BASIS
    # 新检查 + 复核：无新正文、无新采用，影响解除，状态 ready
    check2b = inspection_json(
        body2,
        facts=[belief_fact(body2, "他停在内门前", "沈砚", "沈砚在内门前")],
        plot="进旧屋",
        ending="停在内门前",
    )
    cid2 = generation.check_draft(
        project,
        Target(project.novel_id, ch2.id, r2.version_id),
        msg2,
        clients_for("", check2b),
        CONFIG,
    )
    assert _revalidate(project, ch2.id, r2.version_id, cid2) is SnapshotStatus.READY
    c2 = authoring.get_chapter(project, ch2.id)
    assert not c2.needs_review and c2.current_snapshot_id is not None
    assert adoption.pending_impacts(project, ch2.id) == ()
    # 第三章同样复核
    check3b = inspection_json(
        body3,
        facts=[belief_fact(body3, "门内没有回答", "沈砚", "敲门后没有回应")],
        plot="敲门",
        ending="门外等待",
    )
    cid3 = generation.check_draft(
        project,
        Target(project.novel_id, ch3.id, r3.version_id),
        msg3,
        clients_for("", check3b),
        CONFIG,
    )
    assert _revalidate(project, ch3.id, r3.version_id, cid3) is SnapshotStatus.READY
    counts_after = {
        t: project.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("adoption", "version")
    }
    assert counts_after["adoption"] == counts_before["adoption"] + 1  # 只有第一章重新采用
    assert counts_after["version"] == counts_before["version"] + 1  # 只有第一章新稿
    # 第三章快照继承新的第一章事实
    snap3 = authoring.get_chapter(project, ch3.id).current_snapshot_id
    props = [
        d.proposition
        for d in database.list_snapshot_fact_details(project.conn, project.novel_id, snap3)
    ]
    assert props == ["沈砚怀疑尸体不是师父", "沈砚在内门前", "敲门后没有回应"]
    # 第四章现在可以写
    r4 = generation.write_chapter(
        project,
        ch4.id,
        WriteMode.FORMAL,
        msg4,
        clients_for(
            "他推门进去。", inspection_json("他推门进去。", plot="进门", ending="看见屋内")
        ),
        CONFIG,
    )
    assert r4.check_id is not None


def test_revalidation_with_unresolved_setting_keeps_chapter_pending(project):
    ch1, msg1, r1, body1 = _write_and_adopt(
        project, 1, "调查旧井", "看到尸体", "井底有尸体。", "井底有尸体", "井底有尸体"
    )
    ch2, msg2, r2, body2 = _write_and_adopt(
        project, 2, "进旧屋", "内门前", "他撬开了锁。", "他撬开了锁", "沈砚开了锁"
    )
    # 回改第一章
    saved = authoring.save_draft(
        project, ch1.id, body1 + "灯灭了。", Expected(current_draft_id=r1.version_id)
    )
    cid = generation.check_draft(
        project,
        Target(project.novel_id, ch1.id, saved.version_id),
        msg1,
        clients_for("", inspection_json(body1 + "灯灭了。", plot="调查旧井", ending="灯灭")),
        CONFIG,
    )

    class R:
        version_id = saved.version_id
        check_id = cid

    adopt_current(project, ch1.id, R)
    # 第二章重新检查时发现新增持久设定，作者不授权 → 复核被拒，仍待复核
    check2b = inspection_json(
        body2,
        issues=[setting_issue(body2, "撬开了锁", "新增开锁能力")],
        plot="进旧屋",
        ending="内门前",
    )
    cid2 = generation.check_draft(
        project,
        Target(project.novel_id, ch2.id, r2.version_id),
        msg2,
        clients_for("", check2b),
        CONFIG,
    )
    with pytest.raises(WorkbenchError) as exc:
        _revalidate(project, ch2.id, r2.version_id, cid2)
    assert exc.value.code is ErrorCode.AUTHORIZATION_REQUIRED
    c2 = authoring.get_chapter(project, ch2.id)
    assert c2.needs_review and adoption.pending_impacts(project, ch2.id)
    # 授权后复核通过
    issue_id = database.get_check(project.conn, project.novel_id, cid2).issues[0].id
    status = _revalidate(
        project,
        ch2.id,
        r2.version_id,
        cid2,
        resolutions=[IssueResolution(issue_id, ResolutionAction.AUTHORIZE_SETTING, "接受开锁能力")],
    )
    assert status is SnapshotStatus.READY
    assert not authoring.get_chapter(project, ch2.id).needs_review
    # 复核不针对非当前采用版本
    other = authoring.save_draft(
        project, ch2.id, "另一稿", Expected(current_draft_id=r2.version_id)
    )
    cid_other = generation.check_draft(
        project,
        Target(project.novel_id, ch2.id, other.version_id),
        msg2,
        clients_for("", inspection_json("另一稿", plot="进旧屋", ending="内门前")),
        CONFIG,
    )
    with pytest.raises(WorkbenchError) as wrong:
        _revalidate(project, ch2.id, other.version_id, cid_other)
    assert wrong.value.code is ErrorCode.INVALID_INPUT


# ---------------------------------------------------------------- T12 阅读体验诊断与试改


def diagnosis_json(body: str, quote: str) -> str:
    return json.dumps(
        {
            "verdict": "开头平铺直叙，缺少悬念。",
            "problems": [
                {
                    "explanation": "第一句直接交代结果，读者没有期待。",
                    "evidence": [evidence(body, quote)],
                }
            ],
            "options": [
                {
                    "description": "先写灯影，再让尸体从阴影里显出来。",
                    "requires_new_setting": False,
                },
                {"description": "让沈砚认出尸体身上的师门信物。", "requires_new_setting": True},
            ],
            "new_settings_requiring_authorization": ["师门信物"],
        },
        ensure_ascii=False,
    )


def test_t12_diagnosis_and_trial_revision_keep_original_and_do_not_change_rules(project):
    ch1, msg1 = plan_chapter(project, 1, "调查旧井", "看到尸体")
    body = "井底有一具尸体。沈砚把灯搁在井沿。"
    r = generation.write_chapter(
        project,
        ch1.id,
        WriteMode.FORMAL,
        msg1,
        clients_for(body, inspection_json(body, plot="调查旧井", ending="看到尸体")),
        CONFIG,
    )
    target = Target(project.novel_id, ch1.id, r.version_id)
    rules_before = authoring.list_story_items(project)
    # 诊断：有证据、有选项、需授权设定单列；不生成改稿、不改规则
    clients = generation.Clients(
        stream=fixed_stream(["x"]),
        complete=fixed_complete(diagnosis_json(body, "井底有一具尸体。")),
    )
    feedback_id, payload = generation.diagnose_draft(
        project, target, "没意思，帮我看看节奏。", clients, CONFIG
    )
    assert payload.problems[0].evidence[0].quote == "井底有一具尸体。"
    assert [o.requires_new_setting for o in payload.options] == [False, True]
    assert payload.new_settings_requiring_authorization == ("师门信物",)
    assert len(authoring.list_versions(project, ch1.id)) == 1
    assert authoring.list_story_items(project) == rules_before
    msgs = database.list_messages(project.conn, project.novel_id)
    assert msgs[-2].body == "没意思，帮我看看节奏。" and msgs[-1].body.startswith("诊断：")
    # 试改：候选稿不替换当前稿，原稿仍在；作者可保留或拒绝
    revised = "灯影落稳之后，井壁下那团灰衣才慢慢显出人形。"
    clients2 = clients_for(revised, inspection_json(revised, plot="调查旧井", ending="看到尸体"))
    r2 = generation.revise_draft(
        project, target, payload.options[0].description, feedback_id, clients2, CONFIG
    )
    assert authoring.get_chapter(project, ch1.id).current_draft_id == r.version_id
    bodies = [v.body for v in authoring.list_versions(project, ch1.id)]
    assert bodies == [body, revised]
    run = database.get_run(project.conn, project.novel_id, r2.run_id)
    assert "按作者选定的修改方向重写" in json.loads(run.request_json)["assumptions"][0]
    # 作者不满意可拒绝候选稿，原稿不受影响
    reject_msg = authoring.record_message(project, "还是不喜欢，不要这版。", ch1.id)
    authoring.reject_draft(project, Target(project.novel_id, ch1.id, r2.version_id), reject_msg)
    assert authoring.get_chapter(project, ch1.id).current_draft_id == r.version_id
    # 作者喜欢则可选它为当前稿再走检查采用
    authoring.choose_current_draft(
        project,
        Target(project.novel_id, ch1.id, r2.version_id),
        Expected(current_draft_id=r.version_id),
    )
    assert authoring.get_chapter(project, ch1.id).current_draft_id == r2.version_id


def test_t12_revision_uses_selected_history_version_and_feedback_survives_trimming(project):
    ch1, msg1 = plan_chapter(project, 1, "调查旧井", "看到尸体")
    old_body = "井底有一具尸体。沈砚把灯搁在井沿。"
    r = generation.write_chapter(
        project,
        ch1.id,
        WriteMode.FORMAL,
        msg1,
        clients_for(old_body, inspection_json(old_body, plot="调查旧井", ending="看到尸体")),
        CONFIG,
    )
    current_body = "当前稿：沈砚只看到一盏灯。"
    current = authoring.save_draft(
        project, ch1.id, current_body, Expected(current_draft_id=r.version_id)
    )
    long_discussion = "这段普通讨论会被裁掉：" + "很长。" * 120
    authoring.record_message(project, long_discussion, ch1.id)
    target = Target(project.novel_id, ch1.id, r.version_id)
    seen: dict[str, str] = {}
    feedback = "这次反馈必须进入诊断：节奏太平。"

    def complete(messages, options, cancel):
        seen["diagnose"] = messages[-1].content
        return fixed_complete(diagnosis_json(old_body, "井底有一具尸体。"))(
            messages, options, cancel
        )

    small_config = generation.GenerationConfig(
        options=OPTIONS,
        limits=ContextLimits(total_tokens=950, template_reserve=500, output_reserve=100),
    )
    feedback_id, payload = generation.diagnose_draft(
        project,
        target,
        feedback,
        generation.Clients(stream=fixed_stream(["x"]), complete=complete),
        small_config,
    )
    assert payload.problems
    assert feedback in seen["diagnose"]
    assert long_discussion not in seen["diagnose"]

    revised = "旧稿试改：井底那团灰衣先显出轮廓。"
    check_json = inspection_json(revised, plot="调查旧井", ending="看到尸体")

    def stream(messages, options, cancel):
        seen["generate"] = messages[-1].content
        yield from fixed_stream([revised])(messages, options, cancel)

    r2 = generation.revise_draft(
        project,
        target,
        payload.options[0].description,
        feedback_id,
        generation.Clients(stream=stream, complete=fixed_complete(check_json)),
        CONFIG,
    )
    run = database.get_run(project.conn, project.novel_id, r2.run_id)
    new_version = authoring.get_version(project, Target(project.novel_id, ch1.id, r2.version_id))
    assert run.input_version_id == r.version_id
    assert new_version.parent_id == r.version_id
    assert old_body in seen["generate"]
    assert current_body not in seen["generate"]
    assert authoring.get_chapter(project, ch1.id).current_draft_id == current.version_id

    ch2, _ = plan_chapter(project, 2, "去旧屋", "门前")
    with pytest.raises(WorkbenchError) as exc:
        generation.revise_draft(
            project,
            Target(project.novel_id, ch2.id, r.version_id),
            payload.options[0].description,
            feedback_id,
            generation.Clients(stream=stream, complete=fixed_complete(check_json)),
            CONFIG,
        )
    assert exc.value.code is ErrorCode.WRONG_SCOPE


def test_t12_required_revision_and_diagnosis_inputs_must_fit_context(project):
    ch1, msg1 = plan_chapter(project, 1, "调查旧井", "看到尸体")
    body = "井底有一具尸体。"
    r = generation.write_chapter(
        project,
        ch1.id,
        WriteMode.FORMAL,
        msg1,
        clients_for(body, inspection_json(body, plot="调查旧井", ending="看到尸体")),
        CONFIG,
    )
    tiny_config = generation.GenerationConfig(
        options=OPTIONS,
        limits=ContextLimits(total_tokens=650, template_reserve=500, output_reserve=100),
    )

    calls = {"diagnose": 0, "revise": 0}

    def complete(messages, options, cancel):
        calls["diagnose"] += 1
        return fixed_complete(diagnosis_json(body, "井底有一具尸体。"))(messages, options, cancel)

    with pytest.raises(WorkbenchError) as feedback_too_large:
        generation.diagnose_draft(
            project,
            Target(project.novel_id, ch1.id, r.version_id),
            "反馈太大：" + "节奏。" * 80,
            generation.Clients(stream=fixed_stream(["x"]), complete=complete),
            tiny_config,
        )
    assert feedback_too_large.value.code is ErrorCode.CONTEXT_TOO_LARGE
    assert calls["diagnose"] == 0

    large = authoring.save_draft(
        project,
        ch1.id,
        "正文太大：" + "井底有一具尸体。" * 80,
        Expected(current_draft_id=r.version_id),
    )

    def stream(messages, options, cancel):
        calls["revise"] += 1
        yield from fixed_stream(["不会调用"])(messages, options, cancel)

    with pytest.raises(WorkbenchError) as body_too_large:
        generation.revise_draft(
            project,
            Target(project.novel_id, ch1.id, large.version_id),
            "压缩节奏",
            msg1,
            generation.Clients(stream=stream, complete=complete),
            tiny_config,
        )
    assert body_too_large.value.code is ErrorCode.CONTEXT_TOO_LARGE
    assert calls == {"diagnose": 0, "revise": 0}

    rows = project.conn.execute(
        "SELECT task FROM run WHERE task IN ('diagnose', 'generate') ORDER BY rowid"
    ).fetchall()
    assert [row[0] for row in rows] == ["generate"]


def test_t12_diagnosis_provider_error_marks_run_failed(project):
    ch1, msg1 = plan_chapter(project, 1, "调查旧井", "看到尸体")
    body = "井底有一具尸体。沈砚把灯搁在井沿。"
    r = generation.write_chapter(
        project,
        ch1.id,
        WriteMode.FORMAL,
        msg1,
        clients_for(body, inspection_json(body, plot="调查旧井", ending="看到尸体")),
        CONFIG,
    )

    def complete(messages, options, cancel):
        raise RuntimeError("provider stopped oddly")

    with pytest.raises(WorkbenchError) as exc:
        generation.diagnose_draft(
            project,
            Target(project.novel_id, ch1.id, r.version_id),
            "帮我诊断节奏。",
            generation.Clients(stream=fixed_stream(["x"]), complete=complete),
            CONFIG,
        )
    assert exc.value.code is ErrorCode.MODEL_FAILED
    row = project.conn.execute(
        "SELECT status, error_code FROM run WHERE task = 'diagnose' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert tuple(row) == ("failed", "MODEL_FAILED")


def test_retry_failed_trial_revision_keeps_selected_source_and_candidate_status(project):
    chapter, message = plan_chapter(project, 1, "调查旧井", "看到尸体")
    original = authoring.save_draft(project, chapter.id, "待试改旧稿。", Expected())
    current = authoring.save_draft(
        project, chapter.id, "当前编辑稿。", Expected(current_draft_id=original.version_id)
    )

    def fail(*args):
        yield StreamEvent(EventKind.ERROR, error="connection_lost")

    with pytest.raises(WorkbenchError):
        generation.revise_draft(
            project,
            Target(project.novel_id, chapter.id, original.version_id),
            "改善节奏",
            message,
            generation.Clients(stream=fail, complete=fixed_complete("{}")),
            CONFIG,
        )
    failed = database.list_runs_with_status(project.conn, project.novel_id, RunStatus.FAILED)[0]
    seen = []

    def stream(messages, options, cancel):
        seen.append(messages[-1].content)
        yield from fixed_stream(["试改结果。"])(messages, options, cancel)

    result = generation.retry_run(
        project,
        failed,
        message,
        generation.Clients(stream=stream, complete=fixed_complete(inspection_json("试改结果。"))),
        CONFIG,
    )
    version = authoring.get_version(
        project, Target(project.novel_id, chapter.id, result.version_id)
    )
    assert version.parent_id == original.version_id
    assert "待试改旧稿。" in seen[0] and "当前编辑稿。" not in seen[0]
    assert authoring.get_chapter(project, chapter.id).current_draft_id == current.version_id

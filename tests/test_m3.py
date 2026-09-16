from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import replace
from types import SimpleNamespace

import pytest

from story_workbench.ai.context import ContextLimits
from story_workbench.application import adoption, authoring, generation, projects
from story_workbench.domain.records import (
    AuthorAction,
    Authority,
    BranchMaterializationStatus,
    CandidateFactInput,
    CheckInput,
    EvidenceInput,
    Expected,
    FactCorrection,
    FactKind,
    ForeshadowStage,
    IssueInput,
    IssueKind,
    IssueResolution,
    Lifecycle,
    ResolutionAction,
    RunStatus,
    Target,
    TaskKind,
    WorkbenchError,
    WriteMode,
)
from story_workbench.infrastructure import database
from story_workbench.infrastructure.provider import CallOptions
from tests.conftest import fixed_complete, fixed_stream
from tests.test_workflow import adopt_current, inspection_json

CONFIG = generation.GenerationConfig(
    options=CallOptions(model="fixed", max_output_tokens=1000, timeout_seconds=5),
    limits=ContextLimits(total_tokens=20000, template_reserve=500, output_reserve=1000),
)


def _adopted(project, number: int, body: str):
    chapter = authoring.create_chapter(project, number)
    msg = authoring.record_message(project, f"第{number}章计划。", chapter.id)
    authoring.save_plan(
        project, chapter.id, f"第{number}章剧情", f"第{number}章结尾", Expected(), msg
    )
    clients = generation.Clients(
        stream=fixed_stream([body]),
        complete=fixed_complete(
            inspection_json(body, plot=f"第{number}章剧情", ending=f"第{number}章结尾")
        ),
    )
    result = generation.write_chapter(project, chapter.id, WriteMode.FORMAL, msg, clients, CONFIG)
    adopted = adopt_current(project, chapter.id, result)
    return authoring.get_chapter(project, chapter.id), result, adopted


def _publish_action(project, chapter, version_id: str, message_id: str) -> AuthorAction:
    return AuthorAction(
        request_id=uuid.uuid4().hex,
        source_message_id=message_id,
        target=Target(project.novel_id, chapter.id, version_id),
        expected=Expected(adopted_id=chapter.adopted_id),
        read_declared=True,
    )


def test_branch_fork_uses_independent_novel_and_branch_workdir(project):
    ch1, result1, adopted1 = _adopted(project, 1, "沈砚在井边看见蓝火。")
    authoring.create_chapter(project, 2)
    main_work = projects.create_workcopy(project, ch1.id).relative_path

    branch = projects.create_branch(project, "蓝火支线", adopted1.adoption_id)
    assert branch.novel_id != project.novel_id
    assert branch.work_subdir == f"branches/{branch.id}"
    assert branch.materialization_status is BranchMaterializationStatus.READY

    projects.switch_branch(project, branch.id)
    chapters = authoring.list_chapters(project)
    assert [c.number for c in chapters] == [1]
    branch_body = authoring.get_version(
        project, Target(project.novel_id, chapters[0].id, chapters[0].current_draft_id)
    ).body
    assert branch_body == "沈砚在井边看见蓝火。"
    branch_work = projects.create_workcopy(project, chapters[0].id).relative_path
    assert branch_work == f"work/branches/{branch.id}/chapter-001.md"
    assert main_work == "work/chapter-001.md"


def test_branch_materialization_failure_leaves_staging_and_blocks_switch(project, monkeypatch):
    _ch1, _result1, adopted1 = _adopted(project, 1, "沈砚在井边看见蓝火。")

    def fail_branch_dir(*_args):
        raise WorkbenchError(ErrorCode.STORAGE_FAILED, "目录创建失败。")

    from story_workbench.domain.records import ErrorCode

    monkeypatch.setattr(projects.files, "ensure_branch_dir", fail_branch_dir)
    with pytest.raises(WorkbenchError):
        projects.create_branch(project, "失败分支", adopted1.adoption_id)

    branch = next(b for b in projects.list_branches(project) if b.name == "失败分支")
    assert branch.materialization_status is BranchMaterializationStatus.STAGING
    with pytest.raises(WorkbenchError):
        projects.switch_branch(project, branch.id)


def test_switch_reconciles_current_branch_workcopy_before_changing_branch(project):
    ch1, _result1, adopted1 = _adopted(project, 1, "沈砚在井边看见蓝火。")
    main_novel_id = project.novel_id
    workcopy = projects.create_workcopy(project, ch1.id)
    (project.root / workcopy.relative_path).write_text("沈砚在井边看见白火。")
    branch = projects.create_branch(project, "蓝火支线", adopted1.adoption_id)

    projects.switch_branch(project, branch.id)

    old_chapter = database.get_chapter(project.conn, main_novel_id, ch1.id)
    assert old_chapter.current_draft_id is not None
    imported = database.get_version(project.conn, main_novel_id, old_chapter.current_draft_id)
    assert imported.body == "沈砚在井边看见白火。"


def test_open_project_interrupts_runs_on_all_branches(project, project_dir):
    _ch1, _result1, adopted1 = _adopted(project, 1, "沈砚在井边看见蓝火。")
    branch = projects.create_branch(project, "蓝火支线", adopted1.adoption_id)
    branch_chapter = database.list_chapters(project.conn, branch.novel_id)[0]
    branch_message = project.conn.execute(
        "SELECT id FROM message WHERE novel_id = ? ORDER BY created_at, rowid LIMIT 1",
        (branch.novel_id,),
    ).fetchone()[0]
    run_id = database.commit_run(
        project.conn,
        branch.novel_id,
        chapter_id=branch_chapter.id,
        task=TaskKind.GENERATE,
        source_message_id=branch_message,
        template_version="m3",
        status=RunStatus.RUNNING,
    )
    project.close()

    reopened, notice = projects.open_project(project_dir)
    try:
        assert run_id in notice.interrupted_run_ids
        run = database.get_run(reopened.conn, branch.novel_id, run_id)
        assert run.status is RunStatus.INTERRUPTED
    finally:
        reopened.close()


def test_publication_is_exact_and_later_adoption_requires_authorization(project):
    ch1, result1, _adopted1 = _adopted(project, 1, "沈砚第一次下井。")
    msg = authoring.record_message(project, "这一版已经发布。", ch1.id)
    published = adoption.mark_published(
        project, _publish_action(project, ch1, result1.version_id, msg)
    )
    assert adoption.list_publications(project, ch1.id)[0].version_id == result1.version_id

    saved = authoring.save_draft(
        project, ch1.id, "沈砚第二次下井。", Expected(current_draft_id=result1.version_id)
    )
    check_msg = authoring.record_message(project, "检查新版。", ch1.id)
    clients = generation.Clients(
        stream=fixed_stream([]),
        complete=fixed_complete(
            inspection_json("沈砚第二次下井。", plot="第1章剧情", ending="第1章结尾")
        ),
    )
    check_id = generation.check_draft(
        project, Target(project.novel_id, ch1.id, saved.version_id), check_msg, clients, CONFIG
    )
    review_msg = authoring.record_message(project, "已读，采用新版。", ch1.id)
    action = AuthorAction(
        request_id=uuid.uuid4().hex,
        source_message_id=review_msg,
        target=Target(project.novel_id, ch1.id, saved.version_id),
        expected=Expected(adopted_id=ch1.adopted_id, plan_id=ch1.current_plan_id),
        check_id=check_id,
        read_declared=True,
    )
    with pytest.raises(WorkbenchError):
        adoption.adopt(project, action)
    action = replace(
        action,
        allow_published_revision=True,
        divergence_reason="作者授权修订已发布原文。",
    )
    adoption.adopt(project, action)
    assert adoption.list_publications(project, ch1.id)[0].id == published.id
    assert project.conn.execute("SELECT COUNT(*) FROM publication_divergence").fetchone()[0] == 1


def test_remediation_and_foreshadow_are_author_bound(project):
    ch1, result1, adopted1 = _adopted(project, 1, "门缝里有一枚银针。")
    msg = authoring.record_message(project, "发布并记录伏笔。", ch1.id)
    publication = adoption.mark_published(
        project, _publish_action(project, ch1, result1.version_id, msg)
    )
    plan = adoption.propose_remediation(project, publication.id, msg, "后续用银针解释门锁。")
    assert plan.publication_id == publication.id

    event = adoption.record_foreshadow(
        project,
        stage=ForeshadowStage.SETUP,
        chapter_id=ch1.id,
        body="银针是后续开锁线索。",
        quote="银针",
        adoption_id=adopted1.adoption_id,
        source_message_id=msg,
        version_id=result1.version_id,
    )
    assert event.stage is ForeshadowStage.SETUP
    assert adoption.list_foreshadow(project)[0].quote == "银针"


def _world_fact(body: str, quote: str, proposition: str) -> dict:
    start = body.index(quote)
    return {
        "kind": "world",
        "subject": None,
        "proposition": proposition,
        "story_time": None,
        "start": start,
        "end": start + len(quote),
        "quote": quote,
    }


def _adopt_body_with_corrected_fact(
    project,
    chapter,
    message_id: str,
    body: str,
    proposition: str,
    *,
    expected_current_draft_id: str | None = None,
    expected_adoption_id: str | None = None,
    allow_published_revision: bool = False,
):
    current = authoring.get_chapter(project, chapter.id)
    saved = authoring.save_draft(
        project,
        chapter.id,
        body,
        Expected(current_draft_id=expected_current_draft_id),
    )
    novel = database.get_novel(project.conn)
    assert novel is not None
    previous = [c for c in authoring.list_chapters(project) if c.number == current.number - 1]
    previous_snapshot_id = previous[0].current_snapshot_id if previous else None
    basis_id = database.commit_basis(
        project.conn,
        project.novel_id,
        chapter.id,
        current.current_plan_id,
        previous_snapshot_id,
        novel.design_revision,
        "formal",
        [],
    )
    run_id = database.commit_run(
        project.conn,
        project.novel_id,
        chapter_id=chapter.id,
        task=TaskKind.INSPECT,
        source_message_id=message_id,
        template_version="test",
        status=RunStatus.SUCCEEDED,
        basis_id=basis_id,
        input_version_id=saved.version_id,
    )
    start = body.index("银针")
    check_id = database.commit_check_result(
        project.conn,
        project.novel_id,
        chapter.id,
        saved.version_id,
        basis_id,
        run_id,
        CheckInput(
            coverage_json='{"checked":["全文"],"unchecked":[]}',
            actual_plot=f"第{chapter.number}章剧情",
            actual_ending=f"第{chapter.number}章结尾",
            issues=(),
            candidate_facts=(
                CandidateFactInput(
                    kind=FactKind.WORLD,
                    proposition=proposition,
                    evidence_start=start,
                    evidence_end=start + len("银针"),
                    quote="银针",
                ),
            ),
        ),
    )
    fact = database.list_candidate_facts(project.conn, project.novel_id, check_id)[0]
    correction_message = authoring.record_message(project, "修正银针事实。", chapter.id)
    adoption.correct_fact(
        project,
        fact.id,
        FactCorrection(
            kind=FactKind.WORLD,
            proposition=proposition,
            evidence_start=fact.evidence_start,
            evidence_end=fact.evidence_end,
            quote=fact.quote,
        ),
        correction_message,
        None,
    )
    latest = authoring.get_chapter(project, chapter.id)
    review_message = authoring.record_message(project, "已读，采用含修正事实的版本。", chapter.id)
    action = AuthorAction(
        request_id=uuid.uuid4().hex,
        source_message_id=review_message,
        target=Target(project.novel_id, chapter.id, saved.version_id),
        expected=Expected(
            adopted_id=latest.adopted_id if expected_adoption_id is None else expected_adoption_id,
            plan_id=latest.current_plan_id,
            current_draft_id=expected_current_draft_id,
            previous_snapshot_id=previous_snapshot_id,
        ),
        check_id=check_id,
        read_declared=True,
        allow_published_revision=allow_published_revision,
        divergence_reason="作者授权修订已发布原文。" if allow_published_revision else "",
    )
    result = SimpleNamespace(version_id=saved.version_id, check_id=check_id)
    return result, adoption.adopt(project, action)


def test_real_impact_analysis_saves_pending_impact_evidence(project):
    ch1, _result1, adopted1 = _adopted(project, 1, "沈砚在门边发现银针。")
    body2 = "沈砚后来用银针打开暗门。"
    ch2, result2, _adopted2 = _adopted(project, 2, body2)
    quote = "银针打开暗门"
    start = body2.index(quote)
    payload = {
        "summary": "银针用途影响后文开门逻辑。",
        "chapters": [
            {
                "chapter_number": 2,
                "status": "affected",
                "reason": "第二章把银针当作开门工具，需要复核来源章修改。",
                "evidence": [{"start": start, "end": start + len(quote), "quote": quote}],
            }
        ],
        "coverage": {"checked": ["第2章已采用正文"], "unchecked": []},
    }
    msg = authoring.record_message(project, "分析银针修改影响。", ch1.id)
    clients = generation.Clients(
        stream=fixed_stream([]), complete=fixed_complete(json.dumps(payload))
    )

    _run_id, report = generation.analyze_impact(project, adopted1.adoption_id, msg, clients, CONFIG)

    impacts = adoption.list_impacts(project, ch2.id)
    assert len(impacts) == 1
    assert impacts[0].status.value == "pending"
    assert report.findings[0].evidence[0].quote == quote
    evidence_rows = project.conn.execute(
        "SELECT impact_id, analysis_id, version_id, start, end, quote FROM impact_evidence"
        " WHERE impact_id IS NOT NULL"
    ).fetchall()
    assert [dict(row) for row in evidence_rows] == [
        {
            "impact_id": impacts[0].id,
            "analysis_id": None,
            "version_id": result2.version_id,
            "start": start,
            "end": start + len(quote),
            "quote": quote,
        }
    ]


def test_branch_fork_copies_formal_closure_without_future_chapters(project):
    ch1 = authoring.create_chapter(project, 1)
    msg1 = authoring.record_message(project, "第1章计划。", ch1.id)
    authoring.save_plan(project, ch1.id, "第1章剧情", "第1章结尾", Expected(), msg1)
    item = authoring.record_item(
        project,
        kind="rule",
        body="银针只是装饰。",
        authority=Authority.AUTHOR_DECISION,
        lifecycle=Lifecycle.DECIDED,
        source_message_id=msg1,
    )
    item = authoring.decide_item(
        project,
        item,
        lifecycle=Lifecycle.DECIDED,
        source_message_id=msg1,
        body="银针可以作为钥匙。",
        authorize_change=True,
    )
    result1, adopted1 = _adopt_body_with_corrected_fact(
        project, ch1, msg1, "沈砚拾起银针。", "银针可以作为钥匙"
    )
    publish_msg = authoring.record_message(project, "发布第一版。", ch1.id)
    publication = adoption.mark_published(
        project, _publish_action(project, ch1, result1.version_id, publish_msg)
    )
    adoption.propose_remediation(project, publication.id, publish_msg, "后文用银针解释开锁。")
    adoption.record_foreshadow(
        project,
        stage=ForeshadowStage.SETUP,
        chapter_id=ch1.id,
        body="银针是后续开锁线索。",
        quote="银针",
        adoption_id=adopted1.adoption_id,
        source_message_id=publish_msg,
        version_id=result1.version_id,
    )

    current_ch1 = authoring.get_chapter(project, ch1.id)
    result2, adopted2 = _adopt_body_with_corrected_fact(
        project,
        current_ch1,
        msg1,
        "沈砚把银针藏进袖中。",
        "银针可以作为钥匙",
        expected_current_draft_id=result1.version_id,
        expected_adoption_id=adopted1.adoption_id,
        allow_published_revision=True,
    )
    assert result2.version_id != result1.version_id
    assert adopted2.adoption_id != adopted1.adoption_id

    ch2, _result_ch2, _adopted_ch2 = _adopted(project, 2, "未来章不应进入第一章分支。")
    assert ch2.number == 2
    branch = projects.create_branch(project, "银针分支", adopted2.adoption_id)

    projects.switch_branch(project, branch.id)
    branch_chapters = authoring.list_chapters(project)
    assert [chapter.number for chapter in branch_chapters] == [1]
    branch_ch1 = branch_chapters[0]
    branch_snapshot_id = branch_ch1.current_snapshot_id
    assert branch_snapshot_id is not None
    facts = database.list_snapshot_fact_details(project.conn, project.novel_id, branch_snapshot_id)
    assert [fact.proposition for fact in facts] == ["银针可以作为钥匙"]
    assert facts[0].fact_revision_id is not None
    assert authoring.list_story_items(project)[0].body == item.body
    assert (
        project.conn.execute(
            "SELECT COUNT(*) FROM story_item WHERE novel_id = ?", (project.novel_id,)
        ).fetchone()[0]
        == 2
    )
    copied_publications = adoption.list_publications(project, branch_ch1.id)
    assert len(copied_publications) == 1
    assert copied_publications[0].published_at == publication.published_at
    assert (
        project.conn.execute(
            "SELECT COUNT(*) FROM publication_divergence WHERE novel_id = ?", (project.novel_id,)
        ).fetchone()[0]
        == 1
    )
    assert (
        project.conn.execute(
            "SELECT COUNT(*) FROM remediation_plan WHERE novel_id = ?", (project.novel_id,)
        ).fetchone()[0]
        == 1
    )
    assert len(adoption.list_foreshadow(project)) == 1


def test_generation_impact_records_each_later_chapter_classification(project):
    _ch1, _result1, adopted1 = _adopted(project, 1, "井边蓝火熄灭。")
    ch2, _result2, _adopted2 = _adopted(project, 2, "蓝火再次亮起。沈砚停下脚步。")
    ch3, _result3, _adopted3 = _adopted(project, 3, "他走进雨里。")
    msg = authoring.record_message(project, "分析蓝火改动影响。", _ch1.id)
    payload = {
        "summary": "第二章受影响，第三章无关。",
        "chapters": [
            {
                "chapter_number": 2,
                "status": "affected",
                "reason": "第二章仍沿用蓝火会再次亮起。",
                "evidence": [{"quote": "蓝火再次亮起。"}],
            },
            {
                "chapter_number": 3,
                "status": "unrelated",
                "reason": "第三章没有依赖蓝火。",
                "evidence": [],
            },
        ],
        "coverage": {"checked": ["第2-3章已采用正文"], "unchecked": []},
    }
    clients = generation.Clients(
        stream=fixed_stream([]),
        complete=fixed_complete(json.dumps(payload, ensure_ascii=False)),
    )

    _run_id, report = generation.analyze_impact(project, adopted1.adoption_id, msg, clients, CONFIG)

    assert [chapter.status for chapter in report.chapters] == ["affected", "unrelated"]
    analyses = project.conn.execute(
        "SELECT target_chapter_id, impact_class FROM impact_analysis ORDER BY created_at, rowid"
    ).fetchall()
    assert [(row["target_chapter_id"], row["impact_class"]) for row in analyses] == [
        (ch2.id, "affected"),
        (ch3.id, "unrelated"),
    ]
    impacts = adoption.pending_impacts(project, ch2.id)
    assert len(impacts) == 1
    assert adoption.pending_impacts(project, ch3.id) == ()


def test_reader_history_includes_prior_remediation_for_current_publication(project):
    ch1, result1, _adopted1 = _adopted(project, 1, "门缝里有一枚银针。")
    msg = authoring.record_message(project, "发布并补救。", ch1.id)
    publication = adoption.mark_published(
        project, _publish_action(project, ch1, result1.version_id, msg)
    )
    adoption.propose_remediation(project, publication.id, msg, "后续用银针解释门锁。")
    ch2, result2, _adopted2 = _adopted(project, 2, "沈砚用银针撬开门锁。")
    msg2 = authoring.record_message(project, "发布第二章并补救。", ch2.id)
    publication2 = adoption.mark_published(
        project, _publish_action(project, ch2, result2.version_id, msg2)
    )
    adoption.propose_remediation(project, publication2.id, msg2, "同章补救不应进入自身上下文。")

    sources = generation.load_sources(project, ch2.id)
    history = "\n".join(item.body for item in sources.reader_history)

    assert "第1章补救计划，尚未成为正文事实：后续用银针解释门锁。" in history
    assert "同章补救不应进入自身上下文" not in history


def test_generation_passes_impact_coverage_to_new_analysis_api(project):
    _ch1, _result1, adopted1 = _adopted(project, 1, "井边蓝火熄灭。")
    _ch2, _result2, _adopted2 = _adopted(project, 2, "蓝火再次亮起。沈砚停下脚步。")
    msg = authoring.record_message(project, "分析蓝火改动影响。", _ch1.id)
    payload = {
        "summary": "第二章受影响。",
        "chapters": [
            {
                "chapter_number": 2,
                "status": "affected",
                "reason": "第二章仍沿用蓝火会再次亮起。",
                "evidence": [{"quote": "蓝火再次亮起。"}],
            }
        ],
        "coverage": {"checked": ["第2章正文"], "unchecked": ["无"]},
    }
    clients = generation.Clients(
        stream=fixed_stream([]),
        complete=fixed_complete(json.dumps(payload, ensure_ascii=False)),
    )

    generation.analyze_impact(project, adopted1.adoption_id, msg, clients, CONFIG)

    row = project.conn.execute(
        "SELECT impact_class, coverage_json FROM impact_analysis ORDER BY created_at, rowid"
    ).fetchone()
    assert row["impact_class"] == "affected"
    assert json.loads(row["coverage_json"]) == {"checked": ["第2章正文"], "unchecked": ["无"]}


def _impact_rebase_fixture(project):
    ch1, _result1, adopted1 = _adopted(project, 1, "前章状态已经稳定。")
    ch2 = authoring.create_chapter(project, 2)
    msg2 = authoring.record_message(project, "第2章计划。", ch2.id)
    authoring.save_plan(project, ch2.id, "第2章剧情", "第2章结尾", Expected(), msg2)
    _result2, adopted2 = _adopt_body_with_corrected_fact(
        project, ch2, msg2, "沈砚确认银针没有变化。", "银针没有变化"
    )
    database.commit_impact_findings(
        project.conn,
        project.novel_id,
        adopted1.adoption_id,
        ((ch2.id, "前章变化需要确认本章是否受影响。", ()),),
    )
    impact = adoption.list_impacts(project, ch2.id)[0]
    analysis_id = database.commit_impact_analysis(
        project.conn,
        project.novel_id,
        target_chapter_id=ch2.id,
        classification="unrelated",
        summary="本章银针事实未受影响。",
        source_adoption_id=adopted1.adoption_id,
        coverage_checked=("第2章正文",),
    )
    return ch1, ch2, adopted1, adopted2, impact, analysis_id


def test_unrelated_impact_resolution_rebases_ready_snapshot(project):
    _ch1, ch2, _adopted1, _adopted2, impact, analysis_id = _impact_rebase_fixture(project)
    old_snapshot_id = database.get_chapter(
        project.conn, project.novel_id, ch2.id
    ).current_snapshot_id
    assert old_snapshot_id is None
    old_review = database.get_adoption(project.conn, project.novel_id, _adopted2.adoption_id)
    old_check_id = database.get_review_check_id(
        project.conn, project.novel_id, old_review.review_id
    )
    affected_msg = authoring.record_message(project, "先标记受影响，稍后复核。", ch2.id)
    affected_resolution_id = database.commit_impact_resolution(
        project.conn, project.novel_id, impact.id, "affected", affected_msg
    )
    assert affected_resolution_id
    assert adoption.list_impacts(project, ch2.id)[0].status.value == "pending"
    msg = authoring.record_message(project, "确认第2章与前章变化无关。", ch2.id)

    resolution_id = database.commit_impact_resolution(
        project.conn,
        project.novel_id,
        impact.id,
        "unrelated",
        msg,
        analysis_id=analysis_id,
        note="确认无关，重定基。",
    )

    assert resolution_id
    assert resolution_id != affected_resolution_id
    resolved = adoption.list_impacts(project, ch2.id)[0]
    assert resolved.status.value == "resolved"
    chapter = database.get_chapter(project.conn, project.novel_id, ch2.id)
    assert not chapter.needs_review
    assert chapter.current_snapshot_id is not None
    snapshot = database.get_snapshot(project.conn, project.novel_id, chapter.current_snapshot_id)
    assert snapshot.status.value == "ready"
    assert snapshot.previous_snapshot_id is not None
    assert (
        database.get_snapshot_check_id(project.conn, project.novel_id, snapshot.id) != old_check_id
    )
    facts = database.list_snapshot_fact_details(project.conn, project.novel_id, snapshot.id)
    assert facts[-1].proposition == "银针没有变化"
    assert facts[-1].fact_revision_id is not None
    assert (
        project.conn.execute(
            "SELECT COUNT(*) FROM impact_resolution WHERE novel_id = ? AND impact_id = ?",
            (project.novel_id, impact.id),
        ).fetchone()[0]
        == 2
    )
    repeat_msg = authoring.record_message(project, "重复确认无关。", ch2.id)
    with pytest.raises(WorkbenchError):
        database.commit_impact_resolution(
            project.conn,
            project.novel_id,
            impact.id,
            "unrelated",
            repeat_msg,
            analysis_id=analysis_id,
        )


def test_unrelated_impact_resolution_stale_if_source_snapshot_changed(project):
    _ch1, ch2, adopted1, _adopted2, impact, analysis_id = _impact_rebase_fixture(project)
    source_adoption = database.get_adoption(project.conn, project.novel_id, adopted1.adoption_id)
    source_chapter = database.get_chapter(
        project.conn, project.novel_id, source_adoption.chapter_id
    )
    with database.transaction(project.conn):
        project.conn.execute(
            "UPDATE snapshot SET status = 'stale' WHERE novel_id = ? AND id = ?",
            (project.novel_id, source_chapter.current_snapshot_id),
        )
    msg = authoring.record_message(project, "确认第2章无关。", ch2.id)

    with pytest.raises(WorkbenchError) as exc:
        database.commit_impact_resolution(
            project.conn,
            project.novel_id,
            impact.id,
            "unrelated",
            msg,
            analysis_id=analysis_id,
        )

    assert exc.value.code.value == "STALE_BASIS"
    assert adoption.list_impacts(project, ch2.id)[0].status.value == "pending"
    assert (
        project.conn.execute(
            "SELECT COUNT(*) FROM impact_resolution WHERE novel_id = ?", (project.novel_id,)
        ).fetchone()[0]
        == 0
    )


def test_branch_keeps_published_snapshot_chain_after_unrelated_rebase(project):
    ch1, result1, adopted1 = _adopted(project, 1, "前章第一版。")
    ch2, result2, adopted2 = _adopted(project, 2, "后章保持不变。")
    publish_message = authoring.record_message(project, "第2章已发布。", ch2.id)
    publication = adoption.mark_published(
        project, _publish_action(project, ch2, result2.version_id, publish_message)
    )

    saved = authoring.save_draft(
        project, ch1.id, "前章第二版。", Expected(current_draft_id=result1.version_id)
    )
    check_message = authoring.record_message(project, "检查第1章修订。", ch1.id)
    clients = generation.Clients(
        stream=fixed_stream([]),
        complete=fixed_complete(
            inspection_json("前章第二版。", plot="第1章剧情", ending="第1章结尾")
        ),
    )
    check_id = generation.check_draft(
        project, Target(project.novel_id, ch1.id, saved.version_id), check_message, clients, CONFIG
    )
    review_message = authoring.record_message(project, "已读，采用第1章修订。", ch1.id)
    revised = adoption.adopt(
        project,
        AuthorAction(
            request_id=uuid.uuid4().hex,
            source_message_id=review_message,
            target=Target(project.novel_id, ch1.id, saved.version_id),
            expected=Expected(adopted_id=adopted1.adoption_id, plan_id=ch1.current_plan_id),
            check_id=check_id,
            read_declared=True,
        ),
    )
    impact = adoption.list_impacts(project, ch2.id)[0]
    analysis_id = database.commit_impact_analysis(
        project.conn,
        project.novel_id,
        target_chapter_id=ch2.id,
        classification="unrelated",
        summary="第2章与前章修订无关。",
        source_adoption_id=revised.adoption_id,
        coverage_checked=("第2章正文",),
    )
    resolution_message = authoring.record_message(project, "确认第2章无关。", ch2.id)
    database.commit_impact_resolution(
        project.conn,
        project.novel_id,
        impact.id,
        "unrelated",
        resolution_message,
        analysis_id=analysis_id,
    )

    branch = projects.create_branch(project, "保留发布历史", adopted2.adoption_id)
    projects.switch_branch(project, branch.id)

    copied_chapter2 = authoring.list_chapters(project)[1]
    copied_publication = adoption.list_publications(project, copied_chapter2.id)[0]
    assert copied_publication.published_at == publication.published_at
    copied_snapshot = database.get_snapshot(
        project.conn, project.novel_id, copied_chapter2.current_snapshot_id
    )
    assert copied_snapshot.previous_snapshot_id is not None
    assert copied_publication.ready_snapshot_id != copied_chapter2.current_snapshot_id


def test_affected_impact_findings_reuse_existing_pending(project):
    ch1, _result1, adopted1 = _adopted(project, 1, "井边蓝火熄灭。")
    ch2, result2, _adopted2 = _adopted(project, 2, "蓝火再次亮起。沈砚停下脚步。")
    database.commit_impact_findings(
        project.conn,
        project.novel_id,
        adopted1.adoption_id,
        ((ch2.id, "第 1 章重新采用，本章依赖的前文状态需要复核。", ()),),
    )
    quote = "蓝火再次亮起"
    start = database.get_version(project.conn, project.novel_id, result2.version_id).body.index(
        quote
    )
    evidence = EvidenceInput(
        version_id=result2.version_id,
        start=start,
        end=start + len(quote),
        quote=quote,
    )

    first_classification_msg = authoring.record_message(project, "确认受影响，稍后复核。", ch2.id)
    first_resolution = database.commit_impact_resolution(
        project.conn,
        project.novel_id,
        adoption.list_impacts(project, ch2.id)[0].id,
        "affected",
        first_classification_msg,
    )
    second_classification_msg = authoring.record_message(project, "再次标记未检查。", ch2.id)
    second_resolution = database.commit_impact_resolution(
        project.conn,
        project.novel_id,
        adoption.list_impacts(project, ch2.id)[0].id,
        "unchecked",
        second_classification_msg,
    )
    assert first_resolution != second_resolution

    ids = database.commit_impact_findings(
        project.conn,
        project.novel_id,
        adopted1.adoption_id,
        ((ch2.id, "模型给出的不同原因。", (evidence,)),),
    )

    impacts = adoption.list_impacts(project, ch2.id)
    assert len(impacts) == 1
    assert ids == (impacts[0].id,)
    assert (
        project.conn.execute(
            "SELECT COUNT(*) FROM impact_evidence WHERE novel_id = ? AND impact_id = ?",
            (project.novel_id, impacts[0].id),
        ).fetchone()[0]
        == 1
    )
    assert (
        project.conn.execute(
            "SELECT COUNT(*) FROM impact_resolution WHERE novel_id = ? AND impact_id = ?",
            (project.novel_id, impacts[0].id),
        ).fetchone()[0]
        == 2
    )


def test_impact_analysis_persists_coverage_and_named_lookup(project):
    ch1, _result1, adopted1 = _adopted(project, 1, "井边蓝火熄灭。")
    ch2, _result2, _adopted2 = _adopted(project, 2, "他走进雨里。")
    msg = authoring.record_message(project, "记录分析。", ch1.id)
    run_id = database.commit_run(
        project.conn,
        project.novel_id,
        chapter_id=ch1.id,
        task=TaskKind.IMPACT,
        source_message_id=msg,
        template_version="test",
        status=RunStatus.SUCCEEDED,
    )

    analysis_id = database.commit_impact_analysis(
        project.conn,
        project.novel_id,
        target_chapter_id=ch2.id,
        classification="unrelated",
        summary="无关。",
        source_adoption_id=adopted1.adoption_id,
        run_id=run_id,
        coverage_checked=("第2章正文",),
        coverage_unchecked=("无",),
    )

    row = project.conn.execute(
        "SELECT coverage_json FROM impact_analysis WHERE novel_id = ? AND id = ?",
        (project.novel_id, analysis_id),
    ).fetchone()
    assert json.loads(row["coverage_json"]) == {"checked": ["第2章正文"], "unchecked": ["无"]}
    assert database.get_impact_analysis_for_target(
        project.conn, project.novel_id, run_id, ch2.id
    ) == (
        analysis_id,
        "unrelated",
    )


def test_list_remediation_plans_is_read_only(project):
    ch1, result1, _adopted1 = _adopted(project, 1, "门缝里有一枚银针。")
    msg = authoring.record_message(project, "发布并补救。", ch1.id)
    publication = adoption.mark_published(
        project, _publish_action(project, ch1, result1.version_id, msg)
    )
    plan = adoption.propose_remediation(project, publication.id, msg, "后续用银针解释门锁。")

    assert database.list_remediation_plans(project.conn, project.novel_id) == (plan,)
    assert database.list_remediation_plans(project.conn, project.novel_id, publication.id) == (
        plan,
    )


def _new_raw_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _apply_schema(conn: sqlite3.Connection, version: int) -> None:
    for target in range(1, version + 1):
        for statement in database._MIGRATIONS[target].split(";"):
            if statement.strip():
                conn.execute(statement)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()


def test_migrate_v2_to_v3_with_data_preserves_integrity():
    conn = _new_raw_conn()
    _apply_schema(conn, 2)
    novel_id = database.new_id()
    conn.execute(
        "INSERT INTO novel (id, created_at, title, design_revision) VALUES (?, ?, ?, 0)",
        (novel_id, database.now_utc(), "旧库"),
    )
    conn.commit()

    assert database.migrate(conn) == 3

    state = conn.execute("SELECT main_novel_id, active_novel_id FROM project_state").fetchone()
    assert (state["main_novel_id"], state["active_novel_id"]) == (novel_id, novel_id)
    assert conn.execute("SELECT COUNT(*) FROM branch").fetchone()[0] == 1
    assert database.integrity_ok(conn)


def test_failed_v2_to_v3_migration_rolls_back_user_version_and_data():
    conn = _new_raw_conn()
    _apply_schema(conn, 2)
    novel_id = database.new_id()
    conn.execute(
        "INSERT INTO novel (id, created_at, title, design_revision) VALUES (?, ?, ?, 0)",
        (novel_id, database.now_utc(), "旧库"),
    )
    conn.execute("CREATE TABLE branch (id TEXT PRIMARY KEY)")
    conn.commit()

    with pytest.raises(WorkbenchError):
        database.migrate(conn)

    assert database.schema_version(conn) == 2
    assert conn.execute("SELECT title FROM novel WHERE id = ?", (novel_id,)).fetchone()[0] == "旧库"
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("SELECT * FROM project_state").fetchall()


def _manual_check(
    project,
    chapter,
    version_id: str,
    message_id: str,
    *,
    issues=(),
    facts=(),
    sources=(),
):
    current = authoring.get_chapter(project, chapter.id)
    previous = [c for c in authoring.list_chapters(project) if c.number == current.number - 1]
    previous_snapshot_id = previous[0].current_snapshot_id if previous else None
    novel = database.get_novel(project.conn)
    assert novel is not None
    basis_id = database.commit_basis(
        project.conn,
        project.novel_id,
        chapter.id,
        current.current_plan_id,
        previous_snapshot_id,
        novel.design_revision,
        "formal",
        list(sources),
    )
    run_id = database.commit_run(
        project.conn,
        project.novel_id,
        chapter_id=chapter.id,
        task=TaskKind.INSPECT,
        source_message_id=message_id,
        template_version="test",
        status=RunStatus.SUCCEEDED,
        basis_id=basis_id,
        input_version_id=version_id,
    )
    check_id = database.commit_check_result(
        project.conn,
        project.novel_id,
        chapter.id,
        version_id,
        basis_id,
        run_id,
        CheckInput(
            coverage_json='{"checked":["全文"],"unchecked":[]}',
            actual_plot=f"第{chapter.number}章剧情",
            actual_ending=f"第{chapter.number}章结尾",
            issues=tuple(issues),
            candidate_facts=tuple(facts),
        ),
    )
    return basis_id, check_id


def _adopt_manual(project, chapter, version_id: str, check_id: str, message_id: str, **kwargs):
    current = authoring.get_chapter(project, chapter.id)
    previous = [c for c in authoring.list_chapters(project) if c.number == current.number - 1]
    previous_snapshot_id = previous[0].current_snapshot_id if previous else None
    action = AuthorAction(
        request_id=uuid.uuid4().hex,
        source_message_id=message_id,
        target=Target(project.novel_id, chapter.id, version_id),
        expected=Expected(
            adopted_id=current.adopted_id,
            plan_id=current.current_plan_id,
            previous_snapshot_id=previous_snapshot_id,
        ),
        check_id=check_id,
        read_declared=True,
        **kwargs,
    )
    return adoption.adopt(project, action)


def test_branch_copies_published_adoption_review_closure_with_issue_resolution(project):
    ch1 = authoring.create_chapter(project, 1)
    msg = authoring.record_message(project, "第1章计划，银针必须保留。", ch1.id)
    authoring.save_plan(project, ch1.id, "第1章剧情", "第1章结尾", Expected(), msg)
    saved1 = authoring.save_draft(project, ch1.id, "沈砚拾起银针。", Expected())
    current = authoring.get_chapter(project, ch1.id)
    novel = database.get_novel(project.conn)
    assert novel is not None
    basis_id = database.commit_basis(
        project.conn,
        project.novel_id,
        ch1.id,
        current.current_plan_id,
        None,
        novel.design_revision,
        "formal",
        [("discussion", {"message_id": msg, "quote": "银针必须保留"})],
    )
    basis_source_id = project.conn.execute(
        "SELECT id FROM basis_source WHERE novel_id = ? AND basis_id = ?",
        (project.novel_id, basis_id),
    ).fetchone()[0]
    run_id = database.commit_run(
        project.conn,
        project.novel_id,
        chapter_id=ch1.id,
        task=TaskKind.INSPECT,
        source_message_id=msg,
        template_version="test",
        status=RunStatus.SUCCEEDED,
        basis_id=basis_id,
        input_version_id=saved1.version_id,
    )
    issue = IssueInput(
        kind=IssueKind.NEW_PERSISTENT_SETTING,
        explanation="银针是新增持久道具。",
        evidence=(EvidenceInput(basis_source_id=basis_source_id, quote="银针必须保留"),),
    )
    check_id = database.commit_check_result(
        project.conn,
        project.novel_id,
        ch1.id,
        saved1.version_id,
        basis_id,
        run_id,
        CheckInput(
            coverage_json='{"checked":["全文"],"unchecked":[]}',
            actual_plot="第1章剧情",
            actual_ending="第1章结尾",
            issues=(issue,),
            candidate_facts=(),
        ),
    )
    issue_id = database.get_check(project.conn, project.novel_id, check_id).issues[0].id
    adopted1 = _adopt_manual(
        project,
        ch1,
        saved1.version_id,
        check_id,
        msg,
        issue_resolutions=(
            IssueResolution(issue_id, ResolutionAction.AUTHORIZE_SETTING, "授权银针道具。"),
        ),
    )
    publication = adoption.mark_published(
        project, _publish_action(project, ch1, saved1.version_id, msg)
    )
    plan2 = authoring.save_plan(
        project,
        ch1.id,
        "第1章修订剧情",
        "第1章修订结尾",
        Expected(plan_id=authoring.get_chapter(project, ch1.id).current_plan_id),
        msg,
    )
    saved2 = authoring.save_draft(
        project, ch1.id, "沈砚把银针藏进袖中。", Expected(current_draft_id=saved1.version_id)
    )
    _basis_id2, check2 = _manual_check(project, ch1, saved2.version_id, msg)
    adopted2 = _adopt_manual(
        project,
        ch1,
        saved2.version_id,
        check2,
        msg,
        allow_published_revision=True,
        divergence_reason="授权修订已发布银针段落。",
    )

    branch = projects.create_branch(project, "复制审阅闭包", adopted2.adoption_id)
    projects.switch_branch(project, branch.id)

    copied_publication = adoption.list_publications(project)[0]
    assert copied_publication.published_at == publication.published_at
    copied_old_adoption = database.get_adoption(
        project.conn, project.novel_id, copied_publication.adoption_id
    )
    copied_chapter = authoring.get_chapter(project, copied_old_adoption.chapter_id)
    copied_new_adoption = database.get_adoption(
        project.conn, project.novel_id, copied_chapter.adopted_id
    )
    copied_plan_revisions = project.conn.execute(
        "SELECT revision FROM plan WHERE novel_id = ? AND chapter_id = ? ORDER BY revision",
        (project.novel_id, copied_chapter.id),
    ).fetchall()
    assert [row["revision"] for row in copied_plan_revisions] == [1, plan2.revision]
    review_plan_revisions = project.conn.execute(
        "SELECT p.revision FROM adoption a"
        " JOIN review r ON r.novel_id = a.novel_id AND r.id = a.review_id"
        " JOIN basis b ON b.novel_id = r.novel_id AND b.id = r.basis_id"
        " JOIN plan p ON p.novel_id = b.novel_id AND p.id = b.plan_id"
        " WHERE a.novel_id = ? AND a.id IN (?, ?) ORDER BY p.revision",
        (
            project.novel_id,
            copied_old_adoption.id,
            copied_new_adoption.id,
        ),
    ).fetchall()
    assert [row["revision"] for row in review_plan_revisions] == [1, plan2.revision]
    old_check_id = database.get_review_check_id(
        project.conn, project.novel_id, copied_old_adoption.review_id
    )
    assert old_check_id != check_id
    assert (
        project.conn.execute(
            "SELECT COUNT(*) FROM issue WHERE novel_id = ? AND check_id = ?",
            (project.novel_id, old_check_id),
        ).fetchone()[0]
        == 1
    )
    copied_issue_id = project.conn.execute(
        "SELECT id FROM issue WHERE novel_id = ? AND check_id = ?",
        (project.novel_id, old_check_id),
    ).fetchone()[0]
    evidence = project.conn.execute(
        "SELECT basis_source_id, quote FROM issue_evidence WHERE novel_id = ? AND issue_id = ?",
        (project.novel_id, copied_issue_id),
    ).fetchone()
    assert evidence["basis_source_id"] is not None
    assert evidence["quote"] == "银针必须保留"
    assert (
        project.conn.execute(
            "SELECT quote FROM basis_source WHERE novel_id = ? AND id = ?",
            (project.novel_id, evidence["basis_source_id"]),
        ).fetchone()[0]
        == "银针必须保留"
    )
    resolution = project.conn.execute(
        "SELECT issue_id, action, explanation FROM resolution WHERE novel_id = ? AND review_id = ?",
        (project.novel_id, copied_old_adoption.review_id),
    ).fetchone()
    assert tuple(resolution) == (
        copied_issue_id,
        ResolutionAction.AUTHORIZE_SETTING.value,
        "授权银针道具。",
    )
    assert (
        project.conn.execute(
            "SELECT COUNT(*) FROM publication_divergence WHERE novel_id = ?",
            (project.novel_id,),
        ).fetchone()[0]
        == 1
    )
    assert adopted1.adoption_id != adopted2.adoption_id


def test_impact_analysis_rejects_invalid_classification_without_writes(project):
    ch1, _result1, adopted1 = _adopted(project, 1, "井边蓝火熄灭。")
    ch2, _result2, _adopted2 = _adopted(project, 2, "他走进雨里。")

    with pytest.raises(WorkbenchError) as exc:
        database.commit_impact_analysis(
            project.conn,
            project.novel_id,
            target_chapter_id=ch2.id,
            classification="maybe",
            summary="非法分类。",
            source_adoption_id=adopted1.adoption_id,
        )

    assert exc.value.code.value == "INVALID_INPUT"
    assert project.conn.execute("SELECT COUNT(*) FROM impact_analysis").fetchone()[0] == 0
    assert project.conn.execute("SELECT COUNT(*) FROM impact_evidence").fetchone()[0] == 0


def test_impact_analysis_evidence_persists_for_all_classes_and_validates_target(project):
    ch1, result1, adopted1 = _adopted(project, 1, "井边蓝火熄灭。")
    ch2, result2, _adopted2 = _adopted(project, 2, "蓝火再次亮起。沈砚停下脚步。")
    body2 = database.get_version(project.conn, project.novel_id, result2.version_id).body
    quote = "蓝火再次亮起"
    start = body2.index(quote)

    for classification in ("affected", "unrelated", "unchecked"):
        database.commit_impact_analysis(
            project.conn,
            project.novel_id,
            target_chapter_id=ch2.id,
            classification=classification,
            summary=f"{classification}。",
            source_adoption_id=adopted1.adoption_id,
            evidence=((result2.version_id, start, start + len(quote), quote),),
        )

    rows = project.conn.execute(
        "SELECT ia.impact_class, ie.version_id, ie.quote FROM impact_analysis ia"
        " JOIN impact_evidence ie ON ie.novel_id = ia.novel_id AND ie.analysis_id = ia.id"
        " ORDER BY ia.created_at, ia.rowid"
    ).fetchall()
    assert [(r["impact_class"], r["version_id"], r["quote"]) for r in rows] == [
        ("affected", result2.version_id, quote),
        ("unrelated", result2.version_id, quote),
        ("unchecked", result2.version_id, quote),
    ]
    before = project.conn.execute("SELECT COUNT(*) FROM impact_analysis").fetchone()[0]
    with pytest.raises(WorkbenchError) as wrong_chapter:
        database.commit_impact_analysis(
            project.conn,
            project.novel_id,
            target_chapter_id=ch2.id,
            classification="affected",
            summary="错章证据。",
            source_adoption_id=adopted1.adoption_id,
            evidence=((result1.version_id, 0, 2, "井边"),),
        )
    assert wrong_chapter.value.code.value == "WRONG_SCOPE"
    with pytest.raises(WorkbenchError) as bad_span:
        database.commit_impact_analysis(
            project.conn,
            project.novel_id,
            target_chapter_id=ch2.id,
            classification="affected",
            summary="错区间。",
            source_adoption_id=adopted1.adoption_id,
            evidence=((result2.version_id, start, start + len(quote), "不匹配"),),
        )
    assert bad_span.value.code.value == "INVALID_INPUT"
    assert project.conn.execute("SELECT COUNT(*) FROM impact_analysis").fetchone()[0] == before

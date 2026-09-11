"""采用与版本的集成流程。当前覆盖 S01/S02：项目、章节关系、不可变版本、指针与恢复。"""

from __future__ import annotations

import sqlite3
import uuid

import pytest

from story_workbench.application import adoption, authoring
from story_workbench.application.projects import create_project, create_workcopy, open_project
from story_workbench.domain.records import (
    AuthorAction,
    CandidateFactInput,
    CheckInput,
    ErrorCode,
    EvidenceInput,
    Expected,
    FactKind,
    IssueInput,
    IssueKind,
    IssueResolution,
    ResolutionAction,
    RunStatus,
    SnapshotStatus,
    Target,
    TaskKind,
    VersionOrigin,
    WorkbenchError,
)
from story_workbench.infrastructure import database


def test_reopen_keeps_data(project_dir):
    proj = create_project(project_dir, "井下回声")
    ch = authoring.create_chapter(proj, 1)
    msg = authoring.record_message(proj, "先写旧井。", ch.id)
    plan = authoring.save_plan(proj, ch.id, "调查旧井", "看到疑似师父的尸体", Expected(), msg)
    saved = authoring.save_draft(proj, ch.id, "沈砚把灯搁在井沿。", Expected())
    proj.close()

    reopened, notice = open_project(project_dir)
    try:
        assert reopened.novel.title == "井下回声"
        chapters = authoring.list_chapters(reopened)
        assert [c.number for c in chapters] == [1]
        assert chapters[0].current_plan_id == plan.id
        assert chapters[0].current_draft_id == saved.version_id
        body = authoring.get_version(reopened, Target(reopened.novel_id, ch.id, saved.version_id))
        assert body.body == "沈砚把灯搁在井沿。"
        assert notice.interrupted_run_ids == ()
    finally:
        reopened.close()


def test_second_process_is_rejected_while_open(project, project_dir):
    with pytest.raises(WorkbenchError) as exc:
        open_project(project_dir)
    assert exc.value.code is ErrorCode.PROJECT_BUSY


def test_chapter_numbers_unique_and_positive(project):
    authoring.create_chapter(project, 1)
    with pytest.raises(WorkbenchError) as dup:
        authoring.create_chapter(project, 1)
    assert dup.value.code is ErrorCode.INVALID_INPUT
    with pytest.raises(WorkbenchError):
        authoring.create_chapter(project, 0)


def test_two_drafts_both_readable_and_pointer_moves(project, chapter):
    first = authoring.save_draft(project, chapter.id, "第一稿", Expected())
    assert first.became_current
    second = authoring.save_draft(
        project, chapter.id, "第二稿", Expected(current_draft_id=first.version_id)
    )
    assert second.became_current
    versions = authoring.list_versions(project, chapter.id)
    assert [v.body for v in versions] == ["第一稿", "第二稿"]
    assert versions[1].parent_id == first.version_id
    assert authoring.get_chapter(project, chapter.id).current_draft_id == second.version_id


def test_stale_edit_base_keeps_both_drafts_without_moving_pointer(project, chapter):
    first = authoring.save_draft(project, chapter.id, "第一稿", Expected())
    second = authoring.save_draft(
        project, chapter.id, "第二稿", Expected(current_draft_id=first.version_id)
    )
    # 另一处仍基于第一稿编辑：保存成功，但不抢占当前稿指针。
    stale = authoring.save_draft(
        project, chapter.id, "基于第一稿的旁支", Expected(current_draft_id=first.version_id)
    )
    assert not stale.became_current
    assert stale.conflicting_current_id == second.version_id
    bodies = {v.body for v in authoring.list_versions(project, chapter.id)}
    assert bodies == {"第一稿", "第二稿", "基于第一稿的旁支"}
    assert authoring.get_chapter(project, chapter.id).current_draft_id == second.version_id


def test_restore_creates_new_version_and_keeps_history(project, chapter):
    first = authoring.save_draft(project, chapter.id, "旧内容", Expected())
    second = authoring.save_draft(
        project, chapter.id, "新内容", Expected(current_draft_id=first.version_id)
    )
    restored = authoring.restore_version(
        project,
        Target(project.novel_id, chapter.id, first.version_id),
        Expected(current_draft_id=second.version_id),
    )
    assert restored.became_current
    versions = authoring.list_versions(project, chapter.id)
    assert len(versions) == 3
    last = versions[-1]
    assert last.body == "旧内容"
    assert last.origin is VersionOrigin.RESTORE
    assert last.restored_from_id == first.version_id
    assert last.parent_id == second.version_id
    assert versions[0].body == "旧内容" and versions[1].body == "新内容"


def test_plan_revisions_are_immutable_and_base_checked(project, chapter, author_message):
    p1 = authoring.save_plan(project, chapter.id, "调查旧井", None, Expected(), author_message)
    p2 = authoring.save_plan(
        project, chapter.id, "调查旧井", "看到尸体", Expected(plan_id=p1.id), author_message
    )
    with pytest.raises(WorkbenchError) as exc:
        authoring.save_plan(
            project, chapter.id, "另写", "另结尾", Expected(plan_id=p1.id), author_message
        )
    assert exc.value.code is ErrorCode.STALE_BASIS
    plans = database.list_plans(project.conn, project.novel_id, chapter.id)
    assert [(p.revision, p.ending) for p in plans] == [(1, None), (2, "看到尸体")]
    assert authoring.get_chapter(project, chapter.id).current_plan_id == p2.id


def test_cross_novel_version_is_rejected(project, chapter, tmp_path):
    other = create_project(tmp_path / "other", "另一本")
    try:
        other_ch = authoring.create_chapter(other, 1)
        other_v = authoring.save_draft(other, other_ch.id, "别的书", Expected())
        with pytest.raises(WorkbenchError) as exc:
            authoring.get_version(project, Target(other.novel_id, other_ch.id, other_v.version_id))
        assert exc.value.code is ErrorCode.WRONG_SCOPE
        # 直接伪造同库跨章父版本引用也被拒绝，且无部分写入。
        ch2 = authoring.create_chapter(project, 2)
        v1 = authoring.save_draft(project, chapter.id, "第一章稿", Expected())
        with pytest.raises(WorkbenchError) as exc2:
            authoring.save_draft(
                project, ch2.id, "第二章稿", Expected(current_draft_id=v1.version_id)
            )
        assert exc2.value.code is ErrorCode.WRONG_SCOPE
        assert authoring.list_versions(project, ch2.id) == []
    finally:
        other.close()


def test_database_level_foreign_keys_reject_cross_chapter_pointer(project, chapter):
    ch2 = authoring.create_chapter(project, 2)
    v1 = authoring.save_draft(project, chapter.id, "第一章稿", Expected())
    with pytest.raises(sqlite3.IntegrityError):
        project.conn.execute(
            "UPDATE chapter SET current_draft_id = ? WHERE id = ?", (v1.version_id, ch2.id)
        )


def test_history_tables_have_no_update_path_in_use_cases(project, chapter):
    v = authoring.save_draft(project, chapter.id, "原文", Expected())
    authoring.save_draft(project, chapter.id, "改文", Expected(current_draft_id=v.version_id))
    assert (
        authoring.get_version(project, Target(project.novel_id, chapter.id, v.version_id)).body
        == "原文"
    )


# ---------------------------------------------------------------- S04/S05：检查绑定与采用事务


def _check_for(project, chapter, version_id, *, issues=(), facts=(), purpose="formal"):
    """用固定数据建立 basis、run 和检查结果，返回 (check_id, basis_id)。"""
    ch = authoring.get_chapter(project, chapter.id)
    prev_snapshot = None
    if ch.number > 1:
        prev = [c for c in authoring.list_chapters(project) if c.number == ch.number - 1]
        prev_snapshot = prev[0].current_snapshot_id if prev else None
    basis_id = database.commit_basis(
        project.conn,
        project.novel_id,
        chapter.id,
        ch.current_plan_id,
        prev_snapshot,
        project.novel.design_revision,
        purpose,
        [("text", {"version_id": version_id, "quote": ""})],
    )
    msg = authoring.record_message(project, "检查这一稿。", chapter.id)
    run_id = database.commit_run(
        project.conn,
        project.novel_id,
        chapter_id=chapter.id,
        task=TaskKind.INSPECT,
        source_message_id=msg,
        template_version="fixed",
        status=RunStatus.SUCCEEDED,
        basis_id=basis_id,
        input_version_id=version_id,
    )
    payload = CheckInput(
        coverage_json='{"checked": ["全文"], "unchecked": []}',
        actual_plot="调查旧井",
        actual_ending="看到疑似师父的尸体",
        issues=tuple(issues),
        candidate_facts=tuple(facts),
    )
    check_id = database.commit_check_result(
        project.conn, project.novel_id, chapter.id, version_id, basis_id, run_id, payload
    )
    return check_id, basis_id


def _action(project, chapter, version_id, check_id, **kw):
    ch = authoring.get_chapter(project, chapter.id)
    prev_snapshot = None
    if ch.number > 1:
        prev = [c for c in authoring.list_chapters(project) if c.number == ch.number - 1]
        prev_snapshot = prev[0].current_snapshot_id if prev else None
    msg = authoring.record_message(project, "全文和检查都看完了，采用这一版。", chapter.id)
    defaults = dict(
        request_id=kw.pop("request_id", uuid.uuid4().hex),
        source_message_id=msg,
        target=Target(project.novel_id, chapter.id, version_id),
        expected=Expected(
            adopted_id=ch.adopted_id, plan_id=ch.current_plan_id, previous_snapshot_id=prev_snapshot
        ),
        check_id=check_id,
        read_declared=True,
    )
    defaults.update(kw)
    return AuthorAction(**defaults)


@pytest.fixture
def planned_chapter(project, chapter, author_message):
    authoring.save_plan(
        project, chapter.id, "调查旧井", "看到疑似师父的尸体", Expected(), author_message
    )
    return authoring.get_chapter(project, chapter.id)


def test_t01_old_review_cannot_adopt_new_draft(project, planned_chapter):
    ch = planned_chapter
    v1 = authoring.save_draft(project, ch.id, "第一稿正文", Expected())
    v2 = authoring.save_draft(
        project, ch.id, "第二稿正文", Expected(current_draft_id=v1.version_id)
    )
    check1, _ = _check_for(project, ch, v1.version_id)
    with pytest.raises(WorkbenchError) as exc:
        adoption.adopt(project, _action(project, ch, v2.version_id, check1))
    assert exc.value.code is ErrorCode.REVIEW_REQUIRED
    assert authoring.get_chapter(project, ch.id).adopted_id is None
    # 对第一稿本身采用可以成功，一次动作完成已读与采用
    result = adoption.adopt(project, _action(project, ch, v1.version_id, check1))
    assert not result.already_adopted
    assert result.state_status is SnapshotStatus.CANDIDATE
    assert authoring.get_chapter(project, ch.id).adopted_id == result.adoption_id


def test_t02_repeat_adoption_is_idempotent_and_restore_needs_new_adoption(project, planned_chapter):
    ch = planned_chapter
    v1 = authoring.save_draft(project, ch.id, "旧内容", Expected())
    check1, _ = _check_for(project, ch, v1.version_id)
    first = adoption.adopt(project, _action(project, ch, v1.version_id, check1, request_id="req-1"))
    # 同一 request_id 重放
    again = adoption.adopt(project, _action(project, ch, v1.version_id, check1, request_id="req-1"))
    assert again.already_adopted and again.adoption_id == first.adoption_id
    # 同一当前版本、新 request_id
    third = adoption.adopt(project, _action(project, ch, v1.version_id, check1))
    assert third.already_adopted and third.adoption_id == first.adoption_id
    count = project.conn.execute("SELECT COUNT(*) FROM adoption").fetchone()[0]
    assert count == 1
    # 编辑成新内容再恢复旧内容：恢复稿是新版本，旧检查不能用，需要新检查与新采用
    v2 = authoring.save_draft(project, ch.id, "新内容", Expected(current_draft_id=v1.version_id))
    restored = authoring.restore_version(
        project,
        Target(project.novel_id, ch.id, v1.version_id),
        Expected(current_draft_id=v2.version_id),
    )
    with pytest.raises(WorkbenchError) as exc:
        adoption.adopt(project, _action(project, ch, restored.version_id, check1))
    assert exc.value.code is ErrorCode.REVIEW_REQUIRED
    check_r, _ = _check_for(project, ch, restored.version_id)
    result = adoption.adopt(project, _action(project, ch, restored.version_id, check_r))
    assert not result.already_adopted
    rec = database.get_adoption(project.conn, project.novel_id, result.adoption_id)
    assert rec.previous_adoption_id == first.adoption_id
    assert len(authoring.list_versions(project, ch.id)) == 3


def test_t03_external_edit_before_adoption_stops_and_keeps_original(project, planned_chapter):
    ch = planned_chapter
    v1 = authoring.save_draft(project, ch.id, "原稿", Expected())
    check1, _ = _check_for(project, ch, v1.version_id)
    first = adoption.adopt(project, _action(project, ch, v1.version_id, check1))
    path = project.root / create_workcopy(project, ch.id).relative_path
    v2 = authoring.save_draft(
        project, ch.id, "应用内第二稿", Expected(current_draft_id=v1.version_id)
    )
    check2, _ = _check_for(project, ch, v2.version_id)
    path.write_text("外部又改了一版")
    with pytest.raises(WorkbenchError) as exc:
        adoption.adopt(project, _action(project, ch, v2.version_id, check2))
    assert exc.value.code is ErrorCode.WORKCOPY_CONFLICT
    assert exc.value.details["imported_version_id"] is not None
    assert authoring.get_chapter(project, ch.id).adopted_id == first.adoption_id
    assert path.read_text() == "外部又改了一版"


def test_t04_cross_novel_check_is_rejected_without_partial_write(
    project, planned_chapter, tmp_path
):
    ch = planned_chapter
    v1 = authoring.save_draft(project, ch.id, "甲书正文", Expected())
    other = create_project(tmp_path / "other", "乙书")
    try:
        och = authoring.create_chapter(other, 1)
        omsg = authoring.record_message(other, "计划", och.id)
        authoring.save_plan(other, och.id, "别的剧情", "别的结尾", Expected(), omsg)
        ov = authoring.save_draft(other, och.id, "乙书正文", Expected())
        ocheck, _ = _check_for(other, och, ov.version_id)
        before = {
            t: project.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("review", "adoption", "snapshot", "resolution")
        }
        with pytest.raises(WorkbenchError) as exc:
            adoption.adopt(project, _action(project, ch, v1.version_id, ocheck))
        assert exc.value.code in (ErrorCode.NOT_FOUND, ErrorCode.WRONG_SCOPE)
        after = {
            t: project.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("review", "adoption", "snapshot", "resolution")
        }
        assert before == after
    finally:
        other.close()


def test_plan_change_after_check_makes_adoption_stale(project, planned_chapter, author_message):
    ch = planned_chapter
    v1 = authoring.save_draft(project, ch.id, "正文", Expected())
    check1, _ = _check_for(project, ch, v1.version_id)
    authoring.save_plan(
        project,
        ch.id,
        "调查旧井",
        "确认尸体是师父",
        Expected(plan_id=ch.current_plan_id),
        author_message,
    )
    with pytest.raises(WorkbenchError) as exc:
        adoption.adopt(project, _action(project, ch, v1.version_id, check1))
    assert exc.value.code is ErrorCode.STALE_BASIS


def test_new_persistent_setting_requires_specific_authorization(project, planned_chapter):
    ch = planned_chapter
    body = "沈砚从袖中抽出细铁丝，拨动锁簧。"
    v1 = authoring.save_draft(project, ch.id, body, Expected())
    start = body.index("从袖中")
    quote = body[start : start + 9]
    issue = IssueInput(
        kind=IssueKind.NEW_PERSISTENT_SETTING,
        explanation="新增开锁能力与工具",
        evidence=(
            EvidenceInput(quote=quote, version_id=v1.version_id, start=start, end=start + 9),
        ),
    )
    check1, _ = _check_for(project, ch, v1.version_id, issues=[issue])
    # 只有已读声明：拒绝
    with pytest.raises(WorkbenchError) as exc:
        adoption.adopt(project, _action(project, ch, v1.version_id, check1))
    assert exc.value.code is ErrorCode.AUTHORIZATION_REQUIRED
    issue_id = database.get_check(project.conn, project.novel_id, check1).issues[0].id
    # 用错误动作处理：仍拒绝
    with pytest.raises(WorkbenchError):
        adoption.adopt(
            project,
            _action(
                project,
                ch,
                v1.version_id,
                check1,
                issue_resolutions=(IssueResolution(issue_id, ResolutionAction.KEEP_AS_NARRATIVE),),
            ),
        )
    # 明确授权该设定：通过，resolution 同事务写入
    result = adoption.adopt(
        project,
        _action(
            project,
            ch,
            v1.version_id,
            check1,
            issue_resolutions=(
                IssueResolution(issue_id, ResolutionAction.AUTHORIZE_SETTING, "接受"),
            ),
        ),
    )
    assert not result.already_adopted
    n = project.conn.execute("SELECT COUNT(*) FROM resolution").fetchone()[0]
    assert n == 1


def test_fake_evidence_makes_check_invalid_and_unsaved(project, planned_chapter):
    ch = planned_chapter
    body = "沈砚把灯搁在井沿。"
    v1 = authoring.save_draft(project, ch.id, body, Expected())
    fake = CandidateFactInput(
        kind=FactKind.WORLD,
        proposition="井底有尸体",
        evidence_start=0,
        evidence_end=3,
        quote="井底有",
    )
    with pytest.raises(WorkbenchError) as exc:
        _check_for(project, ch, v1.version_id, facts=[fake])
    assert exc.value.code is ErrorCode.MODEL_FAILED
    assert project.conn.execute("SELECT COUNT(*) FROM check_result").fetchone()[0] == 0
    assert project.conn.execute("SELECT COUNT(*) FROM candidate_fact").fetchone()[0] == 0
    # 引用正确即可保存，台词可作为人物认知而不需要世界事实
    ok = CandidateFactInput(
        kind=FactKind.CHARACTER_BELIEF,
        subject="沈砚",
        proposition="灯在井沿",
        evidence_start=0,
        evidence_end=9,
        quote=body[0:9],
    )
    check_id, _ = _check_for(project, ch, v1.version_id, facts=[ok])
    assert (
        project.conn.execute("SELECT kind FROM candidate_fact").fetchone()[0] == "character_belief"
    )


def test_chapter_two_needs_ready_snapshot_of_chapter_one(project, planned_chapter):
    ch1 = planned_chapter
    v1 = authoring.save_draft(project, ch1.id, "第一章正文", Expected())
    check1, _ = _check_for(project, ch1, v1.version_id)
    adoption.adopt(project, _action(project, ch1, v1.version_id, check1))  # 状态待更新
    ch2 = authoring.create_chapter(project, 2)
    msg = authoring.record_message(project, "第二章计划", ch2.id)
    authoring.save_plan(project, ch2.id, "进旧屋", "停在内门前", Expected(), msg)
    v2 = authoring.save_draft(project, ch2.id, "第二章正文", Expected())
    check2, _ = _check_for(project, ch2, v2.version_id)
    with pytest.raises(WorkbenchError) as exc:
        adoption.adopt(project, _action(project, ch2, v2.version_id, check2))
    assert exc.value.code is ErrorCode.STALE_BASIS
    assert "上一章" in exc.value.message


def test_readoption_of_chapter_one_marks_later_chapters_for_review(project, planned_chapter):
    ch1 = planned_chapter
    v1 = authoring.save_draft(project, ch1.id, "第一章正文", Expected())
    check1, _ = _check_for(project, ch1, v1.version_id)
    first = adoption.adopt(project, _action(project, ch1, v1.version_id, check1))
    # 人为把第一章快照置为 ready 以模拟 R01 完成
    snap = project.conn.execute(
        "SELECT id FROM snapshot WHERE adoption_id = ?", (first.adoption_id,)
    ).fetchone()[0]
    with database.transaction(project.conn):
        project.conn.execute("UPDATE snapshot SET status = 'ready' WHERE id = ?", (snap,))
        project.conn.execute(
            "UPDATE chapter SET current_snapshot_id = ? WHERE id = ?", (snap, ch1.id)
        )
    ch2 = authoring.create_chapter(project, 2)
    msg = authoring.record_message(project, "第二章计划", ch2.id)
    authoring.save_plan(project, ch2.id, "进旧屋", "停在内门前", Expected(), msg)
    v2 = authoring.save_draft(project, ch2.id, "第二章正文", Expected())
    check2, _ = _check_for(project, ch2, v2.version_id)
    second = adoption.adopt(project, _action(project, ch2, v2.version_id, check2))
    snap2 = project.conn.execute(
        "SELECT id FROM snapshot WHERE adoption_id = ?", (second.adoption_id,)
    ).fetchone()[0]
    with database.transaction(project.conn):
        project.conn.execute("UPDATE snapshot SET status = 'ready' WHERE id = ?", (snap2,))
        project.conn.execute(
            "UPDATE chapter SET current_snapshot_id = ? WHERE id = ?", (snap2, ch2.id)
        )
    # 回改第一章并重新采用
    v1b = authoring.save_draft(
        project, ch1.id, "第一章正文，增加手腕线索。", Expected(current_draft_id=v1.version_id)
    )
    check1b, _ = _check_for(project, ch1, v1b.version_id)
    re = adoption.adopt(project, _action(project, ch1, v1b.version_id, check1b))
    assert not re.already_adopted
    ch2_now = authoring.get_chapter(project, ch2.id)
    assert ch2_now.needs_review
    assert ch2_now.adopted_id == second.adoption_id  # 后章采用记录不变
    assert (
        database.get_snapshot(project.conn, project.novel_id, snap2).status is SnapshotStatus.STALE
    )
    impacts = project.conn.execute("SELECT status, source_adoption_id FROM impact").fetchall()
    assert [(r[0], r[1]) for r in impacts] == [("pending", re.adoption_id)]
    # 第一章旧快照过时，但历史采用仍在
    assert (
        database.get_snapshot(project.conn, project.novel_id, snap).status is SnapshotStatus.STALE
    )
    assert project.conn.execute("SELECT COUNT(*) FROM adoption").fetchone()[0] == 3

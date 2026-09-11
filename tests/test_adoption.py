"""采用与版本的集成流程。当前覆盖 S01/S02：项目、章节关系、不可变版本、指针与恢复。"""

from __future__ import annotations

import sqlite3

import pytest

from story_workbench.application import authoring
from story_workbench.application.projects import create_project, open_project
from story_workbench.domain.records import (
    ErrorCode,
    Expected,
    Target,
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

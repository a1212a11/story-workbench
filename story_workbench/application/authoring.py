"""讨论、决定、计划、正文编辑及历史恢复的用例。"""

from __future__ import annotations

from story_workbench.domain import rules
from story_workbench.domain.records import (
    Authority,
    ChapterRecord,
    ErrorCode,
    Expected,
    Lifecycle,
    MessageRole,
    PlanRecord,
    SaveResult,
    StoryItemRecord,
    Target,
    VersionOrigin,
    VersionRecord,
    WorkbenchError,
)
from story_workbench.infrastructure import database

from . import projects
from .projects import Project


def record_message(
    project: Project,
    text: str,
    chapter_id: str | None = None,
    role: MessageRole = MessageRole.AUTHOR,
) -> str:
    """作者或模型原文落库；正文中的指令只是数据，不被执行。"""
    if chapter_id is not None:
        database.get_chapter(project.conn, project.novel_id, chapter_id)
    return database.commit_message(project.conn, project.novel_id, role.value, text, chapter_id)


def create_chapter(project: Project, number: int) -> ChapterRecord:
    return database.commit_new_chapter(project.conn, project.novel_id, number)


def get_chapter(project: Project, chapter_id: str) -> ChapterRecord:
    return database.get_chapter(project.conn, project.novel_id, chapter_id)


def list_chapters(project: Project) -> list[ChapterRecord]:
    return database.list_chapters(project.conn, project.novel_id)


def save_plan(
    project: Project,
    chapter_id: str,
    plot: str | None,
    ending: str | None,
    expected: Expected,
    source_message_id: str,
) -> PlanRecord:
    """保留原计划、追加新修订。本章相关检查因 plan_id 变化而自然失效。"""
    return database.commit_plan(
        project.conn,
        project.novel_id,
        chapter_id,
        plot,
        ending,
        source_message_id,
        expected.plan_id,
    )


def save_draft(project: Project, chapter_id: str, text: str, expected: Expected) -> SaveResult:
    """保存作者编辑稿。

    先对账外部工作文件：若外部有修改会先导入为新稿，此时作者本次编辑的基准已过时，
    本稿仍会保存但不抢占当前稿指针，由入口展示两稿供作者选择。
    编辑基准是 expected.current_draft_id：与章节当前编辑稿一致时新稿成为当前稿；
    不一致时保存为独立稿并返回冲突，不丢作者输入、不自动合并。
    """
    projects.reconcile_workcopy(project, chapter_id)
    body = rules.normalize_newlines(text)
    return database.commit_version(
        project.conn,
        project.novel_id,
        chapter_id,
        body,
        VersionOrigin.AUTHOR,
        parent_id=expected.current_draft_id,
        expected_current_draft_id=expected.current_draft_id,
    )


def get_version(project: Project, target: Target) -> VersionRecord:
    rules.require_same_scope(project.novel_id, target.novel_id, "正文版本")
    version = database.get_version(project.conn, project.novel_id, target.version_id)
    if version.chapter_id != target.chapter_id:
        rules.require_same_scope(target.chapter_id, version.chapter_id, "正文版本所属章节")
    return version


def list_versions(project: Project, chapter_id: str) -> list[VersionRecord]:
    return database.list_versions(project.conn, project.novel_id, chapter_id)


def restore_version(project: Project, target: Target, expected: Expected) -> SaveResult:
    """用历史正文创建新的恢复稿；不删除历史。恢复为正式正文仍需另走采用。"""
    source = get_version(project, target)
    return database.commit_version(
        project.conn,
        project.novel_id,
        source.chapter_id,
        source.body,
        VersionOrigin.RESTORE,
        parent_id=expected.current_draft_id,
        expected_current_draft_id=expected.current_draft_id,
        restored_from_id=source.id,
    )


# ---------------------------------------------------------------- 计划、故事条目、拒绝稿与稿件选择


def get_current_plan(project: Project, chapter_id: str) -> PlanRecord | None:
    chapter = database.get_chapter(project.conn, project.novel_id, chapter_id)
    if chapter.current_plan_id is None:
        return None
    return database.get_plan(project.conn, project.novel_id, chapter.current_plan_id)


def list_plans(project: Project, chapter_id: str) -> list[PlanRecord]:
    return database.list_plans(project.conn, project.novel_id, chapter_id)


def adopted_version_id(project: Project, chapter_id: str) -> str | None:
    chapter = database.get_chapter(project.conn, project.novel_id, chapter_id)
    return database.get_adopted_version_id(project.conn, project.novel_id, chapter)


def list_story_items(project: Project) -> list[StoryItemRecord]:
    return database.list_story_items_current(project.conn, project.novel_id)


def record_item(
    project: Project,
    *,
    kind: str,
    body: str,
    authority: Authority,
    lifecycle: Lifecycle,
    source_message_id: str,
    chapter_id: str | None = None,
) -> StoryItemRecord:
    """新建故事条目。候选想法用 AUTHOR_IDEA/CANDIDATE；模型建议用 AI_SUGGESTION/DRAFT。"""
    if lifecycle is Lifecycle.DECIDED and authority is not Authority.AUTHOR_DECISION:
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "只有作者决定才能标记为已确定。")
    return database.commit_story_item(
        project.conn,
        project.novel_id,
        kind=kind,
        body=body,
        authority=authority,
        lifecycle=lifecycle,
        source_message_id=source_message_id,
        chapter_id=chapter_id,
    )


def decide_item(
    project: Project,
    item: StoryItemRecord,
    *,
    lifecycle: Lifecycle,
    source_message_id: str,
    body: str | None = None,
    authorize_change: bool = False,
) -> StoryItemRecord:
    """作者对候选做决定、拒绝、归档或修订。

    改变已确定的决定必须 authorize_change=True（先说明影响并获得同意）；
    决定的新增或变更会递增 design_revision，使旧依据保守过时。
    """
    new_body = item.body if body is None else body
    if item.lifecycle is Lifecycle.DECIDED and not authorize_change:
        raise WorkbenchError(
            ErrorCode.AUTHORIZATION_REQUIRED,
            "这是已确定的内容，改变它需要明确授权。",
            {"story_item_id": item.id},
        )
    authority = Authority.AUTHOR_DECISION if lifecycle is Lifecycle.DECIDED else item.authority
    if (
        lifecycle in (Lifecycle.REJECTED, Lifecycle.ARCHIVED)
        and item.authority is Authority.AI_SUGGESTION
    ):
        authority = Authority.AI_SUGGESTION
    return database.commit_story_item(
        project.conn,
        project.novel_id,
        kind=item.kind,
        body=new_body,
        authority=authority,
        lifecycle=lifecycle,
        source_message_id=source_message_id,
        chapter_id=item.chapter_id,
        logical_id=item.logical_id,
        expected_revision_id=item.id,
    )


def reject_draft(project: Project, target: Target, source_message_id: str, reason: str = "") -> str:
    """标记拒绝稿：不删除版本，不进入正式上下文；若它是当前编辑稿则回退到其父版本。"""
    version = get_version(project, target)
    return database.commit_draft_rejection(
        project.conn, project.novel_id, version.id, source_message_id, reason
    )


def choose_current_draft(project: Project, target: Target, expected: Expected) -> None:
    """作者明确选择某一稿继续编辑；不新建版本。"""
    version = get_version(project, target)
    database.commit_choose_current_draft(
        project.conn, project.novel_id, version.chapter_id, version.id, expected.current_draft_id
    )

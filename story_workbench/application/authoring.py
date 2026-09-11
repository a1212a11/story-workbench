"""讨论、决定、计划、正文编辑及历史恢复的用例。"""

from __future__ import annotations

from story_workbench.domain import rules
from story_workbench.domain.records import (
    ChapterRecord,
    Expected,
    MessageRole,
    PlanRecord,
    SaveResult,
    Target,
    VersionOrigin,
    VersionRecord,
)
from story_workbench.infrastructure import database

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

    编辑基准是 expected.current_draft_id：与章节当前编辑稿一致时新稿成为当前稿；
    不一致时保存为独立稿并返回冲突，不丢作者输入、不自动合并。
    文件对账（S03）接入后在此之前调用 projects.reconcile_workcopy。
    """
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

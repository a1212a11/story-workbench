"""项目生命周期：创建、打开、关闭、文件对账（S03）、备份与重启恢复（R03/R04）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from story_workbench.domain import rules
from story_workbench.domain.records import (
    ErrorCode,
    NovelRecord,
    ReconcileOutcome,
    ReconcileResult,
    RecoveryNotice,
    SyncStatus,
    WorkbenchError,
)
from story_workbench.infrastructure import database, files


@dataclass(slots=True)
class Project:
    """项目资源句柄；由启动入口显式传给用例，不进入 domain、ai 或模型任务。"""

    root: Path
    novel: NovelRecord
    conn: sqlite3.Connection
    lock: files.ProjectLock

    @property
    def novel_id(self) -> str:
        return self.novel.id

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            self.lock.release()


def create_project(path: Path, title: str) -> Project:
    """在空目录或不存在的目录创建新作品。已有数据库时拒绝，改用 open_project。"""
    root = Path(path)
    if (root / files.DB_FILENAME).exists():
        raise WorkbenchError(
            ErrorCode.INVALID_INPUT, "目录已经是一个作品项目，请直接打开。", {"path": str(root)}
        )
    if not title.strip():
        raise WorkbenchError(ErrorCode.INVALID_INPUT, "作品需要一个标题。")
    files.ensure_layout(root)
    lock = files.ProjectLock(root / files.LOCK_FILENAME)
    lock.acquire()
    try:
        conn = database.connect(root / files.DB_FILENAME)
        database.migrate(conn)
        novel = database.commit_new_novel(conn, title.strip())
    except Exception:
        lock.release()
        raise
    return Project(root=root, novel=novel, conn=conn, lock=lock)


def open_project(path: Path) -> tuple[Project, RecoveryNotice]:
    """校验布局、版本、锁；未结束任务标 interrupted；不自动重新调用模型。"""
    root = Path(path)
    db_path = root / files.DB_FILENAME
    if not db_path.exists():
        raise WorkbenchError(ErrorCode.NOT_FOUND, "这里没有作品项目。", {"path": str(root)})
    lock = files.ProjectLock(root / files.LOCK_FILENAME)
    lock.acquire()
    try:
        files.ensure_layout(root)
        conn = database.connect(db_path)
        database.migrate(conn)
        if not database.integrity_ok(conn):
            raise WorkbenchError(
                ErrorCode.STORAGE_FAILED, "作品数据库完整性检查未通过。", {"path": str(db_path)}
            )
        novel = database.get_novel(conn)
        if novel is None:
            raise WorkbenchError(ErrorCode.STORAGE_FAILED, "数据库中没有作品记录。")
        interrupted = database.commit_mark_runs_interrupted(conn, novel.id)
    except Exception:
        lock.release()
        raise
    notes: list[str] = []
    if interrupted:
        notes.append("上次有未完成的任务已标记为中断，可以查看后决定是否重试。")
    notice = RecoveryNotice(interrupted_run_ids=interrupted, notes=tuple(notes))
    return Project(root=root, novel=novel, conn=conn, lock=lock), notice


# ---------------------------------------------------------------- 工作副本对账（S03）


def _workcopy_path(chapter_number: int) -> str:
    return f"{files.WORK_DIR}/chapter-{chapter_number:03d}.md"


def create_workcopy(project: Project, chapter_id: str) -> ReconcileResult:
    """为章节建立外部工作文件。

    首次用排他创建写入当前编辑稿；若同名文件已存在，先把它作为外部稿导入，绝不覆盖。
    """
    chapter = database.get_chapter(project.conn, project.novel_id, chapter_id)
    existing = database.get_workcopy(project.conn, project.novel_id, chapter_id)
    if existing is not None:
        return reconcile_workcopy(project, chapter_id)
    relative_path = _workcopy_path(chapter.number)
    observation = files.observe_workcopy(project.root, relative_path, None)
    if observation.exists:
        database.commit_workcopy_binding(
            project.conn,
            project.novel_id,
            chapter_id,
            relative_path,
            None,
            None,
            SyncStatus.PENDING,
        )
        return reconcile_workcopy(project, chapter_id)
    body = ""
    if chapter.current_draft_id is not None:
        body = database.get_version(project.conn, project.novel_id, chapter.current_draft_id).body
    actual = files.write_new(project.root, relative_path, body)
    database.commit_workcopy_binding(
        project.conn,
        project.novel_id,
        chapter_id,
        actual,
        chapter.current_draft_id,
        rules.content_hash(body),
        SyncStatus.IN_SYNC,
    )
    return ReconcileResult(ReconcileOutcome.IN_SYNC, actual, message="已建立工作文件。")


def reconcile_workcopy(project: Project, chapter_id: str) -> ReconcileResult:
    """读取、生成、采用前调用：外部变化入库为新稿；遇冲突保留两稿，不覆盖文件。"""
    wc = database.get_workcopy(project.conn, project.novel_id, chapter_id)
    if wc is None:
        return ReconcileResult(ReconcileOutcome.NO_WORKCOPY)
    try:
        observation = files.observe_workcopy(project.root, wc.relative_path, wc.base_hash)
    except WorkbenchError as exc:
        if exc.code is ErrorCode.WORKCOPY_CONFLICT:
            database.commit_workcopy_status(
                project.conn, project.novel_id, chapter_id, SyncStatus.PENDING
            )
            return ReconcileResult(
                ReconcileOutcome.UNREADABLE, wc.relative_path, message=exc.message
            )
        raise
    if not observation.exists:
        if wc.base_hash is not None:
            database.commit_workcopy_status(
                project.conn, project.novel_id, chapter_id, SyncStatus.PENDING
            )
        return ReconcileResult(
            ReconcileOutcome.FILE_MISSING,
            wc.relative_path,
            message="工作文件不存在；数据库中的稿件保留，未视为清空。",
        )
    if observation.decode_error:
        database.commit_workcopy_status(
            project.conn, project.novel_id, chapter_id, SyncStatus.PENDING
        )
        return ReconcileResult(
            ReconcileOutcome.UNREADABLE,
            wc.relative_path,
            message="工作文件不是 UTF-8 文本，暂不导入；数据库稿件保留。",
        )
    if not observation.changed_from_baseline:
        if wc.sync_status is not SyncStatus.IN_SYNC:
            database.commit_workcopy_status(
                project.conn, project.novel_id, chapter_id, SyncStatus.IN_SYNC
            )
        return ReconcileResult(ReconcileOutcome.IN_SYNC, wc.relative_path)
    assert observation.content is not None
    body = rules.normalize_newlines(observation.content)
    chapter = database.get_chapter(project.conn, project.novel_id, chapter_id)
    if chapter.current_draft_id is not None:
        current = database.get_version(project.conn, project.novel_id, chapter.current_draft_id)
        if current.content_hash == rules.content_hash(body):
            # 文件内容已与当前稿一致（例如刚导出后未改），只更新基准，不新建版本。
            database.commit_workcopy_binding(
                project.conn,
                project.novel_id,
                chapter_id,
                wc.relative_path,
                current.id,
                current.content_hash,
                SyncStatus.IN_SYNC,
            )
            return ReconcileResult(ReconcileOutcome.IN_SYNC, wc.relative_path)
    result = database.commit_external_import(
        project.conn, project.novel_id, chapter_id, body, wc.base_version_id
    )
    if result.became_current:
        return ReconcileResult(
            ReconcileOutcome.IMPORTED,
            wc.relative_path,
            imported_version_id=result.version_id,
            message="发现外部修改，已保存为新稿并作为当前编辑稿。",
        )
    return ReconcileResult(
        ReconcileOutcome.CONFLICT,
        wc.relative_path,
        imported_version_id=result.version_id,
        conflicting_current_id=result.conflicting_current_id,
        message="外部修改与应用内新稿同时存在，两稿都已保留，请选择要继续的稿件。",
    )


def export_version(project: Project, chapter_id: str, version_id: str) -> str:
    """把指定版本导出为独立新文件；不触碰工作文件。返回相对路径。"""
    chapter = database.get_chapter(project.conn, project.novel_id, chapter_id)
    version = database.get_version(project.conn, project.novel_id, version_id)
    if version.chapter_id != chapter_id:
        raise WorkbenchError(ErrorCode.WRONG_SCOPE, "版本不属于该章。", {"version_id": version_id})
    name = f"{files.EXPORTS_DIR}/chapter-{chapter.number:03d}-{version.id[:8]}.md"
    return files.write_new(project.root, name, version.body)


def write_recovery(project: Project, chapter_id: str, text: str) -> str:
    """数据库保存失败时把作者输入写成唯一恢复文件；返回相对路径。"""
    chapter = database.get_chapter(project.conn, project.novel_id, chapter_id)
    stamp = database.now_utc().replace(":", "").replace("-", "")[:15]
    name = f"{files.RECOVERY_DIR}/chapter-{chapter.number:03d}-{stamp}.md"
    return files.write_new(project.root, name, text)

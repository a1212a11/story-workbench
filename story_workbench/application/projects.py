"""项目生命周期：创建、打开、关闭、文件对账（S03）、备份与重启恢复（R03/R04）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from story_workbench.domain.records import ErrorCode, NovelRecord, RecoveryNotice, WorkbenchError
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

"""项目生命周期：创建、打开、关闭、文件对账（S03）、备份与重启恢复（R03/R04）。"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from story_workbench.domain import rules
from story_workbench.domain.records import (
    BranchRecord,
    ErrorCode,
    NovelRecord,
    ReconcileOutcome,
    ReconcileResult,
    RecoveryNotice,
    RunStatus,
    SyncStatus,
    WorkbenchError,
)
from story_workbench.infrastructure import database, files


@dataclass(slots=True)
class Project:
    """项目资源句柄；由启动入口显式传给用例，不进入 domain、ai 或模型任务。"""

    root: Path
    novel: NovelRecord
    branch: BranchRecord
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
        branch = database.current_branch(conn)
    except Exception:
        lock.release()
        raise
    return Project(root=root, novel=novel, branch=branch, conn=conn, lock=lock)


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
        branch = database.current_branch(conn)
        interrupted = database.commit_mark_project_runs_interrupted(conn)
    except Exception:
        lock.release()
        raise
    notes: list[str] = []
    if interrupted:
        notes.append("上次有未完成的任务已标记为中断，可以查看后决定是否重试。")
    notice = RecoveryNotice(interrupted_run_ids=interrupted, notes=tuple(notes))
    return Project(root=root, novel=novel, branch=branch, conn=conn, lock=lock), notice


# ---------------------------------------------------------------- 工作副本对账（S03）


def _workcopy_path(project: Project, chapter_number: int) -> str:
    directory = files.WORK_DIR
    if project.branch.work_subdir:
        directory = f"{files.WORK_DIR}/{project.branch.work_subdir}"
    return f"{directory}/chapter-{chapter_number:03d}.md"


def list_branches(project: Project) -> tuple[BranchRecord, ...]:
    return database.list_branches(project.conn)


def create_branch(project: Project, name: str, source_adoption_id: str) -> BranchRecord:
    branch = database.commit_branch_from_adoption(
        project.conn,
        project.novel_id,
        name,
        source_adoption_id,
    )
    files.ensure_branch_dir(project.root, branch.id)
    if branch.work_subdir:
        files.safe_relative(project.root, files.WORK_DIR + "/" + branch.work_subdir).mkdir(
            parents=True, exist_ok=True
        )
    return database.commit_branch_ready(project.conn, branch.id)


def switch_branch(project: Project, branch_id: str) -> BranchRecord:
    for workcopy in database.list_workcopies(project.conn, project.novel_id):
        reconcile_workcopy(project, workcopy.chapter_id)
    branch = database.get_branch(project.conn, branch_id)
    project.novel = database.switch_branch(project.conn, branch.id)
    branch = database.get_branch(project.conn, branch.id)
    project.branch = branch
    files.ensure_layout(project.root)
    if branch.work_subdir:
        files.safe_relative(project.root, files.WORK_DIR + "/" + branch.work_subdir).mkdir(
            parents=True, exist_ok=True
        )
    return branch


def fork_branch(project: Project, name: str, source_adoption_id: str) -> BranchRecord:
    return create_branch(project, name, source_adoption_id)


def create_workcopy(project: Project, chapter_id: str) -> ReconcileResult:
    """为章节建立外部工作文件。

    首次用排他创建写入当前编辑稿；若同名文件已存在，先把它作为外部稿导入，绝不覆盖。
    """
    chapter = database.get_chapter(project.conn, project.novel_id, chapter_id)
    existing = database.get_workcopy(project.conn, project.novel_id, chapter_id)
    if existing is not None:
        return reconcile_workcopy(project, chapter_id)
    relative_path = _workcopy_path(project, chapter.number)
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


# ---------------------------------------------------------------- 备份与恢复（R04）


@dataclass(frozen=True, slots=True)
class BackupReport:
    destination: str
    file_count: int
    schema_version: int
    interrupted_run_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RestoreReport:
    path: str
    file_count: int
    schema_version: int
    interrupted_run_ids: tuple[str, ...]
    problems: tuple[str, ...]


def backup(project: Project, destination: Path) -> BackupReport:
    """一致性备份到空新目录：数据库快照、work/exports/recovery 文件、清单。

    备份前后复核外部文件；检测到变化则删除本次输出并报告未完成，不给出成功报告。
    不包含项目锁与任何密钥。
    """
    destination = Path(destination)
    files.require_empty_or_missing(destination)
    before = files.snapshot_file_hashes(project.root)
    destination.mkdir(parents=True, exist_ok=True)
    try:
        database.backup_to(project.conn, destination / files.DB_FILENAME)
        copied = files.copy_files(project.root, destination, sorted(before))
        after = files.snapshot_file_hashes(project.root)
        if before != after or copied != before:
            raise WorkbenchError(
                ErrorCode.STORAGE_FAILED,
                "备份期间工作文件发生变化，本次备份未完成，请重试。",
                {
                    "changed": sorted(
                        set(before) ^ set(after) | {k for k in before if after.get(k) != before[k]}
                    )
                },
            )
        schema_version = database.schema_version(project.conn)
        interrupted = tuple(
            database.list_runs_with_status(project.conn, project.novel_id, RunStatus.RUNNING)
            + database.list_runs_with_status(project.conn, project.novel_id, RunStatus.PENDING)
        )
        manifest = {
            "schema_version": schema_version,
            "novel_id": project.novel_id,
            "created_at": database.now_utc(),
            "files": copied,
            "database": files.DB_FILENAME,
        }
        (destination / files.MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        files.remove_tree(destination)
        raise
    return BackupReport(str(destination), len(copied), schema_version, interrupted)


def restore_backup(source: Path, new_path: Path) -> RestoreReport:
    """把备份恢复到空新目录并校验：清单摘要、数据库完整性、外键与引用。绝不覆盖原项目。"""
    source, new_path = Path(source), Path(new_path)
    manifest_path = source / files.MANIFEST_FILENAME
    if not manifest_path.exists():
        raise WorkbenchError(ErrorCode.NOT_FOUND, "备份目录缺少清单文件。", {"path": str(source)})
    files.require_empty_or_missing(new_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected: dict[str, str] = manifest["files"]
    actual = files.snapshot_file_hashes(source)
    if actual != expected:
        raise WorkbenchError(
            ErrorCode.STORAGE_FAILED,
            "备份文件与清单不一致，拒绝恢复。",
            {
                "mismatch": sorted(
                    set(actual) ^ set(expected)
                    | {k for k in expected if actual.get(k) != expected[k]}
                )
            },
        )
    db_version, db_ok = database.verify_database_file(source / files.DB_FILENAME)
    if not db_ok:
        raise WorkbenchError(ErrorCode.STORAGE_FAILED, "备份数据库完整性检查未通过，拒绝恢复。")
    if db_version > database.SCHEMA_VERSION:
        raise WorkbenchError(
            ErrorCode.INVALID_INPUT,
            "备份来自更新版本的应用，当前应用无法恢复。",
            {"db_version": db_version},
        )
    new_path.mkdir(parents=True, exist_ok=True)
    try:
        files.ensure_layout(new_path)
        files.copy_files(source, new_path, [files.DB_FILENAME] + sorted(expected))
        project, notice = open_project(new_path)
        try:
            problems = database.verify_references(project.conn, project.novel_id)
            schema_version = database.schema_version(project.conn)
            restored = files.snapshot_file_hashes(new_path)
        finally:
            project.close()
        if restored != expected or problems:
            raise WorkbenchError(
                ErrorCode.STORAGE_FAILED,
                "恢复后校验未通过。",
                {"problems": problems, "file_mismatch": restored != expected},
            )
    except Exception:
        files.remove_tree(new_path)
        raise
    return RestoreReport(
        str(new_path), len(expected), schema_version, notice.interrupted_run_ids, tuple(problems)
    )

"""工作副本读取、独立文件写入、项目锁与备份文件校验。唯一的文件 I/O 入口。

不更新正文或采用表；文件观察值沿调用栈返回应用，由应用决定是否入库。
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from story_workbench.domain.records import ErrorCode, WorkbenchError

DB_FILENAME = "story.sqlite3"
LOCK_FILENAME = "project.lock"
WORK_DIR = "work"
EXPORTS_DIR = "exports"
RECOVERY_DIR = "recovery"


@dataclass(frozen=True, slots=True)
class FileObservation:
    """一次文件读取的观察值：内容、摘要与相对基准的变化状态。"""

    relative_path: str
    exists: bool
    content: str | None
    content_hash: str | None
    changed_from_baseline: bool
    decode_error: bool = False


class ProjectLock:
    """由操作系统持有的排他锁；进程崩溃时自动释放。macOS/Linux 用 flock。"""

    def __init__(self, path: Path):
        self._path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise WorkbenchError(
                ErrorCode.PROJECT_BUSY,
                "这个项目正在被另一个进程使用。",
                {"lock": str(self._path)},
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None


def ensure_layout(root: Path) -> None:
    """创建项目目录布局；已有目录不动。"""
    root.mkdir(parents=True, exist_ok=True)
    for name in (WORK_DIR, EXPORTS_DIR, RECOVERY_DIR):
        (root / name).mkdir(exist_ok=True)


def safe_relative(root: Path, relative_path: str) -> Path:
    """拒绝越界与符号链接目标；路径由应用生成，不直接拼作者标题。"""
    candidate = (root / relative_path).resolve()
    root_resolved = root.resolve()
    if root_resolved not in candidate.parents and candidate != root_resolved:
        raise WorkbenchError(
            ErrorCode.INVALID_INPUT, "路径越出项目目录。", {"relative_path": relative_path}
        )
    if candidate.exists() and candidate.is_symlink():
        raise WorkbenchError(
            ErrorCode.INVALID_INPUT, "不接受符号链接。", {"relative_path": relative_path}
        )
    return candidate


def observe_workcopy(root: Path, relative_path: str, baseline_hash: str | None) -> FileObservation:
    """读取工作文件，前后两次读取内容一致才作为观察值；不产生数据库版本。"""
    path = safe_relative(root, relative_path)
    if not path.exists():
        return FileObservation(relative_path, False, None, None, baseline_hash is not None)
    try:
        first = path.read_bytes()
        second = path.read_bytes()
    except OSError as exc:
        raise WorkbenchError(
            ErrorCode.STORAGE_FAILED, "读取工作文件失败。", {"relative_path": relative_path}
        ) from exc
    if first != second:
        raise WorkbenchError(
            ErrorCode.WORKCOPY_CONFLICT,
            "工作文件在读取期间发生变化，请稍后重试。",
            {"relative_path": relative_path},
        )
    try:
        content = first.decode("utf-8")
    except UnicodeDecodeError:
        return FileObservation(relative_path, True, None, None, True, decode_error=True)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return FileObservation(relative_path, True, content, digest, digest != baseline_hash)


def write_new(root: Path, relative_path: str, content: str) -> str:
    """排他创建；同名已存在时自动换名，绝不覆盖。返回实际相对路径。"""
    target = safe_relative(root, relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    stem, suffix = target.stem, target.suffix
    for attempt in range(1000):
        candidate = target if attempt == 0 else target.with_name(f"{stem}-{attempt}{suffix}")
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        except OSError as exc:
            raise WorkbenchError(
                ErrorCode.STORAGE_FAILED, "写入新文件失败。", {"relative_path": relative_path}
            ) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        return str(candidate.relative_to(root.resolve()))
    raise WorkbenchError(
        ErrorCode.STORAGE_FAILED, "无法为新文件找到可用名称。", {"relative_path": relative_path}
    )


# ---------------------------------------------------------------- 备份文件处理（R04）

MANIFEST_FILENAME = "manifest.json"
BACKUP_DIRS = (WORK_DIR, EXPORTS_DIR, RECOVERY_DIR)


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot_file_hashes(root: Path) -> dict[str, str]:
    """列出 work/exports/recovery 下全部普通文件及其摘要；跳过符号链接。"""
    result: dict[str, str] = {}
    for name in BACKUP_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            result[str(path.relative_to(root))] = _hash_bytes(path.read_bytes())
    return result


def copy_files(root: Path, destination: Path, relative_paths: list[str]) -> dict[str, str]:
    """把列出的文件复制到目标目录（排他创建），返回复制后的摘要。"""
    copied: dict[str, str] = {}
    for rel in relative_paths:
        data = (root / rel).read_bytes()
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        copied[rel] = _hash_bytes(data)
    return copied


def require_empty_or_missing(path: Path) -> None:
    if path.exists():
        if not path.is_dir() or any(path.iterdir()):
            raise WorkbenchError(
                ErrorCode.INVALID_INPUT, "恢复目标必须是空目录或不存在的目录。", {"path": str(path)}
            )


def remove_tree(path: Path) -> None:
    """只用于清理本次未完成的备份输出。"""
    import shutil

    shutil.rmtree(path, ignore_errors=True)

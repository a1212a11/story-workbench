"""作品目录：目录约定、正式/自由文件判定、初始化、文件读写与搜索、git 历史、对话与用量记录。

这是唯一碰文件系统与 git 的模块。写文件只有 `write_text` 一个入口，原子替换；
它不判断是否需要审批，审批由 tools 层在调用前完成。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from story_agent.layout import (
    CHAPTER_PATTERN,
    CHAPTERS_DIR,
    CHARACTERS_DIR,
    DRAFTS_DIR,
    HIDDEN_DIRS,
    LOG_FILE,
    PRIVATE_DIR,
    REWIND_PREFIX,
    SETTINGS_DIR,
    SKILLS_BACKUP_DIR,
    SKILLS_DIR,
    SKILLS_RECORD,
    SUMMARY_RECORD,
    TEMPLATES,
    TRANSCRIPT_DIR,
    TRANSCRIPT_RECORD,
    USAGE_RECORD,
    WORK_FILE,
    WorkspaceError,
    normalize,
)
from story_agent.layout import chapter_file as chapter_file
from story_agent.layout import is_formal as is_formal

SKILLS_SOURCE = Path(__file__).parent / "skills"


def describe_time(moment: datetime, now: datetime | None = None) -> str:
    """人话时间：「今天 21:03」「昨天 09:12」「9月16日 10:43」「2025年3月2日」。"""
    now = now or datetime.now(moment.tzinfo)
    local = moment.astimezone(now.tzinfo)
    day = local.date()
    today = now.date()
    clock = local.strftime("%H:%M")
    if day == today:
        return f"今天 {clock}"
    if day == today - timedelta(days=1):
        return f"昨天 {clock}"
    if day.year == today.year:
        return f"{day.month}月{day.day}日 {clock}"
    return f"{day.year}年{day.month}月{day.day}日"


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    ref: str  # 短提交号
    when: datetime
    message: str
    files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SearchHit:
    path: str
    line: int
    text: str


class Git:
    """作品目录里的 git。机器上没有 git 时 available 为 False，所有操作静默跳过。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.available = shutil.which("git") is not None

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=作者",
                "-c",
                "user.email=author@story.local",
                "-c",
                "core.quotepath=false",
                *args,
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=check,
            env={**os.environ, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        )

    @property
    def initialized(self) -> bool:
        return self.available and (self.root / ".git").exists()

    def init(self) -> None:
        if not self.available or self.initialized:
            return
        self._run("init", "-q", "-b", "main")

    def commit(self, message: str, paths: tuple[str, ...] = ()) -> str | None:
        """提交指定文件（默认全部）；没有变化时不提交，返回 None。"""
        if not self.initialized:
            return None
        # 删掉一个从没提交过的文件时，路径既不在盘上也不在库里，add 会报错；那就是没变化
        self._run("add", "-A", "--", *(paths or (".",)), check=False)
        status = self._run("status", "--porcelain", "--", *(paths or (".",)), check=False)
        if not status.stdout.strip():
            return None
        self._run("commit", "-q", "-m", message)
        return self._run("rev-parse", "--short", "HEAD").stdout.strip()

    def log(self, path: str | None = None, limit: int = 20) -> list[HistoryEntry]:
        if not self.initialized:
            return []
        args = ["log", f"-n{limit}", "--format=%h%x1f%aI%x1f%s", "--name-only"]
        if path:
            args += ["--", path]
        result = self._run(*args, check=False)
        if result.returncode != 0:
            return []
        entries: list[HistoryEntry] = []
        files: list[str] = []
        current: tuple[str, datetime, str] | None = None
        for line in result.stdout.splitlines():
            if "\x1f" in line:
                if current is not None:
                    entries.append(HistoryEntry(*current, tuple(files)))
                ref, when, message = line.split("\x1f", 2)
                current, files = (ref, datetime.fromisoformat(when), message), []
            elif line.strip() and current is not None:
                files.append(line.strip())
        if current is not None:
            entries.append(HistoryEntry(*current, tuple(files)))
        return entries

    def show(self, path: str, ref: str) -> str | None:
        if not self.initialized:
            return None
        result = self._run("show", f"{ref}:{path}", check=False)
        return result.stdout if result.returncode == 0 else None

    def head(self) -> str | None:
        if not self.initialized:
            return None
        result = self._run("rev-parse", "--short", "HEAD", check=False)
        return result.stdout.strip() or None if result.returncode == 0 else None

    def restore_tree(self, ref: str, message: str) -> str | None:
        """把受管文件整体退回到某次提交时的样子，作为一次新提交；历史不丢。没有变化返回 None。"""
        if not self.initialized:
            return None
        if self._run("cat-file", "-e", f"{ref}^{{commit}}", check=False).returncode != 0:
            return None
        self._run("read-tree", "--reset", "-u", ref)
        return self.commit(message)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Workspace:
    """一个作品目录。所有路径都是相对于目录的 POSIX 字符串。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.git = Git(self.root)
        self.updated_skills: tuple[str, ...] = ()  # 这次打开时跟着新版更新了的技能

    # ------------------------------------------------------------ 打开与初始化

    @property
    def exists(self) -> bool:
        return (self.root / WORK_FILE).exists()

    @property
    def title(self) -> str:
        if not self.exists:
            return self.root.name
        first = self.read_text(WORK_FILE).splitlines()[:1]
        line = first[0].strip() if first else ""
        return line.lstrip("#").strip().strip("《》") or self.root.name

    @classmethod
    def create(cls, root: Path, title: str) -> Workspace:
        ws = cls(root)
        if ws.exists:
            raise WorkspaceError("这个目录已经是一部作品。")
        ws.root.mkdir(parents=True, exist_ok=True)
        for name in (SETTINGS_DIR, CHARACTERS_DIR, CHAPTERS_DIR, DRAFTS_DIR, TRANSCRIPT_DIR):
            (ws.root / name).mkdir(exist_ok=True)
        (ws.root / PRIVATE_DIR).mkdir(exist_ok=True)
        ws._write_raw(
            WORK_FILE,
            f"# 《{title.strip()}》\n\n"
            "## 题材与基调\n\n（一句话说这是个什么故事。）\n\n"
            "## 篇幅\n\n（预计多少章、每章多少字。）\n\n"
            "## 作者的偏好与禁忌\n\n（喜欢什么样的文字，不要什么。）\n\n"
            "## 文风约定\n\n（写正文的硬约定，一条一行：视角与人称、引号样式、"
            "计时用这个世界的说法、句长节奏、幽默怎么用。）\n",
        )
        for name, template in TEMPLATES.items():
            ws._write_raw(name, template)
        ws.install_skills()
        ws._write_raw(".gitignore", f"{PRIVATE_DIR}/\n")
        ws.git.init()
        ws.git.commit(f"开书：《{title.strip()}》")
        return ws

    @classmethod
    def open(cls, root: Path) -> Workspace:
        ws = cls(root)
        if not ws.exists:
            raise WorkspaceError(f"这里还没有作品：{root}")
        (ws.root / PRIVATE_DIR).mkdir(exist_ok=True)
        (ws.root / TRANSCRIPT_DIR).mkdir(exist_ok=True)
        updated = ws.install_skills()
        if updated:
            ws.updated_skills = tuple(updated)
            ws.append_log("技能更新到新版：" + "、".join(updated))
        ws.git.init()
        return ws

    def install_skills(self, overwrite: bool = False) -> list[str]:
        """把默认技能复制到作品目录。

        作者改过的不覆盖；没改过的跟着程序的新版更新。靠 .story/技能.json 里记的哈希分辨：
        老作品没有记录，分不清是作者改的还是旧版，就更新并把原来那份留在 .story/技能备份/。
        返回这次更新了的文件名。
        """
        target = self.root / SKILLS_DIR
        target.mkdir(exist_ok=True)
        record_path = self.root / PRIVATE_DIR / SKILLS_RECORD
        record: dict[str, str] = {}
        if record_path.is_file():
            try:
                loaded = json.loads(record_path.read_text(encoding="utf-8"))
                record = {k: v for k, v in loaded.items() if isinstance(v, str)}
            except (OSError, ValueError, AttributeError):
                record = {}
        updated: list[str] = []
        for source in sorted(SKILLS_SOURCE.glob("*.md")):
            dest = target / source.name
            shipped = source.read_text(encoding="utf-8")
            if dest.exists() and not overwrite:
                current = dest.read_text(encoding="utf-8")
                if current == shipped:
                    record[source.name] = _digest(shipped)
                    continue
                known = record.get(source.name)
                if known is not None and known != _digest(current):
                    continue  # 作者改过，尊重他的版本
                if known is None:
                    backup_dir = self.root / PRIVATE_DIR / SKILLS_BACKUP_DIR
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    (backup_dir / f"{source.stem}-{stamp}.md").write_text(current, encoding="utf-8")
                updated.append(source.name)
            dest.write_text(shipped, encoding="utf-8")
            record[source.name] = _digest(shipped)
        record_path.parent.mkdir(exist_ok=True)
        record_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return updated

    # ------------------------------------------------------------ 文件

    def path(self, rel: str) -> Path:
        return self.root / normalize(rel)

    def file_exists(self, rel: str) -> bool:
        return self.path(rel).is_file()

    def read_text(self, rel: str) -> str:
        target = self.path(rel)
        if not target.is_file():
            raise WorkspaceError(f"没有这个文件：{normalize(rel)}")
        return target.read_text(encoding="utf-8")

    def _write_raw(self, rel: str, text: str) -> None:
        target = self.root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-", suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def write_text(self, rel: str, text: str) -> None:
        """原子写入。审批在调用前由 tools 层完成；这里不判断。"""
        self._write_raw(normalize(rel), text)

    def delete_text(self, rel: str) -> None:
        """删除文件。审批在调用前由 tools 层完成；历史里仍能找回。"""
        target = self.path(rel)
        if not target.is_file():
            raise WorkspaceError(f"没有这个文件：{normalize(rel)}")
        target.unlink()

    def list_dir(self, rel: str = "") -> list[str]:
        base = self.root if not rel else self.path(rel)
        if not base.is_dir():
            raise WorkspaceError(f"没有这个目录：{normalize(rel) if rel else ''}")
        names = []
        for entry in sorted(base.iterdir(), key=lambda p: p.name):
            if entry.name.startswith(".") or entry.name in HIDDEN_DIRS:
                continue
            names.append(entry.name + ("/" if entry.is_dir() else ""))
        return names

    def all_files(self) -> list[str]:
        result = []
        for path in sorted(self.root.rglob("*.md")):
            rel = path.relative_to(self.root).as_posix()
            if rel.split("/", 1)[0] in HIDDEN_DIRS or path.name.startswith("."):
                continue
            result.append(rel)
        return result

    def chapters(self) -> list[tuple[int, str]]:
        """已存在的章节文件，按章号。"""
        found = []
        for rel in self.all_files():
            head, _, name = rel.rpartition("/")
            match = CHAPTER_PATTERN.match(name)
            if head == CHAPTERS_DIR and match:
                found.append((int(match.group(1)), rel))
        return sorted(found)

    def search(self, pattern: str, rel: str | None = None, regex: bool = False) -> list[SearchHit]:
        if not pattern:
            raise WorkspaceError("搜索内容不能为空。")
        matcher = re.compile(pattern) if regex else re.compile(re.escape(pattern))
        scope = normalize(rel) if rel else ""
        hits: list[SearchHit] = []
        for file in self.all_files():
            if scope and file != scope and not file.startswith(scope + "/"):
                continue
            for number, line in enumerate(self.read_text(file).splitlines(), start=1):
                if matcher.search(line):
                    hits.append(SearchHit(file, number, line.strip()))
        return hits

    # ------------------------------------------------------------ 历史

    def commit(self, message: str, *paths: str) -> str | None:
        return self.git.commit(message, tuple(normalize(p) for p in paths))

    def history(self, rel: str | None = None, limit: int = 20) -> list[HistoryEntry]:
        return self.git.log(normalize(rel) if rel else None, limit)

    def show(self, rel: str, ref: str) -> str | None:
        if not re.fullmatch(r"[0-9a-fA-F]{4,40}", ref):
            raise WorkspaceError("历史编号格式不对。")
        return self.git.show(normalize(rel), ref)

    # ------------------------------------------------------------ 对话与用量记录

    def _private(self, name: str) -> Path:
        directory = self.root / PRIVATE_DIR
        directory.mkdir(exist_ok=True)
        return directory / name

    def append_transcript(self, role: str, text: str, now: datetime | None = None) -> None:
        """对话记录：机器可读的 .story/对话.jsonl，加上给作者看的 对话/日期.md。"""
        now = now or datetime.now(UTC).astimezone()
        record: dict = {"time": now.isoformat(timespec="seconds"), "role": role, "text": text}
        if role == "author":
            head = self.git.head()
            if head:
                record["head"] = head  # 说这句话时文件的状态，撤回时用
        with self._private(TRANSCRIPT_RECORD).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        prefix = {"author": "> ", "assistant": "⏺ ", "system": "· "}.get(role, "")
        daily = self.root / TRANSCRIPT_DIR / f"{now:%Y-%m-%d}.md"
        daily.parent.mkdir(exist_ok=True)
        with daily.open("a", encoding="utf-8") as handle:
            handle.write(f"{now:%H:%M} {prefix}{text.rstrip()}\n\n")

    def recent_transcript(self, limit: int = 40) -> list[tuple[str, str]]:
        return self.all_transcript()[-limit:]

    def all_records(self) -> list[dict]:
        """对话记录的原始条目（含审批等系统条目），按时间顺序。"""
        path = self._private(TRANSCRIPT_RECORD)
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
        return records

    def all_transcript(self) -> list[tuple[str, str]]:
        return [
            (r["role"], r.get("text", ""))
            for r in self.all_records()
            if r.get("role") in ("author", "assistant")
        ]

    def author_messages(self) -> list[dict]:
        """作者说过的话，带在原始记录里的序号与当时的文件状态；撤回列表用。"""
        return [
            {
                "index": i,
                "text": r.get("text", ""),
                "time": r.get("time", ""),
                "head": r.get("head"),
            }
            for i, r in enumerate(self.all_records())
            if r.get("role") == "author"
        ]

    def rewind_transcript(self, index: int) -> int:
        """删掉第 index 条起的全部对话记录；删掉的另存为 .story/撤回-时间.jsonl。返回删掉的条数。"""
        records = self.all_records()
        if not 0 <= index < len(records):
            raise WorkspaceError("没有这条记录。")
        removed = records[index:]
        stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
        self._private(f"{REWIND_PREFIX}{stamp}.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in removed), encoding="utf-8"
        )
        self._private(TRANSCRIPT_RECORD).write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records[:index]),
            encoding="utf-8",
        )
        summary, covered = self.read_summary()
        if covered > index:
            self.write_summary(summary, index)
        first = removed[0].get("text", "")
        self.append_transcript(
            "system", f"撤回了 {len(removed)} 条记录，回到「{first[:30]}」之前。"
        )
        return len(removed)

    def read_summary(self) -> tuple[str, int]:
        """对话摘要与它覆盖到的对话条数。"""
        path = self._private(SUMMARY_RECORD)
        if not path.exists():
            return "", 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return "", 0
        return str(data.get("summary", "")), int(data.get("covered", 0))

    def write_summary(self, summary: str, covered: int) -> None:
        self._private(SUMMARY_RECORD).write_text(
            json.dumps({"summary": summary, "covered": covered}, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )

    def append_log(self, line: str) -> None:
        """开发者日志 .story/日志.txt：模型调用、工具调用与结果摘要、错误。总是写。"""
        stamp = datetime.now(UTC).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        with self._private(LOG_FILE).open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {line.rstrip()}\n")

    def record_usage(self, usage: dict[str, int], model: str, purpose: str) -> None:
        record = {
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "model": model,
            "purpose": purpose,
            **{k: int(v) for k, v in usage.items()},
        }
        with self._private(USAGE_RECORD).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

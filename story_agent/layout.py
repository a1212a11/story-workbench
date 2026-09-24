"""作品目录的布局：文件与目录的名字、正式/自由文件的划分、路径规范化。

这是目录名字的唯一来源。代码里要用 作品.md、章节/ 之类的名字，从这里取；
发给模型的文案里手写的名字，由 tests/agent/test_structure.py 核对是否都在这里定义过。
本模块不 import 包内任何模块。
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

# 作品目录里作者看得见的文件与目录
WORK_FILE = "作品.md"
OUTLINE_FILE = "大纲.md"
STATE_FILE = "故事状态.md"
DECISIONS_FILE = "决定.md"
SETTINGS_DIR = "设定"
CHARACTERS_DIR = "人物"
CHAPTERS_DIR = "章节"
DRAFTS_DIR = "草稿"
SKILLS_DIR = "技能"
TRANSCRIPT_DIR = "对话"
NOTES_FILE = f"{DRAFTS_DIR}/代理笔记.md"  # 代理自己的心得，每轮自动放进系统提示
SAMPLES_DIR = f"{DRAFTS_DIR}/范文"  # 重写时给模型看的范文

# .story/：程序自己的记录，不对作者和代理开放，不进 git
PRIVATE_DIR = ".story"
SKILLS_RECORD = "技能.json"  # 装进去的每份技能的内容哈希，用来分辨作者改没改
SKILLS_BACKUP_DIR = "技能备份"
TRANSCRIPT_RECORD = "对话.jsonl"
SUMMARY_RECORD = "摘要.json"
LOG_FILE = "日志.txt"
USAGE_RECORD = "用量.jsonl"
REWIND_PREFIX = "撤回-"  # 撤回时删掉的记录另存为 撤回-时间.jsonl
# .story/ 里记录的格式版本：改了上面任何一份记录的结构就加一，并在 Workspace.open 里写迁移
FORMAT_RECORD = "格式"
STORY_FORMAT = 1

FORMAL_FILES = (WORK_FILE, OUTLINE_FILE, DECISIONS_FILE)
FORMAL_DIRS = (SETTINGS_DIR, CHARACTERS_DIR, CHAPTERS_DIR)
FREE_FILES = (STATE_FILE,)
FREE_DIRS = (DRAFTS_DIR, SKILLS_DIR)
HIDDEN_DIRS = (".git", PRIVATE_DIR)
CHAPTER_PATTERN = re.compile(r"^第(\d{2,3})章\.md$")

# 给工具描述用的两句现成话，改布局时跟着变
FORMAL_DESCRIPTION = "、".join((*FORMAL_FILES, *(d + "/" for d in FORMAL_DIRS)))
FREE_DESCRIPTION = "、".join((*FREE_FILES, *(d + "/" for d in FREE_DIRS)))

TEMPLATES = {
    OUTLINE_FILE: "# 大纲\n\n（全书走向；每章一段：剧情、结尾状态、状态。还没有内容。）\n",
    STATE_FILE: (
        "# 故事状态\n\n"
        "（到目前为止正文确立的事实、各人物知道什么、读者知道什么、未回收的线索。"
        "由代理在每章定稿后更新。）\n"
    ),
    DECISIONS_FILE: "# 作者决定\n\n（追加日志，带日期；已有条目不改写。）\n",
}


class WorkspaceError(Exception):
    """面向作者的错误说明，中文。"""


def chapter_number(rel: str) -> int | None:
    """路径里「第NN章」的章号，正文与草稿（草稿/第02章-初稿.md）都认；没有就是 None。"""
    match = re.search(r"第\s*(\d+)\s*章", rel)
    return int(match.group(1)) if match else None


def normalize(path: str) -> str:
    """把模型或作者给的路径规范成相对于作品目录的 POSIX 路径；拒绝越界与隐藏目录。"""
    text = str(path).strip().replace("\\", "/")
    if not text or text.startswith("/") or text.startswith("~"):
        raise WorkspaceError("路径必须是作品目录里的相对路径。")
    parts = [p for p in PurePosixPath(text).parts if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise WorkspaceError("路径不能离开作品目录。")
    if parts[0] in HIDDEN_DIRS:
        raise WorkspaceError("这个目录不对作者和代理开放。")
    return "/".join(parts)


def is_formal(rel: str) -> bool:
    """正式文件：作品.md、大纲.md、决定.md 与设定/、人物/、章节/ 下的一切。"""
    rel = normalize(rel)
    if rel in FORMAL_FILES:
        return True
    return rel.split("/", 1)[0] in FORMAL_DIRS


def chapter_file(number: int) -> str:
    return f"{CHAPTERS_DIR}/第{number:02d}章.md"

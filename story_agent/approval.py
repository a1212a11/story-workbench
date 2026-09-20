"""审批卡与作者端口：界面和工具之间的最小合同。

正式文件的每一处改动都以一张审批卡的形式给作者看，作者批准、不批准或打字说意见。
`AuthorPort` 是界面要实现的全部：输出整行、输出流式片段、审批、打开编辑器。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

APPROVE_WORDS = frozenset({"", "y", "yes", "好", "好的", "可以", "批准", "行", "同意", "ok"})
REJECT_WORDS = frozenset({"n", "no", "不", "不行", "不批准", "不要", "拒绝", "否"})


@dataclass(frozen=True, slots=True)
class ApprovalCard:
    title: str  # 修改 章节/第02章.md
    diff: str  # 统一 diff；界面着色显示
    summary: str  # 共 2 处 · 新增 1 个文件
    kind: str  # edit / restore


@dataclass(frozen=True, slots=True)
class Approval:
    approved: bool
    note: str = ""  # 作者打字的意见；批准时为空


def parse_answer(text: str) -> Approval:
    """Enter、y、「好」是批准；n、「不」是不批准；其余文字是不批准并带意见。"""
    stripped = text.strip()
    lowered = stripped.lower().rstrip("。！!")
    if lowered in APPROVE_WORDS:
        return Approval(True)
    if lowered in REJECT_WORDS:
        return Approval(False)
    return Approval(False, stripped)


class AuthorPort(Protocol):
    def say(self, text: str) -> None:
        """输出整行文字。"""

    def emit(self, chunk: str) -> None:
        """输出流式片段，不换行。"""

    def approve(self, card: ApprovalCard) -> Approval:
        """显示审批卡并等待作者作答。"""

    def edit(self, initial: str, title: str) -> str | None:
        """打开全屏编辑器让作者自己改；保存返回新文本，取消返回 None。"""

"""独立重写：用一个不带对话历史的新上下文，从主角脑子里把一章重写一遍。

只给它作品.md、大纲里本章那段、人物文件、范文和初稿；故事状态与设定不给，重写不该动事实。
大纲的剧情、决定与结尾状态是硬约束。返回全文，不写文件，落正文仍由 edit 走审批。
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING

from story_agent import prompts
from story_agent.config import estimate_tokens
from story_agent.layout import (
    CHARACTERS_DIR,
    OUTLINE_FILE,
    SAMPLES_DIR,
    WORK_FILE,
    WorkspaceError,
)
from story_agent.provider import ChatMessage, ProviderError

if TYPE_CHECKING:
    from story_agent.tools import ToolContext

REWRITE_SYSTEM = prompts.load("重写")


def outline_section(outline: str, chapter: int) -> str:
    """大纲里「## 第N章」那一段（到下一个二级标题为止）；没有就返回空串。"""
    numerals = "一二三四五六七八九十"
    forms = {str(chapter), f"{chapter:02d}"}
    if 1 <= chapter <= 10:
        forms.add(numerals[chapter - 1])
    elif 10 < chapter < 20:
        forms.add("十" + numerals[chapter - 11])
    pattern = re.compile(
        r"^##\s*第\s*(" + "|".join(re.escape(f) for f in forms) + r")\s*章.*$", re.M
    )
    match = pattern.search(outline)
    if match is None:
        return ""
    rest = outline[match.end() :]
    nxt = re.search(r"^##\s", rest, re.M)
    return (match.group(0) + (rest[: nxt.start()] if nxt else rest)).strip()


def chapter_number(rel: str) -> int | None:
    match = re.search(r"第\s*(\d+)\s*章", rel)
    return int(match.group(1)) if match else None


def _materials(ctx: ToolContext, chapter: int | None, draft: str) -> str:
    """作品.md、本章大纲、人物文件、一篇范文；超出预算时从后往前丢，初稿永远保留。"""
    ws = ctx.workspace
    assert ctx.config is not None
    budget = ctx.config.budget - estimate_tokens(REWRITE_SYSTEM) - estimate_tokens(draft) - 800
    blocks: list[tuple[str, str]] = []
    if ws.file_exists(WORK_FILE):
        blocks.append((WORK_FILE, ws.read_text(WORK_FILE)))
    if chapter is not None and ws.file_exists(OUTLINE_FILE):
        section = outline_section(ws.read_text(OUTLINE_FILE), chapter)
        if section:
            blocks.append((f"{OUTLINE_FILE} 里本章那段", section))
    blocks.extend(
        (rel, ws.read_text(rel)) for rel in ws.all_files() if rel.startswith(CHARACTERS_DIR + "/")
    )
    samples = [rel for rel in ws.all_files() if rel.startswith(SAMPLES_DIR + "/")]
    if samples:
        blocks.append((f"范文 {samples[0]}（学法不学句）", ws.read_text(samples[0])))
    parts: list[str] = []
    used = 0
    for title, text in blocks:
        text = text.strip()
        if not text:
            continue
        block = f"### {title}\n{text}\n"
        cost = estimate_tokens(block)
        if used + cost > budget:
            parts.append(f"### {title}\n（篇幅所限，这份材料没有给重写）\n")
            continue
        used += cost
        parts.append(block)
    return "\n".join(parts)


def run_rewrite(ctx: ToolContext, draft_rel: str, chapter: int | None, focus: str) -> str:
    ws = ctx.workspace
    assert ctx.clients is not None and ctx.config is not None
    draft = ws.read_text(draft_rel)
    if not draft.strip():
        raise WorkspaceError(f"{draft_rel} 是空的，没有可重写的正文。")
    if chapter is None:
        chapter = chapter_number(draft_rel)
    user = "\n".join(
        (
            f"## 这次着重\n{focus}\n" if focus else "",
            "## 材料",
            _materials(ctx, chapter, draft),
            f"## 初稿：{draft_rel}",
            draft,
        )
    )
    messages = [ChatMessage("system", REWRITE_SYSTEM), ChatMessage("user", user)]
    # 重写要产出整章，是一次性的长调用，给双倍时间；用写作模型
    writing = ctx.config.writing
    options = replace(writing, timeout_seconds=writing.timeout_seconds * 2)
    try:
        result = ctx.clients.complete(messages, options, ctx.cancel)
    except ProviderError as exc:
        raise WorkspaceError(f"重写请求失败（{exc}）。") from None
    ws.record_usage(result.usage, options.model, f"重写 {draft_rel}")
    if result.truncated:
        raise WorkspaceError("重写输出被截断，结果不可用。")
    text = result.text.strip()
    if not text:
        raise WorkspaceError("重写没有返回正文。")
    return f"重写 {draft_rel}" + (f"（着重：{focus}）" if focus else "") + "：\n" + text

"""独立审读与盲读：用不带对话历史的新上下文读一章，每条引用都经核对，核不上的丢弃。

审读是编辑读：给全部材料，清单来自 技能/审读.md，作者可改。这是旧包"检查"里唯一保留的思路。
盲读是读者读：只给本章和上一章正文，问走神、复述、出戏和看不懂的地方，不打分。
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from story_agent import prompts
from story_agent.layout import (
    CHAPTERS_DIR,
    CHARACTERS_DIR,
    OUTLINE_FILE,
    SETTINGS_DIR,
    SKILLS_DIR,
    STATE_FILE,
    WORK_FILE,
    WorkspaceError,
    chapter_number,
)
from story_agent.provider import ChatMessage, ProviderError, estimate_tokens

if TYPE_CHECKING:
    from story_agent.tools import ToolContext

REVIEW_SYSTEM = prompts.load("审读")
BLIND_SYSTEM = prompts.load("盲读")

_MATERIAL_ORDER = (WORK_FILE, OUTLINE_FILE, STATE_FILE)


def _materials(ctx: ToolContext, chapter_rel: str, chapter: str) -> str:
    """作品、大纲、故事状态、人物与设定；超出预算时从后往前丢，正文永远保留。"""
    ws = ctx.workspace
    assert ctx.config is not None
    budget = ctx.config.budget - estimate_tokens(REVIEW_SYSTEM) - estimate_tokens(chapter) - 800
    files = [f for f in _MATERIAL_ORDER if ws.file_exists(f)]
    for directory in (CHARACTERS_DIR, SETTINGS_DIR):
        files.extend(f for f in ws.all_files() if f.startswith(directory + "/"))
    parts: list[str] = []
    used = 0
    for rel in files:
        text = ws.read_text(rel).strip()
        if not text:
            continue
        block = f"### {rel}\n{text}\n"
        cost = estimate_tokens(block)
        if used + cost > budget:
            parts.append(f"### {rel}\n（篇幅所限，这份材料没有给审读）\n")
            continue
        used += cost
        parts.append(block)
    return "\n".join(parts)


def _checklist(ctx: ToolContext) -> str:
    rel = f"{SKILLS_DIR}/审读.md"
    return ctx.workspace.read_text(rel).strip() if ctx.workspace.file_exists(rel) else ""


def _json_object(text: str, label: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start : end + 1]) if 0 <= start < end else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise WorkspaceError(f"{label}结果不是可解析的 JSON。")
    return data


def _parse(text: str) -> tuple[list[dict], str]:
    data = _json_object(text, "审读")
    remarks = data.get("remarks")
    if not isinstance(remarks, list):
        raise WorkspaceError("审读结果缺少 remarks。")
    cleaned = [r for r in remarks if isinstance(r, dict) and isinstance(r.get("quote"), str)]
    summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
    return cleaned, summary


def run_review(ctx: ToolContext, chapter_rel: str, focus: str) -> str:
    from story_agent.tools import verify_quotes

    ws = ctx.workspace
    assert ctx.clients is not None and ctx.config is not None
    chapter = ws.read_text(chapter_rel)
    if not chapter.strip():
        raise WorkspaceError(f"{chapter_rel} 是空的，没有可审读的正文。")
    checklist = _checklist(ctx)
    user = "\n".join(
        (
            "## 审读清单",
            checklist or "（作品目录里没有 技能/审读.md，按通用要求审。）",
            "",
            f"## 这次重点\n{focus}" if focus else "",
            "## 材料",
            _materials(ctx, chapter_rel, chapter),
            f"## 待审正文：{chapter_rel}",
            chapter,
        )
    )
    messages = [ChatMessage("system", REVIEW_SYSTEM), ChatMessage("user", user)]
    # 审读要读完整章再给结论，是一次性的长调用，给双倍时间
    writing = ctx.config.writing
    options = replace(writing, timeout_seconds=writing.timeout_seconds * 2)
    try:
        result = ctx.clients.complete(messages, options, ctx.cancel)
    except ProviderError as exc:
        raise WorkspaceError(f"审读请求失败（{exc}）。") from None
    ws.record_usage(result.usage, options.model, f"审读 {chapter_rel}")
    if result.truncated:
        raise WorkspaceError("审读输出被截断，结果不可用。")
    remarks, summary = _parse(result.text)
    verified = verify_quotes(chapter, [r["quote"] for r in remarks])
    lines = [f"审读 {chapter_rel}" + (f"（重点：{focus}）" if focus else "") + "："]
    dropped = 0
    kept = 0
    for remark, (_, line_no, _) in zip(remarks, verified, strict=True):
        if line_no is None:
            dropped += 1
            continue
        kept += 1
        kind = str(remark.get("kind", "提醒")).strip() or "提醒"
        lines.append(f"{kept}. {kind} · 第 {line_no} 行「{remark['quote']}」")
        if remark.get("explanation"):
            lines.append(f"   {remark['explanation']}")
        if remark.get("suggestion"):
            lines.append(f"   建议：{remark['suggestion']}")
    if kept == 0:
        lines.append("没有需要处理的提醒。")
    if dropped:
        lines.append(f"（另有 {dropped} 条提醒引用核不上正文，已丢弃。）")
    if summary:
        lines.append(f"总体：{summary}")
    return "\n".join(lines)


def _previous_chapter(ctx: ToolContext, chapter_rel: str) -> str | None:
    number = chapter_number(chapter_rel)
    if number is None or number <= 1:
        return None
    for rel in ctx.workspace.all_files():
        if rel.startswith(CHAPTERS_DIR + "/") and chapter_number(rel) == number - 1:
            return rel
    return None


def _items(value: object, keys: tuple[str, str]) -> list[tuple[str, str]]:
    """[{quote, why}] → [(quote, why)]；不是这个样子的项丢掉。"""
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    return [
        (item[keys[0]], str(item.get(keys[1]) or ""))
        for item in value
        if isinstance(item, dict) and isinstance(item.get(keys[0]), str) and item[keys[0]]
    ]


def run_blind_read(ctx: ToolContext, chapter_rel: str) -> str:
    from story_agent.tools import verify_quotes

    ws = ctx.workspace
    assert ctx.clients is not None and ctx.config is not None
    chapter = ws.read_text(chapter_rel)
    if not chapter.strip():
        raise WorkspaceError(f"{chapter_rel} 是空的，没有可读的正文。")
    parts = []
    previous = _previous_chapter(ctx, chapter_rel)
    if previous is not None:
        prev_text = ws.read_text(previous).strip()
        budget = ctx.config.budget - estimate_tokens(BLIND_SYSTEM) - estimate_tokens(chapter) - 800
        if prev_text and estimate_tokens(prev_text) <= budget:
            parts += [f"## 上一章：{previous}", prev_text, ""]
    parts += [f"## 这一章：{chapter_rel}", chapter]
    messages = [ChatMessage("system", BLIND_SYSTEM), ChatMessage("user", "\n".join(parts))]
    # 盲读也要读完整章再说感受，同审读给双倍时间
    writing = ctx.config.writing
    options = replace(writing, timeout_seconds=writing.timeout_seconds * 2)
    try:
        result = ctx.clients.complete(messages, options, ctx.cancel)
    except ProviderError as exc:
        raise WorkspaceError(f"盲读请求失败（{exc}）。") from None
    ws.record_usage(result.usage, options.model, f"盲读 {chapter_rel}")
    if result.truncated:
        raise WorkspaceError("盲读输出被截断，结果不可用。")
    data = _json_object(result.text, "盲读")

    dropped = 0

    def located(pairs: list[tuple[str, str]]) -> list[tuple[int, str, str]]:
        nonlocal dropped
        found = []
        for (quote, note), (_, line_no, _) in zip(
            pairs, verify_quotes(chapter, [q for q, _ in pairs]), strict=True
        ):
            if line_no is None:
                dropped += 1
            else:
                found.append((line_no, quote, note))
        return found

    lines = [f"盲读 {chapter_rel}（读者只看了" + ("上一章和" if previous else "") + "这一章）："]
    drift = located(_items(data.get("drift"), ("quote", "why")))
    if drift:
        line_no, quote, why = drift[0]
        lines.append(f"走神：第 {line_no} 行「{quote}」" + (f"——{why}" if why else ""))
    else:
        lines.append("走神：没有。")
    retell = data.get("retell")
    lines.append(
        f"读者复述：{retell.strip() if isinstance(retell, str) and retell.strip() else '复述不出'}"
    )
    for title, key, note_key in (
        ("像作者在说话", "author_voice", "why"),
        ("看不懂", "confused", "question"),
    ):
        found = located(_items(data.get(key), ("quote", note_key)))
        if found:
            lines.append(f"{title}：")
            lines.extend(
                f"- 第 {n} 行「{q}」" + (f"——{note}" if note else "") for n, q, note in found
            )
    wanted = data.get("next")
    if isinstance(wanted, str) and wanted.strip():
        lines.append(f"读完：{wanted.strip()}")
    if dropped:
        lines.append(f"（另有 {dropped} 处引用核不上正文，已丢弃。）")
    return "\n".join(lines)

"""代理的工具：read、search、edit、history、verify、review、rewrite、open_editor、skill、ask。

只有 `_apply_edit` 会写文件，`edit`、`history restore`、`open_editor` 都走它。
正式文件在写之前弹审批卡；作者不批准就一个字不变。作者在界面上看到的内容与回给模型的
文字一致。
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Any

from story_agent.approval import Approval, ApprovalCard, AuthorPort
from story_agent.config import AgentConfig, Clients
from story_agent.layout import (
    FORMAL_DESCRIPTION,
    FREE_DESCRIPTION,
    SKILLS_DIR,
    TRANSCRIPT_DIR,
    WORK_FILE,
    WorkspaceError,
    is_formal,
    normalize,
)
from story_agent.provider import CancelSignal, ToolSpec
from story_agent.workspace import Workspace, describe_time

PAID_TOOLS = frozenset({"review", "rewrite"})
SKILLS = {
    "开书": "目录还空着，从一句想法起步",
    "共创": "和作者一起设计世界观、人物、体系、大纲",
    "人物": "建人物文件：想要什么、怕什么、心里和嘴上怎么说话",
    "剧情": "设计一卷的走向或前几章的方案",
    "定章": "定一章的剧情与结尾状态",
    "写章": "写一章正文",
    "审读": "审一章正文的清单",
    "故事状态": "一章定稿后更新故事状态",
    "回改": "改了前面的章节之后核对后文",
    "修订": "按作者的反馈改稿",
}
MAX_DIFF_LINES = 400


def _spec(name: str, description: str, properties: dict, required=()) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": list(required),
            "additionalProperties": False,
        },
    )


_PATH = {"type": "string", "description": "作品目录里的相对路径，例如 章节/第02章.md"}

TOOL_SPECS: tuple[ToolSpec, ...] = (
    _spec(
        "read",
        "读一个文件的全文，或列出一个目录。正文会完整显示给作者。省略 path 列出作品目录。",
        {"path": _PATH},
    ),
    _spec(
        "search",
        "在作品目录里全文搜索，返回 文件:行号: 内容。默认按原文匹配，regex 为 true 时按正则。",
        {
            "pattern": {"type": "string", "description": "要找的文字"},
            "path": {**_PATH, "description": "只在这个文件或目录里找；省略为全部"},
            "regex": {"type": "boolean"},
        },
        ["pattern"],
    ),
    _spec(
        "edit",
        "唯一的写文件工具。给 changes（若干处 old→new 定点替换，old 必须在文件里恰好出现一次）"
        "或 content（整个文件的新内容，用于新建或重写）。"
        f"正式文件（{FORMAL_DESCRIPTION}）会先弹审批卡给作者看 diff，"
        "作者不批准则文件不变，你会拿到作者的意见。"
        f"自由文件（{FREE_DESCRIPTION}）直接保存。"
        "delete 为 true 时删除这个文件（正式文件同样先审批）：作废的旧章、旧草稿直接删，"
        "不要归档到别处，历史里都在，history restore 能找回。"
        "reason 用一句人话说明这次改动，会写进历史。"
        "要在末尾追加内容，把最后一段原文作为 old、原文加新内容作为 new；old 不能为空。",
        {
            "path": _PATH,
            "reason": {
                "type": "string",
                "description": "这次改动的一句话说明，例如「改了守夜人的段落」",
            },
            "changes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old": {"type": "string", "description": "要替换的原文，须唯一"},
                        "new": {"type": "string", "description": "替换后的文字"},
                    },
                    "required": ["old", "new"],
                    "additionalProperties": False,
                },
            },
            "content": {"type": "string", "description": "整个文件的新内容"},
            "delete": {"type": "boolean", "description": "true 时删除这个文件"},
        },
        ["path", "reason"],
    ),
    _spec(
        "history",
        "改动历史。action=list 列出最近的改动（可限定 path）；action=show 看某个文件在某次改动时的"
        "内容（需要 path 与 ref）；action=restore 把文件恢复到某次改动时的内容（需要 path 与 ref，"
        "正式文件会弹审批卡）。ref 是 list 给出的编号。",
        {
            "action": {"type": "string", "enum": ["list", "show", "restore"]},
            "path": _PATH,
            "ref": {"type": "string", "description": "改动编号，来自 list"},
        },
        ["action"],
    ),
    _spec(
        "verify",
        "核对若干引用是否逐字出现在文件里。返回每条命中的行号或「没有找到」。"
        "引用正文说话前先用它核对，别凭记忆引。",
        {
            "path": _PATH,
            "quotes": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        ["path", "quotes"],
    ),
    _spec(
        "review",
        "独立审读一章（付费，会调用一次模型）。用不带对话历史的新上下文，只给这一章、作品.md、大纲、"
        "故事状态、人物与设定和 技能/审读.md 里的清单，返回带原文引用的提醒；引用核不上的会被丢弃。"
        "focus 可指定这次重点看什么。一轮里失败一次后不要再调。",
        {
            "path": _PATH,
            "focus": {"type": "string", "description": "这次重点看什么，例如「人物认知是否越界」"},
        },
        ["path"],
    ),
    _spec(
        "rewrite",
        "独立重写一章（付费，会调用一次模型，用写作模型）。用不带对话历史的新上下文，只给初稿、作品.md、"
        "大纲里本章那段、人物文件和一篇范文，从主角脑子里把这章重写一遍；大纲的剧情与结尾是它的硬约束。"
        "返回重写后的全文，不写文件：你读一遍，把初稿里更好的句子捡回来、过火的收一收，"
        "再用 edit 落正文。"
        "chapter 缺省从路径里的「第NN章」推。一轮只能调一次。",
        {
            "path": _PATH,
            "chapter": {"type": "integer", "description": "大纲里的章号，用来取本章剧情与结尾"},
            "focus": {"type": "string", "description": "这次着重什么，例如「让他嘴硬一点」"},
        },
        ["path"],
    ),
    _spec(
        "ask",
        "问作者一个需要他拍板的问题，带 2 到 5 个选项。作者可以按上下键选一个，"
        "也可以直接打字说别的。只在真的有岔路时用；方向已经明确就直接做，别用它凑对话。",
        {
            "question": {"type": "string", "description": "一句话的问题"},
            "options": {
                "type": "array",
                "items": {"type": "string", "description": "一个选项，一句话说清它的味道"},
                "minItems": 2,
                "maxItems": 5,
            },
        },
        ["question", "options"],
    ),
    _spec(
        "skill",
        "查方法：读一份写作方法，照着做。"
        + "；".join(f"{k}（{v}）" for k, v in SKILLS.items())
        + "。做这几类事之前先查一次。方法文件在作品目录的 技能/ 里，作者可能改过，以文件为准。",
        {"name": {"type": "string", "description": "方法名，例如 写章"}},
        ["name"],
    ),
    _spec(
        "open_editor",
        "打开全屏编辑器让作者自己改这个文件。只在作者说要亲自改时用。保存即视为作者批准。",
        {"path": _PATH},
        ["path"],
    ),
)

TOOL_NAMES = frozenset(spec.name for spec in TOOL_SPECS)

LABELS = {
    "read": "读取",
    "search": "搜索",
    "edit": "修改",
    "history": "历史",
    "verify": "核对引用",
    "review": "审读",
    "rewrite": "重写",
    "open_editor": "打开编辑器",
    "skill": "查方法",
    "ask": "请作者定",
}


def describe_call(name: str, args: dict[str, Any] | None) -> str:
    """工具调用的中文标签，形如「修改(章节/第02章.md · 改了守夜人的段落)」；供界面显示。"""
    args = args or {}
    parts: list[str] = []
    path = args.get("path")
    if isinstance(path, str) and path.strip():
        parts.append(path.strip())
    action = args.get("action")
    if isinstance(action, str):
        parts.append({"list": "列出", "show": "查看", "restore": "恢复"}.get(action, action))
    if args.get("delete") is True:
        parts.append("删除")
    for key in ("reason", "pattern", "focus", "question", "name"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            short = value.strip().splitlines()[0]
            parts.append(short if len(short) <= 24 else short[:24] + "…")
            break
    label = LABELS.get(name, name)
    return f"{label}({' · '.join(parts)})" if parts else f"{label}()"


# ---------------------------------------------------------------- 执行上下文


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    text: str
    is_error: bool = False
    paid_failure: bool = False


@dataclass
class Session:
    """一轮作者发言内的状态。"""

    paid_failed: bool = False
    executed: list[str] = field(default_factory=list)
    paid_done: list[str] = field(default_factory=list)  # 本轮已经成功调过的付费工具


@dataclass
class ToolContext:
    workspace: Workspace
    port: AuthorPort
    clients: Clients | None
    config: AgentConfig | None
    session: Session
    cancel: CancelSignal = field(default_factory=CancelSignal)


def _text(args: dict[str, Any], key: str, required: bool = False) -> str | None:
    value = args.get(key)
    if value is None:
        if required:
            raise WorkspaceError(f"需要给出 {key}。")
        return None
    if not isinstance(value, str):
        raise WorkspaceError(f"参数 {key} 必须是文字。")
    return value


# ---------------------------------------------------------------- 写入口


def _diff(rel: str, old: str | None, new: str | None) -> str:
    before = old.splitlines(keepends=True) if old is not None else []
    after = new.splitlines(keepends=True) if new is not None else []
    lines = list(difflib.unified_diff(before, after, fromfile=rel, tofile=rel, n=2, lineterm=""))
    text = "".join(line if line.endswith("\n") else line + "\n" for line in lines)
    if text.count("\n") > MAX_DIFF_LINES:
        head = text.splitlines()[:MAX_DIFF_LINES]
        text = "\n".join(head) + f"\n…（diff 太长，只显示前 {MAX_DIFF_LINES} 行）\n"
    return text


def _apply_edit(
    ctx: ToolContext,
    rel: str,
    new: str | None,
    *,
    reason: str,
    summary: str,
    kind: str,
    title: str,
) -> ToolOutcome:
    """唯一的写路径：正式文件先审批，批准后原子写入并提交历史。new 为 None 表示删除。"""
    ws = ctx.workspace
    old = ws.read_text(rel) if ws.file_exists(rel) else None
    if old == new:
        return ToolOutcome(f"{rel} 没有变化。")
    diff = _diff(rel, old, new)
    if old is None and new is None:
        raise WorkspaceError(f"没有这个文件：{rel}")
    if old is None:
        # 新文件：全文显示在对话里，卡片只提示，作者不必在卡片里读长文
        ctx.port.say(f"── {rel}（新建）──")
        ctx.port.say(new.rstrip("\n"))
        diff = f"全文 {len(new)} 字已显示在上方对话里，PageUp 上翻阅读。"
    if is_formal(rel):
        card = ApprovalCard(title=title, diff=diff, summary=summary, kind=kind)
        approval: Approval = ctx.port.approve(card)
        ws.append_transcript(
            "system",
            f"审批：{title}（{summary}）→ "
            + (
                "批准"
                if approval.approved
                else f"不批准{'：' + approval.note if approval.note else ''}"
            ),
        )
        if not approval.approved:
            note = f"作者说：{approval.note}" if approval.note else "作者没有说明原因。"
            return ToolOutcome(f"作者没有批准这次改动，{rel} 没有变化。{note}")
    elif old is not None:
        ctx.port.say(diff.rstrip("\n"))
    if new is None:
        ws.delete_text(rel)
    else:
        ws.write_text(rel, new)
    ref = ws.commit(reason or title, rel)
    line = f"已{'删除' if new is None else '保存'} {rel}（{summary}）。"
    if ref:
        line += f" 历史编号 {ref}。"
    ctx.port.say(line)
    return ToolOutcome(line)


def _closest_line(text: str, wanted: str) -> str | None:
    """找出与 wanted 最相似的一行（按首行比），帮模型看出是哪几个字对不上。"""
    first = wanted.strip().splitlines()[0] if wanted.strip() else ""
    if not first:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matches = difflib.get_close_matches(first, lines, n=1, cutoff=0.5)
    if not matches:
        return None
    line = matches[0]
    return line if len(line) <= 80 else line[:80] + "…"


def _apply_changes(text: str, changes: list[Any]) -> tuple[str, int]:
    if not changes:
        raise WorkspaceError("changes 不能为空。")
    for index, change in enumerate(changes, start=1):
        if not isinstance(change, dict) or not isinstance(change.get("old"), str):
            raise WorkspaceError(f"第 {index} 处改动格式不对，需要 old 与 new。")
        old, new = change["old"], change.get("new", "")
        if not isinstance(new, str):
            raise WorkspaceError(f"第 {index} 处改动的 new 必须是文字。")
        if not old:
            raise WorkspaceError(f"第 {index} 处改动的 old 不能为空。")
        count = text.count(old)
        if count == 0:
            hint = _closest_line(text, old)
            raise WorkspaceError(
                f"第 {index} 处改动的原文在文件里没有找到：「{old[:40]}」"
                + (f" 最接近的一行是：「{hint}」" if hint else "")
            )
        if count > 1:
            raise WorkspaceError(
                f"第 {index} 处改动的原文在文件里出现了 {count} 次，请给更长的原文。"
            )
        text = text.replace(old, new, 1)
    return text, len(changes)


# ---------------------------------------------------------------- 各工具


def read(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    ws = ctx.workspace
    rel = _text(args, "path") or ""
    if not rel or ws.path(rel).is_dir():
        names = ws.list_dir(rel)
        header = f"{normalize(rel) + '/' if rel else '作品目录'}："
        text = header + ("\n" + "\n".join("  " + n for n in names) if names else "（空）")
        ctx.port.say(text)
        return ToolOutcome(text)
    rel = normalize(rel)
    body = ws.read_text(rel)
    ctx.port.say(f"── {rel} ──")
    ctx.port.say(body.rstrip("\n") if body.strip() else "（空文件）")
    return ToolOutcome(f"── {rel} ──\n{body}")


def search(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    pattern = _text(args, "pattern", required=True) or ""
    hits = ctx.workspace.search(pattern, _text(args, "path"), bool(args.get("regex")))
    if not hits:
        text = f"没有找到「{pattern}」。"
    else:
        shown = hits[:60]
        text = "\n".join(f"{h.path}:{h.line}: {h.text}" for h in shown)
        if len(hits) > len(shown):
            text += f"\n…共 {len(hits)} 处，只显示前 {len(shown)} 处。"
    ctx.port.say(text)
    return ToolOutcome(text)


def edit(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    ws = ctx.workspace
    rel = normalize(_text(args, "path", required=True) or "")
    if rel.startswith(TRANSCRIPT_DIR + "/"):
        raise WorkspaceError("对话记录由程序维护，不能改。")
    reason = (_text(args, "reason") or "").strip()
    content = args.get("content")
    changes = args.get("changes")
    exists = ws.file_exists(rel)
    if args.get("delete") is True:
        if rel == WORK_FILE:
            raise WorkspaceError("作品.md 是作品的门面，不能删。")
        if not exists:
            raise WorkspaceError(f"没有这个文件：{rel}")
        lines = ws.read_text(rel).count("\n")
        title = f"删除 {rel}" + (f" · {reason}" if reason else "")
        summary = f"删除，共 {lines} 行"
        return _apply_edit(
            ctx, rel, None, reason=reason, summary=summary, kind="delete", title=title
        )
    if content is not None:
        if not isinstance(content, str):
            raise WorkspaceError("content 必须是文字。")
        new = content
        summary = "重写全文" if exists else "新建文件"
    elif changes is not None:
        if not exists:
            raise WorkspaceError(f"{rel} 还不存在；新建文件请给 content。")
        if not isinstance(changes, list):
            raise WorkspaceError("changes 必须是列表。")
        new, count = _apply_changes(ws.read_text(rel), changes)
        summary = f"共 {count} 处"
    else:
        raise WorkspaceError("需要给 changes 或 content。")
    title = f"{'修改' if exists else '新建'} {rel}" + (f" · {reason}" if reason else "")
    return _apply_edit(ctx, rel, new, reason=reason, summary=summary, kind="edit", title=title)


def history(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    ws = ctx.workspace
    action = _text(args, "action", required=True)
    rel = _text(args, "path")
    if not ws.git.available:
        text = "这台机器上没有找到 git，历史功能不可用；其他一切照常。"
        ctx.port.say(text)
        return ToolOutcome(text)
    if action == "list":
        entries = ws.history(rel, limit=15)
        if not entries:
            text = "还没有改动记录。"
        else:
            lines = []
            for entry in entries:
                files = "、".join(entry.files) if entry.files else ""
                lines.append(
                    f"{entry.ref} · {describe_time(entry.when)} · {entry.message}"
                    + (f" · {files}" if files and not rel else "")
                )
            text = "\n".join(lines)
        ctx.port.say(text)
        return ToolOutcome(text)
    if rel is None:
        raise WorkspaceError("需要给出 path。")
    rel = normalize(rel)
    ref = (_text(args, "ref") or "").strip()
    if not ref:
        raise WorkspaceError("需要给出 ref（改动编号）。")
    old = ws.show(rel, ref)
    if old is None:
        raise WorkspaceError(f"{ref} 里没有 {rel}。")
    if action == "show":
        ctx.port.say(f"── {rel} @ {ref} ──")
        ctx.port.say(old.rstrip("\n"))
        return ToolOutcome(f"── {rel} @ {ref} ──\n{old}")
    if action == "restore":
        return _apply_edit(
            ctx,
            rel,
            old,
            reason=f"恢复 {rel} 到 {ref}",
            summary=f"恢复到 {ref}",
            kind="restore",
            title=f"恢复 {rel} 到 {ref} 时的内容",
        )
    raise WorkspaceError("action 只能是 list、show 或 restore。")


def verify_quotes(text: str, quotes: list[str]) -> list[tuple[str, int | None, int]]:
    """每条引用 → (引用, 首次出现的行号或 None, 出现次数)。"""
    lines = text.splitlines()
    result = []
    for quote in quotes:
        count = text.count(quote) if quote else 0
        line_no = None
        if count:
            for number, line in enumerate(lines, start=1):
                if quote in line:
                    line_no = number
                    break
            if line_no is None:  # 引用跨行
                offset = text.find(quote)
                line_no = text.count("\n", 0, offset) + 1
        result.append((quote, line_no, count))
    return result


def verify(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    rel = normalize(_text(args, "path", required=True) or "")
    quotes = args.get("quotes")
    if not isinstance(quotes, list) or not quotes or not all(isinstance(q, str) for q in quotes):
        raise WorkspaceError("quotes 必须是非空的文字列表。")
    body = ctx.workspace.read_text(rel)
    lines = []
    missing = 0
    for quote, line_no, count in verify_quotes(body, quotes):
        short = quote if len(quote) <= 60 else quote[:60] + "…"
        if line_no is None:
            missing += 1
            lines.append(f"✗ 没有找到：「{short}」")
        else:
            extra = f"（出现 {count} 次）" if count > 1 else ""
            lines.append(f"✓ 第 {line_no} 行{extra}：「{short}」")
    text = "\n".join(lines) + (f"\n{missing} 条引用核不上。" if missing else "\n全部核对无误。")
    ctx.port.say(text)
    return ToolOutcome(text)


def review(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    from story_agent.review import run_review

    rel = normalize(_text(args, "path", required=True) or "")
    focus = (_text(args, "focus") or "").strip()
    text = run_review(ctx, rel, focus)
    ctx.port.say(text)
    return ToolOutcome(text)


def rewrite(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    from story_agent.rewrite import run_rewrite

    rel = normalize(_text(args, "path", required=True) or "")
    chapter = args.get("chapter")
    if chapter is not None and (type(chapter) is not int or chapter < 1):
        raise WorkspaceError("chapter 须是正整数。")
    focus = (_text(args, "focus") or "").strip()
    text = run_rewrite(ctx, rel, chapter, focus)
    ctx.port.say(text)
    return ToolOutcome(text)


def ask(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    question = (_text(args, "question", required=True) or "").strip()
    raw = args.get("options")
    options = (
        [o.strip() for o in raw if isinstance(o, str) and o.strip()]
        if isinstance(raw, list)
        else []
    )
    if not question:
        raise WorkspaceError("问题不能为空。")
    if not 2 <= len(options) <= 5:
        raise WorkspaceError("要给 2 到 5 个选项。")
    answer = ctx.port.choose(question, tuple(options)).strip()
    if not answer:
        return ToolOutcome("作者没有选，等他自己说。别再追问同一件事。")
    ctx.workspace.append_transcript("system", f"选择：{question} → {answer}")
    return ToolOutcome(f"作者答：{answer}")


def skill(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    name = (_text(args, "name", required=True) or "").strip().removesuffix(".md")
    rel = f"{SKILLS_DIR}/{name}.md"
    if name not in SKILLS or not ctx.workspace.file_exists(rel):
        raise WorkspaceError("没有这份方法。可用的是：" + "、".join(SKILLS))
    text = ctx.workspace.read_text(rel)
    ctx.port.say(f"── {rel} ──")
    ctx.port.say(text.rstrip("\n"))
    return ToolOutcome(f"── {rel} ──\n{text}")


def open_editor(ctx: ToolContext, args: dict[str, Any]) -> ToolOutcome:
    ws = ctx.workspace
    rel = normalize(_text(args, "path", required=True) or "")
    if rel.startswith(TRANSCRIPT_DIR + "/"):
        raise WorkspaceError("对话记录由程序维护，不能改。")
    initial = ws.read_text(rel) if ws.file_exists(rel) else ""
    edited = ctx.port.edit(initial, rel)
    if edited is None:
        text = f"作者取消了编辑，{rel} 没有变化。"
        ctx.port.say(text)
        return ToolOutcome(text)
    if edited == initial:
        return ToolOutcome(f"{rel} 没有变化。")
    ws.write_text(rel, edited)
    ref = ws.commit(f"作者亲自修改 {rel}", rel)
    text = f"作者已保存 {rel}。" + (f" 历史编号 {ref}。" if ref else "")
    ctx.port.say(text)
    return ToolOutcome(text)


HANDLERS = {
    "read": read,
    "search": search,
    "edit": edit,
    "history": history,
    "verify": verify,
    "review": review,
    "rewrite": rewrite,
    "open_editor": open_editor,
    "skill": skill,
    "ask": ask,
}
assert set(HANDLERS) == set(TOOL_NAMES)


def execute(ctx: ToolContext, name: str, args: dict[str, Any] | None) -> ToolOutcome:
    """执行一个工具调用，返回给模型的文字结果。"""
    handler = HANDLERS.get(name)
    if handler is None:
        return ToolOutcome(f"没有名为 {name} 的工具。", is_error=True)
    if name in PAID_TOOLS:
        if ctx.session.paid_failed:
            return ToolOutcome(
                "本轮已有一次模型请求失败，不再自动发起新的付费请求；把情况告诉作者，由作者决定是否重试。",
                is_error=True,
            )
        if ctx.clients is None or ctx.config is None:
            return ToolOutcome("模型尚未接入（没有密钥），不能发起付费调用。", is_error=True)
        if name in ctx.session.paid_done:
            label = LABELS.get(name, name)
            return ToolOutcome(
                f"这一轮已经{label}过一次，不再重复；先按结果改，作者要再来一遍就下一轮。",
                is_error=True,
            )
    ctx.session.executed.append(name)
    try:
        outcome = handler(ctx, dict(args or {}))
        if name in PAID_TOOLS:
            ctx.session.paid_done.append(name)
        return outcome
    except WorkspaceError as exc:
        paid = name in PAID_TOOLS
        if paid:
            ctx.session.paid_failed = True
        ctx.port.say(f"没有完成：{exc}")
        return ToolOutcome(f"没有完成：{exc}", is_error=True, paid_failure=paid)

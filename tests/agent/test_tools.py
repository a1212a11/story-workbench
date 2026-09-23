"""工具：唯一写路径经审批；不批准一个字不变；核对引用；历史与恢复；审读丢弃核不上的引用。"""

from __future__ import annotations

import json

from story_agent import tools
from story_agent.tools import Session, ToolContext, describe_call, execute
from story_agent.workspace import Workspace
from tests.agent.conftest import CHAPTER, CONFIG, FakePort, scripted_clients


def ctx_for(workspace: Workspace, port: FakePort, clients=None, config=None) -> ToolContext:
    return ToolContext(workspace, port, clients, config, Session())


def commits(workspace: Workspace) -> int:
    return len(workspace.history(limit=100))


# ---------------------------------------------------------------- edit


def test_formal_edit_not_approved_changes_nothing(chapter_workspace: Workspace):
    port = FakePort(answers=["再短一点"])
    before = commits(chapter_workspace)
    outcome = execute(
        ctx_for(chapter_workspace, port),
        "edit",
        {
            "path": "章节/第01章.md",
            "reason": "删掉犹豫",
            "changes": [{"old": "他没有下去。", "new": "他下去了。"}],
        },
    )
    assert not outcome.is_error
    assert "作者没有批准" in outcome.text and "作者说：再短一点" in outcome.text
    assert chapter_workspace.read_text("章节/第01章.md") == CHAPTER
    assert commits(chapter_workspace) == before
    card = port.cards[0]
    assert card.title == "修改 章节/第01章.md · 删掉犹豫" and card.kind == "edit"
    assert "-他没有下去。" in card.diff and "+他下去了。" in card.diff and card.summary == "共 1 处"
    assert any("不批准：再短一点" in r for r in _system_lines(chapter_workspace))


def test_formal_edit_approved_writes_and_commits(chapter_workspace: Workspace):
    port = FakePort(answers=[""])
    outcome = execute(
        ctx_for(chapter_workspace, port),
        "edit",
        {
            "path": "章节/第01章.md",
            "reason": "删掉犹豫",
            "changes": [{"old": "他没有下去。", "new": "他下去了。"}],
        },
    )
    assert not outcome.is_error and "已保存 章节/第01章.md（共 1 处）" in outcome.text
    assert "他下去了。" in chapter_workspace.read_text("章节/第01章.md")
    latest = chapter_workspace.history(limit=1)[0]
    assert latest.message == "删掉犹豫" and latest.files == ("章节/第01章.md",)
    assert any("→ 批准" in r for r in _system_lines(chapter_workspace))


def test_new_formal_file_and_free_file_paths(workspace: Workspace):
    port = FakePort(answers=["好"])
    outcome = execute(
        ctx_for(workspace, port),
        "edit",
        {"path": "人物/沈砚.md", "reason": "立人物", "content": "# 沈砚\n\n矿工。\n"},
    )
    assert port.cards[0].title == "新建 人物/沈砚.md · 立人物"
    assert port.cards[0].summary == "新建文件" and "PageUp 上翻阅读" in port.cards[0].diff
    assert "── 人物/沈砚.md（新建）──" in port.output and "矿工。" in port.output
    assert workspace.read_text("人物/沈砚.md") == "# 沈砚\n\n矿工。\n" and not outcome.is_error

    outcome = execute(
        ctx_for(workspace, port),
        "edit",
        {"path": "故事状态.md", "reason": "整理", "content": "# 故事状态\n\n沈砚在井边。\n"},
    )
    assert len(port.cards) == 1  # 自由文件不弹卡
    assert "+沈砚在井边。" in port.output and "已保存 故事状态.md" in outcome.text
    assert workspace.history(limit=1)[0].message == "整理"


def test_delete_goes_through_approval_and_stays_in_history(chapter_workspace: Workspace):
    from story_agent.tools import describe_call

    # 正式文件：先弹卡；不批准就不动
    port = FakePort(answers=["不"])
    args = {"path": "章节/第01章.md", "reason": "旧稿作废", "delete": True}
    outcome = execute(ctx_for(chapter_workspace, port), "edit", args)
    assert "没有批准" in outcome.text and chapter_workspace.file_exists("章节/第01章.md")
    card = port.cards[0]
    assert card.title == "删除 章节/第01章.md · 旧稿作废" and card.kind == "delete"
    assert port.cards[0].summary.startswith("删除，共 ") and "-他没有下去。" in port.cards[0].diff
    assert describe_call("edit", args) == "修改(章节/第01章.md · 删除 · 旧稿作废)"
    # 批准：文件没了，历史里有这一笔，restore 能找回
    port = FakePort(answers=[""])
    before = commits(chapter_workspace)
    outcome = execute(ctx_for(chapter_workspace, port), "edit", args)
    assert "已删除 章节/第01章.md" in outcome.text and not outcome.is_error
    assert not chapter_workspace.file_exists("章节/第01章.md")
    assert chapter_workspace.chapters() == []
    latest = chapter_workspace.history(limit=1)[0]
    assert commits(chapter_workspace) == before + 1 and latest.message == "旧稿作废"
    assert latest.files == ("章节/第01章.md",)
    older = chapter_workspace.history(limit=2)[1]
    port = FakePort(answers=[""])
    execute(
        ctx_for(chapter_workspace, port),
        "history",
        {"action": "restore", "path": "章节/第01章.md", "ref": older.ref},
    )
    assert "他没有下去。" in chapter_workspace.read_text("章节/第01章.md")
    # 自由文件直接删，不弹卡；作品.md 和不存在的文件不能删
    chapter_workspace.write_text("草稿/旧稿归档.md", "# 旧稿\n")
    port = FakePort()
    outcome = execute(
        ctx_for(chapter_workspace, port),
        "edit",
        {"path": "草稿/旧稿归档.md", "reason": "清掉", "delete": True},
    )
    assert not outcome.is_error and not port.cards
    assert not chapter_workspace.file_exists("草稿/旧稿归档.md")
    for path, message in (("作品.md", "不能删"), ("草稿/没有.md", "没有这个文件")):
        outcome = execute(
            ctx_for(chapter_workspace, port), "edit", {"path": path, "reason": "x", "delete": True}
        )
        assert outcome.is_error and message in outcome.text, path


def test_edit_rejects_bad_changes_and_forbidden_paths(chapter_workspace: Workspace):
    port = FakePort(answers=[""])
    ctx = ctx_for(chapter_workspace, port)
    cases = [
        (
            {"path": "章节/第01章.md", "reason": "x", "changes": [{"old": "没有这句", "new": "y"}]},
            "没有找到",
        ),
        (
            {
                "path": "章节/第01章.md",
                "reason": "x",
                "changes": [{"old": "他没有下去!", "new": "y"}],
            },
            "最接近的一行是：「他没有下去。」",
        ),
        (
            {"path": "章节/第01章.md", "reason": "x", "changes": [{"old": "井", "new": "y"}]},
            "出现了",
        ),
        ({"path": "章节/第01章.md", "reason": "x"}, "changes 或 content"),
        (
            {"path": "章节/第09章.md", "reason": "x", "changes": [{"old": "a", "new": "b"}]},
            "还不存在",
        ),
        ({"path": "对话/今天.md", "reason": "x", "content": "y"}, "对话记录"),
        ({"path": "../外面.md", "reason": "x", "content": "y"}, "离开作品目录"),
    ]
    for args, expected in cases:
        outcome = execute(ctx, "edit", args)
        assert outcome.is_error and expected in outcome.text, (args, outcome.text)
    assert chapter_workspace.read_text("章节/第01章.md") == CHAPTER and not port.cards
    assert execute(
        ctx, "edit", {"path": "章节/第01章.md", "reason": "x", "content": CHAPTER}
    ).text.endswith("没有变化。")


# ---------------------------------------------------------------- read / search / verify


def test_read_search_and_verify(chapter_workspace: Workspace):
    port = FakePort()
    ctx = ctx_for(chapter_workspace, port)
    listing = execute(ctx, "read", {})
    assert "章节/" in listing.text and ".story" not in listing.text
    body = execute(ctx, "read", {"path": "章节/第01章.md"})
    assert body.text.startswith("── 章节/第01章.md ──") and "井底有一具尸体" in body.text
    assert "井底有一具尸体" in port.output  # 作者看到全文
    assert execute(ctx, "read", {"path": "章节/第07章.md"}).is_error
    hits = execute(ctx, "search", {"pattern": "师父"})
    assert hits.text == "章节/第01章.md:3: 沈砚把灯搁在井沿。井底有一具尸体，他怀疑是师父。"
    assert "没有找到" in execute(ctx, "search", {"pattern": "灯塔"}).text
    check = execute(
        ctx, "verify", {"path": "章节/第01章.md", "quotes": ["他怀疑是师父", "他相信是师父"]}
    )
    assert (
        "✓ 第 3 行：「他怀疑是师父」" in check.text and "✗ 没有找到：「他相信是师父」" in check.text
    )
    assert "1 条引用核不上" in check.text
    assert execute(ctx, "verify", {"path": "章节/第01章.md", "quotes": []}).is_error


# ---------------------------------------------------------------- history


def test_history_list_show_and_restore_through_approval(chapter_workspace: Workspace):
    port = FakePort(answers=["", "n", ""])
    ctx = ctx_for(chapter_workspace, port)
    execute(
        ctx,
        "edit",
        {
            "path": "章节/第01章.md",
            "reason": "改结尾",
            "changes": [{"old": "他没有下去。", "new": "他下去了。"}],
        },
    )
    listing = execute(ctx, "history", {"action": "list", "path": "章节/第01章.md"})
    lines = listing.text.splitlines()
    assert "改结尾" in lines[0] and "第一章初稿" in lines[1] and "今天" in lines[0]
    ref = lines[1].split(" · ")[0]
    shown = execute(ctx, "history", {"action": "show", "path": "章节/第01章.md", "ref": ref})
    assert "他没有下去。" in shown.text
    refused = execute(ctx, "history", {"action": "restore", "path": "章节/第01章.md", "ref": ref})
    assert "作者没有批准" in refused.text and "他下去了。" in chapter_workspace.read_text(
        "章节/第01章.md"
    )
    restored = execute(ctx, "history", {"action": "restore", "path": "章节/第01章.md", "ref": ref})
    assert "已保存" in restored.text and chapter_workspace.read_text("章节/第01章.md") == CHAPTER
    assert port.cards[-1].kind == "restore" and port.cards[-1].title.startswith(
        "恢复 章节/第01章.md"
    )
    assert execute(ctx, "history", {"action": "show", "path": "章节/第01章.md"}).is_error
    assert execute(ctx, "history", {"action": "list"}).text.count("\n") >= 2


def test_history_without_git_says_so(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    ws = Workspace.create(tmp_path / "x", "x")
    outcome = execute(ctx_for(ws, FakePort()), "history", {"action": "list"})
    assert "没有找到 git" in outcome.text and not outcome.is_error


# ---------------------------------------------------------------- open_editor


def test_open_editor_save_and_cancel(chapter_workspace: Workspace):
    port = FakePort(edits=[None, CHAPTER + "\n作者补了一句。\n"])
    ctx = ctx_for(chapter_workspace, port)
    cancelled = execute(ctx, "open_editor", {"path": "章节/第01章.md"})
    assert "取消" in cancelled.text and chapter_workspace.read_text("章节/第01章.md") == CHAPTER
    saved = execute(ctx, "open_editor", {"path": "章节/第01章.md"})
    assert "作者已保存" in saved.text and "作者补了一句" in chapter_workspace.read_text(
        "章节/第01章.md"
    )
    assert chapter_workspace.history(limit=1)[0].message == "作者亲自修改 章节/第01章.md"
    assert not port.cards  # 作者亲自改不再弹卡


# ---------------------------------------------------------------- review


def _review_json(*remarks) -> str:
    return json.dumps({"remarks": list(remarks), "summary": "总体可读。"}, ensure_ascii=False)


def test_review_keeps_only_verifiable_quotes_and_records_usage(chapter_workspace: Workspace):
    clients = scripted_clients(
        [],
        _review_json(
            {
                "kind": "新设定",
                "quote": "他怀疑是师父",
                "explanation": "师父此前未出现。",
                "suggestion": "先立人物。",
            },
            {
                "kind": "冲突",
                "quote": "他确定是师父",
                "explanation": "编造的引用。",
                "suggestion": "无",
            },
        ),
    )
    chapter_workspace.write_text("人物/沈砚.md", "# 沈砚\n\n矿工。\n")
    port = FakePort()
    outcome = execute(
        ctx_for(chapter_workspace, port, clients, CONFIG),
        "review",
        {"path": "章节/第01章.md", "focus": "人物"},
    )
    assert not outcome.is_error
    assert "1. 新设定 · 第 3 行「他怀疑是师父」" in outcome.text
    assert "他确定是师父" not in outcome.text and "1 条提醒引用核不上正文，已丢弃" in outcome.text
    assert "总体：总体可读。" in outcome.text and "重点：人物" in outcome.text
    sent = clients.completes[0]
    assert sent[0].role == "system" and "独立审读" in sent[0].content
    assert "### 人物/沈砚.md" in sent[1].content and "## 审读清单" in sent[1].content
    assert "认知越界" in sent[1].content  # 来自 技能/审读.md
    usage = (chapter_workspace.root / ".story" / "用量.jsonl").read_text()
    assert "审读 章节/第01章.md" in usage


def test_review_failure_blocks_further_paid_calls_in_the_turn(chapter_workspace: Workspace):
    from tests.agent.conftest import provider_failure

    clients = scripted_clients([], provider_failure())
    ctx = ctx_for(chapter_workspace, FakePort(), clients, CONFIG)
    first = execute(ctx, "review", {"path": "章节/第01章.md"})
    assert first.is_error and first.paid_failure and "审读请求失败" in first.text
    second = execute(ctx, "review", {"path": "章节/第01章.md"})
    assert second.is_error and "不再自动发起" in second.text and len(clients.completes) == 1
    # 非付费工具不受影响
    assert not execute(ctx, "read", {"path": "章节/第01章.md"}).is_error


def test_review_without_model_or_with_bad_json(chapter_workspace: Workspace):
    refused = execute(ctx_for(chapter_workspace, FakePort()), "review", {"path": "章节/第01章.md"})
    assert refused.is_error and "没有密钥" in refused.text
    clients = scripted_clients([], "这不是 JSON")
    bad = execute(
        ctx_for(chapter_workspace, FakePort(), clients, CONFIG),
        "review",
        {"path": "章节/第01章.md"},
    )
    assert bad.is_error and "JSON" in bad.text


def test_rewrite_uses_writing_model_and_only_the_materials_it_should(chapter_workspace: Workspace):
    from dataclasses import replace

    from story_agent.provider import CallOptions

    ws = chapter_workspace
    ws.write_text(
        "大纲.md",
        "# 大纲\n\n## 全书走向\n\n井下有秘密。\n\n## 第一章 井\n\n- 剧情：沈砚下井。\n"
        "- 情绪线：不敢想师父。\n- 结尾状态：他没有下去。\n\n## 第二章\n\n- 剧情：别的。\n",
    )
    ws.write_text("人物/沈砚.md", "# 沈砚\n\n## 他心里怎么说话\n- 井里的东西不会自己上来。\n")
    ws.write_text("设定/井.md", "# 井\n\n井是圆的。\n")
    ws.write_text("故事状态.md", "# 故事状态\n\n师父已死。\n")
    ws.write_text("草稿/范文/值事棚.md", "范文：学法不学句。\n\n他数到第三遍。\n")
    ws.write_text("草稿/第01章-初稿.md", "# 第一章\n\n沈砚把灯搁在井沿。他很难过。\n")
    writing = CallOptions(model="anthropic/claude-opus-5", max_output_tokens=800, timeout_seconds=5)
    config = replace(CONFIG, writing_options=writing)
    clients = scripted_clients([], "# 第一章\n\n沈砚把灯搁在井沿。灯焰跳了两下，他没看。\n")
    port = FakePort()
    outcome = execute(
        ctx_for(ws, port, clients, config),
        "rewrite",
        {"path": "草稿/第01章-初稿.md", "focus": "让他嘴硬一点"},
    )
    assert not outcome.is_error
    assert outcome.text.startswith("重写 草稿/第01章-初稿.md（着重：让他嘴硬一点）：\n# 第一章")
    assert "灯焰跳了两下" in port.output  # 全文显示给作者
    sent = clients.completes[0]
    assert (
        sent[0].role == "system"
        and "从主角脑子里" in sent[0].content
        and "硬约束" in sent[0].content
    )
    body = sent[1].content
    # 给了：作品、本章大纲段（不含第二章）、人物、范文、初稿
    assert "### 作品.md" in body and "情绪线：不敢想师父" in body and "别的。" not in body
    assert "### 人物/沈砚.md" in body and "井里的东西不会自己上来" in body
    assert "范文 草稿/范文/值事棚.md" in body and "他数到第三遍" in body
    assert "## 初稿：草稿/第01章-初稿.md" in body and "他很难过" in body
    assert "## 这次着重\n让他嘴硬一点" in body
    # 没给：设定与故事状态，重写不该动事实
    assert "井是圆的" not in body and "师父已死" not in body
    # 用写作模型、双倍时限；用量记的是写作模型
    used = clients.complete_options[0]
    assert used.model == "anthropic/claude-opus-5" and used.timeout_seconds == 10
    assert "重写 草稿/第01章-初稿.md" in (ws.root / ".story" / "用量.jsonl").read_text()
    assert "anthropic/claude-opus-5" in (ws.root / ".story" / "用量.jsonl").read_text()
    # 章号：显式给的优先；空初稿、非法章号报错
    ws.write_text("草稿/试写.md", "# 试\n\n一句。\n")
    clients = scripted_clients([], "# 试\n\n一句半。\n")
    execute(ctx_for(ws, port, clients, config), "rewrite", {"path": "草稿/试写.md", "chapter": 2})
    assert "剧情：别的" in clients.completes[0][1].content
    ws.write_text("草稿/空.md", "\n")
    empty = execute(ctx_for(ws, port, clients, config), "rewrite", {"path": "草稿/空.md"})
    assert empty.is_error and "是空的" in empty.text
    bad = execute(
        ctx_for(ws, port, clients, config), "rewrite", {"path": "草稿/试写.md", "chapter": 0}
    )
    assert bad.is_error and "正整数" in bad.text
    # 截断：结果不可用
    from story_agent.provider import CompleteResult

    def truncated(messages, options, cancel):
        return CompleteResult(
            text="半截", usage={"input_tokens": 1, "output_tokens": 1}, truncated=True
        )

    clients = scripted_clients([], "x")
    clients.complete = truncated
    cut = execute(ctx_for(ws, port, clients, config), "rewrite", {"path": "草稿/试写.md"})
    assert cut.is_error and "截断" in cut.text


def test_each_paid_tool_runs_once_per_turn(chapter_workspace: Workspace):
    clients = scripted_clients([], _review_json())
    ctx = ctx_for(chapter_workspace, FakePort(), clients, CONFIG)
    assert not execute(ctx, "review", {"path": "章节/第01章.md"}).is_error
    again = execute(ctx, "review", {"path": "章节/第01章.md"})
    assert again.is_error and "已经审读过一次" in again.text and len(clients.completes) == 1
    # 另一种付费工具同一轮仍可用一次
    chapter_workspace.write_text("草稿/第01章-初稿.md", "# 第一章\n\n一句。\n")
    clients.complete = scripted_clients([], "# 第一章\n\n一句半。\n").complete
    assert not execute(ctx, "rewrite", {"path": "草稿/第01章-初稿.md"}).is_error
    third = execute(ctx, "rewrite", {"path": "草稿/第01章-初稿.md"})
    assert third.is_error and "已经重写过一次" in third.text
    # 没有密钥的文案不再只说审读
    refused = execute(
        ctx_for(chapter_workspace, FakePort()), "rewrite", {"path": "草稿/第01章-初稿.md"}
    )
    assert refused.is_error and "没有密钥" in refused.text


def test_outline_section_finds_a_chapter_by_number_or_numeral():
    from story_agent.rewrite import chapter_number, outline_section

    outline = (
        "# 大纲\n\n## 第一章 井\n\n- 剧情：a\n\n## 第02章\n\n- 剧情：b\n\n"
        "## 第十二章\n\n- 剧情：c\n"
    )
    assert outline_section(outline, 1) == "## 第一章 井\n\n- 剧情：a"
    assert outline_section(outline, 2) == "## 第02章\n\n- 剧情：b"
    assert outline_section(outline, 12) == "## 第十二章\n\n- 剧情：c"
    assert outline_section(outline, 3) == ""
    assert chapter_number("草稿/第07章-初稿.md") == 7 and chapter_number("草稿/试写.md") is None


# ---------------------------------------------------------------- 杂项


def test_unknown_tool_specs_and_labels(workspace: Workspace):
    assert execute(ctx_for(workspace, FakePort()), "delete_everything", {}).is_error
    names = {spec.name for spec in tools.TOOL_SPECS}
    assert names == {
        "read",
        "search",
        "edit",
        "history",
        "verify",
        "review",
        "rewrite",
        "open_editor",
        "skill",
        "ask",
    }
    for spec in tools.TOOL_SPECS:
        assert spec.input_schema["type"] == "object" and spec.description
        assert set(spec.input_schema["required"]) <= set(spec.input_schema["properties"])
    assert describe_call("edit", {"path": "章节/第02章.md", "reason": "改了守夜人的段落"}) == (
        "修改(章节/第02章.md · 改了守夜人的段落)"
    )
    assert (
        describe_call("history", {"action": "restore", "path": "大纲.md"}) == "历史(大纲.md · 恢复)"
    )
    assert describe_call("read", {}) == "读取()"


def _system_lines(workspace: Workspace) -> list[str]:
    path = workspace.root / ".story" / "对话.jsonl"
    return [json.loads(line)["text"] for line in path.read_text().splitlines() if line.strip()]


def test_skill_tool_reads_a_method_file_and_lists_what_exists(workspace: Workspace):
    from story_agent.tools import SKILLS

    port = FakePort()
    ctx = ctx_for(workspace, port)
    out = execute(ctx, "skill", {"name": "写章"})
    assert not out.is_error and "── 技能/写章.md ──" in out.text and "动笔前读什么" in out.text
    assert "动笔前读什么" in port.output  # 作者也看得到，只是默认折叠
    assert execute(ctx, "skill", {"name": "写章.md"}).text == out.text  # 带不带后缀都行
    bad = execute(ctx, "skill", {"name": "写小说"})
    assert bad.is_error and "可用的是：" in bad.text and "共创" in bad.text
    assert set(SKILLS) == {p.stem for p in (workspace.root / "技能").glob("*.md")}


def test_ask_tool_puts_the_choice_back_to_the_model_and_into_the_record(workspace: Workspace):
    port = FakePort(answers=["被管网老工收养：工造底子和市井人脉都从这儿来"])
    ctx = ctx_for(workspace, port)
    out = execute(
        ctx,
        "ask",
        {
            "question": "谁把他捡走？",
            "options": [
                "被管网老工收养：工造底子和市井人脉都从这儿来",
                "被地下帮派捡走：学的是偷摸走私那套",
            ],
        },
    )
    assert not out.is_error and out.text == "作者答：被管网老工收养：工造底子和市井人脉都从这儿来"
    assert port.asked[0][0] == "谁把他捡走？" and len(port.asked[0][1]) == 2
    records = [r for r in workspace.all_records() if r["role"] == "system"]
    assert "选择：谁把他捡走？ → 被管网老工收养" in records[-1]["text"]

    # 作者跳过：不再追问
    skipped = execute(
        ctx_for(workspace, FakePort()), "ask", {"question": "那这个呢？", "options": ["甲", "乙"]}
    )
    assert "作者没有选" in skipped.text and not skipped.is_error
    # 选项不够或问题为空：不打扰作者
    bad = execute(
        ctx_for(workspace, FakePort()), "ask", {"question": "只有一个", "options": ["甲"]}
    )
    assert bad.is_error and "2 到 5 个选项" in bad.text

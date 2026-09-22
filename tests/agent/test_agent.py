"""代理循环：开场轮、一句话到审批过的改动、不批准回到模型、口头宣称零改动、上限、中断、无密钥。"""

from __future__ import annotations

import json
from dataclasses import replace

from story_agent.agent import Agent, fit_transcript, history_to_transcript
from story_agent.provider import CancelSignal, ChatMessage, TextBlock, ToolResultBlock
from story_agent.workspace import Workspace
from tests.agent.conftest import CHAPTER, CONFIG, FakePort, Reply, scripted_clients, use


def make_agent(workspace, script, answers=None, *, clients=True, **kwargs):
    port = FakePort(answers)
    clients_obj = scripted_clients(script) if clients else None
    agent = Agent(
        workspace, port, clients=clients_obj, config=CONFIG if clients else None, **kwargs
    )
    return agent, port, clients_obj


def tool_results(messages: list[ChatMessage]) -> list[ToolResultBlock]:
    return [
        block
        for message in messages
        if not isinstance(message.content, str)
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]


def transcript_rows(workspace: Workspace) -> list[dict]:
    path = workspace.root / ".story" / "对话.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------- 开场


def test_opening_without_model_is_local_and_free(workspace):
    agent, port, _ = make_agent(workspace, [], clients=False)
    result = agent.opening()
    assert result.calls == 0 and "《井下回声》" in port.output and "模型尚未接入" in port.output
    assert "大纲还是空的" in port.output and transcript_rows(workspace) == []


def test_opening_reads_files_and_persists_only_the_reply(workspace):
    script = [
        Reply(
            "我先看看现状。",
            (
                use("read", "a", path="作品.md"),
                use("read", "b", path="大纲.md"),
                use("read", "c", path="故事状态.md"),
            ),
        ),
        Reply("这本书还是空的。我们先聊一句话：这是个什么故事？"),
    ]
    agent, port, clients = make_agent(workspace, script)
    result = agent.opening()
    assert result.calls == 2 and result.tools == ("read", "read", "read")
    assert "这本书还是空的" in port.output or "这本书还是空的" in "".join(port.chunks)
    first_call = clients.calls[0]
    assert first_call[0].role == "system" and "《井下回声》" in first_call[0].content
    assert "技能/：" in first_call[0].content and "作者看不见" in first_call[1].content
    results = tool_results(clients.calls[1])
    assert len(results) == 3 and "## 题材与基调" in results[0].content
    rows = transcript_rows(workspace)
    assert [r["role"] for r in rows] == ["assistant"]  # 隐藏提示不落库


# ---------------------------------------------------------------- 一句话到审批过的改动


def test_author_request_drives_read_edit_review_and_approval(chapter_workspace):
    review_json = json.dumps({"remarks": [], "summary": "干净。"}, ensure_ascii=False)
    script = [
        Reply("我先读一下。", (use("read", "a", path="章节/第01章.md"),)),
        Reply(
            "把犹豫删掉。",
            (
                use(
                    "edit",
                    "b",
                    path="章节/第01章.md",
                    reason="删掉犹豫",
                    changes=[{"old": "他没有下去。", "new": "他下去了。"}],
                ),
            ),
        ),
        Reply("再审一遍。", (use("review", "c", path="章节/第01章.md"),)),
        Reply("改好了，审读没有提醒。要不要接着写第二章？"),
    ]
    port = FakePort(answers=[""])
    clients = scripted_clients(script, review_json)
    agent = Agent(chapter_workspace, port, clients=clients, config=CONFIG)
    result = agent.handle("结尾别犹豫，让他下去")
    assert result.calls == 4 and result.tools == ("read", "edit", "review")
    assert result.reply.endswith("要不要接着写第二章？") and not result.stopped_early
    assert "他下去了。" in chapter_workspace.read_text("章节/第01章.md")
    assert chapter_workspace.history(limit=1)[0].message == "删掉犹豫"
    assert port.cards[0].kind == "edit" and "+他下去了。" in port.cards[0].diff
    rows = transcript_rows(chapter_workspace)
    assert [r["role"] for r in rows] == ["author", "system", "assistant"]
    assert rows[0]["text"] == "结尾别犹豫，让他下去" and rows[-1]["text"] == result.reply
    assert agent.session_usage == {"input_tokens": 12, "output_tokens": 16}
    usage_lines = (chapter_workspace.root / ".story" / "用量.jsonl").read_text().splitlines()
    assert len(usage_lines) == 5  # 4 次对话 + 1 次审读


def test_rejection_with_note_reaches_the_model_and_changes_nothing(chapter_workspace):
    script = [
        Reply(
            "",
            (
                use(
                    "edit",
                    "a",
                    path="章节/第01章.md",
                    reason="改结尾",
                    changes=[{"old": "他没有下去。", "new": "他下去了。"}],
                ),
            ),
        ),
        Reply(
            "明白，那我让他先喊一声。",
            (
                use(
                    "edit",
                    "b",
                    path="章节/第01章.md",
                    reason="先喊一声",
                    changes=[{"old": "他没有下去。", "new": "他朝井底喊了一声，没有回应。"}],
                ),
            ),
        ),
        Reply("改好了。"),
    ]
    agent, port, clients = make_agent(chapter_workspace, script, answers=["别让他下去，太快了", ""])
    result = agent.handle("结尾改一下")
    assert result.tools == ("edit", "edit")
    first_result = tool_results(clients.calls[1])[0]
    assert "作者没有批准" in first_result.content and "别让他下去，太快了" in first_result.content
    assert not first_result.is_error
    body = chapter_workspace.read_text("章节/第01章.md")
    assert "他下去了。" not in body and "他朝井底喊了一声" in body
    assert len(chapter_workspace.history(limit=10)) == 3  # 开书 + 初稿 + 一次批准


def test_model_claiming_a_change_without_edit_changes_nothing(chapter_workspace):
    agent, port, _ = make_agent(chapter_workspace, [Reply("我已经把结尾改成他下去了。")])
    agent.handle("结尾让他下去")
    assert chapter_workspace.read_text("章节/第01章.md") == CHAPTER
    assert len(chapter_workspace.history(limit=10)) == 2


def test_free_file_edit_needs_no_approval(workspace):
    script = [
        Reply(
            "",
            (
                use(
                    "edit",
                    "a",
                    path="故事状态.md",
                    reason="整理",
                    content="# 故事状态\n\n沈砚在井边。\n",
                ),
            ),
        ),
        Reply("状态更新了。"),
    ]
    agent, port, _ = make_agent(workspace, script)
    agent.handle("更新一下状态")
    assert not port.cards and "沈砚在井边" in workspace.read_text("故事状态.md")


# ---------------------------------------------------------------- 边界


def test_turn_stops_after_call_limit(workspace):
    script = [Reply("", (use("read", f"r{i}", path="作品.md"),)) for i in range(5)]
    agent, port, _ = make_agent(workspace, script, max_calls=2)
    result = agent.handle("一直读")
    assert result.calls == 2 and result.stopped_early and "先停下来" in port.output


def test_cancel_stops_before_any_write_and_drops_half_reply(chapter_workspace):
    def interrupt(cancel):
        cancel.set()

    script = [
        Reply(
            "我来改结尾……",
            (
                use(
                    "edit",
                    "a",
                    path="章节/第01章.md",
                    reason="改",
                    changes=[{"old": "他没有下去。", "new": "他下去了。"}],
                ),
            ),
            hook=interrupt,
        ),
        Reply("不该到这里。"),
    ]
    agent, port, _ = make_agent(chapter_workspace, script, answers=[""])
    cancel = CancelSignal()
    result = agent.handle("改结尾", cancel)
    assert result.cancelled and result.tools == () and not port.cards
    assert chapter_workspace.read_text("章节/第01章.md") == CHAPTER
    assert "已中断" in port.output
    assert [r["role"] for r in transcript_rows(chapter_workspace)] == ["author"]
    assert agent.transcript == []  # 半截回复与本轮发言都不留在对话里


def test_provider_error_and_truncation_stop_the_turn(workspace):
    agent, port, _ = make_agent(workspace, [Reply(error="APITimeoutError")])
    result = agent.handle("你好")
    assert result.stopped_early and "请求失败（APITimeoutError）" in port.output
    agent, port, _ = make_agent(workspace, [Reply("很长很长", truncated=True)])
    result = agent.handle("你好")
    assert result.stopped_early and "输出被截断" in port.output


def test_without_model_author_words_are_recorded_and_no_request_is_made(workspace):
    agent, port, _ = make_agent(workspace, [], clients=False)
    result = agent.handle("先记一下：主角叫沈砚")
    assert result.calls == 0 and "模型尚未接入" in port.output
    assert [r["role"] for r in transcript_rows(workspace)] == ["author"]


def test_transcript_is_restored_on_reopen(workspace):
    workspace.append_transcript("author", "写第一章")
    workspace.append_transcript("assistant", "好。")
    workspace.append_transcript("assistant", "写完了。")
    agent, _, clients = make_agent(workspace, [Reply("继续。")])
    agent.handle("接着来")
    sent = clients.calls[0]
    assert [m.role for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[2].content == "好。\n\n写完了。"


def test_history_and_transcript_fitting():
    rows = [("assistant", "抢先说"), ("author", "a"), ("author", "b"), ("assistant", "c")]
    assert [(m.role, m.content) for m in history_to_transcript(rows)] == [
        ("user", "a\n\nb"),
        ("assistant", "c"),
    ]
    transcript = [
        ChatMessage("user", "一" * 100),
        ChatMessage("assistant", (TextBlock("二" * 100),)),
        ChatMessage("user", (ToolResultBlock("t", "三" * 100),)),
        ChatMessage("user", "四" * 10),
    ]
    kept = fit_transcript(transcript, 150, len)
    assert [m.role for m in kept] == ["user"] and kept[0].content == "四" * 10
    assert fit_transcript(transcript, 10_000, len) == transcript


# ---------------------------------------------------------------- 记忆：重开不叫模型、摘要、笔记


def test_reopen_reports_status_locally_without_calling_the_model(chapter_workspace):
    chapter_workspace.append_transcript("author", "写第一章")
    chapter_workspace.append_transcript("assistant", "写完了。")
    agent, port, clients = make_agent(chapter_workspace, [Reply("不该被调用")])
    result = agent.opening()
    assert result.calls == 0 and clients.calls == [] and "已有 1 章正文" in port.output
    assert "模型尚未接入" not in port.output
    assert [m.role for m in agent.transcript] == ["user", "assistant"]


def _long_conversation(workspace, rounds: int = 25) -> None:
    """撑满上下文预算的对话：作者每句都很长。"""
    for i in range(rounds):
        workspace.append_transcript("author", f"第 {i} 句：" + "很长的话" * 500)
        workspace.append_transcript("assistant", f"记下了 {i}。")


def test_compacting_triggers_on_budget_not_on_message_count(workspace):
    # 条数很多但内容短：远没用满预算，不该压缩
    for i in range(40):
        workspace.append_transcript("author", f"第 {i} 句")
        workspace.append_transcript("assistant", f"好 {i}。")
    quiet = Agent(workspace, FakePort(), clients=scripted_clients([], "x"), config=CONFIG)
    assert len(quiet.transcript) == 80 and not quiet.needs_compacting


def test_opening_does_not_compact_and_compacting_is_explicit(workspace):
    _long_conversation(workspace)
    clients = scripted_clients([], "作者喜欢长篇大论。")
    agent = Agent(workspace, FakePort(), clients=clients, config=CONFIG)
    # 打开作品不调用模型，界面不会白屏卡住
    assert clients.completes == [] and len(agent.transcript) == 50 and agent.summary == ""
    assert agent.needs_compacting

    assert agent.compact_pending() is True
    assert agent.summary == "作者喜欢长篇大论。" and len(clients.completes) == 1
    sent = clients.completes[0][1].content
    assert "第 0 句" in sent and "第 14 句" in sent and "第 24 句" not in sent
    covered = workspace.read_summary()[1]
    assert workspace.read_summary()[0] == "作者喜欢长篇大论。" and covered == 30
    assert len(agent.transcript) == 20  # 最近 20 条原样留着
    used, limit = agent.context_usage()
    assert used < limit * 0.7  # 压完之后离预算还远
    assert "之前对话的摘要：\n作者喜欢长篇大论。" in agent.system_prompt()
    assert '"purpose": "摘要"' in (workspace.root / ".story" / "用量.jsonl").read_text()

    again = Agent(workspace, FakePort(), clients=scripted_clients([], "x"), config=CONFIG)
    assert again.summary == "作者喜欢长篇大论。" and not again.needs_compacting
    offline = Agent(workspace, FakePort(), clients=None, config=None)
    assert offline.summary == "作者喜欢长篇大论。" and not offline.needs_compacting


def test_failed_compaction_keeps_the_old_conversation_instead_of_dropping_it(workspace):
    _long_conversation(workspace)
    # 推理型号可能把输出预算全用在思考上，正文为空
    clients = scripted_clients([], "")
    agent = Agent(workspace, FakePort(), clients=clients, config=CONFIG)
    assert agent.compact_pending() is False
    assert workspace.read_summary() == ("", 0)  # 覆盖范围没有推进
    assert len(agent.transcript) == 50  # 旧对话照常带着，不会凭空消失
    assert "摘要没有正文" in (workspace.root / ".story" / "日志.txt").read_text()


def test_empty_summary_on_disk_never_swallows_records(workspace):
    for i in range(6):
        workspace.append_transcript("author", f"第 {i} 句")
    workspace.write_summary("", 4)  # 旧版本留下的坏状态
    agent = Agent(workspace, FakePort(), clients=None, config=None)
    assert agent._covered == 0
    assert "第 0 句" in str(agent.transcript[0].content)  # 连续发言会并成一条，六句都在
    workspace.write_summary("有内容的摘要", 99)  # 撤回之后覆盖范围超过了记录数
    assert Agent(workspace, FakePort(), clients=None, config=None)._covered == 0


def test_overflow_within_a_session_is_compacted_not_dropped(workspace):
    for i in range(6):
        workspace.append_transcript("author", f"第 {i} 段。" + "很长的话" * 4000)
        workspace.append_transcript("assistant", f"回应 {i}。")
    clients = scripted_clients([Reply("好。")], "作者说了六段长话。")
    agent = Agent(workspace, FakePort(), clients=clients, config=CONFIG)
    assert clients.completes == []  # 条数没超，打开时不压缩
    agent.handle("继续")
    assert len(clients.completes) == 1 and agent.summary == "作者说了六段长话。"
    assert "第 0 段" in clients.completes[0][1].content
    assert workspace.read_summary()[0] == "作者说了六段长话。"
    system = clients.calls[0][0].content
    assert "作者说了六段长话。" in system


def test_notes_and_preferences_appear_in_the_system_prompt(workspace):
    workspace.write_text("草稿/代理笔记.md", "作者拒绝过整章重写，只接受定点改。")
    agent, _, _ = make_agent(workspace, [])
    prompt = agent.system_prompt()
    assert "作者拒绝过整章重写" in prompt and "草稿/代理笔记.md" in prompt
    assert "作者的偏好与禁忌" in prompt  # 偏好与笔记都自动带在提示里


def test_turn_reminder_is_sent_to_the_model_but_not_persisted(chapter_workspace):
    agent, _, clients = make_agent(chapter_workspace, [Reply("好。")])
    agent.handle("一起完善世界观")
    sent = clients.calls[0][-1].content
    assert sent.startswith("一起完善世界观") and "先 skill 查对应的方法" in sent
    rows = transcript_rows(chapter_workspace)
    assert rows[0]["text"] == "一起完善世界观"


def test_api_error_description_has_status_and_message_but_no_secrets():
    from story_agent.provider import describe_api_error

    class Boom(Exception):
        status_code = 402
        message = "Insufficient credits.  Add more at\nopenrouter.ai"

    text = describe_api_error(Boom())
    assert text == "Boom · HTTP 402 · Insufficient credits. Add more at openrouter.ai"
    assert describe_api_error(TimeoutError()) == "TimeoutError"


def test_log_file_records_calls_tools_and_failures(chapter_workspace):
    script = [
        Reply("看看。", (use("read", "a", path="章节/第01章.md"),)),
        Reply("完了。", error="APIStatusError · HTTP 500 · upstream"),
    ]
    agent, port, _ = make_agent(chapter_workspace, script)
    agent.handle("看第一章")
    log = (chapter_workspace.root / ".story" / "日志.txt").read_text()
    lines = log.splitlines()
    assert any(
        "模型 完成 · " in line and "↑3 ↓4" in line and "1 个工具调用" in line for line in lines
    )
    assert any('工具 read {"path": "章节/第01章.md"}' in line for line in lines)
    assert any("结果 read 完成 ── 章节/第01章.md ──" in line for line in lines)
    assert any("模型 失败" in line and "HTTP 500" in line for line in lines)
    assert all(line[:4].isdigit() for line in lines)  # 每行带时间戳


def test_persona_stays_lean_and_sends_details_to_the_skill_files(workspace):
    agent, _, _ = make_agent(workspace, [])
    prompt = agent.system_prompt()
    # 留在提示里的只有不可违反的规矩和查方法的入口
    assert "先用 skill 查一次方法" in prompt and "开书、共创" in prompt
    assert "不要说你没做的事" in prompt and "没调用工具，就别说" in prompt
    assert "改它们要经作者批准" in prompt
    # 方法细节下放到技能文件，不再挤在提示里
    for detail in ("落到画面", "带一件作者没提过的东西", "同一主题始终用同一个文件"):
        assert detail not in prompt, detail
    assert "带一件作者没提过的东西" in (workspace.root / "技能" / "共创.md").read_text()


def test_context_usage_counts_system_prompt_and_transcript(workspace):
    agent, _, _ = make_agent(workspace, [])
    used, limit = agent.context_usage()
    assert limit == CONFIG.budget and used > 0
    agent.transcript.append(ChatMessage("user", "一" * 1000))
    used_after, _ = agent.context_usage()
    assert used_after - used == 1200  # 中文按 1.2 估算


def test_compaction_disables_thinking_and_review_gets_longer_time(chapter_workspace):
    from story_agent.provider import CallOptions, _request_kwargs
    from story_agent.tools import Session, ToolContext, execute

    # 压缩是机械活：关掉思考
    _long_conversation(chapter_workspace)
    clients = scripted_clients([], "要点。")
    agent = Agent(chapter_workspace, FakePort(), clients=clients, config=CONFIG)
    assert agent.compact_pending() is True
    assert clients.complete_options[-1].no_thinking is True

    # 审读是判断活：保留思考，但一次性调用给双倍时间
    review_clients = scripted_clients([], json.dumps({"remarks": [], "summary": "干净。"}))
    ctx = ToolContext(chapter_workspace, FakePort(), review_clients, CONFIG, Session())
    assert not execute(ctx, "review", {"path": "章节/第01章.md"}).is_error
    used = review_clients.complete_options[-1]
    assert used.no_thinking is False
    assert used.timeout_seconds == CONFIG.options.timeout_seconds * 2

    # 供应商层：no_thinking 才会带上关闭思考的参数
    plain = CallOptions(model="m", max_output_tokens=10, timeout_seconds=5)
    assert "thinking" not in _request_kwargs([], plain, False)
    assert _request_kwargs([], replace(plain, no_thinking=True), False)["thinking"] == {
        "type": "disabled"
    }


def test_connection_error_is_retried_automatically(chapter_workspace):
    from story_agent.provider import is_retryable

    # 连接层错误可以安全重试；鉴权、参数错误不行
    class Boom(Exception):
        pass

    Boom.__name__ = "APIConnectionError"
    assert is_retryable(Boom())
    bad = type("AuthenticationError", (Exception,), {})()
    assert not is_retryable(bad)
    server = type("APIStatusError", (Exception,), {"status_code": 503})()
    assert is_retryable(server)

    # 第一次连不上，自动重试一次就成了；作者不用重打
    script = [
        Reply(error="APIConnectionError · Connection error.", retryable=True),
        Reply("好的。"),
    ]
    agent, port, clients = make_agent(chapter_workspace, script)
    result = agent.handle("是不是可以完全丢弃管网的设计")
    assert result.reply == "好的。" and not result.failed
    assert "网络没连上，1 秒后重试（第 1 次）" in port.output and len(clients.calls) == 2
    assert (
        "连接出错，1 秒后重试第 1 次"
        in (chapter_workspace.root / ".story" / "日志.txt").read_text()
    )


def test_gives_up_after_the_last_retry(chapter_workspace):
    from story_agent.agent import RETRY_DELAYS

    script = [Reply(error="APIConnectionError · x", retryable=True)] * (len(RETRY_DELAYS) + 1)
    agent, port, clients = make_agent(chapter_workspace, script)
    result = agent.handle("那句话")
    assert result.failed and len(clients.calls) == len(RETRY_DELAYS) + 1
    assert "请求失败（APIConnectionError · x）" in port.output
    log = (chapter_workspace.root / ".story" / "日志.txt").read_text()
    assert f"重试 {len(RETRY_DELAYS)} 次仍未接通" in log


def test_a_turn_cut_halfway_says_what_landed_and_records_it(chapter_workspace):
    from story_agent.agent import RETRY_DELAYS

    script = [
        Reply(
            "我来记一下。",
            (
                use(
                    "edit",
                    "e",
                    path="故事状态.md",
                    reason="记一下",
                    content="# 故事状态\n\n改过了。\n",
                ),
            ),
        ),
        *[Reply(error="APIConnectionError · x", retryable=True)] * (len(RETRY_DELAYS) + 1),
    ]
    agent, port, _ = make_agent(chapter_workspace, script)
    result = agent.handle("记一下状态")
    assert result.failed and result.tools == ("edit",)
    assert "断开前这一轮已经做了：修改；这些改动都在。" in port.output
    assert "改过了。" in chapter_workspace.read_text("故事状态.md")  # 做过的事不白做
    rows = transcript_rows(chapter_workspace)
    assert rows[-1]["role"] == "assistant" and "这一轮没说完。已经做了：修改" in rows[-1]["text"]


def test_a_billed_failure_is_never_retried(chapter_workspace):
    # 已经吐了字再断：可能已经计费，不自动重试
    script = [Reply("说到一半", error="APIConnectionError · x", retryable=True), Reply("不该调用")]
    agent, port, clients = make_agent(chapter_workspace, script)
    result = agent.handle("写点什么")
    assert result.failed and len(clients.calls) == 1
    # 鉴权之类的错误也不重试
    agent, port, clients = make_agent(chapter_workspace, [Reply(error="AuthenticationError")])
    assert agent.handle("再来").failed and len(clients.calls) == 1


def test_thinking_only_reply_is_retried_then_reported(chapter_workspace):
    # 只有思考、没有正文：这一轮等于没说话，先重试一次
    agent, port, clients = make_agent(
        chapter_workspace, [Reply("", thinking="想了很多"), Reply("这次说出来了。")]
    )
    result = agent.handle("姐夫的道统定哪个")
    assert result.reply == "这次说出来了。" and len(clients.calls) == 2
    assert "它刚才只在想，没说出来" in port.output
    assert transcript_rows(chapter_workspace)[-1]["text"] == "这次说出来了。"

    # 重试还是只有思考：明确告诉作者，别让他把思考当成回复
    agent, port, clients = make_agent(
        chapter_workspace, [Reply("", thinking="还是只在想"), Reply("", thinking="仍然只在想")]
    )
    result = agent.handle("再来一次")
    assert result.reply == "" and len(clients.calls) == 2
    assert "只给了思考过程，没有正文" in port.output and "没有记进对话" in port.output
    assert transcript_rows(chapter_workspace)[-1]["role"] == "author"  # 确实没存助手消息


def test_a_tool_only_turn_is_not_treated_as_empty(chapter_workspace):
    # 只调工具不说话是正常的，不该重试也不该报「只在想」
    script = [Reply("", (use("read", "r", path="章节/第01章.md"),)), Reply("看完了。")]
    agent, port, clients = make_agent(chapter_workspace, script)
    result = agent.handle("看看第一章")
    assert result.reply == "看完了。" and len(clients.calls) == 2
    assert "只在想" not in port.output and "没有正文" not in port.output

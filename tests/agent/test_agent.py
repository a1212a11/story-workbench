"""代理循环：开场轮、一句话到审批过的改动、不批准回到模型、口头宣称零改动、上限、中断、无密钥。"""

from __future__ import annotations

import json

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

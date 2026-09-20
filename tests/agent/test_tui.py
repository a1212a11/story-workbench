"""Textual 界面：开场轮、审批卡的三种回答与 Esc、编辑器、中断、没有斜杠命令、新目录建作品。"""

from __future__ import annotations

import asyncio
import time

from textual.widgets import Static, TextArea

from story_agent import __main__ as entry
from story_agent.tui import EditorScreen, PromptArea, WorkbenchApp, colorize_diff
from story_agent.workspace import Workspace
from tests.agent.conftest import CHAPTER, CONFIG, Reply, scripted_clients, use

OPENING = [Reply("看过了。这本书有一章，我们接着改？")]


def _chapter_workspace(path):
    ws = Workspace.create(path, "井下回声")
    ws.write_text("章节/第01章.md", CHAPTER)
    ws.commit("第一章初稿", "章节/第01章.md")
    return ws


async def _wait(app: WorkbenchApp, pilot, predicate, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause(0.05)
        if predicate():
            return
    raise AssertionError(f"等待超时。当前对话：\n{app.transcript_text()}")


async def _send(pilot, text: str) -> None:
    """中文经输入法进入输入框，不是逐键；测试直接插入文本再按 Enter。"""
    if text:
        pilot.app.query_one("#input", PromptArea).insert(text)
        await pilot.pause(0.05)
    await pilot.press("enter")


def _run(coro) -> None:
    asyncio.run(coro)


def _edit_reply(uid="e", new="他下去了。"):
    return Reply(
        "改结尾。",
        (
            use(
                "edit",
                uid,
                path="章节/第01章.md",
                reason="改结尾",
                changes=[{"old": "他没有下去。", "new": new}],
            ),
        ),
    )


def test_opening_turn_and_approval_by_enter(tmp_path):
    path = tmp_path / "novel"
    _chapter_workspace(path)
    clients = scripted_clients([*OPENING, _edit_reply(), Reply("改好了。")])

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 32)) as pilot:
            await _wait(app, pilot, lambda: "我们接着改？" in app.transcript_text())
            assert "《井下回声》" in app.transcript_text() and not app.busy
            await _send(pilot, "结尾让他下去")
            await _wait(app, pilot, lambda: app.mode == "approve")
            title = str(app.query_one("#card-title", Static).content)
            assert "修改 章节/第01章.md · 改结尾" in title and "共 1 处" in title
            assert app.current_card is not None and "+他下去了。" in app.current_card.diff
            await _send(pilot, "")
            await _wait(app, pilot, lambda: "改好了。" in app.transcript_text())
            assert app.mode == "chat" and "> ⏎ 批准" in app.transcript_text()
            assert "他下去了。" in Workspace(path).read_text("章节/第01章.md")
            assert "↑" in app.status_text and "井下回声" in app.status_text
            await pilot.press("ctrl+d")

    _run(scenario())


def test_rejection_with_note_and_with_n(tmp_path):
    path = tmp_path / "novel"
    _chapter_workspace(path)
    clients = scripted_clients(
        [
            *OPENING,
            _edit_reply("e1"),
            Reply("那再试一版。", (_edit_reply("e2", "他喊了一声。").uses)),
            Reply("先不改了。"),
        ]
    )

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 32)) as pilot:
            await _wait(app, pilot, lambda: "我们接着改？" in app.transcript_text())
            await _send(pilot, "结尾改一下")
            await _wait(app, pilot, lambda: app.mode == "approve")
            await _send(pilot, "太快了，别让他下去")
            await _wait(
                app,
                pilot,
                lambda: (
                    app.mode == "approve"
                    and "喊" in (app.current_card.diff if app.current_card else "")
                ),
            )
            await _send(pilot, "n")
            await _wait(app, pilot, lambda: "先不改了。" in app.transcript_text())
            assert Workspace(path).read_text("章节/第01章.md") == CHAPTER
            results = [
                block.content
                for call in clients.calls
                for message in call
                if not isinstance(message.content, str)
                for block in message.content
                if hasattr(block, "tool_use_id")
            ]
            assert any("太快了，别让他下去" in r for r in results)
            assert any("作者没有说明原因" in r for r in results)
            assert (
                "> 太快了，别让他下去" in app.transcript_text() and "> n" in app.transcript_text()
            )
            await pilot.press("ctrl+d")

    _run(scenario())


def test_escape_rejects_card_and_interrupts_turn(tmp_path):
    path = tmp_path / "novel"
    _chapter_workspace(path)
    slow_started = []

    def slow(cancel):
        slow_started.append(True)
        deadline = time.monotonic() + 5
        while not cancel.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)

    clients = scripted_clients(
        [
            *OPENING,
            _edit_reply(),
            Reply("没批准就算了。"),
            Reply("我想想……", hook=slow),
            Reply("不该出现。"),
        ]
    )

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 32)) as pilot:
            await _wait(app, pilot, lambda: "我们接着改？" in app.transcript_text())
            await _send(pilot, "改结尾")
            await _wait(app, pilot, lambda: app.mode == "approve")
            await pilot.press("escape")
            await _wait(app, pilot, lambda: "没批准就算了。" in app.transcript_text())
            assert Workspace(path).read_text("章节/第01章.md") == CHAPTER
            await _send(pilot, "再想想")
            await _wait(app, pilot, lambda: bool(slow_started))
            await pilot.press("escape")
            await _wait(app, pilot, lambda: "已中断" in app.transcript_text())
            assert "不该出现。" not in app.transcript_text() and not app.busy
            await pilot.press("ctrl+d")

    _run(scenario())


def test_editor_save_and_cancel_through_open_editor(tmp_path):
    path = tmp_path / "novel"
    _chapter_workspace(path)
    clients = scripted_clients(
        [
            *OPENING,
            Reply("你来改。", (use("open_editor", "o1", path="章节/第01章.md"),)),
            Reply("保存了。"),
            Reply("再来。", (use("open_editor", "o2", path="章节/第01章.md"),)),
            Reply("没改。"),
        ]
    )

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 32)) as pilot:
            await _wait(app, pilot, lambda: "我们接着改？" in app.transcript_text())
            await _send(pilot, "我自己改")
            await _wait(app, pilot, lambda: isinstance(app.screen, EditorScreen))
            app.screen.query_one("#editor", TextArea).insert("作者补了一句。")
            await pilot.press("ctrl+s")
            await _wait(app, pilot, lambda: "保存了。" in app.transcript_text())
            assert "作者补了一句。" in Workspace(path).read_text("章节/第01章.md")
            assert "作者已保存" in app.transcript_text()
            await _send(pilot, "再开一次")
            await _wait(app, pilot, lambda: isinstance(app.screen, EditorScreen))
            await pilot.press("ctrl+q")
            await _wait(app, pilot, lambda: "没改。" in app.transcript_text())
            assert "取消了编辑" in app.transcript_text() and not isinstance(
                app.screen, EditorScreen
            )
            await pilot.press("ctrl+d")

    _run(scenario())


def test_new_directory_creates_work_and_slash_text_goes_to_agent(tmp_path):
    path = tmp_path / "寒灯协定"
    clients = scripted_clients(
        [Reply("新书。先说一句：这是个什么故事？"), Reply("没有斜杠命令，直接说就行。")]
    )

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 32)) as pilot:
            await _wait(app, pilot, lambda: "这是个什么故事" in app.transcript_text())
            assert Workspace(path).exists and Workspace(path).title == "寒灯协定"
            assert "已按目录名新建《寒灯协定》" in app.transcript_text()
            await _send(pilot, "/help")
            await _wait(app, pilot, lambda: "没有斜杠命令" in app.transcript_text())
            assert clients.calls[-1][-1].content == "/help"
            await pilot.press("ctrl+d")

    _run(scenario())


def test_without_model_opening_is_local(tmp_path):
    path = tmp_path / "novel"
    _chapter_workspace(path)

    async def scenario():
        app = WorkbenchApp(path, clients=None, config=None)
        async with app.run_test(size=(100, 32)) as pilot:
            await _wait(app, pilot, lambda: "模型尚未接入" in app.transcript_text())
            assert "已有 1 章正文" in app.transcript_text() and "模型未接入" in app.status_text
            await _send(pilot, "记一下")
            await _wait(app, pilot, lambda: "你的话已记录" in app.transcript_text())
            await pilot.press("ctrl+d")

    _run(scenario())


def test_colorize_diff_and_entrypoint_help(capsys):
    text = colorize_diff("--- a\n+++ b\n@@ -1 +1 @@\n-旧\n+新\n 同")
    styles = [(span.style, text.plain[span.start : span.end]) for span in text.spans]
    assert (
        ("red", "-旧") in styles and ("green", "+新") in styles and ("dim", "@@ -1 +1 @@") in styles
    )
    assert entry.main([]) == 0 and "作品目录" in capsys.readouterr().out
    assert entry.main(["somewhere"]) == 2  # 非交互终端


def test_reopen_replays_recent_conversation_without_a_model_call(tmp_path):
    path = tmp_path / "novel"
    ws = _chapter_workspace(path)
    ws.append_transcript("author", "给我看看第一章")
    ws.append_transcript("assistant", "以上是第一章全文。")
    clients = scripted_clients([Reply("接着改开头。")])

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 32)) as pilot:
            await _wait(app, pilot, lambda: "已有 1 章正文" in app.transcript_text())
            text = app.transcript_text()
            assert "── 上次的对话 ──" in text and "> 给我看看第一章" in text
            assert "⏺ 以上是第一章全文。" in text
            assert clients.calls == []  # 重新打开不叫模型
            await _send(pilot, "改开头")
            await _wait(app, pilot, lambda: "接着改开头。" in app.transcript_text())
            sent = clients.calls[0]
            assert [m.role for m in sent] == ["system", "user", "assistant", "user"]
            await pilot.press("ctrl+d")

    _run(scenario())


def test_clicking_transcript_does_not_steal_keyboard_focus(tmp_path):
    path = tmp_path / "novel"
    _chapter_workspace(path)

    async def scenario():
        app = WorkbenchApp(path, clients=None, config=None)
        async with app.run_test(size=(100, 32)) as pilot:
            await _wait(app, pilot, lambda: "模型尚未接入" in app.transcript_text())
            await pilot.click("#transcript", offset=(10, 5))
            await pilot.pause(0.1)
            await pilot.press("a")
            await pilot.pause(0.1)
            assert app.query_one("#input", PromptArea).text == "a"
            app.set_focus(None)  # 极端情况：焦点被清空
            await pilot.press("b")
            await pilot.pause(0.1)
            assert isinstance(app.focused, PromptArea)
            await pilot.press("ctrl+d")

    _run(scenario())


def test_chinese_wraps_between_characters_not_after_the_number():
    from textual._wrap import compute_wrap_offsets

    text = "⏺ 1. 严格保留前两段原封不动，这一段很长，用来测试中文换行。"
    breaks = compute_wrap_offsets(text, 20, 8)
    assert breaks and breaks[0] > len("⏺ 1. ")  # 不在编号后面整体挪行
    assert all(text[b] not in "，。" for b in breaks)  # 行首不出现收尾标点


def test_quiet_tool_outputs_are_collapsed_by_default(tmp_path):
    from story_agent.tui import ToolBlock

    path = tmp_path / "novel"
    _chapter_workspace(path)
    clients = scripted_clients(
        [
            *OPENING,
            Reply("看看。", (use("read", "r", path="章节/第01章.md"),)),
            Reply(
                "改。",
                (
                    use(
                        "edit",
                        "e",
                        path="章节/第01章.md",
                        reason="改",
                        changes=[{"old": "他没有下去。", "new": "他下去了。"}],
                    ),
                ),
            ),
            Reply("好了。"),
        ]
    )

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 32)) as pilot:
            await _wait(app, pilot, lambda: "我们接着改？" in app.transcript_text())
            await _send(pilot, "改结尾")
            await _wait(app, pilot, lambda: app.mode == "approve")
            await _send(pilot, "")
            await _wait(app, pilot, lambda: "好了。" in app.transcript_text())
            blocks = list(app.query(ToolBlock))
            assert [b.name for b in blocks] == ["read", "edit"]
            assert blocks[0].collapsed and not blocks[0].body().display
            assert not blocks[1].collapsed and blocks[1].body().display
            assert "点击展开" in str(blocks[0].query_one(".tool-footer", Static).content)
            await pilot.press("ctrl+o")
            await pilot.pause(0.1)
            assert app.verbose and not blocks[0].collapsed
            await pilot.press("ctrl+o")
            await pilot.pause(0.1)
            assert blocks[0].collapsed and not blocks[1].collapsed
            blocks[0].on_click()
            assert not blocks[0].collapsed
            await pilot.press("ctrl+d")

    _run(scenario())

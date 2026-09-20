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
            await pilot.pause(0.2)
            assert "命令：/help" in app.transcript_text() and len(clients.calls) == 1
            await _send(pilot, "/random")
            await _wait(app, pilot, lambda: "没有斜杠命令" in app.transcript_text())
            assert clients.calls[-1][-1].content.startswith("/random")
            await _send(pilot, "/exit")
            await pilot.pause(0.2)
            assert app.stopping.is_set()

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
    from story_agent.tui import PALETTE

    text = colorize_diff("--- a\n+++ b\n@@ -1 +1 @@\n-旧\n+新\n 同")
    styles = [(span.style, text.plain[span.start : span.end]) for span in text.spans]
    assert (PALETTE["red"], "-旧") in styles and (PALETTE["green"], "+新") in styles
    assert (PALETTE["muted"], "@@ -1 +1 @@") in styles
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
    from rich._wrap import divide_line

    status = "《寒灯协定》 · 已有 4 章正文 · 最近改动：今天 10:35 更新故事状态至第四章定稿状态。"
    breaks = divide_line(status, 70)
    assert (
        breaks
        and status[: breaks[0]].rstrip() != "《寒灯协定》 · 已有 4 章正文 · 最近改动：今天 10:35"
    )
    assert all(status[b] not in "，。" for b in breaks)


def test_static_status_line_uses_the_full_width(tmp_path):
    path = tmp_path / "novel"
    _chapter_workspace(path)

    async def scenario():
        app = WorkbenchApp(path, clients=None, config=None)
        async with app.run_test(size=(140, 30)) as pilot:
            await _wait(app, pilot, lambda: "已有 1 章正文" in app.transcript_text())
            await pilot.pause(0.2)
            from rich.cells import cell_len

            line = [w for w in app.query(".system") if "已有 1 章正文" in str(w.content)][-1]
            cells, width = cell_len(str(line.content)), line.region.width
            assert line.region.height == -(-cells // width)  # 行数正好是填满宽度所需的行数
            await pilot.press("ctrl+d")

    _run(scenario())


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
            assert [b.tool_name for b in blocks] == ["read", "edit"]
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


def test_page_keys_scroll_the_transcript_and_the_card(tmp_path):
    from textual.containers import VerticalScroll

    path = tmp_path / "novel"
    ws = _chapter_workspace(path)
    for i in range(40):
        ws.append_transcript("author", f"第 {i} 句")
        ws.append_transcript("assistant", f"回 {i}")
    long_new = "\n\n".join(f"第 {i} 段，很长的新段落。" for i in range(60))
    clients = scripted_clients(
        [
            Reply(
                "改。", (use("edit", "e", path="章节/第01章.md", reason="长改", content=long_new),)
            ),
            Reply("好了。"),
        ]
    )

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "已有 1 章正文" in app.transcript_text())
            transcript = app.query_one("#transcript", VerticalScroll)
            await pilot.pause(0.3)
            at_end = transcript.scroll_y
            assert at_end > 0
            await pilot.press("pageup")
            await pilot.pause(0.1)
            assert transcript.scroll_y < at_end
            await pilot.press("pagedown")
            await pilot.pause(0.1)
            assert transcript.scroll_y == at_end
            await _send(pilot, "重写")
            await _wait(app, pilot, lambda: app.mode == "approve")
            card = app.query_one("#card-diff", VerticalScroll)
            await pilot.pause(0.2)
            assert card.scroll_y == 0
            transcript_before = transcript.scroll_y
            await pilot.press("pagedown")
            await pilot.pause(0.1)
            assert card.scroll_y > 0 and transcript.scroll_y == transcript_before
            await _send(pilot, "n")
            await _wait(app, pilot, lambda: "好了。" in app.transcript_text())
            await pilot.press("ctrl+d")

    _run(scenario())


def test_ctrl_c_needs_a_second_press_to_quit(tmp_path, monkeypatch):
    from story_agent import tui

    path = tmp_path / "novel"
    _chapter_workspace(path)
    monkeypatch.setattr(tui, "QUIT_CONFIRM_SECONDS", 0.6)

    async def scenario():
        app = WorkbenchApp(path, clients=None, config=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "模型尚未接入" in app.transcript_text())
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            assert not app.stopping.is_set()
            assert "再按一次 Ctrl-C" in str(app.query_one("#hint-left", Static).content)
            await pilot.pause(0.8)  # 超时后提示恢复，再按一次仍不退出
            assert "再按一次" not in str(app.query_one("#hint-left", Static).content)
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            assert not app.stopping.is_set()
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            assert app.stopping.is_set()

    _run(scenario())


def test_new_file_card_lets_page_keys_scroll_the_transcript(tmp_path):
    from textual.containers import VerticalScroll

    path = tmp_path / "novel"
    _chapter_workspace(path)
    long_new = "# 第二章\n\n" + "\n\n".join(f"第 {i} 段，新章节的段落。" for i in range(80))
    clients = scripted_clients(
        [
            *OPENING,
            Reply(
                "写。",
                (use("edit", "e", path="章节/第02章.md", reason="写第二章", content=long_new),),
            ),
            Reply("好了。"),
        ]
    )

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "我们接着改？" in app.transcript_text())
            await _send(pilot, "写第二章")
            await _wait(app, pilot, lambda: app.mode == "approve")
            await pilot.pause(0.3)
            assert "PageUp 上翻阅读" in app.current_card.diff
            transcript = app.query_one("#transcript", VerticalScroll)
            before = transcript.scroll_y
            assert before > 0
            await pilot.press("pageup")
            await pilot.pause(0.1)
            assert transcript.scroll_y < before
            await _send(pilot, "")
            await _wait(app, pilot, lambda: "好了。" in app.transcript_text())
            await pilot.press("ctrl+d")

    _run(scenario())


def test_status_line_shows_estimated_cost_when_prices_are_known(tmp_path):
    from dataclasses import replace

    path = tmp_path / "novel"
    _chapter_workspace(path)
    clients = scripted_clients([Reply("好。")])

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=replace(CONFIG, prices=(1000.0, 1000.0)))
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "≈$" in app.status_text)
            assert "≈$0.01" in app.status_text  # 开场轮 7 tokens × $1000/M
            await pilot.press("ctrl+d")

    _run(scenario())


def test_error_mid_reply_keeps_one_rendered_copy_and_shows_the_reason(tmp_path):
    from textual.widgets import Markdown

    path = tmp_path / "novel"
    _chapter_workspace(path)
    clients = scripted_clients(
        [*OPENING, Reply("说到一半**就断了**。", error="APIStatusError · HTTP 429 · rate limited")]
    )

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "我们接着改？" in app.transcript_text())
            await _send(pilot, "世界观怎么样")
            await _wait(app, pilot, lambda: "HTTP 429" in app.transcript_text())
            await pilot.pause(0.2)
            rows = [r for r in app.query(".reply") if r.query(Markdown)]
            last = rows[-1]
            assert len(last.query(Markdown)) == 1
            leftovers = [
                w for w in last.children if isinstance(w, Static) and not w.has_class("dot")
            ]
            assert not leftovers  # 流式框已移除
            assert "请求失败（APIStatusError · HTTP 429 · rate limited）" in app.transcript_text()
            await pilot.press("ctrl+d")

    _run(scenario())


def test_dev_mode_shows_tool_args_and_call_stats(tmp_path):
    from story_agent.tui import ToolBlock

    path = tmp_path / "novel"
    _chapter_workspace(path)
    clients = scripted_clients(
        [*OPENING, Reply("看看。", (use("read", "r", path="章节/第01章.md"),)), Reply("看完了。")]
    )

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "我们接着改？" in app.transcript_text())
            await _send(pilot, "/dev")
            await pilot.pause(0.1)
            assert app.dev and "开发者模式已打开" in app.transcript_text()
            await _send(pilot, "看第一章")
            await _wait(app, pilot, lambda: "看完了。" in app.transcript_text())
            text = app.transcript_text()
            assert '参数 {"path": "章节/第01章.md"}' in text
            assert "⎿  模型完成 · ↑3 ↓4" in text
            block = list(app.query(ToolBlock))[-1]
            assert not block.collapsed  # 开发者模式下全部展开
            await _send(pilot, "/dev")
            await pilot.pause(0.1)
            assert not app.dev and block.collapsed
            await pilot.press("ctrl+d")

    _run(scenario())


def test_slash_opens_command_palette_with_filter_completion_and_execution(tmp_path):
    from textual.containers import Vertical

    path = tmp_path / "novel"
    _chapter_workspace(path)

    async def scenario():
        app = WorkbenchApp(path, clients=None, config=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "模型尚未接入" in app.transcript_text())
            area = app.query_one("#input", PromptArea)
            panel = app.query_one("#palette", Vertical)
            assert not panel.display
            await pilot.press("slash")
            await pilot.pause(0.1)
            assert panel.display and [c for c, _ in app.palette_items] == ["/help", "/dev", "/exit"]
            await pilot.press("e")
            await pilot.pause(0.1)
            assert [c for c, _ in app.palette_items] == ["/exit"]
            await pilot.press("backspace")
            await pilot.pause(0.1)
            assert len(app.palette_items) == 3 and app.palette_index == 0
            await pilot.press("down")
            await pilot.pause(0.05)
            assert app.palette_index == 1
            await pilot.press("tab")
            await pilot.pause(0.1)
            assert area.text == "/dev" and app.palette_open
            await pilot.press("escape")
            await pilot.pause(0.05)
            assert not panel.display and area.text == "/dev"
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert app.dev and area.text == ""
            await pilot.press("slash")
            await pilot.press("e")
            await pilot.pause(0.1)
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert app.stopping.is_set()

    _run(scenario())


def test_palette_only_when_slash_is_the_first_character(tmp_path):
    from textual.containers import Vertical

    path = tmp_path / "novel"
    _chapter_workspace(path)

    async def scenario():
        app = WorkbenchApp(path, clients=None, config=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "模型尚未接入" in app.transcript_text())
            area = app.query_one("#input", PromptArea)
            area.insert("他说")
            await pilot.press("slash")
            await pilot.pause(0.1)
            assert not app.query_one("#palette", Vertical).display and area.text == "他说/"
            await pilot.press("enter")  # 普通发送，不当命令
            await pilot.pause(0.1)
            assert "> 他说/" in app.transcript_text()
            await pilot.press("ctrl+d")

    _run(scenario())


def test_double_escape_rewinds_conversation_and_files(tmp_path):
    from textual.containers import Vertical

    path = tmp_path / "novel"
    _chapter_workspace(path)
    clients = scripted_clients([*OPENING, _edit_reply(), Reply("改好了。"), Reply("不该调用")])

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 34)) as pilot:
            await _wait(app, pilot, lambda: "我们接着改？" in app.transcript_text())
            await _send(pilot, "结尾让他下去")
            await _wait(app, pilot, lambda: app.mode == "approve")
            await _send(pilot, "")
            await _wait(app, pilot, lambda: "改好了。" in app.transcript_text())
            assert "他下去了。" in Workspace(path).read_text("章节/第01章.md")
            await pilot.press("escape")
            await pilot.press("escape")
            await pilot.pause(0.2)
            panel = app.query_one("#palette", Vertical)
            assert panel.display and app.palette_mode == "rewind"
            assert app.palette_items[0][1] == "结尾让他下去"
            await pilot.press("enter")
            await pilot.pause(0.1)
            assert app.palette_mode == "rewind-scope"
            await pilot.press("down")
            await pilot.press("enter")
            await _wait(app, pilot, lambda: "已撤回" in app.transcript_text())
            await pilot.pause(0.3)
            ws = Workspace(path)
            assert ws.read_text("章节/第01章.md") == CHAPTER
            assert ws.history(limit=1)[0].message.startswith("撤回到「结尾让他下去」")
            assert [r for r, _ in ws.all_transcript()] == ["assistant"]  # 只剩开场回复
            assert app.agent is not None and len(app.agent.transcript) == 0
            assert "文件已退回到那时" in app.transcript_text()
            assert not panel.display and app.palette_mode == "commands"
            assert app.query_one("#input", PromptArea).text == "结尾让他下去"  # 那句话回到输入框
            await pilot.press("ctrl+d")

    _run(scenario())


def test_status_line_has_a_context_gauge_that_warns_when_nearly_full(tmp_path):
    from dataclasses import replace

    from story_agent.tui import context_gauge

    assert context_gauge(42, 100) == ("▮▮▮▮▯▯▯▯▯▯ 42%", 0.42)
    assert context_gauge(0, 0) == ("", 0.0)
    assert context_gauge(300, 100)[0].startswith("▮" * 10)

    path = tmp_path / "novel"
    _chapter_workspace(path)
    tight = replace(
        CONFIG,
        context_tokens=2_600,
        template_reserve=100,
        options=replace(CONFIG.options, max_output_tokens=100),
    )

    async def scenario():
        app = WorkbenchApp(path, clients=None, config=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "%" in app.status_text)
            assert "▯" in app.status_text or "▮" in app.status_text
        app = WorkbenchApp(path, clients=scripted_clients([Reply("好。")]), config=tight)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(
                app, pilot, lambda: "%" in app.status_text and "好。" in app.transcript_text()
            )
            ratio = int(app.status_text.rsplit(" ", 1)[-1].rstrip("%"))
            assert ratio >= 85
            assert "快满了" not in app.status_text  # 红色就是提示，不加字

    _run(scenario())


def test_rewind_list_items_stay_on_one_line(tmp_path):
    path = tmp_path / "novel"
    ws = _chapter_workspace(path)
    ws.append_transcript(
        "author",
        "一个一个来吧，需要设计修炼的方式，以及前三章的剧情设计，还至少要加入真善美的主题，" * 3,
    )
    ws.append_transcript("assistant", "好。")
    ws.append_transcript("author", "短的一句")
    ws.append_transcript("assistant", "好。")

    async def scenario():
        app = WorkbenchApp(path, clients=None, config=None)
        async with app.run_test(size=(90, 30)) as pilot:
            await _wait(app, pilot, lambda: "已有 1 章正文" in app.transcript_text())
            await pilot.press("escape")
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert app.palette_mode == "rewind"
            items = list(app.query(".palette-item"))
            assert len(items) == 2
            assert all(item.region.height == 1 for item in items)
            assert str(items[0].content).startswith("❯ ") and "短的一句" in str(items[0].content)
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert app.palette_mode == "commands"
            await pilot.press("ctrl+d")

    _run(scenario())


def test_app_uses_the_story_theme_with_claude_orange(tmp_path):
    from story_agent.tui import PALETTE

    path = tmp_path / "novel"
    _chapter_workspace(path)

    async def scenario():
        app = WorkbenchApp(path, clients=None, config=None)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.1)
            assert app.theme == "story"
            assert app.current_theme.accent.lower() == PALETTE["orange"].lower()
            assert app.current_theme.ansi and app.current_theme.background == "ansi_default"
            assert app.native_ansi_color
            await pilot.press("ctrl+d")

    _run(scenario())


def test_clean_markdown_moves_punctuation_out_of_bold_and_drops_rules():
    from story_agent.tui import clean_markdown

    assert (
        clean_markdown("他有一股**“见不得好人遭罪”的硬骨头**。")
        == "他有一股“**见不得好人遭罪”的硬骨头**。"
    )
    assert clean_markdown("**本土化质感扎实**：细节") == "**本土化质感扎实**：细节"
    assert clean_markdown("**《寒灯协定》，**后面") == "《**寒灯协定**》，后面"
    assert clean_markdown("一\n---\n二\n* * *\n三") == "一\n二\n三"
    assert clean_markdown("列表\n- 项一\n- 项二") == "列表\n- 项一\n- 项二"


def test_reply_markdown_hides_rules_and_flattens_headings(tmp_path):
    from textual.widgets._markdown import MarkdownH3, MarkdownHorizontalRule

    path = tmp_path / "novel"
    _chapter_workspace(path)
    clients = scripted_clients([Reply("### 二、主题\n\n---\n\n一股**“硬骨头”**。\n\n- 一\n- 二")])

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "硬骨头" in app.transcript_text())
            await pilot.pause(0.3)
            assert not list(app.query(MarkdownHorizontalRule))  # 分隔线在渲染前就去掉了
            heading = app.query_one(MarkdownH3)
            assert heading.styles.text_style.bold and heading.region.height == 1
            assert "**" not in "".join(str(p.content) for p in app.query("MarkdownParagraph"))
            await pilot.press("ctrl+d")

    _run(scenario())


def test_nested_lists_render_without_blank_lines(tmp_path):
    from textual.widgets import Markdown

    path = tmp_path / "novel"
    _chapter_workspace(path)
    body = "标题\n\n1. 甲\n   - 乙\n   - 丙\n2. 丁\n\n结尾一句。"
    clients = scripted_clients([Reply(body)])

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "结尾一句" in app.transcript_text())
            await pilot.pause(0.3)
            markdown = list(app.query(Markdown))[-1]
            # 标题、空行、四个列表项、空行、结尾：一共 8 行，列表内部没有空行
            assert markdown.region.height == 8
            await pilot.press("ctrl+d")

    _run(scenario())


def test_transcript_has_no_scrollbar_and_only_follows_when_at_bottom(tmp_path):
    from textual.containers import VerticalScroll

    path = tmp_path / "novel"
    ws = _chapter_workspace(path)
    for i in range(30):
        ws.append_transcript("author", f"第 {i} 句")
        ws.append_transcript("assistant", f"回 {i}")
    clients = scripted_clients([Reply("新回复。")])

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 24)) as pilot:
            await _wait(app, pilot, lambda: "已有 1 章正文" in app.transcript_text())
            transcript = app.query_one("#transcript", VerticalScroll)
            assert transcript.styles.scrollbar_size_vertical == 0
            await pilot.pause(0.2)
            assert transcript.is_vertical_scroll_end and app.follow
            await pilot.press("pageup")
            await pilot.pause(0.1)
            above = transcript.scroll_y
            assert not app.follow and not transcript.is_vertical_scroll_end
            app.show_line("有人在下面说话")  # 新内容不把作者拽回底部
            await pilot.pause(0.2)
            assert transcript.scroll_y == above
            await _send(pilot, "接着来")  # 作者说话就回到底部
            await _wait(app, pilot, lambda: "新回复。" in app.transcript_text())
            await pilot.pause(0.2)
            assert app.follow and transcript.is_vertical_scroll_end
            await pilot.press("ctrl+d")

    _run(scenario())


def test_turn_ends_with_duration_and_finish_time(tmp_path):
    import re as _re

    from story_agent.tui import _duration

    assert _duration(43.2) == "43 秒" and _duration(125) == "2 分 5 秒"
    path = tmp_path / "novel"
    _chapter_workspace(path)
    clients = scripted_clients([Reply("开场。"), Reply("好。")])

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "开场。" in app.transcript_text())
            await _wait(app, pilot, lambda: "✻ 用时" in app.transcript_text())
            await _send(pilot, "你好")
            await _wait(app, pilot, lambda: "好。" in app.transcript_text())
            await pilot.pause(0.2)
            lines = [
                line for line in app.transcript_text().splitlines() if line.startswith("✻ 用时")
            ]
            assert len(lines) == 2
            assert _re.fullmatch(r"✻ 用时 \d+ 秒 · 完成于 \d\d:\d\d", lines[-1])
            await pilot.press("ctrl+d")

    _run(scenario())


def test_selection_colors_are_high_contrast_and_ctrl_c_copies(tmp_path, monkeypatch):
    from textual.screen import Screen

    from story_agent import tui

    path = tmp_path / "novel"
    _chapter_workspace(path)
    copied: list[str] = []
    monkeypatch.setattr(tui, "copy_text", lambda app, text: copied.append(text))
    cleared: list[bool] = []
    monkeypatch.setattr(Screen, "clear_selection", lambda self: cleared.append(True))

    async def scenario():
        app = WorkbenchApp(path, clients=None, config=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "已有 1 章正文" in app.transcript_text())
            variables = app.current_theme.variables
            assert variables["screen-selection-background"] == "ansi_bright_blue"
            assert variables["screen-selection-foreground"] == "ansi_black"
            monkeypatch.setattr(Screen, "get_selected_text", lambda self: "井底有一具尸体")
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            assert copied == ["井底有一具尸体"] and cleared  # 复制并清掉选中
            assert "已复制 7 个字" in str(app.query_one("#hint-left", Static).content)
            assert not app.stopping.is_set()
            monkeypatch.setattr(Screen, "get_selected_text", lambda self: None)
            await pilot.press("ctrl+c")  # 没有选中时照旧：第一次提示，第二次才退出
            await pilot.pause(0.1)
            assert "再按一次 Ctrl-C" in str(app.query_one("#hint-left", Static).content)
            assert not app.stopping.is_set()
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            assert app.stopping.is_set()

    _run(scenario())


def test_ctrl_c_in_the_editor_copies_but_never_quits(tmp_path, monkeypatch):
    from story_agent import tui

    path = tmp_path / "novel"
    _chapter_workspace(path)
    copied: list[str] = []
    monkeypatch.setattr(tui, "copy_text", lambda app, text: copied.append(text))
    clients = scripted_clients(
        [
            *OPENING,
            Reply("你来改。", (use("open_editor", "o", path="章节/第01章.md"),)),
            Reply("好。"),
        ]
    )

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "我们接着改？" in app.transcript_text())
            await _send(pilot, "我自己改")
            await _wait(app, pilot, lambda: isinstance(app.screen, EditorScreen))
            editor = app.screen.query_one("#editor", TextArea)
            editor.select_all()
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            assert copied and CHAPTER.strip() in copied[0]
            assert not app.stopping.is_set() and isinstance(app.screen, EditorScreen)
            editor.selection = editor.selection.__class__((0, 0), (0, 0))
            await pilot.press("ctrl+c")  # 没有选中也不退出，编辑中的文字不会丢
            await pilot.pause(0.1)
            assert not app.stopping.is_set() and isinstance(app.screen, EditorScreen)
            await pilot.press("ctrl+q")
            await _wait(app, pilot, lambda: "好。" in app.transcript_text())
            await pilot.press("ctrl+d")

    _run(scenario())


def test_compacting_notice_turns_into_its_result(tmp_path):
    path = tmp_path / "novel"
    ws = _chapter_workspace(path)
    for i in range(25):
        ws.append_transcript("author", f"第 {i} 句：" + "很长的话" * 500)
        ws.append_transcript("assistant", f"记下了 {i}。")
    clients = scripted_clients([Reply("接着聊。")], "作者说了很多长话。")

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "旧对话已整理成摘要。" in app.transcript_text())
            await pilot.pause(0.2)
            shown = [str(w.content) for w in app.query(".system")]
            assert "正在整理之前的对话……" not in shown  # 那一行已经变成结果，不会一直挂着
            assert "旧对话已整理成摘要。" in shown
            assert app.agent is not None and app.agent.summary == "作者说了很多长话。"
            assert not app.busy
            await pilot.press("ctrl+d")

    _run(scenario())


def test_escape_while_thinking_never_opens_the_rewind_list(tmp_path):
    from textual.containers import Vertical

    path = tmp_path / "novel"
    ws = _chapter_workspace(path)
    ws.append_transcript("author", "之前说过的一句")
    ws.append_transcript("assistant", "好。")
    started: list[bool] = []

    def slow(cancel):
        started.append(True)
        deadline = time.monotonic() + 5
        while not cancel.is_set() and time.monotonic() < deadline:
            time.sleep(0.05)

    clients = scripted_clients([Reply("想想……", hook=slow), Reply("不该出现。")])

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "已有 1 章正文" in app.transcript_text())
            await _send(pilot, "写点什么")
            await _wait(app, pilot, lambda: bool(started))
            for _ in range(4):  # 连按四次想让它停下来
                await pilot.press("escape")
                await pilot.pause(0.05)
            await _wait(app, pilot, lambda: "已中断" in app.transcript_text())
            await pilot.pause(0.3)
            assert not app.query_one("#palette", Vertical).display
            await pilot.press("escape")  # 冷却期内再按也不进入撤回
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert app.palette_mode == "commands"
            await pilot.press("ctrl+d")

    _run(scenario())


def test_first_escape_says_what_the_second_one_does_and_typing_cancels_it(tmp_path):
    from textual.containers import Vertical

    path = tmp_path / "novel"
    ws = _chapter_workspace(path)
    ws.append_transcript("author", "之前说过的一句")
    ws.append_transcript("assistant", "好。")

    async def scenario():
        app = WorkbenchApp(path, clients=None, config=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "已有 1 章正文" in app.transcript_text())
            await _wait(app, pilot, lambda: not app.busy)
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert "再按一次 Esc 撤回" in str(app.query_one("#hint-left", Static).content)
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert app.palette_mode == "rewind"
            app.query_one("#input", PromptArea).insert("我要说的话")  # 打字就收起列表
            await pilot.pause(0.1)
            assert app.palette_mode == "commands"
            assert not app.query_one("#palette", Vertical).display
            await pilot.press("enter")  # Enter 正常发言，不会误触撤回
            await pilot.pause(0.2)
            assert "> 我要说的话" in app.transcript_text()
            assert [r for r, _ in Workspace(path).all_transcript()] == [
                "author",
                "assistant",
                "author",
            ]
            await pilot.press("ctrl+d")

    _run(scenario())


def test_thinking_streams_dim_then_collapses_to_one_line(tmp_path):
    from story_agent.tui import ThinkingBlock

    path = tmp_path / "novel"
    _chapter_workspace(path)
    clients = scripted_clients(
        [Reply("开场。"), Reply("答案在这里。", thinking="先想一想这个世界靠什么活着。")]
    )

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "开场。" in app.transcript_text())
            await _send(pilot, "聊聊产业")
            await _wait(app, pilot, lambda: "答案在这里。" in app.transcript_text())
            await pilot.pause(0.3)
            blocks = list(app.query(ThinkingBlock))
            assert len(blocks) == 1
            block = blocks[0]
            assert block.text == "先想一想这个世界靠什么活着。"  # 思考内容留着，能展开看
            assert block.collapsed  # 回答开始后收起
            assert "✻ 思考了" in str(block.query_one(".thinking-header", Static).content)
            # 思考排在回复之前，且不算进回复正文
            body = app.transcript_text()
            assert body.index("✻ 思考了") < body.index("答案在这里。")
            assert "先想一想" not in [t for _, t in Workspace(path).all_transcript()][-1]
            await pilot.press("ctrl+o")
            await pilot.pause(0.1)
            assert not block.collapsed  # Ctrl-O 展开
            block.on_click()
            assert block.collapsed  # 点一下收起
            await pilot.press("ctrl+d")

    _run(scenario())

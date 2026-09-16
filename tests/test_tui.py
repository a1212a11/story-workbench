"""Textual 对话式工作台：作者输入 → 引擎线程 → 界面回显；问题卡、编辑器、取消与退出。"""

from __future__ import annotations

import asyncio
import threading
import time

from textual.widgets import TextArea

from story_workbench.application import authoring
from story_workbench.application.projects import create_project
from story_workbench.tui.app import EditorScreen, PromptArea, WorkbenchApp
from tests.test_assistant import BODY, Reply, scripted_clients, use
from tests.test_workflow import CONFIG, inspection_json


def _prepared_project(path, plan=True):
    project = create_project(path, "井下回声")
    chapter = authoring.create_chapter(project, 1)
    if plan:
        msg = authoring.record_message(project, "计划", chapter.id)
        from story_workbench.domain.records import Expected

        authoring.save_plan(project, chapter.id, "调查旧井", "看到尸体", Expected(), msg)
    project.close()
    return chapter


async def _wait(app: WorkbenchApp, pilot, predicate, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause(0.05)
        if predicate():
            return
    raise AssertionError(f"等待超时。当前对话：\n{app.transcript_text()}")


async def _send(pilot, text: str) -> None:
    """中文经输入法进入输入框，不是逐键；测试直接插入文本再按 Enter。"""
    pilot.app.query_one("#input", PromptArea).insert(text)
    await pilot.pause(0.05)
    await pilot.press("enter")


def _run(coro) -> None:
    asyncio.run(coro)


def test_author_message_drives_tools_and_streams_body(tmp_path):
    path = tmp_path / "novel"
    _prepared_project(path)
    clients, _ = scripted_clients(
        [
            Reply("", (use("write_chapter", chapter=1, mode="formal"),)),
            Reply(BODY),
            Reply("写完了。"),
        ],
        inspection_json(BODY, plot="调查旧井", ending="看到尸体"),
    )

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "《井下回声》" in app.transcript_text())
            await _wait(app, pilot, lambda: "fixed" in app.status_text)
            await _send(pilot, "写第一章")
            await _wait(app, pilot, lambda: "写完了。" in app.transcript_text())
            text = app.transcript_text()
            assert "> 写第一章" in text and BODY in text and "生成结束" in text
            assert "⏺ 写作(第一章 · 正式)" in text and "⎿  完成" in text
            await _wait(app, pilot, lambda: "↑" in app.status_text and "↑0 " not in app.status_text)
            # 工具输出可折叠
            from story_workbench.tui.app import ToolBlock

            block = app.query_one(ToolBlock)
            assert block.body().display
            await pilot.press("ctrl+o")
            assert not block.body().display
            await pilot.press("ctrl+d")

    _run(scenario())
    from story_workbench.application.projects import open_project

    project, _ = open_project(path)
    try:
        chapter = authoring.list_chapters(project)[0]
        assert [v.body for v in authoring.list_versions(project, chapter.id)] == [BODY]
    finally:
        project.close()


def test_question_card_collects_read_declaration_for_adoption(tmp_path):
    path = tmp_path / "novel"
    _prepared_project(path)
    clients, _ = scripted_clients(
        [
            Reply("", (use("write_chapter", chapter=1, mode="formal"),)),
            Reply(BODY),
            Reply("写完了。"),
            Reply("", (use("adopt_draft", "t2", chapter=1),)),
            Reply("已采用。"),
        ],
        inspection_json(BODY, plot="调查旧井", ending="看到尸体"),
    )

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "《井下回声》" in app.transcript_text())
            await _send(pilot, "写第一章")
            await _wait(app, pilot, lambda: "写完了。" in app.transcript_text())
            await _send(pilot, "采用")
            await _wait(app, pilot, lambda: app.mode == "answer")
            assert "输入「已读，采用」" in str(app.query_one("#question").content)
            await _send(pilot, "已读，采用")
            await _wait(app, pilot, lambda: "已采用。" in app.transcript_text())
            assert "已采用第一章这一稿，故事状态更新完成。" in app.transcript_text()
            assert app.mode == "chat"
            await pilot.press("ctrl+d")

    _run(scenario())


def test_slash_edit_opens_editor_and_saves(tmp_path):
    path = tmp_path / "novel"
    _prepared_project(path, plan=False)

    async def scenario():
        app = WorkbenchApp(path, clients=None, config=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "未检测到模型密钥" in app.transcript_text())
            await _send(pilot, "/go 1")
            await _wait(app, pilot, lambda: "现在在第一章" in app.transcript_text())
            await _send(pilot, "/edit")
            await _wait(app, pilot, lambda: isinstance(app.screen, EditorScreen))
            app.screen.query_one("#editor", TextArea).insert("第一版。")
            await pilot.pause(0.05)
            await pilot.press("ctrl+s")
            await _wait(
                app, pilot, lambda: "已保存，这是第一章现在的编辑稿" in app.transcript_text()
            )
            await _send(pilot, "/edit")
            await _wait(app, pilot, lambda: isinstance(app.screen, EditorScreen))
            await pilot.press("ctrl+q")
            await _wait(app, pilot, lambda: "已取消编辑，正文没有改变。" in app.transcript_text())
            # 没有模型时自然语言只记录
            await _send(pilot, "写第一章")
            await _wait(app, pilot, lambda: "模型尚未接入，你的话已记录" in app.transcript_text())
            await pilot.press("ctrl+d")

    _run(scenario())
    from story_workbench.application.projects import open_project

    project, _ = open_project(path)
    try:
        chapter = authoring.list_chapters(project)[0]
        assert [v.body for v in authoring.list_versions(project, chapter.id)] == ["第一版。"]
    finally:
        project.close()


def test_escape_cancels_model_task_after_persisting_cancel(tmp_path):
    path = tmp_path / "novel"
    _prepared_project(path, plan=False)
    escaped = threading.Event()

    def wait_for_escape(cancel):
        escaped.wait(timeout=5)

    clients, _ = scripted_clients([Reply("先写一段很长的话。", hook=wait_for_escape)])

    async def scenario():
        app = WorkbenchApp(path, clients=clients, config=CONFIG)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: "《井下回声》" in app.transcript_text())
            await _send(pilot, "随便聊聊")
            await _wait(app, pilot, lambda: "先写一段" in app.transcript_text())
            await pilot.press("escape")
            escaped.set()
            await _wait(app, pilot, lambda: "助手没有完成回复" in app.transcript_text())
            assert "取消已记录" in app.transcript_text()
            await pilot.press("ctrl+d")

    _run(scenario())
    from story_workbench.application.projects import open_project
    from story_workbench.domain.records import RunStatus

    project, _ = open_project(path)
    try:
        rows = project.conn.execute("SELECT task, status FROM run").fetchall()
        assert [(r["task"], RunStatus(r["status"])) for r in rows] == [
            ("assist", RunStatus.CANCELLED)
        ]
    finally:
        project.close()


def test_new_project_asks_title_through_question_card(tmp_path):
    path = tmp_path / "fresh"

    async def scenario():
        app = WorkbenchApp(path, clients=None, config=None)
        async with app.run_test(size=(100, 30)) as pilot:
            await _wait(app, pilot, lambda: app.mode == "answer")
            assert "作品标题" in str(app.query_one("#question").content)
            await _send(pilot, "寒灯协定")
            await _wait(app, pilot, lambda: "《寒灯协定》" in app.transcript_text())
            await pilot.press("ctrl+d")

    _run(scenario())
    assert (path / "story.sqlite3").exists()

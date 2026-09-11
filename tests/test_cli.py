"""作者交互入口：编辑器能力验证（P03）与 CLI 流程（C01/C02）。

编辑器与 CLI 都通过 prompt_toolkit 的管道输入和空输出驱动，不需要真实终端。
输入法（IME）体验无法在此自动化，留待人工 T05 验证。
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import PipeInput, create_pipe_input
from prompt_toolkit.output import DummyOutput

from story_workbench import cli
from story_workbench.__main__ import main
from story_workbench.application import authoring
from story_workbench.application.projects import Project, open_project
from story_workbench.cli_editor import StreamPane, build_editor, edit_text
from story_workbench.domain.records import ErrorCode, Expected, WorkbenchError
from story_workbench.infrastructure import database

ENTER = "\r"
CTRL_S = "\x13"
CTRL_Q = "\x11"
CTRL_C = "\x03"
LEFT = "\x1b[D"
HOME = "\x1b[H"
END = "\x1b[F"


def bracketed_paste(text: str) -> str:
    """终端粘贴时发送的括号粘贴序列；prompt_toolkit 会整体插入。"""
    return "\x1b[200~" + text + "\x1b[201~"


@pytest.fixture
def pipe() -> Iterator[PipeInput]:
    """把管道输入和空输出绑定为当前会话；测试内所有 prompt_toolkit 应用共用。"""
    with create_pipe_input() as pipe_input:
        with create_app_session(input=pipe_input, output=DummyOutput()):
            yield pipe_input


# ---------------------------------------------------------------- P03 编辑器能力


def test_editor_accepts_multiline_chinese_and_saves(pipe):
    pipe.send_text("沈砚把灯搁在井沿，顺着绳子往下。" + ENTER + "井壁下蜷着一个人。" + CTRL_S)
    assert edit_text("", "第一章") == "沈砚把灯搁在井沿，顺着绳子往下。\n井壁下蜷着一个人。"


def test_editor_accepts_pasted_multiline_chinese_at_once(pipe):
    pasted = "门板贴着耳侧，凉得像井壁。\r\n他直起身。\r那声音太短。"
    pipe.send_text(bracketed_paste(pasted) + CTRL_S)
    assert edit_text("", "第三章") == "门板贴着耳侧，凉得像井壁。\n他直起身。\n那声音太短。"


def test_editor_cursor_moves_then_inserts_in_the_middle(pipe):
    # 光标初始在正文开头；End 到行尾，左移两格，再插入。
    pipe.send_text(END + LEFT + LEFT + "慢慢" + CTRL_S)
    assert edit_text("沈砚下井", "第一章") == "沈砚慢慢下井"
    pipe.send_text(HOME + "夜里，" + CTRL_S)
    assert edit_text("沈砚下井", "第一章") == "夜里，沈砚下井"


def test_editor_save_returns_modified_text_keeping_untouched_lines(pipe):
    original = "第一段。\n第二段。"
    pipe.send_text(END + "他停了一会儿。" + CTRL_S)
    assert edit_text(original, "第二章") == "第一段。他停了一会儿。\n第二段。"


def test_editor_cancel_returns_none_and_leaves_original_untouched(pipe):
    original = "他把灯搁在脚边。"
    pipe.send_text(END + "侧耳贴近门板。" + CTRL_Q)
    assert edit_text(original, "第二章") is None
    assert original == "他把灯搁在脚边。"


def test_editor_hints_are_text_not_only_color():
    app = build_editor("", "第一章")
    texts = []
    for control in app.layout.find_all_controls():
        value = getattr(control, "text", None)
        if value is not None:
            texts.append(value() if callable(value) else value)
    joined = "".join(str(t) for t in texts)
    assert "Ctrl-S" in joined and "保存并返回" in joined
    assert "Ctrl-Q" in joined and "取消编辑" in joined
    assert "编辑：第一章" in joined


# ---------------------------------------------------------------- P03 流式输出与输入共存


def test_stream_output_keeps_editor_input_and_cancel_responsive(pipe):
    """异步流每 10ms 追加一段中文到显示区时，按键仍被处理，取消信号能停止流。"""

    async def scenario() -> tuple[str | None, list[str], asyncio.Event, StreamPane]:
        cancelled = asyncio.Event()
        pane = StreamPane("AI 输出", on_cancel=cancelled.set)
        app = build_editor("", "第一章", stream=pane)
        produced: list[str] = []

        async def producer() -> None:
            for i in range(500):
                if cancelled.is_set():
                    return
                chunk = f"第{i}段。"
                produced.append(chunk)
                pane.append(chunk)
                await asyncio.sleep(0.01)

        async def author() -> None:
            await asyncio.sleep(0.05)
            pipe.send_text("沈砚")
            await asyncio.sleep(0.05)
            pipe.send_text(CTRL_C)
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            await asyncio.sleep(0.05)
            pipe.send_text("下井" + CTRL_S)

        running = asyncio.create_task(app.run_async())
        stream = asyncio.create_task(producer())
        await author()
        result = await asyncio.wait_for(running, timeout=2)
        await asyncio.wait_for(stream, timeout=2)
        return result, produced, cancelled, pane

    result, produced, cancelled, pane = asyncio.run(scenario())
    assert result == "沈砚下井"
    assert cancelled.is_set()
    assert 3 <= len(produced) < 500
    assert pane.text == "".join(produced)
    assert pane.text.startswith("第0段。第1段。")


# ---------------------------------------------------------------- C01/C02 CLI 流程


def run_cli(pipe: PipeInput, path: Path, keystrokes: str) -> str:
    """把整段按键预先送进管道后运行 CLI；未处理的按键会顺延给下一个提示或编辑器。"""
    lines: list[str] = []
    pipe.send_text(keystrokes)
    assert cli.run(path, say=lines.append) == 0
    return "\n".join(lines)


def reopen(path: Path) -> Iterator[Project]:
    project, _ = open_project(path)
    try:
        yield project
    finally:
        project.close()


def bodies(path: Path) -> list[str]:
    for project in reopen(path):
        chapter = authoring.list_chapters(project)[0]
        return [v.body for v in authoring.list_versions(project, chapter.id)]
    return []


NEW_NOVEL = "井下回声" + ENTER + "n" + ENTER


def test_cli_creates_project_plans_edits_and_shows_body(pipe, tmp_path):
    path = tmp_path / "novel"
    script = "".join(
        [
            NEW_NOVEL,
            "t" + ENTER + "第一章写沈砚调查旧井。" + ENTER,
            "p" + ENTER + "沈砚下井发现尸体" + ENTER + "沈砚在井底，怀疑尸体是师父" + ENTER,
            "e" + ENTER + "沈砚把灯搁在井沿。" + ENTER + "井壁下蜷着一个人。" + CTRL_S,
            "e" + ENTER + END + "这一句不该保存。" + CTRL_Q,
            "v" + ENTER,
            "q" + ENTER,
        ]
    )
    out = run_cli(pipe, path, script)
    assert "这里还没有作品" in out
    assert "已新建第一章" in out
    assert "已记录你的话" in out and "AI 讨论尚未接入" in out
    assert "剧情与结尾已记录（第 1 次）" in out
    assert "已保存，这是第一章现在的编辑稿" in out
    assert "已取消编辑，正文没有改变" in out
    assert "第一章 · 修改稿 · 未采用\n\n沈砚把灯搁在井沿。\n井壁下蜷着一个人。" in out
    assert "这一句不该保存" not in out
    assert "已退出" in out
    for project in reopen(path):
        assert project.novel.title == "井下回声"
        chapters = authoring.list_chapters(project)
        assert [c.number for c in chapters] == [1]
        assert chapters[0].current_plan_id is not None
    assert bodies(path) == ["沈砚把灯搁在井沿。\n井壁下蜷着一个人。"]


def test_cli_reopens_existing_project_and_restores_history(pipe, tmp_path):
    path = tmp_path / "novel"
    run_cli(pipe, path, NEW_NOVEL + "e" + ENTER + "第一版。" + CTRL_S + "q" + ENTER)
    script = "".join(
        [
            "c" + ENTER + "1" + ENTER,
            "e" + ENTER + END + "第二版补充。" + CTRL_S,
            "r" + ENTER + "1" + ENTER,
            "v" + ENTER,
            "q" + ENTER,
        ]
    )
    out = run_cli(pipe, path, script)
    assert "已打开《井下回声》" in out
    assert "现在在第一章" in out
    assert "第一章的历史版本：" in out
    assert "修改稿 · 现在的编辑稿" in out
    assert "已用这个版本创建恢复稿" in out
    assert "第一章 · 恢复稿 · 未采用\n\n第一版。" in out
    assert bodies(path) == ["第一版。", "第一版。第二版补充。", "第一版。"]


def test_cli_keeps_buffer_when_save_fails_and_allows_retry(pipe, tmp_path, monkeypatch):
    original = database.commit_version
    failures = {"left": 2}

    def flaky(*args, **kwargs):
        if failures["left"] > 0:
            failures["left"] -= 1
            raise WorkbenchError(ErrorCode.STORAGE_FAILED, "磁盘暂时不可写。")
        return original(*args, **kwargs)

    monkeypatch.setattr(database, "commit_version", flaky)
    path = tmp_path / "novel"
    script = "".join(
        [
            NEW_NOVEL,
            "e" + ENTER + "作者刚写的一段。" + CTRL_S,  # 第一次保存失败
            "e" + ENTER + END + "回到编辑器再补一句。" + CTRL_S,  # 缓冲仍在编辑器里
            "r" + ENTER,  # 第二次失败后重试，成功
            "v" + ENTER + "q" + ENTER,
        ]
    )
    out = run_cli(pipe, path, script)
    assert out.count("保存没有成功：磁盘暂时不可写。 你刚写的文字还在") == 2
    assert "已保存，这是第一章现在的编辑稿" in out
    assert bodies(path) == ["作者刚写的一段。回到编辑器再补一句。"]


def test_cli_abandon_after_save_failure_leaves_previous_draft(pipe, tmp_path, monkeypatch):
    path = tmp_path / "novel"
    run_cli(pipe, path, NEW_NOVEL + "e" + ENTER + "旧稿。" + CTRL_S + "q" + ENTER)

    def boom(*args, **kwargs):
        raise WorkbenchError(ErrorCode.STORAGE_FAILED, "磁盘故障。")

    monkeypatch.setattr(database, "commit_version", boom)
    script = "c" + ENTER + "1" + ENTER + "e" + ENTER + END + "新句。" + CTRL_S + "q" + ENTER
    out = run_cli(pipe, path, script + "q" + ENTER)
    assert "你刚写的文字还在" in out
    assert "已放弃这次修改；之前保存过的正文没有改变" in out
    monkeypatch.undo()
    assert bodies(path) == ["旧稿。"]


def test_cli_conflicting_save_keeps_both_drafts_and_lets_author_pick(pipe, tmp_path, monkeypatch):
    """编辑期间当前稿被换掉时，作者稿保存为另一稿；选稿一后成为编辑稿。"""
    path = tmp_path / "novel"
    run_cli(pipe, path, NEW_NOVEL + "e" + ENTER + "基准稿。" + CTRL_S + "q" + ENTER)
    real_edit = cli.edit_text
    seen: dict[str, object] = {}

    class RacingWorkbench(cli.Workbench):
        def edit_body(self):
            seen["project"], seen["chapter"] = self.project, self.refresh()
            return super().edit_body()

    def edit_and_race(initial: str, title: str):
        chapter = seen["chapter"]
        authoring.save_draft(
            seen["project"],
            chapter.id,
            "别处保存的稿。",
            Expected(current_draft_id=chapter.current_draft_id),
        )
        return real_edit(initial, title)

    monkeypatch.setattr(cli, "Workbench", RacingWorkbench)
    monkeypatch.setattr(cli, "edit_text", edit_and_race)
    script = "".join(
        [
            "c" + ENTER + "1" + ENTER,
            "e" + ENTER + END + "我的修改。" + CTRL_S,
            "1" + ENTER,
            "v" + ENTER + "q" + ENTER,
        ]
    )
    out = run_cli(pipe, path, script)
    assert "两稿都在" in out
    assert "稿一 · 你刚保存的修改稿：\n基准稿。我的修改。" in out
    assert "稿二 · 现在的编辑稿：\n别处保存的稿。" in out
    assert "已把你的修改稿作为现在的编辑稿" in out
    assert "第一章 · 恢复稿 · 未采用\n\n基准稿。我的修改。" in out
    assert bodies(path) == [
        "基准稿。",
        "别处保存的稿。",
        "基准稿。我的修改。",
        "基准稿。我的修改。",
    ]


def test_cli_adopt_is_placeholder_and_unknown_command_is_explained(pipe, tmp_path):
    script = "井下回声" + ENTER + "a" + ENTER + "xyz" + ENTER + "e" + ENTER + "q" + ENTER
    out = run_cli(pipe, tmp_path / "novel", script)
    assert "采用功能尚未接入" in out
    assert "不认识「xyz」" in out
    assert "还没有选中章节" in out


def test_main_without_terminal_exits_two_and_names_directory(tmp_path, capsys):
    assert main([str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "交互终端" in err and str(tmp_path) in err
    assert not any(tmp_path.iterdir())

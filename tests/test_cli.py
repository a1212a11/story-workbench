"""作者交互入口：编辑器能力验证（P03）与 CLI 流程（C01–C03）。

编辑器与 CLI 都通过 prompt_toolkit 的管道输入和空输出驱动，不需要真实终端；
写作、检查与讨论用固定客户端函数注入，不绕过任务解析与证据校验。
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
from story_workbench.ai.context import ContextLimits
from story_workbench.application import authoring, generation
from story_workbench.application.projects import Project, open_project
from story_workbench.cli_editor import StreamPane, build_editor, edit_text
from story_workbench.domain.records import ErrorCode, Expected, Lifecycle, WorkbenchError
from story_workbench.infrastructure import database
from story_workbench.infrastructure.provider import CallOptions
from tests.conftest import fixed_complete, fixed_stream
from tests.test_workflow import belief_fact, inspection_json, setting_issue

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


CONFIG = generation.GenerationConfig(
    options=CallOptions(model="fixed", max_output_tokens=2000, timeout_seconds=5),
    limits=ContextLimits(total_tokens=20000, template_reserve=500, output_reserve=2000),
)


def run_cli(
    pipe: PipeInput,
    path: Path,
    keystrokes: str,
    *,
    clients: generation.Clients | None = None,
    emit: list[str] | None = None,
) -> str:
    """把整段按键预先送进管道后运行 CLI；未处理的按键会顺延给下一个提示或编辑器。"""
    lines: list[str] = []
    pipe.send_text(keystrokes)
    config = CONFIG if clients is not None else None
    chunks = emit if emit is not None else []
    assert cli.run(path, say=lines.append, clients=clients, config=config, emit=chunks.append) == 0
    return "\n".join(lines)


def sequenced_clients(*rounds: tuple[str, str]) -> generation.Clients:
    """每次生成依次返回下一份正文（分两块流式输出），每次检查依次返回下一份检查结果。"""
    bodies = [body for body, _ in rounds]
    checks = [check for _, check in rounds]

    def stream(messages, options, cancel):
        body = bodies.pop(0)
        yield from fixed_stream([body[:5], body[5:]])(messages, options, cancel)

    def complete(messages, options, cancel):
        return fixed_complete(checks.pop(0))(messages, options, cancel)

    return generation.Clients(stream=stream, complete=complete)


def count(path: Path, table: str) -> int:
    for project in reopen(path):
        return project.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return -1


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
    assert "已记录你的话" in out and "模型尚未接入" in out
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
    assert out.count("已把这段文字另存为恢复文件：") == 2
    assert "已保存，这是第一章现在的编辑稿" in out
    assert bodies(path) == ["作者刚写的一段。回到编辑器再补一句。"]
    recovered = (path / "recovery").glob("chapter-001-*.md")
    assert {f.read_text(encoding="utf-8") for f in recovered} == {
        "作者刚写的一段。",
        "作者刚写的一段。回到编辑器再补一句。",
    }


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


def test_cli_without_model_explains_and_unknown_command_is_explained(pipe, tmp_path):
    script = "".join(
        [
            "井下回声" + ENTER + "a" + ENTER + "xyz" + ENTER + "e" + ENTER,
            "n" + ENTER + "w" + ENTER + "k" + ENTER + "a" + ENTER + "i" + ENTER,
            "q" + ENTER,
        ]
    )
    out = run_cli(pipe, tmp_path / "novel", script)
    assert out.count("还没有选中章节") == 2
    assert "不认识「xyz」" in out
    assert "模型尚未接入，暂时不能写作" in out
    assert "模型尚未接入，暂时不能检查" in out
    assert "第一章没有可以采用的稿件" in out
    assert "第一章还没有正文" in out


def test_main_without_terminal_exits_two_and_names_directory(tmp_path, capsys):
    assert main([str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "交互终端" in err and str(tmp_path) in err
    assert not any(tmp_path.iterdir())


# ---------------------------------------------------------------- C03 写作、检查与采用

PLAN = "p" + ENTER + "沈砚下井发现尸体" + ENTER + "沈砚在井底，怀疑尸体是师父" + ENTER
ADOPT = "a" + ENTER + "已读，采用" + ENTER
BODY_1 = "沈砚把灯搁在井沿。井底有一具尸体，他怀疑是师父。"
CHECK_1 = inspection_json(
    BODY_1,
    facts=[belief_fact(BODY_1, "他怀疑是师父", "沈砚", "沈砚怀疑尸体是师父")],
    plot="沈砚下井发现尸体",
    ending="沈砚在井底，怀疑尸体是师父",
)


def test_cli_plan_write_review_adopt_and_state_ready(pipe, tmp_path):
    """计划→写→查看检查→采用→状态完成；已读声明缺失不采用；同一稿重复采用提示无需重复。"""
    path = tmp_path / "novel"
    chunks: list[str] = []
    script = "".join(
        [
            NEW_NOVEL + PLAN,
            "w" + ENTER + ENTER,  # 无额外要求
            "i" + ENTER,
            "a" + ENTER + ENTER,  # 回车取消，没有声明已读
            ADOPT,
            ADOPT,  # 同一稿再次采用
            "l" + ENTER + "q" + ENTER,
        ]
    )
    out = run_cli(pipe, path, script, clients=sequenced_clients((BODY_1, CHECK_1)), emit=chunks)
    assert chunks == [BODY_1[:5], BODY_1[5:], "\n"]
    assert "开始写作，输出会逐段显示" in out
    assert "生成结束。以下是完整正文：\n第一章 · 写作稿 · 未采用\n\n" + BODY_1 in out
    assert "实际剧情：沈砚下井发现尸体" in out
    assert "未发现需要处理的问题" in out
    assert "人物认知 · 沈砚：沈砚怀疑尸体是师父（原文：「他怀疑是师父」）" in out
    assert "第一章 · 写作稿 · 未采用 的最新检查" in out
    assert "没有采用。采用需要明确声明已读全文并采用" in out
    assert "已采用第一章这一稿，故事状态更新完成" in out
    assert "当前稿已采用，无需重复操作" in out
    assert "第一章 · 有正文 · 已有剧情与结尾 · 已采用" in out
    assert count(path, "adoption") == 1
    for project in reopen(path):
        chapter = authoring.list_chapters(project)[0]
        assert chapter.adopted_id is not None and chapter.current_snapshot_id is not None
        assert authoring.adopted_version_id(project, chapter.id) == chapter.current_draft_id


def test_cli_rejects_overreaching_draft_then_rewrites_and_adopts(pipe, tmp_path):
    """越界稿：不接受新增设定→列出未处理问题、零采用；拒绝→重写→采用。"""
    path = tmp_path / "novel"
    bad = "沈砚从袖中抽出细铁丝，凭着早年练熟的手艺拨动锁簧。"
    bad_check = inspection_json(
        bad,
        issues=[setting_issue(bad, "从袖中抽出细铁丝", "新增开锁能力与工具")],
        plot="开锁",
        ending="门外",
    )
    script = "".join(
        [
            NEW_NOVEL + PLAN,
            "w" + ENTER + ENTER,
            "a" + ENTER + "已读，采用" + ENTER + "不接受" + ENTER,
            "j" + ENTER + "不加开锁能力" + ENTER,
            "w" + ENTER + ENTER,
            ADOPT,
            "q" + ENTER,
        ]
    )
    clients = sequenced_clients((bad, bad_check), (BODY_1, CHECK_1))
    out = run_cli(pipe, path, script, clients=clients)
    assert "问题 1 · 新增持久设定：新增开锁能力与工具\n    原文：「从袖中抽出细铁丝」" in out
    assert "    建议：删除该段。" in out
    assert "这一稿有未处理的问题，没有采用：\n  · 新增持久设定：新增开锁能力与工具" in out
    assert "这一稿已标记拒绝，不会进入正式故事记录" in out
    assert "这一章现在没有编辑稿" in out
    assert "已采用第一章这一稿，故事状态更新完成" in out
    assert count(path, "adoption") == 1
    for project in reopen(path):
        chapter = authoring.list_chapters(project)[0]
        adopted = authoring.adopted_version_id(project, chapter.id)
        versions = {v.id: v.body for v in authoring.list_versions(project, chapter.id)}
        assert versions[adopted] == BODY_1
        assert database.list_rejected_version_ids(project.conn, project.novel_id) == {
            next(vid for vid, body in versions.items() if body == bad)
        }


def test_cli_authorizes_new_setting_in_one_action(pipe, tmp_path):
    body = "沈砚从袖中抽出细铁丝，拨动锁簧。"
    check = inspection_json(
        body,
        issues=[setting_issue(body, "从袖中抽出细铁丝", "新增开锁能力")],
        plot="开锁",
        ending="门外",
    )
    script = NEW_NOVEL + PLAN + "w" + ENTER + ENTER + ADOPT + "接受" + ENTER + "q" + ENTER
    out = run_cli(pipe, tmp_path / "novel", script, clients=sequenced_clients((body, check)))
    assert "已采用第一章这一稿，故事状态更新完成" in out
    for project in reopen(tmp_path / "novel"):
        rows = project.conn.execute("SELECT action, explanation FROM resolution").fetchall()
        assert [(r["action"], r["explanation"]) for r in rows] == [
            ("authorize_setting", "作者接受")
        ]


def test_cli_exploratory_draft_requires_accepting_plot_and_ending(pipe, tmp_path):
    path = tmp_path / "novel"
    body = "门板贴着耳侧。他轻轻敲了三下。门内没有回答。"
    check = inspection_json(
        body,
        facts=[belief_fact(body, "门内没有回答", "沈砚", "敲门后没有回应")],
        plot="敲门",
        ending="门外等待",
    )
    script = "".join(
        [
            NEW_NOVEL,
            "w" + ENTER + ENTER + ENTER,  # 正式写作缺两项，留空 → 不开始
            "x" + ENTER + "门内有轻微声响" + ENTER,
            ADOPT + ENTER + ENTER + "放弃" + ENTER,
            ADOPT + "听见声响后敲门" + ENTER + ENTER + "接受" + ENTER,
            "q" + ENTER,
        ]
    )
    out = run_cli(pipe, path, script, clients=sequenced_clients((body, check)))
    assert "正式写作需要本章剧情和结尾状态，现在还缺剧情和结尾状态" in out
    assert "仍有缺项，没有开始写作" in out
    assert "试写：第一章的剧情和结尾状态还没有确定" in out
    assert "这是试写稿，采用时需要接受探索出的剧情与结尾" in out
    assert "没有采用。试写稿需要明确接受剧情与结尾" in out
    assert "已采用第一章这一稿，故事状态更新完成" in out
    assert count(path, "adoption") == 1
    for project in reopen(path):
        row = project.conn.execute("SELECT accepted_plot, accepted_ending FROM review").fetchone()
        assert (row["accepted_plot"], row["accepted_ending"]) == ("听见声响后敲门", "门外等待")
        run = project.conn.execute(
            "SELECT request_json FROM run WHERE task = 'generate'"
        ).fetchone()
        assert "门内有轻微声响" in run["request_json"]


def test_cli_ambiguous_adopt_lists_candidates_and_writes_nothing(pipe, tmp_path):
    """两份未采用稿且没有展示过：“采用那版”列出候选，adoption 计数与 adopted_id 都不变。"""
    path = tmp_path / "novel"
    run_cli(
        pipe,
        path,
        NEW_NOVEL
        + PLAN
        + "w"
        + ENTER
        + ENTER
        + "e"
        + ENTER
        + END
        + "作者补一句。"
        + CTRL_S
        + "q"
        + ENTER,
        clients=sequenced_clients((BODY_1, CHECK_1)),
    )
    before = count(path, "adoption")
    script = "".join(
        [
            "c" + ENTER + "1" + ENTER,
            "采用那版" + ENTER + ENTER,  # 回车取消
            "采用那版" + ENTER + "2" + ENTER,  # 查看稿二（修改稿，尚无检查）
            "a" + ENTER,
            "q" + ENTER,
        ]
    )
    out = run_cli(pipe, path, script)
    assert out.count("第一章有多份稿件，无法确定“那版”指哪一稿；没有做任何采用记录") == 2
    assert "写作稿 · 沈砚把灯搁在井沿。井底有一具尸体，他怀疑是师父。" in out
    assert (
        "修改稿 · 现在的编辑稿 · 沈砚把灯搁在井沿。井底有一具尸体，他怀疑是师父。作者补一句。"
        in out
    )
    assert "看完后用 a 采用这一稿" in out
    assert "这一稿还没有检查，不能采用。先用 k 检查" in out
    assert count(path, "adoption") == before == 0
    assert count(path, "review") == 0
    for project in reopen(path):
        assert authoring.list_chapters(project)[0].adopted_id is None


def test_cli_stale_basis_after_plan_change_needs_recheck(pipe, tmp_path):
    body = BODY_1
    check_after = inspection_json(body, plot="改后的剧情", ending="改后的结尾")
    script = "".join(
        [
            NEW_NOVEL + PLAN + "w" + ENTER + ENTER,
            "p" + ENTER + "改后的剧情" + ENTER + "改后的结尾" + ENTER,
            ADOPT,
            "k" + ENTER,
            ADOPT,
            "q" + ENTER,
        ]
    )
    clients = sequenced_clients((body, CHECK_1), ("", check_after))
    out = run_cli(pipe, tmp_path / "novel", script, clients=clients)
    assert "没有采用：相关依据已经变化" in out and "请用 k 重新检查后再采用" in out
    assert "已完成对 第一章 · 写作稿 · 未采用 的检查" in out
    assert "实际剧情：改后的剧情" in out
    assert "已采用第一章这一稿，故事状态更新完成" in out


def test_cli_discussion_streams_reply_and_saves_messages(pipe, tmp_path):
    path = tmp_path / "novel"
    chunks: list[str] = []
    clients = generation.Clients(
        stream=fixed_stream(["可以让他", "先在井口犹豫。"]), complete=fixed_complete("{}")
    )
    script = NEW_NOVEL + "t" + ENTER + "第一章从哪写起？" + ENTER + "q" + ENTER
    out = run_cli(pipe, path, script, clients=clients, emit=chunks)
    assert "AI：" in out
    assert chunks == ["可以让他", "先在井口犹豫。", "\n"]
    for project in reopen(path):
        msgs = database.list_messages(project.conn, project.novel_id)
        assert [(m.role.value, m.body) for m in msgs] == [
            ("author", "第一章从哪写起？"),
            ("model", "可以让他先在井口犹豫。"),
        ]
        assert authoring.list_story_items(project) == []


def test_cli_candidate_and_decision_entries_require_authorization_to_change(pipe, tmp_path):
    path = tmp_path / "novel"
    script = "".join(
        [
            NEW_NOVEL,
            "d" + ENTER + "师父可能是反派" + ENTER,
            "f" + ENTER + "1" + ENTER,
            "f" + ENTER + "1" + ENTER + "师父是反派" + ENTER + "不" + ENTER,
            "f" + ENTER + "1" + ENTER + "师父是反派" + ENTER + "授权" + ENTER,
            "f" + ENTER + "师父右腕有旧伤" + ENTER,
            "q" + ENTER,
        ]
    )
    out = run_cli(pipe, path, script)
    assert "已记为候选想法。候选不会自动成为决定或正式事实" in out
    assert "  1. 候选想法 · 师父可能是反派" in out
    assert "已确定：师父可能是反派" in out
    assert "没有授权，已确定的内容保持不变" in out
    assert "已按你的授权修改这条决定：师父是反派" in out
    assert "已确定：师父右腕有旧伤" in out
    for project in reopen(path):
        items = authoring.list_story_items(project)
        assert [(i.body, i.lifecycle) for i in items] == [
            ("师父是反派", Lifecycle.DECIDED),
            ("师父右腕有旧伤", Lifecycle.DECIDED),
        ]
        assert project.novel.design_revision >= 3

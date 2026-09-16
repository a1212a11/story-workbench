"""旧版命令行入口（单字母命令）。流程实现在 console.workbench；本模块只负责命令表、主循环
与 prompt_toolkit 端口。对话式工作台见 docs/conversational-workbench.md。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from prompt_toolkit import PromptSession

from story_workbench.application import generation
from story_workbench.application.projects import Project, create_project, open_project
from story_workbench.cli_editor import edit_text
from story_workbench.cli_model import run_model
from story_workbench.console.workbench import (  # noqa: F401  旧测试与调用方仍从本模块引用
    READ_DECLARATIONS,
    Shown,
    chapter_label,
    chinese_number,
    draft_label,
    elapsed_seconds,
    first_line,
    local_time,
    print_inline,
)
from story_workbench.console.workbench import (
    Workbench as FlowWorkbench,
)
from story_workbench.domain.records import ErrorCode, WorkbenchError

HELP = (
    "可用操作：\n"
    "  n 新建章节    c 选章          l 章节列表\n"
    "  t 讨论        p 剧情与结尾    d 记为候选      f 确定\n"
    "  w 写          x 试写          v 查看全文      e 编辑正文\n"
    "  k 检查        i 查看检查      a 采用          j 拒绝\n"
    "  b 阅读诊断与试改  u 复核已采用正文  y 重试失败阶段\n"
    "  m 标记发布    s 分支          z 影响分析      o 伏笔\n"
    "  g 补救讨论    r 恢复历史版本  H 历史状态      h 帮助        q 退出"
)


class PromptPort:
    """基于 prompt_toolkit 的作者端口：行提示、全屏编辑器、asyncio 模型等待。"""

    def __init__(
        self,
        session: PromptSession[str],
        say: Callable[[str], None],
        emit: Callable[[str], None] | None = None,
    ) -> None:
        self.session = session
        self.say = say
        self.emit = emit or print_inline

    def ask(self, prompt: str) -> str:
        return self.session.prompt(prompt)

    def edit(self, initial: str, title: str) -> str | None:
        return edit_text(initial, title)

    def run_model(self, project, clients, config, operation, *args, **kwargs):
        return run_model(
            project, self.session, self.say, clients, config, operation, *args, **kwargs
        )


class Workbench(FlowWorkbench):
    """旧版命令循环：字母或中文别名映射到流程方法。"""

    def __init__(
        self,
        project: Project,
        session: PromptSession[str],
        say: Callable[[str], None] = print,
        *,
        clients: generation.Clients | None = None,
        config: generation.GenerationConfig | None = None,
        emit: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(project, PromptPort(session, say, emit), clients=clients, config=config)
        self.session = session
        self.commands: dict[str, Callable[[], bool]] = {}
        for keys, handler in (
            (("n", "新建章节", "新建"), self.new_chapter),
            (("c", "选章"), self.choose_chapter),
            (("l", "章节列表", "列表"), self.show_chapters),
            (("t", "讨论"), self.discuss),
            (("p", "剧情与结尾", "计划"), self.set_plan),
            (("d", "记为候选", "候选"), self.record_candidate),
            (("f", "确定", "就这么定"), self.decide),
            (("w", "写", "写作"), self.write),
            (("x", "试写"), self.explore),
            (("v", "查看全文", "全文"), self.show_body),
            (("e", "编辑正文", "编辑"), self.edit_body),
            (("k", "检查"), self.check),
            (("i", "查看检查"), self.show_latest_check),
            (("b", "阅读诊断与试改", "阅读诊断", "诊断"), self.diagnose),
            (("a", "采用", "采用那版", "采用此稿"), self.adopt),
            (("j", "拒绝", "不要这版"), self.reject),
            (("m", "标记发布", "发布"), self.publish),
            (("s", "分支"), self.branches),
            (("z", "影响分析"), self.analyze_impact),
            (("o", "伏笔"), self.foreshadow),
            (("g", "补救讨论"), self.remediate),
            (("H", "历史状态"), self.history_status),
            (("r", "恢复历史版本", "恢复"), self.restore),
            (("u", "复核"), self.revalidate),
            (("y", "重试"), self.retry),
            (("h", "帮助", "?"), self.help),
        ):
            for key in keys:
                self.commands[key] = handler

    def loop(self) -> None:
        self.say(f"已打开《{self.project.novel.title}》。")
        self.show_chapters()
        self.say(HELP)
        while True:
            where = chapter_label(self.chapter) if self.chapter else "未选章"
            try:
                command = self.ask(f"{where} > ")
            except (EOFError, KeyboardInterrupt):
                self.say("已退出。")
                return
            if command in ("q", "退出"):
                self.say("已退出，所有保存过的内容都在。")
                return
            handler = self.commands.get(command)
            if handler is None:
                if command:
                    self.say(f"不认识「{command}」。输入 h 查看可用操作。")
                continue
            try:
                handler()
            except WorkbenchError as exc:
                self.say(f"没有完成：{exc.message}")
            except (EOFError, KeyboardInterrupt):
                self.say("已中止这一步；保存过的内容都在。")

    def help(self) -> bool:
        self.say(HELP)
        return True


def open_or_create(path: Path, session: PromptSession[str], say: Callable[[str], None]) -> Project:
    """目录里已有作品就打开；没有则询问标题并创建。"""
    try:
        project, notice = open_project(path)
    except WorkbenchError as exc:
        if exc.code is not ErrorCode.NOT_FOUND:
            raise
        say(f"这里还没有作品：{path}")
        while True:
            title = session.prompt("作品标题：").strip()
            if title:
                return create_project(path, title)
            say("标题不能为空。")
    for note in notice.notes:
        say(note)
    return project


def run(
    path: Path,
    say: Callable[[str], None] = print,
    *,
    clients: generation.Clients | None = None,
    config: generation.GenerationConfig | None = None,
    emit: Callable[[str], None] | None = None,
) -> int:
    """clients/config 由启动入口装配；为 None 时写作、检查与 AI 讨论提示模型尚未接入。"""
    session: PromptSession[str] = PromptSession()
    try:
        project = open_or_create(path, session, say)
    except (EOFError, KeyboardInterrupt):
        say("已退出。")
        return 1
    except WorkbenchError as exc:
        say(f"无法打开作品：{exc.message}")
        return 1
    try:
        Workbench(project, session, say, clients=clients, config=config, emit=emit).loop()
    finally:
        project.close()
    return 0

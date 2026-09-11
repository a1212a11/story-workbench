"""首期终端交互：作品与章节、讨论记录、章节计划、全文查看、正文编辑与历史恢复。

只调用 application 用例；不写 SQL、不调用模型客户端。面向作者的文案是中文叙事语言，
不显示内部 ID。采用入口（C03）与 AI 讨论回复（G 阶段）尚未接入，只留占位提示。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from prompt_toolkit import PromptSession

from story_workbench.application import authoring
from story_workbench.application.projects import Project, create_project, open_project
from story_workbench.cli_editor import edit_text
from story_workbench.domain.records import (
    ChapterRecord,
    ErrorCode,
    Expected,
    Target,
    VersionOrigin,
    VersionRecord,
    WorkbenchError,
)

_DIGITS = "零一二三四五六七八九"
_ORIGIN_LABEL = {
    VersionOrigin.AUTHOR: "修改稿",
    VersionOrigin.GENERATED: "写作稿",
    VersionOrigin.EXTERNAL: "外部稿",
    VersionOrigin.RESTORE: "恢复稿",
}

HELP = (
    "可用操作：\n"
    "  n 新建章节    c 选章      l 章节列表\n"
    "  t 讨论        p 剧情与结尾    v 查看全文\n"
    "  e 编辑正文    r 恢复历史版本  a 采用\n"
    "  h 帮助        q 退出"
)


def chinese_number(number: int) -> str:
    """1 → 一，12 → 十二，105 → 一百零五；章号只用于展示。"""
    if number < 10:
        return _DIGITS[number]
    if number < 20:
        return "十" + (_DIGITS[number % 10] if number % 10 else "")
    if number < 100:
        return _DIGITS[number // 10] + "十" + (_DIGITS[number % 10] if number % 10 else "")
    if number < 1000:
        hundreds = _DIGITS[number // 100] + "百"
        rest = number % 100
        if rest == 0:
            return hundreds
        if rest < 10:
            return hundreds + "零" + _DIGITS[rest]
        return hundreds + chinese_number(rest)
    return str(number)


def chapter_label(chapter: ChapterRecord) -> str:
    return f"第{chinese_number(chapter.number)}章"


def local_time(created_at: str) -> str:
    return datetime.fromisoformat(created_at).astimezone().strftime("%Y-%m-%d %H:%M")


def draft_label(chapter: ChapterRecord, version: VersionRecord) -> str:
    """形如「第一章 · 修改稿 · 未采用」；不显示内部 ID。"""
    adoption = "未采用" if chapter.adopted_id is None else "本章已有采用记录"
    return f"{chapter_label(chapter)} · {_ORIGIN_LABEL[version.origin]} · {adoption}"


class Workbench:
    """一个已打开作品的交互循环。say 只负责输出文字，便于测试捕获。"""

    def __init__(
        self, project: Project, session: PromptSession[str], say: Callable[[str], None] = print
    ) -> None:
        self.project = project
        self.session = session
        self.say = say
        self.chapter: ChapterRecord | None = None
        self.commands: dict[str, Callable[[], bool]] = {}
        for keys, handler in (
            (("n", "新建章节", "新建"), self.new_chapter),
            (("c", "选章"), self.choose_chapter),
            (("l", "章节列表", "列表"), self.show_chapters),
            (("t", "讨论"), self.discuss),
            (("p", "剧情与结尾", "计划"), self.set_plan),
            (("v", "查看全文", "全文"), self.show_body),
            (("e", "编辑正文", "编辑"), self.edit_body),
            (("r", "恢复历史版本", "恢复"), self.restore),
            (("a", "采用"), self.adopt),
            (("h", "帮助", "?"), self.help),
        ):
            for key in keys:
                self.commands[key] = handler

    # ------------------------------------------------------------ 输入输出

    def ask(self, prompt: str) -> str:
        return self.session.prompt(prompt).strip()

    def refresh(self) -> ChapterRecord | None:
        if self.chapter is not None:
            self.chapter = authoring.get_chapter(self.project, self.chapter.id)
        return self.chapter

    def require_chapter(self) -> ChapterRecord | None:
        chapter = self.refresh()
        if chapter is None:
            self.say("还没有选中章节。先用 n 新建章节，或用 c 选章。")
        return chapter

    def current_draft(self, chapter: ChapterRecord) -> VersionRecord | None:
        if chapter.current_draft_id is None:
            return None
        target = Target(self.project.novel_id, chapter.id, chapter.current_draft_id)
        return authoring.get_version(self.project, target)

    # ------------------------------------------------------------ 主循环

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

    def help(self) -> bool:
        self.say(HELP)
        return True

    # ------------------------------------------------------------ 作品与章节

    def show_chapters(self) -> bool:
        chapters = authoring.list_chapters(self.project)
        if not chapters:
            self.say("这部作品还没有章节。用 n 新建第一章。")
            return True
        self.say(f"《{self.project.novel.title}》的章节：")
        for chapter in chapters:
            marks = ["有正文" if chapter.current_draft_id else "还没有正文"]
            if chapter.current_plan_id:
                marks.append("已有剧情与结尾")
            if self.chapter is not None and chapter.id == self.chapter.id:
                marks.append("当前")
            self.say(f"  {chapter.number:>3}. {chapter_label(chapter)} · {' · '.join(marks)}")
        return True

    def new_chapter(self) -> bool:
        chapters = authoring.list_chapters(self.project)
        number = max((c.number for c in chapters), default=0) + 1
        self.chapter = authoring.create_chapter(self.project, number)
        self.say(f"已新建{chapter_label(self.chapter)}，现在就在这一章。")
        return True

    def choose_chapter(self) -> bool:
        chapters = authoring.list_chapters(self.project)
        if not chapters:
            self.say("这部作品还没有章节。用 n 新建第一章。")
            return True
        self.show_chapters()
        answer = self.ask("要去第几章（输入数字，回车取消）：")
        if not answer:
            return True
        by_number = {c.number: c for c in chapters}
        if not answer.isdigit() or int(answer) not in by_number:
            self.say("没有这一章。")
            return True
        self.chapter = by_number[int(answer)]
        self.say(f"现在在{chapter_label(self.chapter)}。")
        return True

    # ------------------------------------------------------------ 讨论与计划

    def discuss(self) -> bool:
        chapter = self.refresh()
        text = self.ask("你说：")
        if not text:
            return True
        authoring.record_message(self.project, text, chapter.id if chapter else None)
        self.say("已记录你的话。AI 讨论尚未接入，之后会在这里回复。")
        return True

    def set_plan(self) -> bool:
        chapter = self.require_chapter()
        if chapter is None:
            return True
        self.say(f"为{chapter_label(chapter)}写下剧情与结尾状态；留空表示这一项暂不确定。")
        plot = self.ask("本章剧情：")
        ending = self.ask("本章结尾状态：")
        if not plot and not ending:
            self.say("两项都为空，计划没有改变。")
            return True
        note = f"本章剧情：{plot or '（暂不确定）'}\n本章结尾状态：{ending or '（暂不确定）'}"
        message_id = authoring.record_message(self.project, note, chapter.id)
        plan = authoring.save_plan(
            self.project,
            chapter.id,
            plot or None,
            ending or None,
            Expected(plan_id=chapter.current_plan_id),
            message_id,
        )
        self.refresh()
        self.say(f"{chapter_label(chapter)}的剧情与结尾已记录（第 {plan.revision} 次）。")
        return True

    # ------------------------------------------------------------ 正文

    def show_body(self) -> bool:
        chapter = self.require_chapter()
        if chapter is None:
            return True
        draft = self.current_draft(chapter)
        if draft is None:
            self.say(f"{chapter_label(chapter)}还没有正文。用 e 开始编辑。")
            return True
        self.say(draft_label(chapter, draft))
        self.say("")
        self.say(draft.body)
        self.say("")
        return True

    def edit_body(self) -> bool:
        chapter = self.require_chapter()
        if chapter is None:
            return True
        draft = self.current_draft(chapter)
        text = edit_text(draft.body if draft else "", f"{chapter_label(chapter)} 正文")
        if text is None:
            self.say("已取消编辑，正文没有改变。")
            return True
        self.save_with_retry(chapter, text)
        return True

    def save_with_retry(self, chapter: ChapterRecord, text: str) -> None:
        """保存失败时作者的文字仍在内存里：可以重试、回到编辑器，或明确放弃。"""
        title = f"{chapter_label(chapter)} 正文"
        while True:
            try:
                result = authoring.save_draft(
                    self.project,
                    chapter.id,
                    text,
                    Expected(current_draft_id=chapter.current_draft_id),
                )
            except WorkbenchError as exc:
                self.say(f"保存没有成功：{exc.message} 你刚写的文字还在，没有丢失。")
                choice = self.ask("r 重试保存 · e 回到编辑器继续修改 · q 放弃这次修改：")
                if choice in ("e", "编辑"):
                    edited = edit_text(text, title)
                    if edited is not None:
                        text = edited
                elif choice in ("q", "放弃"):
                    self.say("已放弃这次修改；之前保存过的正文没有改变。")
                    return
                continue
            break
        fresh = self.refresh() or chapter
        if result.became_current:
            self.say(f"已保存，这是{chapter_label(fresh)}现在的编辑稿。")
            return
        self.say("在你编辑期间，这一章的编辑稿已经变化。你的修改已保存为另一稿，两稿都在。")
        mine = authoring.get_version(
            self.project, Target(self.project.novel_id, chapter.id, result.version_id)
        )
        other = self.current_draft(fresh)
        self.say("稿一 · 你刚保存的修改稿：")
        self.say(mine.body)
        self.say("稿二 · 现在的编辑稿：")
        self.say(other.body if other else "（空）")
        choice = self.ask("要以哪一稿继续？1 或 2（回车保留稿二）：")
        if choice == "1":
            restored = authoring.restore_version(
                self.project,
                Target(self.project.novel_id, chapter.id, mine.id),
                Expected(current_draft_id=fresh.current_draft_id),
            )
            if restored.became_current:
                self.say("已把你的修改稿作为现在的编辑稿；另一稿保留在历史里。")
            else:
                self.say("编辑稿又变化了，请用 r 查看历史版本后再选择。")
        else:
            self.say("保留稿二为编辑稿；你的修改稿留在历史里，可以用 r 恢复。")

    def restore(self) -> bool:
        chapter = self.require_chapter()
        if chapter is None:
            return True
        versions = authoring.list_versions(self.project, chapter.id)
        if not versions:
            self.say(f"{chapter_label(chapter)}还没有任何正文版本。")
            return True
        self.say(f"{chapter_label(chapter)}的历史版本：")
        for index, version in enumerate(versions, start=1):
            marks = [local_time(version.created_at), _ORIGIN_LABEL[version.origin]]
            if version.id == chapter.current_draft_id:
                marks.append("现在的编辑稿")
            self.say(f"  {index:>3}. {' · '.join(marks)}")
        answer = self.ask("恢复第几个版本（输入序号，回车取消）：")
        if not answer:
            return True
        if not answer.isdigit() or not 1 <= int(answer) <= len(versions):
            self.say("没有这个序号。")
            return True
        chosen = versions[int(answer) - 1]
        result = authoring.restore_version(
            self.project,
            Target(self.project.novel_id, chapter.id, chosen.id),
            Expected(current_draft_id=chapter.current_draft_id),
        )
        if result.became_current:
            self.say("已用这个版本创建恢复稿，并作为现在的编辑稿。历史版本都还在。")
        else:
            self.say("恢复稿已保存，但编辑稿在此期间变化了；请查看历史版本后再选择。")
        return True

    def adopt(self) -> bool:
        self.say("采用功能尚未接入。")
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


def run(path: Path, say: Callable[[str], None] = print) -> int:
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
        Workbench(project, session, say).loop()
    finally:
        project.close()
    return 0
